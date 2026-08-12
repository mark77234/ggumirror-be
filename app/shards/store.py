"""조각 저장소.

`AuthStore`와 같은 방식이다 — Protocol 하나 + Firestore 구현 하나 + test fake 하나.
계층을 더 쌓지 않는다.

**잔액 변경은 반드시 한 transaction 안에서 끝난다.** 그래서 protocol이 `credit`/`debit`
같은 조각난 연산 대신 "원장 한 줄을 적으면서 잔액을 갱신한다"는 **하나의 연산**을 갖는다.
그러지 않으면 원장만 남고 잔액이 안 바뀌는 상태가 생긴다.
"""

from __future__ import annotations

from typing import Protocol

from app.auth.store import StoreUnavailable  # noqa: F401  (같은 실패 타입을 쓴다)
from app.shards.models import (
    InsufficientShards,
    ShardLedgerEntry,
    ShardReason,
    ShardWallet,
    utcnow,
)


class ShardStore(Protocol):
    def wallet(self, user_id: str) -> ShardWallet:
        """지금 잔액. 없으면 **빈 지갑(0)**을 돌려준다 — 문서를 만들지 않는다."""

    def apply(
        self,
        user_id: str,
        delta: int,
        reason: ShardReason,
        idempotency_key_hash: str | None,
    ) -> tuple[ShardWallet, ShardLedgerEntry]:
        """원장 한 줄을 적으면서 잔액을 갱신한다. **원자적이어야 한다.**

        - `idempotency_key_hash`가 이미 있으면 **아무것도 하지 않고** 그때 결과를 돌려준다
        - 잔액이 모자라면 `InsufficientShards` — 잔액도 원장도 그대로다
        """


class InMemoryShardStore:
    """test / local용. Firestore에 붙지 않는다.

    실제 Firestore transaction의 **의미**를 그대로 흉내 낸다 — 중복 무시, 잔액 부족 거부,
    잔액과 원장을 함께 갱신. 그래서 서비스 규칙을 여기서 다 시험할 수 있다.
    """

    def __init__(self) -> None:
        self.wallets: dict[str, ShardWallet] = {}
        self.entries: list[ShardLedgerEntry] = []
        # idempotency hash → 그때 만든 원장 줄
        self._applied: dict[str, ShardLedgerEntry] = {}

    def wallet(self, user_id: str) -> ShardWallet:
        return self.wallets.get(user_id) or ShardWallet.empty(user_id)

    def apply(
        self,
        user_id: str,
        delta: int,
        reason: ShardReason,
        idempotency_key_hash: str | None,
    ) -> tuple[ShardWallet, ShardLedgerEntry]:
        if idempotency_key_hash is not None:
            if existing := self._applied.get(idempotency_key_hash):
                # 같은 사건이 다시 왔다. 두 번 반영하지 않는다.
                return self.wallet(existing.user_id), existing

        current = self.wallet(user_id)
        balance = current.balance + delta
        if balance < 0:
            raise InsufficientShards()

        entry = ShardLedgerEntry(
            id=ShardLedgerEntry.new_id(),
            user_id=user_id,
            delta=delta,
            balance_after=balance,
            reason=reason,
            idempotency_key_hash=idempotency_key_hash,
        )
        wallet = ShardWallet(
            user_id=user_id,
            balance=balance,
            lifetime_earned=current.lifetime_earned + max(delta, 0),
            lifetime_spent=current.lifetime_spent + max(-delta, 0),
            created_at=current.created_at,
            updated_at=utcnow(),
        )

        self.wallets[user_id] = wallet
        self.entries.append(entry)
        if idempotency_key_hash is not None:
            self._applied[idempotency_key_hash] = entry
        return wallet, entry
