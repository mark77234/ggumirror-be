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
    #: AI 거울 생성. 스티커와 **다른 값**이라 원장만 보고 어디에 썼는지 알 수 있어야 한다.
    AI_MIRROR = "ai_mirror"
    # 상점 구매/판매. **콘텐츠 종류마다 나눈다** — 등록비(`*_publish_fee`)와 같은 규칙이다.
    # 원장만 보고 거울인지 스티커인지 알 수 있어야 한다.
    #
    # `mirror_*` 값은 **바꾸지 않는다.** 이름이 이제 "거울 전용"이라는 뜻이 됐을 뿐이고,
    # rename하면 과거 원장을 읽는 코드가 조용히 깨진다.
    MIRROR_PURCHASE = "mirror_purchase"
    MIRROR_SALE = "mirror_sale"
    STICKER_PURCHASE = "sticker_purchase"
    STICKER_SALE = "sticker_sale"
    # 상점 등록 비용. **콘텐츠 종류마다 값이 다르므로 reason도 나눈다** —
    # 하나로 합치면 원장만 보고 거울인지 스티커인지 알 수 없다.
    #
    # `mirror_publish_fee`라는 이름은 **바꾸지 않는다.** 값을 rename하면 과거 원장을
    # 읽는 코드가 조용히 깨진다. (production 원장에 아직 0건이지만 규칙은 규칙이다.)
    #: 운영자가 상품을 내렸을 때 판매자에게 주는 보상. **구매 환불이 아니다** —
    #: 산 사람은 그대로 갖고 있고, 되돌리는 것은 등록비 쪽이다. 그래서
    #: `refund` · `iap_refund`와 섞지 않는다(원장에서 셋을 구분할 수 있어야 한다).
    MARKETPLACE_MODERATION_COMPENSATION = "marketplace_moderation_compensation"
    MIRROR_PUBLISH_FEE = "mirror_publish_fee"
    STICKER_PUBLISH_FEE = "sticker_publish_fee"
    # AI 생성 실패 복구(A-1B). **Apple 환불에 재사용하지 않는다** — 다른 사건이다.
    REFUND = "refund"
    # Apple 환불로 실제 회수한 조각(B-6F-B). 회수 못 한 몫은 원장에 남지 않는다.
    IAP_REFUND = "iap_refund"
    # Apple이 환불을 되돌렸을 때 **회수했던 만큼만** 복구(B-6F-C).
    IAP_REFUND_REVERSED = "iap_refund_reversed"
    # 거울 보관 공간 확장. 상점 구매와 **다른 사건**이라 이유를 나눈다 —
    # 원장만 보고 무엇에 썼는지 알 수 있어야 한다.
    MIRROR_CAPACITY_PURCHASE = "mirror_capacity_purchase"
    #: 내장 템플릿 구매. **판매자가 없다** — 파는 사람 없이 사라지는 값이라
    #: Marketplace의 `mirror_purchase`/`mirror_sale` 짝과 섞지 않는다.
    CATALOG_TEMPLATE_PURCHASE = "catalog_template_purchase"
    ADMIN_ADJUSTMENT = "admin_adjustment"


