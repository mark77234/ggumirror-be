"""조각을 움직이는 **유일한 통로.**

앞으로 출석(B-4) · AdMob SSV(B-5) · IAP(B-6) · 패스(B-7) · 상점(B-8)이
전부 이 두 함수만 부른다. Firestore 잔액을 직접 만지는 코드를 기능마다 복제하지 않는다.

**client가 이 함수를 직접 부를 수 있는 통로는 없다.** 범용 credit/debit endpoint를
만들지 않았고, 만들지 않는다. client가 `reason=rewarded_ad, amount=10000`을 보내면
서버가 그대로 믿는 구조가 되기 때문이다.

각 기능은 자기가 신뢰할 수 있는 사건을 먼저 검증한 뒤에만 여기로 온다:

    출석    → 서버 날짜로 하루 한 번인지 확인 → credit(+1, external_event_id="2026-08-12")
    광고    → Google SSV 서명 검증 → credit(+1, external_event_id=<SSV transaction_id>)
    IAP     → StoreKit transaction 검증 → credit(+N, external_event_id=<transaction id>)
    상점    → 서버가 만든 구매 id → debit(가격) / credit(판매자)
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from app.shards.models import (
    MAX_DELTA,
    DocumentKey,
    ExclusiveClaim,
    InvalidShardAmount,
    PeriodQuota,
    RefundPlan,
    ShardMutationResult,
    ShardReason,
    ShardRefundResult,
    ShardTransactionContext,
    ShardRefundReversalResult,
    ShardWallet,
    idempotency_hash,
)
from app.shards.store import ShardStore

logger = logging.getLogger(__name__)


class ShardLedgerService:
    def __init__(self, store: ShardStore) -> None:
        self._store = store

    # MARK: - 읽기

    def wallet(self, user_id: str) -> ShardWallet:
        wallet = self._store.wallet(user_id)
        logger.info("shard_wallet_read balance=%d", wallet.balance)
        return wallet

    def has_event(self, user_id: str, reason: ShardReason, external_event_id: str) -> bool:
        """이 사건이 이미 반영됐는가. **원장이 답한다.**

        출석 상태 조회(B-4)처럼 지급 없이 물어보기만 할 때 쓴다. 열쇠를 만드는 방식은
        `_apply`와 **같은 함수 하나**다 — 두 곳에서 따로 만들면 언젠가 어긋난다.
        """
        return self._store.event_applied(idempotency_hash(user_id, reason, external_event_id))

    def quota_used(self, user_id: str, reason: ShardReason, period: str) -> int:
        """그 기간에 이 이유로 몇 번 지급됐는지. **표시용 읽기**다.

        광고 버튼의 `오늘 2 / 5`를 그리는 데 쓴다. 지급 경로에서 부르지 않는다 —
        세어보고 지급하면 동시 요청이 상한을 넘긴다.
        """
        return self._store.quota_used(self._quota_key(user_id, reason, period))

    @staticmethod
    def _quota_key(user_id: str, reason: ShardReason, period: str) -> str:
        """counter 문서의 열쇠. 원장 열쇠와 **같은 함수**로 만들되 namespace를 분리한다.

        raw user id도 날짜도 문서 ID에 남지 않는다.
        """
        return idempotency_hash(user_id, reason, f"quota:{period}")

    # MARK: - 쓰기

    def credit(
        self,
        user_id: str,
        amount: int,
        reason: ShardReason,
        external_event_id: str | None = None,
        period: str | None = None,
        limit: int | None = None,
        claim: ExclusiveClaim | None = None,
    ) -> ShardMutationResult:
        """조각을 준다.

        `external_event_id`는 **그 사건을 유일하게 가리키는 값**이다
        (AdMob SSV transaction_id · StoreKit transaction id · 출석 날짜 …).
        같은 값이 다시 오면 **한 번만** 반영된다 — 재시도와 중복 callback이 잔액을 부풀리지 않는다.

        결과의 `applied`가 **이번 호출이 실제로 지급했는가**다. 재시도로 들어온 요청에
        "지급했다"고 답하지 않으려면 이 값을 쓴다 — 따로 조회해서 짐작하지 않는다.

        `period` + `limit`을 함께 주면 **그 기간에 `limit`번까지만** 지급한다
        (광고 보상의 하루 5회). 확인과 증가가 지급과 같은 transaction 안에서 일어나고,
        이미 찼으면 `QuotaExceeded`가 난다 — 아무것도 기록되지 않는다.
        """
        quota = (
            PeriodQuota(key=self._quota_key(user_id, reason, period), limit=limit)
            if period is not None and limit is not None
            else None
        )
        return self._apply(
            user_id, self._checked(amount), reason, external_event_id, quota, claim
        )

    def debit(
        self,
        user_id: str,
        amount: int,
        reason: ShardReason,
        external_event_id: str | None = None,
    ) -> ShardMutationResult:
        """조각을 쓴다. 잔액이 모자라면 `InsufficientShards` — 아무것도 기록되지 않는다."""
        return self._apply(user_id, -self._checked(amount), reason, external_event_id)

    def refund_iap(
        self,
        user_id: str,
        external_event_id: str,
        purchase: DocumentKey,
        record: DocumentKey,
        document: dict,
        plan: Callable[[dict], RefundPlan],
    ) -> ShardRefundResult:
        """Apple 환불을 반영한다. **`debit`이 아니다.**

        `debit`의 음수 delta는 `lifetime_spent`로 집계되는데, 환불은 사용자가 쓴 것이
        아니다. 그 칸에 넣으면 "얼마나 썼는가"가 거짓말이 되므로 projection이 다른
        전용 경로를 쓴다 — generic debit의 의미는 그대로 둔다.

        되돌릴 양은 `plan`이 **원본 구매 claim을 보고** 정한다. 알림이 말한 값도,
        지금의 catalog 값도 쓰지 않는다(catalog는 나중에 바뀔 수 있고, 그러면
        예전 구매를 잘못된 금액으로 되돌리게 된다).

        열쇠는 다른 이유와 **같은 함수**로 만든다 — user scope도 여기서 강제한다.
        """
        key = idempotency_hash(user_id, ShardReason.IAP_REFUND, external_event_id)
        result = self._store.refund(user_id, purchase, record, document, key, plan)
        # 값만 남긴다. 누구인지 · 어떤 transaction인지는 남기지 않는다.
        logger.info(
            "shard_refund reason=%s requested=%d recovered=%d unrecovered=%d balance=%d applied=%s",
            ShardReason.IAP_REFUND.value, result.requested, result.recovered,
            result.unrecovered, result.wallet.balance, result.applied,
        )
        return result

    def reverse_iap_refund(
        self,
        user_id: str,
        external_event_id: str,
        purchase: DocumentKey,
        record: DocumentKey,
        remaining: Callable[[dict], int],
    ) -> ShardRefundReversalResult:
        """Apple이 되돌린 환불만큼 복구한다. **`credit`이 아니다.**

        `credit`은 `lifetime_earned`를 올리는데, 이 조각은 원래 구매 때 이미 earned로
        세어졌다 — 또 올리면 한 결제를 두 번 센다. 그래서 projection이 다른 전용 경로다.

        복구량은 `remaining`이 **환불 record를 보고** 정한다(`recovered - reversed`).
        원본 지급량도, Apple이 요청했던 양도, catalog 값도 쓰지 않는다.
        """
        key = idempotency_hash(user_id, ShardReason.IAP_REFUND_REVERSED, external_event_id)
        result = self._store.reverse_refund(user_id, purchase, record, key, remaining)
        logger.info(
            "shard_refund_reversed reason=%s restored=%d balance=%d applied=%s",
            ShardReason.IAP_REFUND_REVERSED.value, result.restored,
            result.wallet.balance, result.applied,
        )
        return result

    # MARK: - 호출자가 소유하는 transaction (B-7B)

    def transaction(self):
        """marketplace처럼 **다른 문서와 함께 commit해야 하는** 호출자가 transaction을 연다."""
        return self._store.transaction()

    def context(self, transaction) -> ShardTransactionContext:
        """**transactional callable 안에서 매번** 부른다.

        Firestore는 commit이 `ABORTED`되면 **같은 transaction 객체로** callable을 다시
        부른다. 시도마다 새 기록으로 시작해야 재시도가 잘못 거절되지 않는다.
        """
        return self._store.context(transaction)

    def apply_in_transaction(
        self,
        context: ShardTransactionContext,
        user_id: str,
        delta: int,
        reason: ShardReason,
        external_event_id: str,
    ) -> ShardMutationResult:
        """이미 열려 있는 transaction에 조각 이동 하나를 얹는다. **commit하지 않는다.**

        marketplace가 유일한 사용자다 — 구매는 **구매자 지갑 · 판매자 지갑 · 원장 두 줄 ·
        ownership**이 한 transaction에서 커밋돼야 하고, `credit`/`debit`은 각자 transaction을
        열어 버려서 "구매자만 차감된" 중간 상태를 만든다.

        **범용 이체 함수가 아니다.** 보내는 사람 · 받는 사람을 한 번에 받는 자리가 없고,
        방향은 `delta`의 부호가 아니라 **호출부가 고른 `reason`**과 함께 읽힌다.
        같은 지갑을 한 transaction에서 두 번 바꾸려 하면 저장소가 거절한다
        (자기 자신에게 파는 경우가 정확히 그 모양이다).

        열쇠는 다른 이유와 **같은 함수**로 만든다 — user scope도 여기서 강제한다.
        """
        key = idempotency_hash(user_id, reason, external_event_id)
        wallet, entry, applied = self._store.apply_in_transaction(
            context, user_id, self._moved(delta), reason, key
        )
        event = "shard_ledger_credit" if entry.delta > 0 else "shard_ledger_debit"
        logger.info(
            "%s reason=%s delta=%d balance=%d applied=%s scoped=True",
            event, reason.value, entry.delta, wallet.balance, applied,
        )
        return ShardMutationResult(wallet=wallet, applied=applied, entry_id=entry.id)

    @staticmethod
    def _moved(delta: int) -> int:
        """0은 이동이 아니다. 크기 검사는 `_checked`와 같은 규칙을 쓴다."""
        if not isinstance(delta, int) or isinstance(delta, bool):
            raise InvalidShardAmount("delta must be an integer")
        if delta == 0:
            raise InvalidShardAmount("delta must not be zero")
        if abs(delta) > MAX_DELTA:
            raise InvalidShardAmount("delta is too large")
        return delta

    # MARK: - 내부

    def _apply(
        self,
        user_id: str,
        delta: int,
        reason: ShardReason,
        external_event_id: str | None,
        quota: PeriodQuota | None = None,
        claim: ExclusiveClaim | None = None,
    ) -> ShardMutationResult:
        # user scope는 **service가 강제한다.** 호출부가 event id에 user id를 넣어주기를
        # 기대하면, 잊은 곳 하나가 사용자끼리 같은 문서를 겨루게 만든다.
        key = idempotency_hash(user_id, reason, external_event_id) if external_event_id else None
        # `applied`는 저장소의 원자적 쓰기 결과다. 여기서 다시 판단하지 않는다.
        wallet, entry, applied = self._store.apply(user_id, delta, reason, key, quota, claim)

        event = "shard_ledger_credit" if delta > 0 else "shard_ledger_debit"
        # 값은 남기되 누구인지 · 어떤 외부 id인지는 남기지 않는다.
        logger.info(
            "%s reason=%s delta=%d balance=%d applied=%s",
            event, reason.value, entry.delta, wallet.balance, applied,
        )
        return ShardMutationResult(wallet=wallet, applied=applied)

    @staticmethod
    def _checked(amount: int) -> int:
        """0 이하나 터무니없이 큰 값을 도메인에서 막는다.

        `bool`이 `int`의 하위 타입이라 `True`가 1로 새어 들어오는 것도 막는다.
        """
        if not isinstance(amount, int) or isinstance(amount, bool):
            raise InvalidShardAmount("amount must be an integer")
        if amount <= 0:
            raise InvalidShardAmount("amount must be positive")
        if amount > MAX_DELTA:
            raise InvalidShardAmount("amount is too large")
        return amount
