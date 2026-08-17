"""조각 IAP 모델.

**Apple이 서명한 값만 믿는다.** client가 보내는 것은 서명된 JWS 하나뿐이고,
수량 · 가격 · productId · userId를 받는 자리가 없다.

지급 수량의 authority는 **서버 catalog**다(`SHARD_PRODUCTS`).
JWS 안의 productId를 열쇠로 쓰고, client가 말한 productId는 쓰지 않는다.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from enum import StrEnum

SCHEMA_VERSION = 1

# productId → 지급 조각. **서버가 정하는 유일한 표다.**
#
# identifier는 App Store Connect에서 만든 실제 값이고 재사용할 수 없다.
# 티어를 바꾸려면 새 product를 만들어야 하므로 이 표를 함부로 고치지 않는다.
SHARD_PRODUCTS: dict[str, int] = {
    "com.mark77234.ggumirror.shards.10": 10,
    "com.mark77234.ggumirror.shards.50": 50,
    "com.mark77234.ggumirror.shards.100": 100,
}

# consumable만 판다. 구독도 영구 entitlement도 아니다.
CONSUMABLE_TYPE = "Consumable"


class IAPEnvironment(StrEnum):
    """Apple이 서명해 보내는 환경. **JWS 안의 값만 믿는다.**"""

    PRODUCTION = "Production"
    SANDBOX = "Sandbox"
    # Xcode StoreKit Testing이 만드는 값. **production backend가 절대 받지 않는다** —
    # 로컬 서명이라 Apple 신뢰 사슬이 없고, 받으면 누구나 조각을 만들 수 있다.
    XCODE = "Xcode"


@dataclass(frozen=True)
class VerifiedTransaction:
    """Apple 서명 검증을 통과한 transaction. **여기 오는 값은 전부 서명된 것이다.**"""

    transaction_id: str
    product_id: str
    bundle_id: str
    environment: str
    # StoreKit `purchase(options: [.appAccountToken(...)])`로 실린 값.
    # 이것이 Apple transaction을 **우리 user에 묶는 유일한 끈**이다.
    app_account_token: str | None
    transaction_type: str = CONSUMABLE_TYPE
    # consumable의 멱등 열쇠가 **아니다**(재구매마다 transaction_id가 새로 나온다).
    # 감사용으로만 둔다.
    original_transaction_id: str | None = None

    @property
    def shard_amount(self) -> int | None:
        return SHARD_PRODUCTS.get(self.product_id)


@dataclass(frozen=True)
class IAPResult:
    """지급 결과. `credited`는 **이번 요청이 원장에 적었는가**다.

    같은 transaction 재전송이면 `False`이고 그때도 `balance`는 정상 현재 잔액이다 —
    실패가 아니다(B-4 `claimed`와 같은 뜻).
    """

    credited: bool
    amount: int
    balance: int


class IAPError(Exception):
    """지급 실패. endpoint에서 client에 맞는 응답으로 바꾼다."""


class IAPUnavailable(IAPError):
    """검증기가 설정되지 않았다. **fail closed** — 검증 없이 지급하지 않는다."""


class InvalidTransaction(IAPError):
    """서명 · bundle · 형식이 맞지 않는다."""


class UnknownProduct(IAPError):
    """서버 catalog에 없는 productId. 추측해서 지급하지 않는다."""


class EnvironmentNotAllowed(IAPError):
    """허용되지 않은 환경(기본은 아무것도 허용하지 않는다)."""


class AccountTokenMismatch(IAPError):
    """`appAccountToken`이 없거나 지금 로그인한 사용자와 다르다.

    이것이 "남의 결제로 내 지갑을 채우는" 경로를 막는다.
    """


class TransactionAlreadyClaimed(IAPError):
    """이 Apple transaction을 **다른 사용자가** 이미 썼다.

    user별 원장 멱등만으로는 막히지 않는다 — 그래서 전역 claim이 따로 있다.
    """


def transaction_claim_id(transaction_id: str) -> str:
    """전역 claim 문서 ID.

    **`user_id`가 들어가지 않는다.** 그게 요점이다 — 같은 Apple transaction이
    다른 사용자 이름으로 다시 들어와도 **같은 문서**를 겨루게 만들어야 한다.
    user를 섞으면 namespace가 갈라져 이중 지급이 열린다.

    raw transaction id를 문서 ID로 쓰지 않는다. 길이 접두사 canonical encoding은
    원장(`idempotency_hash`)과 같은 규칙이다.
    """
    canonical = "|".join(
        f"{len(part.encode())}:{part}" for part in ("iap_transaction", transaction_id)
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def transaction_log_id(transaction_id: str) -> str:
    """로그에 남기는 짧은 지문. raw id를 남기지 않는다(B-5 SSV와 같은 규칙)."""
    return hashlib.sha256(transaction_id.encode()).hexdigest()[:12]


def same_account_token(token: str | None, user_id: str) -> bool:
    """`appAccountToken`이 이 사용자인지.

    Apple은 UUID를 소문자로 돌려주지만 표기가 흔들려도 같은 UUID면 같은 사람이다.
    문자열 비교로 두면 대문자 하나 때문에 정당한 결제가 거절된다.
    """
    if not token:
        return False
    try:
        return uuid.UUID(token) == uuid.UUID(user_id)
    except (ValueError, AttributeError, TypeError):
        return False


def parse_allowed_environments(raw: str) -> frozenset[str]:
    """`IAP_ALLOWED_ENVIRONMENTS`를 읽는다. **비어 있으면 아무것도 허용하지 않는다.**

    `Xcode`는 값에 적혀 있어도 **절대 허용하지 않는다** — Xcode StoreKit Testing의
    서명은 로컬에서 만들어지므로, 받아 주면 조각을 무한히 만들 수 있다.
    로컬 테스트는 client UX/복구 확인용이고 실제 지급 검증은 Sandbox/TestFlight로 한다.
    """
    allowed = {
        part.strip()
        for part in raw.split(",")
        if part.strip() in {IAPEnvironment.PRODUCTION, IAPEnvironment.SANDBOX}
    }
    return frozenset(allowed)
