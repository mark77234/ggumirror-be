"""User / Session / identity mapping.

**꾸미러 user ID는 internal UUID다.** Apple subject를 public ID로 쓰지 않는다.
Apple subject는 Firestore 내부 mapping에서만 쓰고, 그것도 raw로 두지 않는다.

지금 필요 없는 field(shard balance · seller · store profile · statistics)를 미리 넣지 않는다.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import uuid4

# 하나뿐인 session 수명. 코드 여러 곳에 숫자를 흩뿌리지 않는다.
# refresh token이 없으므로 너무 짧으면 계속 다시 로그인해야 한다.
SESSION_LIFETIME = timedelta(days=30)

# access token 길이. 32 byte urlsafe → 43자.
SESSION_TOKEN_BYTES = 32

APPLE_PROVIDER = "apple"


def issue_session_token() -> str:
    """opaque access token. 꾸미러 자체 JWT를 만들지 않는다 —
    server가 취소할 수 있어야 하고, JWT는 그걸 어렵게 만든다.
    """
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def utcnow() -> datetime:
    """server 시계. client가 보낸 시간을 생성 시각의 근거로 쓰지 않는다."""
    return datetime.now(timezone.utc)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def identity_key(provider: str, subject: str) -> str:
    """identity document key.

    raw Apple subject를 document ID에 그대로 쓰지 않는다 — Firestore console,
    export, index 이름 등 여러 곳에 그대로 남는다. deterministic hash면
    "같은 subject → 같은 문서"라는 성질(중복 User 방지의 근거)은 그대로 유지된다.
    """
    return sha256_hex(f"{provider}:{subject}")


@dataclass(frozen=True)
class User:
    """꾸미러 사용자. Apple과의 연결은 여기 없다 — identity mapping이 따로 있다."""

    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    #: 사람에게 보이는 이름. **없는 것이 정상이다** — 1.0.7 사용자에게는 이 값이 없고,
    #: 화면은 그때 "이름을 정해주세요"를 보여 준다. 여기에 기본 이름을 지어 넣지 않는다.
    display_name: str | None = None
    #: 마지막으로 **사용자가 직접** 바꾼 시각. Apple이 넣어 준 최초 값은 여기 남기지
    #: 않는다 — 그래야 첫 로그인 직후에도 한 번은 바로 고칠 수 있다(30일 규칙 밖).
    display_name_changed_at: datetime | None = None


@dataclass(frozen=True)
class Session:
    """opaque access token 하나. **raw token은 저장하지 않는다.**"""

    token_hash: str
    user_id: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None

    def is_valid(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        return self.revoked_at is None and self.expires_at > now


def new_session(user_id: str, token: str, now: datetime | None = None) -> Session:
    now = now or utcnow()
    return Session(
        token_hash=sha256_hex(token),
        user_id=user_id,
        created_at=now,
        expires_at=now + SESSION_LIFETIME,
    )
