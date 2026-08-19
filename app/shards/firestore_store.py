"""Firestore 구현.

collection 이름은 기존 규칙(`ggumirror_` prefix)을 따른다.
같은 project에 다른 service가 들어오더라도 우리 것만 만진다.

**핵심은 transaction 하나다.** 읽기 → 중복 확인 → 잔액 계산 → 원장 기록 → 잔액 갱신이
전부 성공하거나 전부 실패한다. Firestore가 충돌 시 자동으로 다시 실행한다 —
그래서 동시에 들어온 요청이 서로의 갱신을 덮어쓰지 않는다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from google.api_core import exceptions as gcp_exceptions
from google.cloud import firestore

from app.auth.store import StoreUnavailable
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
    ShardTransactionContext,
    ShardLedgerEntry,
    ShardReason,
    ShardRefundResult,
    ShardRefundReversalResult,
    ShardWallet,
    utcnow,
)

logger = logging.getLogger(__name__)

WALLETS = "ggumirror_shard_wallets"
LEDGER = "ggumirror_shard_ledger"
# 기간당 지급 횟수 counter. **잔액의 authority가 아니다** — 원장이 경제의 진실이고,
# 이 문서는 "하루 5회" 같은 상한을 원자적으로 세기 위한 operational projection이다.
# 문서 ID는 hash라서 raw user id도 날짜도 노출되지 않는다.
QUOTAS = "ggumirror_shard_quotas"


class FirestoreShardStore:
    def __init__(self, client: firestore.Client) -> None:
        self._db = client

    # MARK: - 읽기

    def wallet(self, user_id: str) -> ShardWallet:
        try:
            snapshot = self._db.collection(WALLETS).document(user_id).get()
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("wallet_read", error) from error

        if not snapshot.exists:
            # 조회만으로 문서를 만들지 않는다. 첫 거래 때 만들어진다.
            return ShardWallet.empty(user_id)
        return _wallet(user_id, snapshot.to_dict() or {})

    def event_applied(self, idempotency_key_hash: str) -> bool:
        """원장 문서 하나가 있는지만 본다. **문서 ID가 곧 사건 식별자**라서 조회 하나로 끝난다."""
        try:
            return self._db.collection(LEDGER).document(idempotency_key_hash).get().exists
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("ledger_event_read", error) from error

    def quota_used(self, quota_key: str) -> int:
        """상태 표시용 읽기. 지급 경로에서는 쓰지 않는다."""
        try:
            snapshot = self._db.collection(QUOTAS).document(quota_key).get()
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("quota_read", error) from error
        return int((snapshot.to_dict() or {}).get("count") or 0) if snapshot.exists else 0

    # MARK: - 쓰기

    def apply(
        self,
        user_id: str,
        delta: int,
        reason: ShardReason,
        idempotency_key_hash: str | None,
        quota: PeriodQuota | None = None,
        claim: ExclusiveClaim | None = None,
    ) -> tuple[ShardWallet, ShardLedgerEntry, bool]:
        wallet_ref = self._db.collection(WALLETS).document(user_id)
        claim_document_ref = (
            self._db.collection(claim.collection).document(claim.key) if claim else None
        )
        # 같은 사건을 두 번 적지 않기 위한 자리표. **document id가 곧 열쇠다** —
        # 이미 있으면 그 자리에 쓰지 못하므로 중복이 구조적으로 막힌다.
        claim_ref = (
            self._db.collection(LEDGER).document(idempotency_key_hash)
            if idempotency_key_hash
            else self._db.collection(LEDGER).document(ShardLedgerEntry.new_id())
        )
        quota_ref = self._db.collection(QUOTAS).document(quota.key) if quota else None

        @firestore.transactional
        def run(transaction: firestore.Transaction) -> tuple[ShardWallet, ShardLedgerEntry, bool]:
            """세 번째 값은 **이번 transaction이 실제로 원장에 적었는가**다.

            transaction 밖에서 미리 조회해 짐작하지 않는다. 그러면 동시에 들어온
            두 요청이 둘 다 "없더라, 내가 적었다"고 답한다 — 잔액은 맞지만 응답이 거짓말이 된다.
            """
            # 읽기는 쓰기보다 먼저. Firestore transaction의 규칙이다.
            #
            # 전역 claim을 **가장 먼저** 본다. 주인이 다르면 여기서 끝난다 —
            # 원장 멱등 열쇠에는 user_id가 들어가므로 이 검사를 대신할 수 없다.
            if claim_document_ref is not None:
                global_claim = claim_document_ref.get(transaction=transaction)
                if global_claim.exists:
                    owner = (global_claim.to_dict() or {}).get(claim.owner_field)
                    if owner != user_id:
                        raise ClaimOwnedByAnother()

            existing_claim = claim_ref.get(transaction=transaction) if idempotency_key_hash else None
            if existing_claim is not None and existing_claim.exists:
                data = existing_claim.to_dict() or {}
                existing = _entry(existing_claim.id, data)
                wallet_snapshot = wallet_ref.get(transaction=transaction)
                current = (
                    _wallet(user_id, wallet_snapshot.to_dict() or {})
                    if wallet_snapshot.exists
                    else ShardWallet.empty(user_id)
                )
                # 중복이다. **quota는 건드리지 않는다** — 재전송이 남은 횟수를 깎으면 안 된다.
                return current, existing, False

            # 상한 확인도 **이 transaction 안에서** 한다. 밖에서 세어보고 들어오면
            # 동시에 도착한 callback들이 전부 "아직 4개다"를 보고 상한을 넘겨 지급한다.
            used = 0
            if quota_ref is not None:
                quota_snapshot = quota_ref.get(transaction=transaction)
                used = int((quota_snapshot.to_dict() or {}).get("count") or 0)
                if used >= quota.limit:
                    # 아무것도 쓰지 않는다 — 잔액 · 원장 · counter 전부 그대로다.
                    raise QuotaExceeded()

            snapshot = wallet_ref.get(transaction=transaction)
            current = (
                _wallet(user_id, snapshot.to_dict() or {})
                if snapshot.exists
                else ShardWallet.empty(user_id)
            )

            balance = current.balance + delta
            if balance < 0:
                # 아무것도 쓰지 않고 끝낸다 — 잔액도 원장도 그대로다.
                raise InsufficientShards()

            now = utcnow()
            entry = ShardLedgerEntry(
                id=claim_ref.id,
                user_id=user_id,
                delta=delta,
                balance_after=balance,
                reason=reason,
                idempotency_key_hash=idempotency_key_hash,
                created_at=now,
            )
            wallet = _moved(current, delta, now, existed=snapshot.exists)

            # 원장 먼저 — 있으면 안 되는 자리에 쓰는 것이므로 create로 막는다.
            # 여기까지 왔다는 것은 이 transaction이 그 줄의 **작성자**라는 뜻이다.
            transaction.create(claim_ref, _entry_document(entry))
            transaction.set(wallet_ref, _wallet_document(wallet))
            if claim_document_ref is not None:
                # 원장 · 잔액과 **한 transaction**이다. `create`라서 우리가 읽은 뒤
                # 다른 요청이 먼저 자리를 잡았으면 commit이 AlreadyExists로 깨진다.
                transaction.create(
                    claim_document_ref,
                    {
                        **claim.document,
                        claim.owner_field: user_id,
                        "ledgerEntryId": entry.id,
                        "createdAt": now,
                    },
                )
            if quota_ref is not None:
                # 지급과 같은 transaction에서 counter가 오른다. 둘이 갈라질 수 없다.
                transaction.set(
                    quota_ref,
                    {"count": used + 1, "limit": quota.limit, "updatedAt": now},
                )
            return wallet, entry, True

        try:
            wallet, entry, applied = run(self._db.transaction())
        except (InsufficientShards, QuotaExceeded, ClaimOwnedByAnother):
            # 도메인 거절이다. 아무것도 기록되지 않았고, 재시도로 달라지지 않는다.
            raise
        except gcp_exceptions.AlreadyExists:
            # 같은 사건이 동시에 두 번 들어왔고, 우리가 읽은 뒤 상대가 먼저 commit했다.
            # **실패가 아니다** — 원장에 이미 그 줄이 있다는 뜻이고, 그것이 정확히
            # "이번 요청은 적지 않았다"는 답이다. 다시 돌리면 중복 분기로 들어가
            # 상대가 만든 줄과 그 시점의 잔액을 일관되게 읽는다.
            logger.info("shard_ledger_duplicate_ignored reason=%s", reason.value)
            try:
                wallet, entry, applied = run(self._db.transaction())
            except gcp_exceptions.GoogleAPIError as error:
                raise self._unavailable("ledger_apply_retry", error) from error
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("ledger_apply", error) from error

        if not applied:
            logger.info("shard_ledger_duplicate_ignored reason=%s", reason.value)
        return wallet, entry, applied


    def refund(
        self,
        user_id: str,
        purchase: DocumentKey,
        record: DocumentKey,
        document: dict,
        idempotency_key_hash: str,
        plan: Callable[[dict], RefundPlan],
    ) -> ShardRefundResult:
        """Apple 환불. **`apply`를 재사용하지 않는다** — projection이 다르다.

        `apply`의 음수 delta는 `lifetimeSpent`로 집계된다. 환불은 사용자가 쓴 것이
        아니므로 그 칸에 넣으면 "얼마나 썼는가"가 거짓말이 된다.
        `lifetimeEarned` · `lifetimeSpent`는 **그대로 두고** `lifetimeRefunded`만 올린다.
        """
        purchase_ref = self._db.collection(purchase.collection).document(purchase.key)
        record_ref = self._db.collection(record.collection).document(record.key)
        wallet_ref = self._db.collection(WALLETS).document(user_id)
        # 원장 문서 ID = 멱등 열쇠. 다른 reason과 같은 규칙이다.
        ledger_ref = self._db.collection(LEDGER).document(idempotency_key_hash)

        @firestore.transactional
        def run(transaction: firestore.Transaction) -> ShardRefundResult:
            # 읽기를 **전부 먼저** 한다. Firestore transaction의 규칙이다.
            purchase_snapshot = purchase_ref.get(transaction=transaction)
            record_snapshot = record_ref.get(transaction=transaction)
            wallet_snapshot = wallet_ref.get(transaction=transaction)

            if not purchase_snapshot.exists:
                # 우리가 조각을 준 적 없는 결제다. 되돌릴 것이 없고, 아무것도 쓰지 않는다.
                raise PurchaseClaimMissing()

            current = (
                _wallet(user_id, wallet_snapshot.to_dict() or {})
                if wallet_snapshot.exists
                else ShardWallet.empty(user_id)
            )

            # 금액의 authority는 **원본 구매 claim**이다 — 알림이 말한 값도,
            # 지금의 catalog 값도 쓰지 않는다(catalog는 나중에 바뀔 수 있다).
            # 대조 검사도 여기서 함께 일어나고, 어긋나면 예외가 나가 아무것도 쓰지 않는다.
            planned = plan(purchase_snapshot.to_dict() or {})
            amount = planned.requested

            if record_snapshot.exists:
                # 같은 환불이 다시 왔다. **두 번 빼지 않는다.**
                data = record_snapshot.to_dict() or {}
                return ShardRefundResult(
                    wallet=current,
                    requested=int(data.get("requestedAmount") or 0),
                    recovered=int(data.get("recoveredAmount") or 0),
                    applied=False,
                    ledger_entry_id=data.get("ledgerEntryId"),
                )

            # **잔액은 음수가 되지 않는다**(B-3 영구 규칙). 회수하지 못한 몫은
            # 빚으로 남기지 않는다 — 나중에 번 조각에서 자동 상계하지 않는다.
            recovered = min(current.balance, amount)
            now = utcnow()

            if recovered > 0:
                entry = ShardLedgerEntry(
                    id=ledger_ref.id,
                    user_id=user_id,
                    delta=-recovered,
                    balance_after=current.balance - recovered,
                    reason=ShardReason.IAP_REFUND,
                    idempotency_key_hash=idempotency_key_hash,
                    created_at=now,
                )
                transaction.create(ledger_ref, _entry_document(entry))
                transaction.set(
                    wallet_ref,
                    _wallet_document(
                        ShardWallet(
                            user_id=user_id,
                            balance=entry.balance_after,
                            # **둘 다 그대로다.** 받은 적도, 쓴 적도 바뀌지 않았다.
                            lifetime_earned=current.lifetime_earned,
                            lifetime_spent=current.lifetime_spent,
                            lifetime_refunded=current.lifetime_refunded + recovered,
                            lifetime_refund_reversed=current.lifetime_refund_reversed,
                            created_at=current.created_at if wallet_snapshot.exists else now,
                            updated_at=now,
                        )
                    ),
                )
            else:
                # 회수할 잔액이 없다. **delta 0짜리 원장 줄을 만들지 않는다** —
                # 원장은 조각이 움직인 기록이고, 여기서는 움직이지 않았다.
                # 그래도 **처리 완료된 환불**이라 record는 남긴다(재시도를 요구하지 않는다).
                entry = None

            # record가 이 환불의 business 멱등이다. `create`라서 우리가 읽은 뒤
            # 다른 요청이 먼저 자리를 잡았으면 commit이 AlreadyExists로 깨진다.
            transaction.create(
                record_ref,
                {
                    **document,
                    **planned.fields,
                    "userId": user_id,
                    "requestedAmount": amount,
                    "recoveredAmount": recovered,
                    "unrecoveredAmount": amount - recovered,
                    "ledgerEntryId": entry.id if entry else None,
                    "createdAt": now,
                },
            )
            return ShardRefundResult(
                wallet=self._projected(current, recovered),
                requested=amount,
                recovered=recovered,
                applied=True,
                ledger_entry_id=entry.id if entry else None,
            )

        try:
            return run(self._db.transaction())
        except gcp_exceptions.AlreadyExists:
            # 같은 환불이 동시에 두 번 들어왔고 상대가 먼저 commit했다. **실패가 아니다** —
            # 다시 돌리면 중복 분기로 들어가 상대가 만든 record를 그대로 읽는다.
            logger.info("shard_refund_duplicate_ignored")
            try:
                return run(self._db.transaction())
            except gcp_exceptions.GoogleAPIError as error:
                raise self._unavailable("refund_retry", error) from error
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("refund", error) from error


    def reverse_refund(
        self,
        user_id: str,
        purchase: DocumentKey,
        record: DocumentKey,
        idempotency_key_hash: str,
        remaining: Callable[[dict], int],
    ) -> ShardRefundReversalResult:
        """환불을 되돌린다. **회수했던 만큼만** 복구한다.

        멱등을 위한 별도 문서를 두지 않는다 — `recovered - reversed`가 0이 되면
        더 복구할 것이 없으므로 record 자체가 멱등 열쇠다. 원장 문서 ID도 결정적이라
        동시 요청에서 한쪽은 `AlreadyExists`로 깨지고 다시 돌아 0을 읽는다.
        """
        record_ref = self._db.collection(record.collection).document(record.key)
        purchase_ref = self._db.collection(purchase.collection).document(purchase.key)
        wallet_ref = self._db.collection(WALLETS).document(user_id)
        ledger_ref = self._db.collection(LEDGER).document(idempotency_key_hash)

        @firestore.transactional
        def run(transaction: firestore.Transaction) -> ShardRefundReversalResult:
            # 읽기를 전부 먼저. Firestore transaction의 규칙이다.
            record_snapshot = record_ref.get(transaction=transaction)
            wallet_snapshot = wallet_ref.get(transaction=transaction)

            if not record_snapshot.exists:
                # **"지금 없다"를 "영영 없다"로 단정하지 않는다.**
                # 우리가 지급한 결제라면 `REFUND`가 아직 안 왔을 수 있다.
                claim = purchase_ref.get(transaction=transaction)
                if not claim.exists:
                    raise RefundRecordMissing()
                raise RefundNotYetProcessed()

            current = (
                _wallet(user_id, wallet_snapshot.to_dict() or {})
                if wallet_snapshot.exists
                else ShardWallet.empty(user_id)
            )
            data = record_snapshot.to_dict() or {}
            restorable = remaining(data)

            if restorable <= 0:
                # 중복이거나 애초에 회수한 것이 없었다. **아무것도 쓰지 않는다.**
                return ShardRefundReversalResult(
                    wallet=current,
                    restored=0,
                    applied=False,
                    ledger_entry_id=data.get("reversalLedgerEntryId"),
                )

            now = utcnow()
            entry = ShardLedgerEntry(
                id=ledger_ref.id,
                user_id=user_id,
                delta=restorable,
                balance_after=current.balance + restorable,
                reason=ShardReason.IAP_REFUND_REVERSED,
                idempotency_key_hash=idempotency_key_hash,
                created_at=now,
            )
            wallet = ShardWallet(
                user_id=user_id,
                balance=entry.balance_after,
                # **원래 구매가 이미 earned를 올렸다.** 또 올리면 한 결제를 두 번 센다.
                lifetime_earned=current.lifetime_earned,
                lifetime_spent=current.lifetime_spent,
                # gross라 줄지 않는다 — 되돌린 몫은 따로 쌓는다.
                lifetime_refunded=current.lifetime_refunded,
                lifetime_refund_reversed=current.lifetime_refund_reversed + restorable,
                created_at=current.created_at if wallet_snapshot.exists else now,
                updated_at=now,
            )

            transaction.create(ledger_ref, _entry_document(entry))
            transaction.set(wallet_ref, _wallet_document(wallet))
            # record는 이미 있는 문서라 `update`다. 환불 사실(recovered 등)은 건드리지 않는다.
            transaction.update(
                record_ref,
                {
                    "reversedAmount": int(data.get("reversedAmount") or 0) + restorable,
                    "reversalLedgerEntryId": entry.id,
                    "reversedAt": now,
                },
            )
            return ShardRefundReversalResult(
                wallet=wallet, restored=restorable, applied=True, ledger_entry_id=entry.id
            )

        try:
            return run(self._db.transaction())
        except gcp_exceptions.AlreadyExists:
            # 같은 되돌리기가 동시에 들어왔고 상대가 먼저 commit했다. **실패가 아니다** —
            # 다시 돌리면 `remaining`이 0이라 아무것도 쓰지 않는다.
            logger.info("shard_refund_reversal_duplicate_ignored")
            try:
                return run(self._db.transaction())
            except gcp_exceptions.GoogleAPIError as error:
                raise self._unavailable("refund_reversal_retry", error) from error
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("refund_reversal", error) from error

    @staticmethod
    def _projected(current: ShardWallet, recovered: int) -> ShardWallet:
        """방금 쓴 값을 그대로 돌려준다. commit 뒤 다시 읽지 않는다."""
        if recovered <= 0:
            return current
        return ShardWallet(
            user_id=current.user_id,
            balance=current.balance - recovered,
            lifetime_earned=current.lifetime_earned,
            lifetime_spent=current.lifetime_spent,
            lifetime_refunded=current.lifetime_refunded + recovered,
            lifetime_refund_reversed=current.lifetime_refund_reversed,
            created_at=current.created_at,
            updated_at=utcnow(),
        )


    # MARK: - 호출자가 소유하는 transaction

    def transaction(self):
        """호출자가 transaction을 소유할 수 있게 열어 준다. **commit은 호출자가 한다.**"""
        return self._db.transaction()

    def context(self, transaction) -> ShardTransactionContext:
        """**transactional callable 안에서 매번** 부른다 — attempt마다 새 기록이다."""
        return ShardTransactionContext(transaction=transaction)

    def apply_in_transaction(
        self,
        context: ShardTransactionContext,
        user_id: str,
        delta: int,
        reason: ShardReason,
        idempotency_key_hash: str,
    ) -> tuple[ShardWallet, ShardLedgerEntry, bool]:
        """**이미 열려 있는 transaction에** 원장 한 줄과 지갑 갱신을 얹는다.

        `apply`와 다른 점은 하나뿐이다 — **transaction을 열지도 commit하지도 않는다.**
        그래서 호출자가 marketplace listing · ownership 같은 다른 문서를 **같은
        transaction**에 함께 넣을 수 있다. 그것이 이 함수가 존재하는 유일한 이유다.

        되는 것:
        - 중복(같은 열쇠가 이미 있음)이면 **아무것도 쓰지 않고** `applied=False`
        - 잔액이 모자라면 `InsufficientShards` — 호출자의 transaction 전체가 취소된다
        - 같은 transaction에서 **같은 지갑을 두 번** 바꾸려 하면 `WalletAlreadyChanged`

        안 되는 것: quota · 전역 claim. 필요하면 `apply`를 쓴다.
        """
        transaction = context.transaction
        ledger_ref = self._db.collection(LEDGER).document(idempotency_key_hash)
        wallet_ref = self._db.collection(WALLETS).document(user_id)

        # 읽기는 쓰기보다 먼저. Firestore transaction의 규칙이다.
        # **재시도마다 다시 읽는다** — 이전 attempt가 본 잔액을 쓰지 않는다.
        existing = ledger_ref.get(transaction=transaction)
        wallet_snapshot = wallet_ref.get(transaction=transaction)

        current = (
            _wallet(user_id, wallet_snapshot.to_dict() or {})
            if wallet_snapshot.exists
            else ShardWallet.empty(user_id)
        )

        if existing.exists:
            # 같은 사건이 다시 왔다. **쓰지 않는다** — 지갑 표시도 하지 않는다.
            return current, _entry(existing.id, existing.to_dict() or {}), False

        # 여기서부터 쓴다. 같은 attempt에서 지갑을 두 번 바꾸는 것을 막는다.
        context.claim(user_id)

        if current.balance + delta < 0:
            # 호출자의 transaction이 통째로 취소된다 — 부분 반영이 없다.
            raise InsufficientShards()

        now = utcnow()
        entry = ShardLedgerEntry(
            id=ledger_ref.id,
            user_id=user_id,
            delta=delta,
            balance_after=current.balance + delta,
            reason=reason,
            idempotency_key_hash=idempotency_key_hash,
            created_at=now,
        )
        wallet = _moved(current, delta, now, existed=wallet_snapshot.exists)

        # 원장은 `create` — 우리가 읽은 뒤 다른 요청이 먼저 적었으면 commit이 깨진다.
        transaction.create(ledger_ref, _entry_document(entry))
        transaction.set(wallet_ref, _wallet_document(wallet))
        return wallet, entry, True

    # MARK: - 내부

    def _unavailable(self, operation: str, error: Exception) -> StoreUnavailable:
        # 실패 사실만 남긴다. Firestore 오류 문자열에 문서 경로가 들어갈 수 있다.
        logger.warning("firestore_failed operation=%s error=%s", operation, type(error).__name__)
        return StoreUnavailable(operation)



def _moved(current: ShardWallet, delta: int, now, *, existed: bool) -> ShardWallet:
    """일반 거래의 지갑 projection. **한 곳에서만 정의한다.**

    `delta > 0`이면 `lifetime_earned`, `delta < 0`이면 `lifetime_spent`가 오른다.
    환불 누적 둘은 **건드리지 않고 그대로 물려준다** — 그건 전용 경로(B-6F-B/C)의 몫이다.
    """
    return ShardWallet(
        user_id=current.user_id,
        balance=current.balance + delta,
        lifetime_earned=current.lifetime_earned + max(delta, 0),
        lifetime_spent=current.lifetime_spent + max(-delta, 0),
        lifetime_refunded=current.lifetime_refunded,
        lifetime_refund_reversed=current.lifetime_refund_reversed,
        created_at=current.created_at if existed else now,
        updated_at=now,
    )



# MARK: - 문서 변환


def _wallet_document(wallet: ShardWallet) -> dict:
    return {
        "userId": wallet.user_id,
        "balance": wallet.balance,
        "lifetimeEarned": wallet.lifetime_earned,
        "lifetimeSpent": wallet.lifetime_spent,
        "lifetimeRefunded": wallet.lifetime_refunded,
        "lifetimeRefundReversed": wallet.lifetime_refund_reversed,
        "createdAt": wallet.created_at,
        "updatedAt": wallet.updated_at,
    }


def _entry_document(entry: ShardLedgerEntry) -> dict:
    return {
        "userId": entry.user_id,
        "delta": entry.delta,
        "balanceAfter": entry.balance_after,
        "reason": entry.reason.value,
        "idempotencyKeyHash": entry.idempotency_key_hash,
        "createdAt": entry.created_at,
        "schemaVersion": entry.schema_version,
    }


def _wallet(user_id: str, data: dict) -> ShardWallet:
    return ShardWallet(
        user_id=user_id,
        balance=int(data.get("balance") or 0),
        lifetime_earned=int(data.get("lifetimeEarned") or 0),
        lifetime_spent=int(data.get("lifetimeSpent") or 0),
        # 예전 문서에는 없는 field다. **migration 없이 0으로 읽는다.**
        lifetime_refunded=int(data.get("lifetimeRefunded") or 0),
        lifetime_refund_reversed=int(data.get("lifetimeRefundReversed") or 0),
        created_at=data.get("createdAt") or utcnow(),
        updated_at=data.get("updatedAt") or utcnow(),
    )


def _entry(entry_id: str, data: dict) -> ShardLedgerEntry:
    return ShardLedgerEntry(
        id=entry_id,
        user_id=str(data.get("userId") or ""),
        delta=int(data.get("delta") or 0),
        balance_after=int(data.get("balanceAfter") or 0),
        reason=ShardReason(data.get("reason") or ShardReason.ADMIN_ADJUSTMENT.value),
        idempotency_key_hash=data.get("idempotencyKeyHash"),
        created_at=data.get("createdAt") or utcnow(),
        schema_version=int(data.get("schemaVersion") or 1),
    )
