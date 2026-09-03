"""User / identity / session 저장소.

abstraction은 딱 하나 있다: 이 Protocol. 이유는 test가 실제 Firestore에 붙지 않기 위해서다.
repository interface 계층을 더 쌓지 않고, 구현은 Firestore 하나 + test fake 하나뿐이다.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Protocol

from app.auth.models import Session, User, identity_key, utcnow
from app.auth.profile import (
    DisplayNameCooldown,
    DisplayNameTaken,
    can_change,
    display_name_key,
    next_change_at,
)


class StoreUnavailable(Exception):
    """저장소에 닿지 못했다. endpoint에서 5xx로 바꾼다. 내부 상세를 client에 넘기지 않는다."""


class AuthStore(Protocol):
    def user_for_identity(self, provider: str, subject: str) -> tuple[User, bool]:
        """identity에 연결된 User를 돌려준다. 없으면 만든다.

        같은 subject로 동시에 두 번 들어와도 User가 두 명 생기면 안 된다.
        반환값의 bool은 "이번에 새로 만들었는지"다.
        """

    def create_guest_user(self) -> User:
        """identity 없는 User를 만든다. **조각 구매에 로그인을 요구하지 않기 위해서다.**

        client가 만든 UUID를 지갑 주인으로 쓰지 않는다 — 그러면 남의 id를 적어
        남의 지갑을 조회·충전할 수 있다. id는 서버가 만들고 session으로만 나간다.
        """

    def link_identity(self, provider: str, subject: str, guest_user_id: str) -> tuple[User, bool]:
        """guest에 identity를 붙인다.

        - 그 identity가 처음이면 **guest가 곧 그 계정이 된다**(`(guest, True)`).
          지갑이 그대로 남으므로 옮길 것이 없다.
        - 이미 다른 User가 그 identity를 갖고 있으면 **아무것도 붙이지 않고**
          `(그 User, False)`다. 조각은 호출부가 원장으로 옮긴다.
        """

    def mark_guest_claimed(self, guest_user_id: str, claimed_by: str) -> None:
        """이 guest 지갑을 누가 넘겨받았는지 적는다. **한 번 적히면 바뀌지 않는다.**"""

    def create_session(self, session: Session) -> None: ...

    def session_by_token_hash(self, token_hash: str) -> Session | None: ...

    def revoke_session(self, token_hash: str, now: datetime | None = None) -> bool:
        """revoke했으면 True. 없거나 이미 revoke된 session이면 False."""

    def user(self, user_id: str) -> User | None: ...

    def is_admin(self, user_id: str) -> bool:
        """**운영자 권한의 authority는 이 한 곳이다.**

        `User` 문서에 `isAdmin`을 두지 않는다 — 그러면 프로필을 쓰는 모든 경로가
        권한을 건드릴 수 있는 자리가 되고, `/users/me` 응답에 실려 나가면 client가
        자기 권한을 아는 것처럼 보인다. 별도 collection이면 **쓰는 경로가 없다** —
        운영자 등록은 사람이 Firestore에서 직접 한다.

        이름 · 이메일 · Apple subject로 판단하지 않는다. 그것들은 사용자가 바꿀 수
        있거나 로그인 때마다 달라진다.
        """


    def seed_display_name(self, user_id: str, name: str) -> User:
        """Apple이 준 이름으로 **비어 있을 때만** 채운다.

        이미 이름이 있으면 아무것도 하지 않는다 — 로그인할 때마다 사용자가 정한
        이름이 Apple 이름으로 되돌아가면 안 된다. 30일 규칙도 소비하지 않는다.
        """

    def set_display_name(self, user_id: str, name: str, now: datetime) -> User:
        """사용자가 직접 바꾼다. **30일 규칙을 여기서 강제한다.**

        읽고-검사하고-쓰는 일이 한 transaction 안에 있어야 한다. 나누면 동시에 들어온
        두 요청이 둘 다 통과해 연속으로 이름을 바꿀 수 있다.
        """


class InMemoryAuthStore:
    """test / local용. Firestore에 붙지 않는다."""

    def __init__(self) -> None:
        self.users: dict[str, User] = {}
        self.identities: dict[str, str] = {}
        self.sessions: dict[str, Session] = {}
        #: 운영자 allowlist. test에서 직접 채운다 — API로 쓰는 경로가 없다.
        self.admins: dict[str, bool] = {}
        #: `display_name_key` → user id. **이름 하나에 사람 하나다.**
        self.name_claims: dict[str, str] = {}

    def seed_display_name(self, user_id: str, name: str) -> User:
        user = self.users[user_id]
        if user.display_name is None:
            # **이미 쓰이는 이름이면 채우지 않는다.** Apple이 준 이름 때문에
            # 로그인이 실패하면 안 되므로, 조용히 비워 둔다 —
            # 상점에 올릴 때 사용자가 직접 고른다.
            key = display_name_key(name)
            if self._name_owner(key) is None:
                self.name_claims[key] = user_id
                user = replace(user, display_name=name)
                self.users[user_id] = user
        return user

    def set_display_name(self, user_id: str, name: str, now: datetime) -> User:
        user = self.users[user_id]
        if not can_change(user.display_name_changed_at, now):
            raise DisplayNameCooldown(next_change_at(user.display_name_changed_at))
        key = display_name_key(name)
        owner = self._name_owner(key)
        if owner is not None and owner != user_id:
            raise DisplayNameTaken(name)
        # **먼저 잡고 나중에 놓는다.** 순서가 반대면 중간에 실패했을 때
        # 예전 이름도 잃고 새 이름도 못 얻는다.
        self.name_claims[key] = user_id
        if user.display_name is not None:
            previous = display_name_key(user.display_name)
            if previous != key:
                self.name_claims.pop(previous, None)
        user = replace(user, display_name=name, display_name_changed_at=now)
        self.users[user_id] = user
        return user

    def _name_owner(self, key: str) -> str | None:
        return self.name_claims.get(key)

    def user_for_identity(self, provider: str, subject: str) -> tuple[User, bool]:
        key = identity_key(provider, subject)
        existing = self.identities.get(key)
        if existing is not None:
            return self.users[existing], False

        user = User()
        self.users[user.id] = user
        self.identities[key] = user.id
        return user, True

    def create_guest_user(self) -> User:
        user = User(is_guest=True)
        self.users[user.id] = user
        return user

    def link_identity(self, provider: str, subject: str, guest_user_id: str) -> tuple[User, bool]:
        key = identity_key(provider, subject)
        existing = self.identities.get(key)
        if existing is not None:
            return self.users[existing], False

        guest = self.users.get(guest_user_id)
        if guest is None or not guest.is_guest:
            # guest가 아닌 User에 identity를 덧붙이지 않는다 — 계정 하나에
            # identity 둘이 붙는 경로를 만들지 않는다.
            raise StoreUnavailable("user is not a guest")

        user = replace(guest, is_guest=False, updated_at=utcnow())
        self.users[user.id] = user
        self.identities[key] = user.id
        return user, True

    def mark_guest_claimed(self, guest_user_id: str, claimed_by: str) -> None:
        guest = self.users.get(guest_user_id)
        if guest is None or guest.claimed_by_user_id is not None:
            return
        self.users[guest_user_id] = replace(guest, claimed_by_user_id=claimed_by)

    def create_session(self, session: Session) -> None:
        self.sessions[session.token_hash] = session

    def session_by_token_hash(self, token_hash: str) -> Session | None:
        return self.sessions.get(token_hash)

    def revoke_session(self, token_hash: str, now: datetime | None = None) -> bool:
        session = self.sessions.get(token_hash)
        if session is None or session.revoked_at is not None:
            return False
        self.sessions[token_hash] = Session(
            token_hash=session.token_hash,
            user_id=session.user_id,
            created_at=session.created_at,
            expires_at=session.expires_at,
            revoked_at=now or utcnow(),
        )
        return True

    def user(self, user_id: str) -> User | None:
        return self.users.get(user_id)

    def is_admin(self, user_id: str) -> bool:
        # 문서가 있어도 `enabled`가 아니면 운영자가 아니다 — 지우지 않고 끌 수 있다.
        return self.admins.get(user_id, False)
