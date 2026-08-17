"""거울 조각 지갑 / 원장 모델.

**서버가 잔액의 유일한 권위다.** client가 보낸 숫자로 잔액을 바꾸지 않는다.

두 가지가 있다:
- **wallet** — 지금 잔액. 빠르게 읽으려고 두는 projection이다
- **ledger** — 무슨 일이 있었는지. **append-only**, 고치거나 지우지 않는다

조각은 **정수**다. 소수점이 필요한 개념이 아니다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4

SCHEMA_VERSION = 1

# 한 번에 움직일 수 있는 최대 조각. 실수나 공격으로 잔액이 폭주하지 않게 막는다.
# ponytail: 상수 하나로 충분하다. 정교한 rate limit이 필요해지면 그때.
MAX_DELTA = 100_000


def utcnow() -> datetime:
    """server 시계. client가 보낸 시간을 근거로 쓰지 않는다."""
    return datetime.now(timezone.utc)


class ShardReason(StrEnum):
    """조각이 움직인 이유. **원장에 남는 값이라 함부로 바꾸지 않는다.**

    여기 있다고 그 기능이 구현된 것은 아니다 — B-4 이후가 이 값을 쓴다.
    """

    DAILY_ATTENDANCE = "daily_attendance"
    REWARDED_AD = "rewarded_ad"
    IAP_PURCHASE = "iap_purchase"
    AI_STICKER = "ai_sticker"
    MIRROR_PURCHASE = "mirror_purchase"
    MIRROR_SALE = "mirror_sale"
    MIRROR_PUBLISH_FEE = "mirror_publish_fee"
    REFUND = "refund"
    ADMIN_ADJUSTMENT = "admin_adjustment"


@dataclass(frozen=True)
class ShardWallet:
    """지금 잔액. 없으면 0으로 본다 — 조회만으로 문서를 만들지 않는다."""

    user_id: str
    balance: int = 0
    lifetime_earned: int = 0
    lifetime_spent: int = 0
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    @staticmethod
    def empty(user_id: str) -> ShardWallet:
        return ShardWallet(user_id=user_id)


@dataclass(frozen=True)
class ShardLedgerEntry:
    """무슨 일이 있었는지 한 줄. **만든 뒤 고치지 않는다.**

    `delta`는 부호 있는 값이다(+는 획득, -는 사용).
    `balance_after`를 함께 남겨 두면 나중에 잔액을 재계산하지 않고도 흐름을 읽을 수 있다.
    """

    id: str
    user_id: str
    delta: int
    balance_after: int
    reason: ShardReason
    # 같은 사건이 두 번 들어오지 않게 하는 열쇠. **원본 값이 아니라 hash를 저장한다.**
    idempotency_key_hash: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    schema_version: int = SCHEMA_VERSION

    @staticmethod
    def new_id() -> str:
        return str(uuid4())


@dataclass(frozen=True)
class PeriodQuota:
    """한 기간에 몇 번까지 허용할지. 광고 보상의 "하루 5회"가 첫 사용자다.

    **잔액의 authority가 아니다.** 경제의 진실은 원장이고, 이건 "몇 번 줬는지"를
    원자적으로 세기 위한 operational counter다.

    확인과 증가가 **지급과 같은 transaction 안에서** 일어나야 한다.
    "세어보고 → 모자라면 준다"로 나누면 동시에 들어온 callback이 상한을 넘겨 지급한다.

    `key`는 이미 hash된 값이다 — raw user id나 날짜를 문서 ID에 노출하지 않는다.
    """

    key: str
    limit: int


@dataclass(frozen=True)
class ExclusiveClaim:
    """**전역에서 한 번만** 쓸 수 있는 자리. 원장 기록과 같은 transaction에서 만든다.

    원장 멱등(`idempotency_hash`)은 열쇠에 `user_id`가 들어가므로 **user 안에서만**
    유일하다. 그래서 같은 외부 사건 id가 다른 사용자 이름으로 들어오면 막히지 않는다.
    IAP transaction(B-6)이 첫 사용자다 — 같은 Apple transaction으로 두 사람이
    각각 조각을 받는 일이 있으면 안 된다.

    이미 있으면 **주인을 본다**: 같은 사용자면 재전송(중복)이고, 다른 사용자면 거절이다.

    `PeriodQuota`와 같은 자리다 — `apply`가 한 transaction 안에서 함께 쓰는 문서다.
    `collection`은 부르는 쪽이 정한다. 원장이 남의 collection 이름을 알고 있지 않다.
    """

    collection: str
    key: str
    document: dict
    # 주인을 판단할 field 이름.
    owner_field: str = "userId"


@dataclass(frozen=True)
class ShardMutationResult:
    """조각을 움직인 결과.

    `applied`는 **이번 호출이 실제로 원장에 줄을 적었는가**다.
    이미 있던 사건이면 `False`이고, 그때도 `wallet`은 정상적인 현재 잔액이다
    (실패가 아니다 — 같은 사건이 두 번 도착하는 것은 정상 동작이다).

    이 값은 **추측하지 않는다.** "먼저 조회해서 없으면 쓴다"로 판단하면
    조회와 쓰기 사이에 다른 요청이 끼어들어 둘 다 "내가 적었다"고 답한다.
    `applied`는 오직 **원장 쓰기를 시도한 그 transaction의 결과**에서 나온다.

    출석(B-4)이 첫 사용자다. 광고 SSV(B-5) · IAP(B-6) · 구매(B-8)도
    "이번 callback이 실제 지급했는가"를 같은 방식으로 알아야 한다.
    """

    wallet: ShardWallet
    applied: bool


def idempotency_hash(user_id: str, reason: ShardReason, external_event_id: str) -> str:
    """같은 사용자의 같은 사건인지 판단하는 열쇠.

    **원본을 저장하지 않는다.** AdMob SSV `transaction_id`나 StoreKit transaction id처럼
    외부 식별자가 그대로 원장에 남으면 안 되기 때문이다. raw user id도 문서 ID에 노출되지 않는다.

    `user_id`가 **반드시** 들어간다. 없으면 출석처럼 event id가 날짜뿐인 경우
    (`daily_attendance` + `2026-08-12`) 모든 사용자가 같은 문서 ID를 겨루게 되고,
    하루에 한 사람만 조각을 받는다. 호출부가 event id에 user id를 넣어주기를
    기대하지 않고 **여기서 강제한다.**

    세 값을 길이 접두사로 이어 붙인다. 어떤 값에 `:`나 `|`가 들어 있어도
    다른 조합과 같은 문자열이 되지 않는다 — `("a:b", "c")`와 `("a", "b:c")`가
    구분되지 않으면 서로 다른 사건이 하나로 합쳐진다.
    """
    canonical = "|".join(
        f"{len(part.encode())}:{part}" for part in (user_id, reason.value, external_event_id)
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class ShardError(Exception):
    """조각 처리 실패. endpoint에서 client에 맞는 응답으로 바꾼다."""


class InsufficientShards(ShardError):
    """잔액이 모자란다. **아무것도 기록되지 않는다** — 잔액도 원장도 그대로다."""


class InvalidShardAmount(ShardError):
    """0 이하이거나 너무 큰 값. 도메인에서 막는다."""


class ClaimOwnedByAnother(ShardError):
    """전역 claim을 **다른 사용자가** 이미 갖고 있다. **아무것도 기록되지 않는다.**

    재전송(같은 사용자)과 구분해서 올린다 — 전자는 정상이고 이것은 거절이다.
    """


class QuotaExceeded(ShardError):
    """그 기간의 상한을 이미 채웠다. **아무것도 기록되지 않는다.**

    실패가 아니라 **정상적인 거절**이다 — 광고를 5번 본 사람이 6번째를 본 것은
    오류가 아니고, 조각만 주지 않으면 된다.
    """
