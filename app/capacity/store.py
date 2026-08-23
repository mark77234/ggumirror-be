"""보관 공간 저장소.

**조각이 움직이는 일은 `ShardLedgerService`가 한다.** 여기는 칸 수와 구매 기록만
책임지고, 둘이 **하나의 transaction**에서 커밋되도록 엮는다 —
`FirestoreMarketplaceStore.acquire`와 같은 모양이다(B-7B / B-7E).

칸 수는 **기존 user 문서**에 얹는다. 새 collection을 만들지 않는다 —
`ggumirror_users/{userId}`는 만들어질 때 한 번만 `set`되고 그 뒤로 덮어쓰이지 않으므로
field 하나를 더해도 안전하다. 지갑과 섞지 않는다(지갑은 자기 collection이 있다).
"""

from __future__ import annotations

import logging
from typing import Protocol

from app.auth.firestore_store import USERS
from app.capacity.models import (
    CapacityPurchaseResult,
    CapacityStoreUnavailable,
    MirrorCapacity,
    SlotPack,
    operation_key,
)
from app.shards.models import ShardReason
from app.shards.service import ShardLedgerService

logger = logging.getLogger(__name__)

#: 구매 시도 기록. 같은 `operationId`가 두 번 경제를 움직이지 못하게 막는 자리다.
OPERATIONS = "ggumirror_mirror_capacity_operations"

#: user 문서에 얹는 field. **없으면 0이다** — 예전 사용자를 위해 migration하지 않는다.
PURCHASED_SLOTS_FIELD = "purchasedMirrorSlots"


class CapacityStore(Protocol):
    def capacity(self, user_id: str) -> MirrorCapacity:
        """지금 칸 수. 조회만으로 문서를 만들지 않는다."""

    def purchase(
        self,
        shards: ShardLedgerService,
        user_id: str,
        pack: SlotPack,
        operation_id: str,
    ) -> CapacityPurchaseResult:
        """**하나의 transaction**에서 조각을 빼고 칸을 늘리고 기록을 남긴다.

        - 같은 `operation_id`가 이미 있으면 아무것도 쓰지 않고 그때 결과를 돌려준다
        - 잔액이 모자라면 `InsufficientShards` — 칸도 기록도 남지 않는다
        """


def _capacity_from(user_id: str, data: dict) -> MirrorCapacity:
    raw = data.get(PURCHASED_SLOTS_FIELD)
    slots = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else 0
    return MirrorCapacity(user_id=user_id, purchased_slots=slots)


def _operation_document(
    user_id: str, pack: SlotPack, operation_id: str, purchased_after: int
) -> dict:
    """무엇을 샀는지. **raw token · Apple 정보는 넣지 않는다.**

    `purchasedSlotsAfter`를 함께 남겨 두면 재시도 응답을 원장 재계산 없이 만든다.
    """
    return {
        "userId": user_id,
        "packId": pack.id,
        "costShards": pack.cost_shards,
        "slotDelta": pack.slot_delta,
        "purchasedSlotsAfter": purchased_after,
    }


class InMemoryCapacityStore:
    """test용. Firestore와 **같은 순서**로 읽고 쓴다."""

    def __init__(self) -> None:
        self._purchased: dict[str, int] = {}
        self._operations: dict[str, dict] = {}

    def capacity(self, user_id: str) -> MirrorCapacity:
        return MirrorCapacity(user_id=user_id, purchased_slots=self._purchased.get(user_id, 0))

    def purchase(
        self,
        shards: ShardLedgerService,
        user_id: str,
        pack: SlotPack,
        operation_id: str,
    ) -> CapacityPurchaseResult:
        key = operation_key(user_id, operation_id)

        # marketplace와 **같은 모양**이다 — 정상 종료면 commit, 예외면 전부 버린다.
        with shards.transaction() as tx:
            scoped = shards.context(tx)

            existing = self._operations.get(key)
            if existing is not None:
                # 이미 처리한 의도다. **경제를 다시 움직이지 않는다.**
                return CapacityPurchaseResult(
                    operation_id=operation_id,
                    pack_id=existing["packId"],
                    charged_shards=existing["costShards"],
                    slot_delta=existing["slotDelta"],
                    applied=False,
                    capacity=MirrorCapacity(
                        user_id=user_id, purchased_slots=existing["purchasedSlotsAfter"]
                    ),
                    balance=shards.wallet(user_id).balance,
                )

            before = self._purchased.get(user_id, 0)
            # 잔액이 모자라면 여기서 끝난다 — 칸도 기록도 남지 않는다.
            debit = shards.apply_in_transaction(
                scoped, user_id, -pack.cost_shards,
                ShardReason.MIRROR_CAPACITY_PURCHASE, operation_id,
            )
            after = before + pack.slot_delta
            document = _operation_document(user_id, pack, operation_id, after)

            def write():
                previous = self._purchased.get(user_id)
                self._purchased[user_id] = after
                self._operations[key] = document

                def undo() -> None:
                    if previous is None:
                        self._purchased.pop(user_id, None)
                    else:
                        self._purchased[user_id] = previous
                    self._operations.pop(key, None)

                return undo

            tx.add(write)

        return CapacityPurchaseResult(
            operation_id=operation_id,
            pack_id=pack.id,
            charged_shards=pack.cost_shards,
            slot_delta=pack.slot_delta,
            applied=debit.applied,
            capacity=MirrorCapacity(user_id=user_id, purchased_slots=after),
            balance=debit.wallet.balance,
        )


