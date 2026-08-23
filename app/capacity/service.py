"""보관 공간 조회 / 확장 구매.

**endpoint가 정책을 알지 않는다.** 가격과 칸 수는 `app.capacity.models`가,
조각 이동은 `ShardLedgerService`가, 원자성은 저장소가 책임진다.
"""

from __future__ import annotations

import logging

from app.capacity.models import (
    CapacityPurchaseResult,
    MirrorCapacity,
    SlotPack,
    pack as find_pack,
)
from app.capacity.store import CapacityStore
from app.shards.service import ShardLedgerService

logger = logging.getLogger(__name__)


class MirrorCapacityService:
    def __init__(self, store: CapacityStore, shards: ShardLedgerService) -> None:
        self._store = store
        self._shards = shards

    def capacity(self, user_id: str) -> MirrorCapacity:
        return self._store.capacity(user_id)

    @staticmethod
    def pack() -> SlotPack:
        """지금 파는 상품. **client가 가격을 적어 두지 않게** 함께 내려 준다."""
        from app.capacity.models import MIRROR_SLOT_PACK

        return MIRROR_SLOT_PACK

    def purchase(
        self, user_id: str, pack_id: str, operation_id: str
    ) -> CapacityPurchaseResult:
        """확장 한 건. **가격과 칸 수는 여기서 정한다** — client가 보낸 값을 믿지 않는다.

        `operation_id`는 **의도 하나**를 가리킨다. 같은 값으로 다시 오면 경제가
        움직이지 않고 그때 결과를 그대로 돌려준다.
        """
        pack = find_pack(pack_id)
        result = self._store.purchase(self._shards, user_id, pack, operation_id)
        logger.info(
            "mirror_capacity_purchase pack=%s applied=%s slots=%d balance=%d",
            result.pack_id, result.applied,
            result.capacity.effective_slots, result.balance,
        )
        return result