@dataclass(frozen=True)
class ShardWallet:
    """지금 잔액. 없으면 0으로 본다 — 조회만으로 문서를 만들지 않는다."""

    user_id: str
    balance: int = 0
    # **누적 획득.** 환불이 나도 줄지 않는다 — 받았다는 사실은 사라지지 않는다.
    lifetime_earned: int = 0
    # **사용자가 실제로 쓴 양.** Apple 환불 회수를 여기 넣지 않는다 — 쓴 적이 없다.
    lifetime_spent: int = 0
    # **Apple 환불로 실제 회수한 양.** requested가 아니라 recovered만 쌓인다.
    # 예전 문서에는 없는 field라 읽을 때 0으로 본다(migration 없음).
    #
    # **gross다 — 환불이 되돌려져도 줄지 않는다**(`lifetime_earned`와 같은 규칙).
    # 되돌려진 몫은 아래 `lifetime_refund_reversed`에 따로 쌓인다.
    lifetime_refunded: int = 0
    # **환불이 되돌려져 복구한 양**(B-6F-C). 이것도 줄지 않는다.
    #
    # 이 field가 없으면 지갑만 보고는 숫자가 맞지 않는다 —
    # `balance == earned - spent - refunded + refund_reversed`가 성립해야
    # 운영자가 지갑 하나로 검산할 수 있다.
    lifetime_refund_reversed: int = 0
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
class DocumentKey:
    """문서 하나를 가리키는 값. **collection 이름은 부르는 쪽이 정한다.**

    `ExclusiveClaim`과 같은 규칙이다 — 원장이 IAP collection 이름을 알고 있지 않다.
    `key`는 이미 hash된 값이다(raw transaction id가 문서 ID에 노출되지 않는다).
    """

    collection: str
    key: str


@dataclass(frozen=True)
class RefundPlan:
    """되돌릴 양 + record에 함께 남길 값.

    **원본 구매 claim을 읽어야 알 수 있다.** 그래서 저장소가 transaction 안에서
    claim을 읽은 뒤 부르는 callback이 이것을 만든다 — 금액 정책과 claim schema는
    IAP 계층에 남고, 원장은 "받은 값을 원자적으로 쓰는 일"만 한다.
    """

    requested: int
    fields: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ShardRefundResult:
    """Apple 환불 한 건의 결과.

    `requested`는 **Apple이 되돌리라고 한 양**, `recovered`는 **지갑에서 실제로 뺀 양**이다.
    잔액이 모자라면 둘이 다르고, 그 차이(`unrecovered`)는 **빚이 아니다** —
    나중에 번 조각에서 자동으로 상계하지 않는다.

    `applied`는 `ShardMutationResult`와 같은 뜻이다 — **이번 호출이 기록했는가**.
    같은 환불이 다시 오면 `False`이고, 그때도 나머지 값은 처음 처리한 그대로다.
    """

    wallet: ShardWallet
    requested: int
    recovered: int
    applied: bool
    ledger_entry_id: str | None = None

    @property
    def unrecovered(self) -> int:
        return self.requested - self.recovered


@dataclass
class ShardTransactionContext:
    """한 transaction **시도(attempt)** 동안의 조각 변경 기록.

    ⚠️ **수명이 attempt 하나여야 한다.** Firestore는 commit이 `ABORTED`되면
    **같은 Python `Transaction` 객체로** callable을 다시 부른다(설치본 2.22.0의
    `_Transactional.__call__`이 loop 안에서 같은 객체를 넘긴다). 그때 `_clean_up()`이
    지우는 것은 `_write_pbs`와 `_id`뿐이라, transaction 객체에 우리가 붙인 표시는
    **다음 attempt까지 살아남는다.**

    그러면 아무것도 commit되지 않았는데 재시도가 `WalletAlreadyChanged`로 거절된다 —
    실제로 재현했다. 그래서 표시를 transaction이 아니라 **이 객체**에 담고,
    callable 안에서 매번 새로 만든다:

        @firestore.transactional
        def run(transaction):
            scoped = shards.context(transaction)   # ← attempt마다 새로 생긴다
            shards.apply_in_transaction(scoped, buyer, -price, ...)

    **commit 권한이 없다.** 여전히 호출자의 Firestore transaction이 commit한다.
    """

    transaction: object
    changed_wallets: set[str] = field(default_factory=set)
    #: 아직 transaction에 내려보내지 않은 쓰기.
    #:
    #: **Firestore transaction은 쓰기 뒤 읽기를 허용하지 않는다.** 지갑 두 개가
    #: 움직이는 구매에서 첫 번째(구매자 차감)가 곧바로 쓰면, 두 번째(판매자 지급)의
    #: 읽기가 `ReadAfterWriteError`로 죽는다 — production에서 실제로 그랬다.
    #: 그래서 계산만 해 두고 `flush()`에서 한 번에 내려보낸다.
    pending_writes: list = field(default_factory=list)

    def stage(self, write) -> None:
        """쓰기를 미뤄 둔다. 아직 transaction을 건드리지 않으므로 뒤에 읽어도 된다."""
        self.pending_writes.append(write)

    def flush(self) -> None:
        """미뤄 둔 쓰기를 전부 내려보낸다. **읽기를 모두 마친 뒤에 부른다.**

        이 뒤로는 같은 transaction에서 읽을 수 없다.
        """
        for write in self.pending_writes:
            write()
        self.pending_writes.clear()

    def claim(self, user_id: str) -> None:
        """이 attempt에서 이 지갑을 바꾼다고 표시한다.

        같은 attempt에서 같은 지갑을 두 번 바꾸면 두 번째가 첫 번째를 덮어쓴다 —
        Firestore transaction의 읽기가 시작 시점 snapshot이기 때문이다.
        자기 자신에게 파는 경우가 정확히 그 모양이라 여기서 막는다.
        """
        if user_id in self.changed_wallets:
            raise WalletAlreadyChanged(user_id)
        self.changed_wallets.add(user_id)