class FirestoreCapacityStore:
    """실제 저장소. 조각 원장과 **같은 Firestore client**를 쓴다."""

    def __init__(self, db) -> None:
        self._db = db

    def capacity(self, user_id: str) -> MirrorCapacity:
        from google.api_core import exceptions as gcp_exceptions

        try:
            snapshot = self._db.collection(USERS).document(user_id).get()
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("capacity_read", error) from error
        return _capacity_from(user_id, snapshot.to_dict() or {} if snapshot.exists else {})

    def purchase(
        self,
        shards: ShardLedgerService,
        user_id: str,
        pack: SlotPack,
        operation_id: str,
    ) -> CapacityPurchaseResult:
        from google.api_core import exceptions as gcp_exceptions
        from google.cloud import firestore

        key = operation_key(user_id, operation_id)
        user_ref = self._db.collection(USERS).document(user_id)
        operation_ref = self._db.collection(OPERATIONS).document(key)

        @firestore.transactional
        def run(transaction) -> CapacityPurchaseResult:
            # ⚠️ context는 attempt마다 새로 만든다(B-7B.1).
            scoped = shards.context(transaction)

            # **우리 문서를 전부 먼저 읽는다.** 그 뒤 조각 primitive가 자기 문서를
            # 읽고 쓴다 — Firestore transaction은 쓰기 뒤 읽기를 허용하지 않는다.
            operation_snapshot = operation_ref.get(transaction=transaction)
            user_snapshot = user_ref.get(transaction=transaction)

            if operation_snapshot.exists:
                # 같은 의도가 이미 처리됐다. **아무것도 쓰지 않는다.**
                stored = operation_snapshot.to_dict() or {}
                return CapacityPurchaseResult(
                    operation_id=operation_id,
                    pack_id=str(stored.get("packId", pack.id)),
                    charged_shards=int(stored.get("costShards", pack.cost_shards)),
                    slot_delta=int(stored.get("slotDelta", pack.slot_delta)),
                    applied=False,
                    capacity=MirrorCapacity(
                        user_id=user_id,
                        purchased_slots=int(stored.get("purchasedSlotsAfter", 0)),
                    ),
                    balance=shards.wallet(user_id).balance,
                )

            before = _capacity_from(
                user_id, user_snapshot.to_dict() or {} if user_snapshot.exists else {}
            ).purchased_slots

            # 잔액이 모자라면 `InsufficientShards`가 올라오고 **transaction 전체가 취소된다.**
            debit = shards.apply_in_transaction(
                scoped, user_id, -pack.cost_shards,
                ShardReason.MIRROR_CAPACITY_PURCHASE, operation_id,
            )
            after = before + pack.slot_delta

            # user 문서는 **field 하나만** 건드린다. 다른 domain의 값을 덮지 않는다.
            transaction.set(user_ref, {PURCHASED_SLOTS_FIELD: after}, merge=True)
            # `create`다 — 우리가 읽은 뒤 다른 요청이 먼저 자리를 잡았으면 commit이 깨진다.
            transaction.create(
                operation_ref, _operation_document(user_id, pack, operation_id, after)
            )

            return CapacityPurchaseResult(
                operation_id=operation_id,
                pack_id=pack.id,
                charged_shards=pack.cost_shards,
                slot_delta=pack.slot_delta,
                applied=debit.applied,
                capacity=MirrorCapacity(user_id=user_id, purchased_slots=after),
                balance=debit.wallet.balance,
            )

        try:
            return run(shards.transaction())
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("capacity_purchase", error) from error

    @staticmethod
    def _unavailable(operation: str, error: Exception) -> CapacityStoreUnavailable:
        logger.error("capacity_store_unavailable op=%s error=%s", operation, type(error).__name__)
        return CapacityStoreUnavailable(operation)
