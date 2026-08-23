"""거울 보관 공간. **조각으로 영구 확장한다.**

보관 공간은 **경제 상품**이지만 원장 그 자체는 아니다. 여기 있는 것은 정책뿐이고,
조각이 실제로 움직이는 일은 기존 `ShardLedgerService`가 한다 —
새 지갑 시스템을 만들지 않는다.

**가격과 칸 수는 서버가 정한다.** client가 `cost`나 `slotDelta`를 보내는 자리가 없다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

# 무료로 주어지는 칸. client의 `MirrorStoragePolicy.freeMirrorSlots`와 같은 값이고
# **여기가 authority다** — client 값은 서버에 닿지 못했을 때의 보수적 기본값이다.
BASE_MIRROR_SLOTS = 5


@dataclass(frozen=True)
class SlotPack:
    """확장 상품 하나. **id만 client에게 받는다.**"""

    id: str
    cost_shards: int
    slot_delta: int


#: 지금 파는 것 하나. 늘리려면 여기에 더한다 — 계산식을 만들지 않는다.
MIRROR_SLOT_PACK = SlotPack(id="mirror_slots_5", cost_shards=10, slot_delta=5)

PACKS: dict[str, SlotPack] = {MIRROR_SLOT_PACK.id: MIRROR_SLOT_PACK}


def pack(pack_id: str) -> SlotPack:
    """모르는 상품이면 거절한다. **client가 만든 상품을 팔지 않는다.**"""
    found = PACKS.get(pack_id)
    if found is None:
        raise UnknownPack(pack_id)
    return found


@dataclass(frozen=True)
class MirrorCapacity:
    """지금 이 사용자가 담을 수 있는 칸.

    **예전 사용자 문서에는 `purchasedSlots` field가 없다.** 그때는 0이다 —
    migration을 돌리지 않는다.
    """

    user_id: str
    purchased_slots: int = 0

    @property
    def base_slots(self) -> int:
        return BASE_MIRROR_SLOTS

    @property
    def effective_slots(self) -> int:
        return self.base_slots + self.purchased_slots

    @staticmethod
    def empty(user_id: str) -> MirrorCapacity:
        return MirrorCapacity(user_id=user_id)


@dataclass(frozen=True)
class CapacityPurchaseResult:
    """구매 한 건의 결과. **client가 아무것도 계산하지 않아도 되게 전부 담는다.**

    `applied`는 **이번 호출이 실제로 경제를 움직였는가**다. 같은 `operation_id`가
    다시 오면 `False`이고, 나머지 값은 처음 처리한 그대로다 — 실패가 아니다.
    """

    operation_id: str
    pack_id: str
    charged_shards: int
    slot_delta: int
    applied: bool
    capacity: MirrorCapacity
    balance: int


def operation_key(user_id: str, operation_id: str) -> str:
    """구매 시도 하나를 가리키는 열쇠.

    **`user + packId`를 쓰지 않는다.** 이 상품은 반복 구매가 가능해서, 그렇게 하면
    두 번째 확장을 영원히 못 산다. 반대로 재시도마다 새 열쇠를 만들면 응답을 잃었을 때
    조각이 두 번 빠진다. 그래서 **의도 하나 = operationId 하나**가 authority다.

    `user_id`를 반드시 섞는다 — 남이 만든 id로 남의 기록을 건드릴 수 없어야 한다.
    길이 접두사는 원장 열쇠(`idempotency_hash`)와 같은 이유다.
    """
    canonical = "|".join(f"{len(part.encode())}:{part}" for part in (user_id, operation_id))
    return hashlib.sha256(canonical.encode()).hexdigest()


class CapacityError(Exception):
    """보관 공간 처리 실패. endpoint에서 client에 맞는 응답으로 바꾼다."""


class UnknownPack(CapacityError):
    """우리가 팔지 않는 상품. **아무것도 기록되지 않는다.**"""


class CapacityStoreUnavailable(CapacityError):
    """저장소에 닿지 못했다. 재시도할 수 있다."""
