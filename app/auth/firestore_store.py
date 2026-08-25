"""Firestore 구현.

collection 이름에 `ggumirror_` prefix를 붙인다. 같은 GCP project에 다른 service가
들어있을 수 있고, **다른 service의 collection은 읽지도 쓰지도 않는다.**

Cloud Run에서는 Application Default Credentials를 쓴다.
service account JSON key를 repository에 넣지 않는다.
"""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import uuid4

from google.api_core import exceptions as gcp_exceptions
from google.cloud import firestore

from app.auth.profile import DisplayNameCooldown, can_change, next_change_at
from app.auth.models import Session, User, identity_key, utcnow
from app.auth.store import StoreUnavailable

logger = logging.getLogger(__name__)

USERS = "ggumirror_users"
IDENTITIES = "ggumirror_auth_identities"
SESSIONS = "ggumirror_sessions"


class FirestoreAuthStore:
    def __init__(self, client: firestore.Client) -> None:
        self._db = client

    # MARK: - identity → user

    def user_for_identity(self, provider: str, subject: str) -> tuple[User, bool]:
        """transaction으로 get-or-create.

        document key가 identity hash라서 같은 subject는 **항상 같은 문서**를 가리킨다.
        transaction이 그 문서를 읽고 없을 때만 만들기 때문에, 동시에 두 번 로그인해도
        한쪽은 재시도 후 상대가 만든 User를 그대로 쓴다.
        """
        identity_ref = self._db.collection(IDENTITIES).document(identity_key(provider, subject))

        @firestore.transactional
        def run(transaction: firestore.Transaction) -> tuple[str, bool]:
            snapshot = identity_ref.get(transaction=transaction)
            if snapshot.exists:
                user_id = snapshot.get("userId")
                if isinstance(user_id, str) and user_id:
                    return user_id, False
                # 있어서는 안 되는 상태다. 새 User로 덮어쓰지 않고 실패시킨다.
                raise StoreUnavailable("identity document has no userId")

            user_id = str(uuid4())
            now = utcnow()
            transaction.set(
                self._db.collection(USERS).document(user_id),
                {"createdAt": now, "updatedAt": now},
            )
            transaction.set(
                identity_ref,
                {
                    "provider": provider,
                    # raw Apple subject를 저장하지 않는다. mapping에 필요한 것은 key뿐이다.
                    "userId": user_id,
                    "createdAt": now,
                },
            )
            return user_id, True

        try:
            user_id, created = run(self._db.transaction())
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("identity_lookup", error) from error

        if created:
            logger.info("user_created")
            return User(id=user_id), True

        user = self.user(user_id)
        if user is None:
            # identity는 있는데 user 문서가 없다. 조용히 새로 만들지 않는다.
            raise StoreUnavailable("user document missing for identity")
        return user, False

    def user(self, user_id: str) -> User | None:
        try:
            snapshot = self._db.collection(USERS).document(user_id).get()
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("user_lookup", error) from error

        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        return User(
            id=user_id,
            created_at=_as_datetime(data.get("createdAt")),
            updated_at=_as_datetime(data.get("updatedAt")),
            # 예전 문서에는 이 두 값이 없다. **migration을 돌리지 않는다** —
            # 없으면 없는 것으로 읽고, 사용자가 이름을 정할 때 비로소 생긴다.
            display_name=data.get("displayName"),
            display_name_changed_at=(
                _as_datetime(data["displayNameChangedAt"])
                if data.get("displayNameChangedAt") is not None
                else None
            ),
        )

    # MARK: - 프로필 이름

    def seed_display_name(self, user_id: str, name: str) -> User:
        """**비어 있을 때만** 채운다. 이미 있으면 그대로 둔다.

        transaction 안에서 읽고 쓴다 — 동시에 두 번 로그인해도 한쪽만 쓴다.
        `displayNameChangedAt`은 **남기지 않는다**: Apple이 넣어 준 값은 사용자가
        고른 것이 아니므로 30일 규칙을 소비하면 안 된다.
        """
        user_ref = self._db.collection(USERS).document(user_id)

        @firestore.transactional
        def run(transaction: firestore.Transaction) -> User:
            snapshot = user_ref.get(transaction=transaction)
            if not snapshot.exists:
                raise StoreUnavailable("user document missing")
            data = snapshot.to_dict() or {}
            if data.get("displayName"):
                return _user_with_profile(user_id, data)
            transaction.update(user_ref, {"displayName": name, "updatedAt": utcnow()})
            return _user_with_profile(user_id, {**data, "displayName": name})

        try:
            return run(self._db.transaction())
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("display_name_seed", error) from error

    def set_display_name(self, user_id: str, name: str, now: datetime) -> User:
        """사용자가 직접 바꾼다. **읽기·검사·쓰기가 한 transaction이다.**

        나누면 동시에 들어온 두 요청이 둘 다 "아직 30일 안 지났나? 아니네" 하고
        지나가 이름이 연속으로 바뀐다.
        """
        user_ref = self._db.collection(USERS).document(user_id)

        @firestore.transactional
        def run(transaction: firestore.Transaction) -> User:
            snapshot = user_ref.get(transaction=transaction)
            if not snapshot.exists:
                raise StoreUnavailable("user document missing")
            data = snapshot.to_dict() or {}
            changed_at = (
                _as_datetime(data["displayNameChangedAt"])
                if data.get("displayNameChangedAt") is not None
                else None
            )
            if not can_change(changed_at, now):
                raise DisplayNameCooldown(next_change_at(changed_at))
            transaction.update(
                user_ref,
                {"displayName": name, "displayNameChangedAt": now, "updatedAt": now},
            )
            return _user_with_profile(
                user_id, {**data, "displayName": name, "displayNameChangedAt": now}
            )

        try:
            return run(self._db.transaction())
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("display_name_set", error) from error

    # MARK: - session

    def create_session(self, session: Session) -> None:
        try:
            # document ID = token hash. raw token은 어디에도 저장하지 않는다.
            self._db.collection(SESSIONS).document(session.token_hash).set(
                {
                    "userId": session.user_id,
                    "createdAt": session.created_at,
                    "expiresAt": session.expires_at,
                    "revokedAt": None,
                }
            )
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("session_create", error) from error

    def session_by_token_hash(self, token_hash: str) -> Session | None:
        try:
            snapshot = self._db.collection(SESSIONS).document(token_hash).get()
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("session_lookup", error) from error

        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        return Session(
            token_hash=token_hash,
            user_id=str(data.get("userId") or ""),
            created_at=_as_datetime(data.get("createdAt")),
            expires_at=_as_datetime(data.get("expiresAt")),
            revoked_at=_as_optional_datetime(data.get("revokedAt")),
        )

    def revoke_session(self, token_hash: str, now: datetime | None = None) -> bool:
        session = self.session_by_token_hash(token_hash)
        if session is None or session.revoked_at is not None:
            return False
        try:
            self._db.collection(SESSIONS).document(token_hash).update({"revokedAt": now or utcnow()})
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("session_revoke", error) from error
        return True

    # MARK: - 내부

    def _unavailable(self, operation: str, error: Exception) -> StoreUnavailable:
        # 실패 사실만 남긴다. Firestore 오류 문자열에 문서 경로가 들어갈 수 있다.
        logger.warning("firestore_failed operation=%s error=%s", operation, type(error).__name__)
        return StoreUnavailable(operation)


def _as_datetime(value: object) -> datetime:
    return value if isinstance(value, datetime) else utcnow()


def _as_optional_datetime(value: object) -> datetime | None:
    return value if isinstance(value, datetime) else None


def _user_with_profile(user_id: str, data: dict) -> User:
    """문서 하나를 User로. **없는 값은 없는 것으로 읽는다** — 기본 이름을 지어내지 않는다."""
    return User(
        id=user_id,
        created_at=_as_datetime(data.get("createdAt")),
        updated_at=_as_datetime(data.get("updatedAt")),
        display_name=data.get("displayName"),
        display_name_changed_at=(
            _as_datetime(data["displayNameChangedAt"])
            if data.get("displayNameChangedAt") is not None
            else None
        ),
    )
