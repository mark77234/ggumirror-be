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

from app.shards.models import (
    MAX_DELTA,
    InvalidShardAmount,
    ShardLedgerEntry,
    ShardReason,
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

    # MARK: - 쓰기

    def credit(
        self,
        user_id: str,
        amount: int,
        reason: ShardReason,
        external_event_id: str | None = None,
    ) -> ShardWallet:
        """조각을 준다.

        `external_event_id`는 **그 사건을 유일하게 가리키는 값**이다
        (AdMob SSV transaction_id · StoreKit transaction id · 출석 날짜 …).
        같은 값이 다시 오면 **한 번만** 반영된다 — 재시도와 중복 callback이 잔액을 부풀리지 않는다.
        """
        return self._apply(user_id, self._checked(amount), reason, external_event_id)

    def debit(
        self,
        user_id: str,
        amount: int,
        reason: ShardReason,
        external_event_id: str | None = None,
    ) -> ShardWallet:
        """조각을 쓴다. 잔액이 모자라면 `InsufficientShards` — 아무것도 기록되지 않는다."""
        return self._apply(user_id, -self._checked(amount), reason, external_event_id)

    # MARK: - 내부

    def _apply(
        self,
        user_id: str,
        delta: int,
        reason: ShardReason,
        external_event_id: str | None,
    ) -> ShardWallet:
        # user scope는 **service가 강제한다.** 호출부가 event id에 user id를 넣어주기를
        # 기대하면, 잊은 곳 하나가 사용자끼리 같은 문서를 겨루게 만든다.
        key = idempotency_hash(user_id, reason, external_event_id) if external_event_id else None
        wallet, entry = self._store.apply(user_id, delta, reason, key)

        event = "shard_ledger_credit" if delta > 0 else "shard_ledger_debit"
        # 값은 남기되 누구인지 · 어떤 외부 id인지는 남기지 않는다.
        logger.info("%s reason=%s delta=%d balance=%d", event, reason.value, entry.delta, wallet.balance)
        return wallet

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
