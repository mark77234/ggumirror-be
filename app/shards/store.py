"""조각 저장소.

`AuthStore`와 같은 방식이다 — Protocol 하나 + Firestore 구현 하나 + test fake 하나.
계층을 더 쌓지 않는다.

**잔액 변경은 반드시 한 transaction 안에서 끝난다.** 그래서 protocol이 `credit`/`debit`
같은 조각난 연산 대신 "원장 한 줄을 적으면서 잔액을 갱신한다"는 **하나의 연산**을 갖는다.
그러지 않으면 원장만 남고 잔액이 안 바뀌는 상태가 생긴다.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Protocol

from app.auth.store import StoreUnavailable  # noqa: F401  (같은 실패 타입을 쓴다)
from app.shards.models import (
    ClaimOwnedByAnother,
    DocumentKey,
    ExclusiveClaim,
    InsufficientShards,
    PeriodQuota,
    PurchaseClaimMissing,
    RefundNotYetProcessed,
    RefundPlan,
    RefundRecordMissing,
    QuotaExceeded,
    ShardLedgerEntry,
    ShardReason,
    ShardRefundResult,
    ShardRefundReversalResult,
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
        claim: ExclusiveClaim | None = None,
    ) -> tuple[ShardWallet, ShardLedgerEntry, bool]:
        """원장 한 줄을 적으면서 잔액을 갱신한다. **원자적이어야 한다.**

        - `idempotency_key_hash`가 이미 있으면 **아무것도 하지 않고** 그때 결과를 돌려준다
        - 잔액이 모자라면 `InsufficientShards` — 잔액도 원장도 그대로다
        - `quota`를 주면 **같은 transaction 안에서** 상한을 확인하고 counter를 올린다.
          이미 찼으면 `QuotaExceeded` — 잔액도 원장도 counter도 그대로다.
          중복(idempotency)으로 판정되면 counter를 올리지 않는다 —
          같은 사건이 두 번 와도 quota가 두 칸 줄면 안 된다
        - `claim`을 주면 **같은 transaction 안에서** 전역 자리를 잡는다.
          이미 있고 주인이 같으면 중복(`applied=False`), **다른 사람이면
          `ClaimOwnedByAnother`** — 잔액도 원장도 claim도 그대로다

        세 번째 값은 **이번 호출이 실제로 줄을 적었는가**(`applied`)다.
        원자적 쓰기의 결과 그 자체여야 한다 — 쓰기 전에 조회해서 짐작한 값이면
        동시에 들어온 두 요청이 둘 다 "내가 적었다"고 답하게 된다.
        """

    def refund(
        self,
        user_id: str,
        purchase: DocumentKey,
        record: DocumentKey,
        document: dict,
        idempotency_key_hash: str,
        plan: Callable[[dict], RefundPlan],
    ) -> ShardRefundResult:
        """Apple 환불을 반영한다. **`apply`를 재사용하지 않는다.**

        `apply`의 음수 delta는 `lifetime_spent`로 집계된다 — 환불은 사용자가 쓴 것이
        아니므로 그 칸에 넣으면 "얼마나 썼는가"가 거짓말이 된다. 그래서 projection이
        다른 **전용 연산**이고, generic debit의 의미는 그대로 둔다.

        한 transaction 안에서 전부 일어난다:

        1. **원본 구매 claim**을 읽는다. 없으면 `PurchaseClaimMissing` — 우리가 준 적 없는
           결제다. 아무것도 기록하지 않는다
        2. `requested(claim_document)`로 되돌릴 양을 정한다. **금액의 authority는 원본 claim**이고
           알림이 말한 값도, 지금의 catalog 값도 쓰지 않는다(catalog는 나중에 바뀔 수 있다)
        3. 환불 record가 이미 있으면 **아무것도 하지 않고** 그때 결과를 돌려준다(`applied=False`)
        4. `recovered = min(balance, requested)` — **잔액은 절대 음수가 되지 않는다.**
           모자란 몫(`unrecovered`)은 빚으로 남기지 않는다
        5. record를 만들고, `recovered > 0`일 때만 원장 한 줄을 적고, 지갑을 갱신한다

        `recovered == 0`도 **처리 완료된 환불**이다 — record는 남기고 delta 0짜리
        원장 줄은 만들지 않는다. 재시도를 요구하지 않는다.
        """

    def reverse_refund(
        self,
        user_id: str,
        purchase: DocumentKey,
        record: DocumentKey,
        idempotency_key_hash: str,
        remaining: Callable[[dict], int],
    ) -> ShardRefundReversalResult:
        """Apple이 되돌린 환불만큼 조각을 복구한다. **회수했던 만큼만.**

        복구량의 authority는 **환불 record의 `recoveredAmount`**다 — 원본 지급량도,
        Apple이 요청했던 `requestedAmount`도, catalog 값도 쓰지 않는다.
        요청 50 중 10만 회수했다면 되돌릴 수 있는 것도 10이다.

        한 transaction 안에서:

        1. 환불 record를 읽는다. 없으면 **원본 구매 claim**을 보고 나눈다 —
           claim도 없으면 우리가 준 적 없는 결제라 `RefundRecordMissing`,
           claim이 있으면 `REFUND`가 아직 안 왔을 수 있어 `RefundNotYetProcessed`다
        2. `remaining(record)`가 남은 복구량을 정한다(`recovered - reversed`)
        3. 0이면 **아무것도 쓰지 않는다** — 중복이거나 회수한 것이 없었던 경우다
        4. 0보다 크면 원장 한 줄(+) · 지갑 · record를 **함께** 갱신한다

        `lifetime_earned`를 올리지 않는다 — 원래 구매가 이미 올렸으므로 두 번 세게 된다.
        `lifetime_refunded`도 줄이지 않는다(gross). 대신 `lifetime_refund_reversed`가 오른다.
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
        # (collection, key) → 저장한 문서. 전역 claim이라 user별로 나누지 않는다.
        self.claims: dict[tuple[str, str], dict] = {}
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
        claim: ExclusiveClaim | None = None,
    ) -> tuple[ShardWallet, ShardLedgerEntry, bool]:
        with self._lock:
            # 전역 claim을 **가장 먼저** 본다. 주인이 다르면 그 자리에서 끝난다 —
            # 원장 멱등은 user 안에서만 유일하므로 이 검사를 대신할 수 없다.
            if claim is not None:
                if existing_claim := self.claims.get((claim.collection, claim.key)):
                    owner = existing_claim.get(claim.owner_field)
                    if owner != user_id:
                        raise ClaimOwnedByAnother()

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
                # 일반 거래는 환불 누적을 **건드리지 않고 그대로 물려준다.**
                lifetime_refunded=current.lifetime_refunded,
                lifetime_refund_reversed=current.lifetime_refund_reversed,
                created_at=current.created_at,
                updated_at=utcnow(),
            )

            self.wallets[user_id] = wallet
            self.entries.append(entry)
            if idempotency_key_hash is not None:
                self._applied[idempotency_key_hash] = entry
            if quota is not None:
                self.quotas[quota.key] = self.quotas.get(quota.key, 0) + 1
            if claim is not None:
                # 원장 · 잔액과 **같은 lock 안에서** 자리를 잡는다. 하나만 성공하는 상태가 없다.
                self.claims[(claim.collection, claim.key)] = {
                    **claim.document,
                    claim.owner_field: user_id,
                    "ledgerEntryId": entry.id,
                }
            return wallet, entry, True

    def refund(
        self,
        user_id: str,
        purchase: DocumentKey,
        record: DocumentKey,
        document: dict,
        idempotency_key_hash: str,
        plan: Callable[[dict], RefundPlan],
    ) -> ShardRefundResult:
        with self._lock:
            claim = self.claims.get((purchase.collection, purchase.key))
            if claim is None:
                # 우리가 조각을 준 적 없는 결제다. 되돌릴 것이 없다.
                raise PurchaseClaimMissing()

            # 금액의 authority는 **원본 구매 claim**이다. 검증도 여기서 함께 일어난다.
            planned = plan(claim)
            amount = planned.requested

            if existing := self.claims.get((record.collection, record.key)):
                # 같은 환불이 다시 왔다. 두 번 빼지 않는다.
                return ShardRefundResult(
                    wallet=self.wallet(user_id),
                    requested=int(existing.get("requestedAmount") or 0),
                    recovered=int(existing.get("recoveredAmount") or 0),
                    applied=False,
                    ledger_entry_id=existing.get("ledgerEntryId"),
                )

            current = self.wallet(user_id)
            recovered = min(current.balance, amount)

            entry = None
            if recovered > 0:
                entry = ShardLedgerEntry(
                    id=idempotency_key_hash,
                    user_id=user_id,
                    delta=-recovered,
                    balance_after=current.balance - recovered,
                    reason=ShardReason.IAP_REFUND,
                    idempotency_key_hash=idempotency_key_hash,
                )
                self.entries.append(entry)
                self._applied[idempotency_key_hash] = entry
                self.wallets[user_id] = ShardWallet(
                    user_id=user_id,
                    balance=current.balance - recovered,
                    # **둘 다 그대로다.** 받은 적도, 쓴 적도 바뀌지 않았다.
                    lifetime_earned=current.lifetime_earned,
                    lifetime_spent=current.lifetime_spent,
                    lifetime_refunded=current.lifetime_refunded + recovered,
                    lifetime_refund_reversed=current.lifetime_refund_reversed,
                    created_at=current.created_at,
                    updated_at=utcnow(),
                )

            self.claims[(record.collection, record.key)] = {
                **document,
                **planned.fields,
                "userId": user_id,
                "requestedAmount": amount,
                "recoveredAmount": recovered,
                "unrecoveredAmount": amount - recovered,
                "ledgerEntryId": entry.id if entry else None,
                "createdAt": utcnow(),
            }
            return ShardRefundResult(
                wallet=self.wallet(user_id),
                requested=amount,
                recovered=recovered,
                applied=True,
                ledger_entry_id=entry.id if entry else None,
            )

    def reverse_refund(
        self,
        user_id: str,
        purchase: DocumentKey,
        record: DocumentKey,
        idempotency_key_hash: str,
        remaining: Callable[[dict], int],
    ) -> ShardRefundReversalResult:
        with self._lock:
            existing = self.claims.get((record.collection, record.key))
            if existing is None:
                # 환불 기록이 없다. **우리가 지급한 결제인지**로 나눈다.
                if self.claims.get((purchase.collection, purchase.key)) is None:
                    raise RefundRecordMissing()
                raise RefundNotYetProcessed()

            restorable = remaining(existing)
            current = self.wallet(user_id)

            if restorable <= 0:
                # 중복이거나 애초에 회수한 것이 없었다. **아무것도 쓰지 않는다.**
                return ShardRefundReversalResult(
                    wallet=current,
                    restored=0,
                    applied=False,
                    ledger_entry_id=existing.get("reversalLedgerEntryId"),
                )

            now = utcnow()
            entry = ShardLedgerEntry(
                id=idempotency_key_hash,
                user_id=user_id,
                delta=restorable,
                balance_after=current.balance + restorable,
                reason=ShardReason.IAP_REFUND_REVERSED,
                idempotency_key_hash=idempotency_key_hash,
                created_at=now,
            )
            self.entries.append(entry)
            self._applied[idempotency_key_hash] = entry
            wallet = ShardWallet(
                user_id=user_id,
                balance=entry.balance_after,
                # **둘 다 그대로다.** 원래 구매가 이미 earned를 올렸으므로 또 올리면 두 번 센다.
                lifetime_earned=current.lifetime_earned,
                lifetime_spent=current.lifetime_spent,
                # gross라 줄지 않는다 — 되돌린 몫은 아래에 따로 쌓는다.
                lifetime_refunded=current.lifetime_refunded,
                lifetime_refund_reversed=current.lifetime_refund_reversed + restorable,
                created_at=current.created_at,
                updated_at=now,
            )
            self.wallets[user_id] = wallet
            self.claims[(record.collection, record.key)] = {
                **existing,
                "reversedAmount": int(existing.get("reversedAmount") or 0) + restorable,
                "reversalLedgerEntryId": entry.id,
                "reversedAt": now,
            }
            return ShardRefundReversalResult(
                wallet=wallet, restored=restorable, applied=True, ledger_entry_id=entry.id
            )
