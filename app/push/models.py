"""기기 push token (Phase F).

**raw token을 문서 자리에 쓰지 않는다.** token은 그 기기로 알림을 보낼 수 있는
열쇠다 — Firestore 문서 ID로 쓰면 콘솔 · 색인 · 로그 어디에나 남는다.
원장 · 소유권 · IAP claim과 **같은 규칙**으로 hash를 자리로 쓴다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from app.shards.models import utcnow

SCHEMA_VERSION = 1

#: APNs token은 32바이트(64 hex)가 표준이지만 늘어난 적이 있다. 넉넉히 두되
#: 무한정 받지 않는다 — 길이를 안 보면 아무 문자열이나 저장된다.
MAX_TOKEN_LENGTH = 200


class PushEnvironment(StrEnum):
    """어느 APNs로 보내야 하는가.

    **client가 고르는 값이 아니다.** 앱이 보낸 문자열을 그대로 믿으면 개발 빌드가
    production APNs로 보내라고 말할 수 있다. 서버는 아는 값만 받고, 모르면 거절한다.
    """

    SANDBOX = "sandbox"
    PRODUCTION = "production"

    @property
    def host(self) -> str:
        return (
            "api.sandbox.push.apple.com"
            if self is PushEnvironment.SANDBOX
            else "api.push.apple.com"
        )


class InvalidPushDevice(Exception):
    """token · environment가 규칙에 맞지 않는다."""


@dataclass(frozen=True)
class PushDevice:
    """한 기기 하나.

    한 사람이 여러 기기를 쓸 수 있고, **한 기기가 여러 사람을 거칠 수도 있다** —
    로그아웃하고 다른 계정으로 들어오면 같은 token이 다른 사람의 것이 된다.
    그래서 문서 자리는 token hash 하나이고 `user_id`는 그 안의 값이다:
    다시 등록하면 주인이 **덮여** 이전 주인에게 남지 않는다.
    """

    id: str
    user_id: str
    token: str
    environment: PushEnvironment
    platform: str = "ios"
    enabled: bool = True
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    schema_version: int = SCHEMA_VERSION


def push_device_id(token: str) -> str:
    """문서 자리. **token 자체가 유일성 열쇠다.**

    `(user, token)`으로 만들지 않는다 — 그러면 같은 기기가 계정마다 문서를 하나씩
    갖게 되고, A로 로그아웃한 뒤에도 A의 문서가 살아 있어 **A가 계속 알림을 받는다.**
    자리를 token 하나로 두면 재등록이 곧 주인 교체다.
    """
    canonical = "|".join(
        f"{len(part.encode())}:{part}" for part in ("push_device", token)
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def token_fingerprint(token: str) -> str:
    """로그에 남길 짧은 지문. **raw token은 어디에도 찍지 않는다.**"""
    return push_device_id(token)[:12]


def checked_token(raw: str) -> str:
    """APNs token은 hex 문자열이다. 그 밖의 것을 저장하지 않는다."""
    token = raw.strip().lower()
    if not token or len(token) > MAX_TOKEN_LENGTH:
        raise InvalidPushDevice("token")
    if len(token) % 2 or any(c not in "0123456789abcdef" for c in token):
        raise InvalidPushDevice("token")
    return token


@dataclass(frozen=True)
class PushMessage:
    """보낼 내용 하나. **구매자 정보가 들어갈 자리가 없다.**"""

    title: str
    body: str
    #: 앱이 탭을 받아 어디로 갈지 정하는 데 쓴다. 알림센터 하나뿐이라 최소로 둔다.
    kind: str = "marketplace_sale"


@dataclass(frozen=True)
class PushOutcome:
    """한 기기에 보낸 결과.

    `terminal`은 **이 token이 앞으로도 안 된다**는 뜻이다(앱 삭제 등).
    일시적 실패와 반드시 구분한다 — 뭉치면 서버가 잠깐 흔들릴 때 사용자의
    기기 등록이 통째로 지워진다.
    """

    device_id: str
    delivered: bool
    terminal: bool = False
    status: int | None = None
