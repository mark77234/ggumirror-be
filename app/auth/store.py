"""User / identity / session 저장소.

abstraction은 딱 하나 있다: 이 Protocol. 이유는 test가 실제 Firestore에 붙지 않기 위해서다.
repository interface 계층을 더 쌓지 않고, 구현은 Firestore 하나 + test fake 하나뿐이다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.auth.models import Session, User, identity_key, utcnow


class StoreUnavailable(Exception):
    """저장소에 닿지 못했다. endpoint에서 5xx로 바꾼다. 내부 상세를 client에 넘기지 않는다."""


class AuthStore(Protocol):
    def user_for_identity(self, provider: str, subject: str) -> tuple[User, bool]:
        """identity에 연결된 User를 돌려준다. 없으면 만든다.

        같은 subject로 동시에 두 번 들어와도 User가 두 명 생기면 안 된다.
        반환값의 bool은 "이번에 새로 만들었는지"다.
        """

    def create_session(self, session: Session) -> None: ...

    def session_by_token_hash(self, token_hash: str) -> Session | None: ...

    def revoke_session(self, token_hash: str, now: datetime | None = None) -> bool:
        """revoke했으면 True. 없거나 이미 revoke된 session이면 False."""

    def user(self, user_id: str) -> User | None: ...


class InMemoryAuthStore:
    """test / local용. Firestore에 붙지 않는다."""

    def __init__(self) -> None:
        self.users: dict[str, User] = {}
        self.identities: dict[str, str] = {}
        self.sessions: dict[str, Session] = {}

    def user_for_identity(self, provider: str, subject: str) -> tuple[User, bool]:
        key = identity_key(provider, subject)
        existing = self.identities.get(key)
        if existing is not None:
            return self.users[existing], False

        user = User()
        self.users[user.id] = user
        self.identities[key] = user.id
        return user, True

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
