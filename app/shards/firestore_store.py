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
    ShardLedgerEntry,
    ShardReason,
    ShardWallet,
    utcnow,
)

logger = logging.getLogger(__name__)

WALLETS = "ggumirror_shard_wallets"
LEDGER = "ggumirror_shard_ledger"


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

    # MARK: - 쓰기

    def apply(
        self,
        user_id: str,
        delta: int,
        reason: ShardReason,
        idempotency_key_hash: str | None,
    ) -> tuple[ShardWallet, ShardLedgerEntry]:
        wallet_ref = self._db.collection(WALLETS).document(user_id)
        # 같은 사건을 두 번 적지 않기 위한 자리표. **document id가 곧 열쇠다** —
        # 이미 있으면 그 자리에 쓰지 못하므로 중복이 구조적으로 막힌다.
        claim_ref = (
            self._db.collection(LEDGER).document(idempotency_key_hash)
            if idempotency_key_hash
            else self._db.collection(LEDGER).document(ShardLedgerEntry.new_id())
        )

        @firestore.transactional
        def run(transaction: firestore.Transaction) -> tuple[ShardWallet, ShardLedgerEntry, bool]:
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
                return current, existing, True

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
            transaction.create(claim_ref, _entry_document(entry))
            transaction.set(wallet_ref, _wallet_document(wallet))
            return wallet, entry, False

        try:
            wallet, entry, duplicate = run(self._db.transaction())
        except InsufficientShards:
            raise
        except gcp_exceptions.AlreadyExists as error:
            # 같은 사건이 동시에 두 번 들어왔다. 한쪽만 남는 것이 정상이다.
            logger.info("shard_ledger_duplicate_ignored reason=%s", reason.value)
            raise StoreUnavailable("ledger_conflict") from error
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("ledger_apply", error) from error

        if duplicate:
            logger.info("shard_ledger_duplicate_ignored reason=%s", reason.value)
        return wallet, entry

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