@dataclass(frozen=True)
class ShardRefundReversalResult:
    """환불 되돌리기 결과.

    `restored`는 **이번 호출이 실제로 복구한 양**이다. 이미 다 되돌렸거나 애초에
    회수한 것이 없으면 0이고, 그때 `applied`는 `False`다 — 실패가 아니라 할 일이 없는 것이다.
    """

    wallet: ShardWallet
    restored: int
    applied: bool
    ledger_entry_id: str | None = None


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
    #: 그 사건의 원장 문서 ID(= 멱등 열쇠). 소유권 기록처럼 **원장을 가리켜야 하는**
    #: 호출부가 다시 계산하지 않게 함께 돌려준다.
    entry_id: str | None = None


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


class PurchaseClaimMissing(ShardError):
    """되돌릴 원본 구매 기록이 없다. **아무것도 기록되지 않는다.**

    우리가 조각을 준 적 없는 결제라는 뜻이다 — 실패가 아니라 **할 일이 없는 것**이다.
    Apple이 다시 보내도 답이 같으므로 재시도를 요구하지 않는다.
    """


class RefundRecordMissing(ShardError):
    """되돌릴 환불 기록이 없다. **아무것도 기록되지 않는다.**

    우리가 회수한 적 없는 결제라는 뜻이다 — 복구할 것이 없다.
    """


class RefundNotYetProcessed(ShardError):
    """환불 기록이 아직 없지만 **그 결제는 우리가 지급한 것**이다.

    `REFUND`가 아직 도착하지 않았을 수 있다 — Apple V2 payload에는 순서를 알려주는
    field가 없고(`notificationUUID` · `signedDate`뿐), 우리는 notification history를
    조회할 수단(`.p8`)도 갖고 있지 않다. 그래서 "지금 없다"를 "영영 없다"로 단정하지 않는다.

    **200으로 삼키면 사용자가 되돌려받아야 할 조각을 영구히 잃는다.**
    재시도를 받을 수 있게 올린다.
    """


class WalletAlreadyChanged(ShardError):
    """**같은 transaction 안에서 한 지갑을 두 번 바꾸려 했다.**

    Firestore transaction의 읽기는 시작 시점 snapshot이라, 두 번째 호출은 첫 번째가
    계산한 잔액을 보지 못한다. 그대로 두면 뒤엣것이 앞엣것을 덮어써 조각이 조용히 사라진다.

    marketplace 자기거래(`buyer == seller`)가 정확히 이 모양이라 여기서 막는다 —
    상위 service의 검사에만 기대지 않는다.
    """


class QuotaExceeded(ShardError):
    """그 기간의 상한을 이미 채웠다. **아무것도 기록되지 않는다.**

    실패가 아니라 **정상적인 거절**이다 — 광고를 5번 본 사람이 6번째를 본 것은
    오류가 아니고, 조각만 주지 않으면 된다.
    """
