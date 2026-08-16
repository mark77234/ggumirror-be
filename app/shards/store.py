"""조각 저장소.

`AuthStore`와 같은 방식이다 — Protocol 하나 + Firestore 구현 하나 + test fake 하나.
계층을 더 쌓지 않는다.

**잔액 변경은 반드시 한 transaction 안에서 끝난다.** 그래서 protocol이 `credit`/`debit`
같은 조각난 연산 대신 "원장 한 줄을 적으면서 잔액을 갱신한다"는 **하나의 연산**을 갖는다.
그러지 않으면 원장만 남고 잔액이 안 바뀌는 상태가 생긴다.
"""

from __future__ import annotations

import threading
from typing import Protocol

from app.auth.store import StoreUnavailable  # noqa: F401  (같은 실패 타입을 쓴다)
from app.shards.models import (
    InsufficientShards,
    PeriodQuota,
    QuotaExceeded,
    ShardLedgerEntry,
    ShardReason,
    ShardWallet,
    utcnow,
)


class ShardStore(Protocol):
    def wallet(self, user_id: str) -> ShardWallet:
        """지금 잔액. 없으면 **빈 지갑(0)**을 돌려준다 — 문서를 만들지 않는다."""

    def event_applied(self, idempotency_key_hash: str) -> bool:
        """이 사건이 이미 원장에 있는가. **읽기 전용**이고 아무것도 만들지 않는다.

        출석 상태 조회처럼 "지급하지 않고 물어보기만" 할 때 쓴다.
        지급 여부의 authority는 여전히 원장이다 — 별도 상태 collection을 두지 않는다.
        """

    def quota_used(self, quota_key: str) -> int:
        """그 기간에 지금까지 몇 번 지급됐는지. **읽기 전용**이고 아무것도 만들지 않는다.

        광고 버튼 상태(`오늘 2 / 5`)를 보여줄 때만 쓴다. 지급 경로에서는 쓰지 않는다 —
        세어보고 지급하면 동시 callback이 상한을 넘긴다.
        """

    def apply(
        self,
        user_id: str,
        delta: int,
        reason: ShardReason,
        idempotency_key_hash: str | None,
        quota: PeriodQuota | None = None,
    ) -> tuple[ShardWallet, ShardLedgerEntry, bool]:
        """원장 한 줄을 적으면서 잔액을 갱신한다. **원자적이어야 한다.**

        - `idempotency_key_hash`가 이미 있으면 **아무것도 하지 않고** 그때 결과를 돌려준다
        - 잔액이 모자라면 `InsufficientShards` — 잔액도 원장도 그대로다
        - `quota`를 주면 **같은 transaction 안에서** 상한을 확인하고 counter를 올린다.
          이미 찼으면 `QuotaExceeded` — 잔액도 원장도 counter도 그대로다.
          중복(idempotency)으로 판정되면 counter를 올리지 않는다 —
          같은 사건이 두 번 와도 quota가 두 칸 줄면 안 된다

        세 번째 값은 **이번 호출이 실제로 줄을 적었는가**(`applied`)다.
        원자적 쓰기의 결과 그 자체여야 한다 — 쓰기 전에 조회해서 짐작한 값이면
        동시에 들어온 두 요청이 둘 다 "내가 적었다"고 답하게 된다.
        """


class InMemoryShardStore:
    """test / local용. Firestore에 붙지 않는다.

    실제 Firestore transaction의 **의미**를 그대로 흉내 낸다 — 중복 무시, 잔액 부족 거부,
    잔액과 원장을 함께 갱신. 그래서 서비스 규칙을 여기서 다 시험할 수 있다.

    `apply`는 **lock 안에서 통째로** 일어난다. Firestore transaction이 주는 원자성이
    없으면 "중복인지 확인하고 → 적는다" 사이에 다른 thread가 끼어들어,
    동시성 test가 실제 저장소와 다른 답을 내놓는다.
    """

    def __init__(self) -> None:
        self.wallets: dict[str, ShardWallet] = {}
        self.entries: list[ShardLedgerEntry] = []
        # idempotency hash → 그때 만든 원장 줄
        self._applied: dict[str, ShardLedgerEntry] = {}
        self.quotas: dict[str, int] = {}
        self._lock = threading.Lock()

    def wallet(self, user_id: str) -> ShardWallet:
        return self.wallets.get(user_id) or ShardWallet.empty(user_id)

    def event_applied(self, idempotency_key_hash: str) -> bool:
        return idempotency_key_hash in self._applied

    def quota_used(self, quota_key: str) -> int:
        return self.quotas.get(quota_key, 0)

    def apply(
        self,
        user_id: str,
        delta: int,
        reason: ShardReason,
        idempotency_key_hash: str | None,
        quota: PeriodQuota | None = None,
    ) -> tuple[ShardWallet, ShardLedgerEntry, bool]:
        with self._lock:
            if idempotency_key_hash is not None:
                if existing := self._applied.get(idempotency_key_hash):
                    # 같은 사건이 다시 왔다. 두 번 반영하지 않고, 적지 않았다고 답한다.
                    # **quota도 올리지 않는다** — 재전송이 남은 횟수를 깎으면 안 된다.
                    return self.wallet(existing.user_id), existing, False

            if quota is not None and self.quotas.get(quota.key, 0) >= quota.limit:
                # 상한을 채웠다. 아무것도 쓰지 않는다.
                raise QuotaExceeded()

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
            if quota is not None:
                self.quotas[quota.key] = self.quotas.get(quota.key, 0) + 1
            return wallet, entry, True
