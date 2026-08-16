"""Firestore 구현.

collection 이름은 기존 규칙(`ggumirror_` prefix)을 따른다.
같은 project에 다른 service가 들어오더라도 우리 것만 만진다.

**핵심은 transaction 하나다.** 읽기 → 중복 확인 → 잔액 계산 → 원장 기록 → 잔액 갱신이
전부 성공하거나 전부 실패한다. Firestore가 충돌 시 자동으로 다시 실행한다 —
그래서 동시에 들어온 요청이 서로의 갱신을 덮어쓰지 않는다.
"""

from __future__ import annotations

import logging

from google.api_core import exceptions as gcp_exceptions
from google.cloud import firestore

from app.auth.store import StoreUnavailable
from app.shards.models import (
    InsufficientShards,
    PeriodQuota,
    QuotaExceeded,
    ShardLedgerEntry,
    ShardReason,
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
    ) -> tuple[ShardWallet, ShardLedgerEntry, bool]:
        wallet_ref = self._db.collection(WALLETS).document(user_id)
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
            claim = claim_ref.get(transaction=transaction) if idempotency_key_hash else None
            if claim is not None and claim.exists:
                data = claim.to_dict() or {}
                existing = _entry(claim.id, data)
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
            wallet = ShardWallet(
                user_id=user_id,
                balance=balance,
                lifetime_earned=current.lifetime_earned + max(delta, 0),
                lifetime_spent=current.lifetime_spent + max(-delta, 0),
                created_at=current.created_at if snapshot.exists else now,
                updated_at=now,
            )

            # 원장 먼저 — 있으면 안 되는 자리에 쓰는 것이므로 create로 막는다.
            # 여기까지 왔다는 것은 이 transaction이 그 줄의 **작성자**라는 뜻이다.
            transaction.create(claim_ref, _entry_document(entry))
            transaction.set(wallet_ref, _wallet_document(wallet))
            if quota_ref is not None:
                # 지급과 같은 transaction에서 counter가 오른다. 둘이 갈라질 수 없다.
                transaction.set(
                    quota_ref,
                    {"count": used + 1, "limit": quota.limit, "updatedAt": now},
                )
            return wallet, entry, True

        try:
            wallet, entry, applied = run(self._db.transaction())
        except (InsufficientShards, QuotaExceeded):
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

    # MARK: - 내부

    def _unavailable(self, operation: str, error: Exception) -> StoreUnavailable:
        # 실패 사실만 남긴다. Firestore 오류 문자열에 문서 경로가 들어갈 수 있다.
        logger.warning("firestore_failed operation=%s error=%s", operation, type(error).__name__)
        return StoreUnavailable(operation)


# MARK: - 문서 변환


def _wallet_document(wallet: ShardWallet) -> dict:
    return {
        "userId": wallet.user_id,
        "balance": wallet.balance,
        "lifetimeEarned": wallet.lifetime_earned,
        "lifetimeSpent": wallet.lifetime_spent,
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
