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
    # 아래 둘은 **환불 알림에서만** 채워진다(구매 transaction에는 없다).
    # `REFUND_FULL` · `REFUND_PRORATED` · `FAMILY_REVOKE` — Apple이 서명한 값이다.
    revocation_type: str | None = None
    # ⚠️ **milliunits다. 0..100 퍼센트가 아니다.** `100% = 100000`, `0.015% = 15`.
    # Apple 공식 문서와 `app-store-server-library`의 field 주석이
    # "The percentage, in milliunits"라고 말한다. 이름에 단위를 박아 둔 이유는
    # 실제로 이것을 0..100으로 오해해 100배 적게 회수하는 버그를 냈기 때문이다.
    #
    # `REFUND_PRORATED`일 때만 온다. **없으면 추측하지 않는다**(mutation 0).
    revocation_percentage_milliunits: int | None = None

    @property
    def shard_amount(self) -> int | None:
        return SHARD_PRODUCTS.get(self.product_id)


@dataclass(frozen=True)
class VerifiedNotification:
    """서명 검증을 통과한 App Store Server Notification V2.

    **여기 오는 값은 전부 Apple이 서명한 것**이고, 안쪽 transaction도 따로 검증됐다.
    """

    notification_type: str
    subtype: str | None
    notification_uuid: str
    bundle_id: str
    app_apple_id: int | None
    environment: str
    # `signedTransactionInfo`가 있는 알림만 채워진다(TEST에는 없다).
    transaction: VerifiedTransaction | None = None


class NotificationOutcome(StrEnum):
    """알림 처리 결과. **응답 status를 여기서 정한다.**"""

    # 검증했고 우리 경제에 할 일이 없다. Apple이 다시 보낼 필요가 없다.
    ACKNOWLEDGED = "acknowledged"
    # 검증은 됐지만 **아직 처리할 수 없다**(B-6F-B/C 미구현, 모르는 타입).
    # **200으로 삼키지 않는다** — Apple이 다시 보내게 둔다.
    DEFERRED = "deferred"


# 검증만 하고 **경제를 건드리지 않는** 알림. 여기 없는 타입은 전부 deferred다.
#
# "모르는 타입이면 일단 200"으로 두면, 조각에 영향을 주는 새 알림이 조용히 사라진다.
# 그래서 **allowlist**로 간다 — 우리가 no-op이라고 판단한 것만 소비한다.
ACKNOWLEDGED_NOTIFICATIONS = frozenset({
    # Apple이 URL 설정을 확인할 때 보낸다. transaction이 없다.
    "TEST",
    # **환불 승인이 아니다.** Apple이 소비 정보를 물어보는 것뿐이고,
    # 우리는 동의 흐름이 없어 응답하지 않는다(조각도 건드리지 않는다).
    "CONSUMPTION_REQUEST",
    # 환불이 거절됐다. 되돌릴 것이 없다.
    "REFUND_DECLINED",
    # **consumable 구매의 정상 알림이다.** 조각 IAP가 consumable이므로 실제로 온다.
    #
    # ⚠️ **지급 authority가 아니다.** 조각은 client가 가져온 서명 transaction을
    # `POST /users/me/iap/shards`가 검증해 지급하고, 그 경로에만 전역 claim이 걸린다.
    # 이 알림으로 또 지급하면 **한 결제에 두 번 지급**된다.
    #
    # deferred로 두면 안 된다 — 정상 알림에 503을 주면 Apple이 영원히 재시도한다.
    "ONE_TIME_CHARGE",
})

# 검증은 하되 **아직 구현하지 않은** 것. 재시도를 받아야 한다.
DEFERRED_NOTIFICATIONS = frozenset({
    # `REFUND`는 B-6F-B에서 실제로 처리한다 — 여기 없다.
    "REFUND_REVERSED",  # B-6F-C
})

# 환불 알림. **allowlist도 deferred도 아닌 세 번째 갈래**라 따로 둔다 —
# 검증한 뒤 조각을 실제로 회수하기 때문이다.
REFUND_NOTIFICATION = "REFUND"

# `revocationType`(Apple 서명 값). library enum과 같은 문자열이고,
# **우리가 새로 만들지 않는다.**
REFUND_FULL = "REFUND_FULL"
REFUND_PRORATED = "REFUND_PRORATED"
FAMILY_REVOKE = "FAMILY_REVOKE"


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


class NotificationNotHandled(IAPError):
    """검증은 됐지만 아직 처리할 수 없다. **재시도 가능한 실패로 올린다.**"""


class AccountTokenMismatch(IAPError):
    """`appAccountToken`이 없거나 지금 로그인한 사용자와 다르다.

    이것이 "남의 결제로 내 지갑을 채우는" 경로를 막는다.
    """


class TransactionAlreadyClaimed(IAPError):
    """이 Apple transaction을 **다른 사용자가** 이미 썼다.

    user별 원장 멱등만으로는 막히지 않는다 — 그래서 전역 claim이 따로 있다.
    """


class RefundMismatch(IAPError):
    """환불 알림이 **원본 구매 기록과 다르다.** 아무것도 되돌리지 않는다.

    product · environment · 주인이 어긋났다는 뜻이라 재시도해도 같다.
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


def refund_record_id(transaction_id: str) -> str:
    """환불 business record의 문서 ID.

    **원본 구매 transaction 하나당 record 하나**다 — `notificationUUID`를 쓰지 않는다.
    Apple이 같은 환불을 다른 UUID로 다시 보내도 같은 자리를 겨뤄야 하기 때문이다.

    `transaction_claim_id`와 같은 규칙이고 namespace만 다르다.
    raw transaction id를 문서 ID로 쓰지 않는다.
    """
    canonical = "|".join(
        f"{len(part.encode())}:{part}" for part in ("iap_refund", transaction_id)
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def account_token_user_id(token: str | None) -> str | None:
    """`appAccountToken`을 우리 user id 표기(canonical UUID 문자열)로 정규화한다.

    Apple 표기가 흔들려도 같은 UUID면 같은 사람이지만, **지갑 문서 ID는 문자열이다** —
    정규화하지 않고 그대로 쓰면 대문자 하나 때문에 **다른 문서**를 만지게 된다.
    우리 user id는 `str(uuid4())`라 이 표기와 같다.
    """
    if not token:
        return None
    try:
        return str(uuid.UUID(token))
    except (ValueError, AttributeError, TypeError):
        return None


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
