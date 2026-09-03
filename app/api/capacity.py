"""GET /users/me/mirror-capacity · POST /users/me/mirror-capacity/purchases

거울 보관 공간. **범용 mutation endpoint가 아니다** — 이 통로로 할 수 있는 일은
"정해진 상품 하나를 산다" 뿐이고, 얼마인지와 몇 칸인지는 서버가 정한다.

client가 보내는 것은 `packId`와 `operationId` 둘뿐이다. `cost`나 `slotDelta`를
실을 자리가 없다.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, mirror_capacity_service
from app.capacity.models import (
    CapacityPurchaseResult,
    CapacityStoreUnavailable,
    MirrorCapacity,
    SlotPack,
    UnknownPack,
)
from app.capacity.service import MirrorCapacityService
from app.shards.models import InsufficientShards

router = APIRouter(prefix="/users/me/mirror-capacity", tags=["capacity"])

UNAVAILABLE = "service unavailable"


class SlotPackPayload(BaseModel):
    """지금 파는 확장 상품. **client가 숫자를 적어 두지 않게 서버가 내려 준다.**"""

    id: str
    cost_shards: int = Field(serialization_alias="costShards")
    slot_delta: int = Field(serialization_alias="slotDelta")

    model_config = {"populate_by_name": True}

    @staticmethod
    def of(pack: SlotPack) -> "SlotPackPayload":
        return SlotPackPayload(
            id=pack.id, cost_shards=pack.cost_shards, slot_delta=pack.slot_delta
        )


class CapacityPayload(BaseModel):
    """담을 수 있는 칸. **몇 개를 쓰고 있는지는 여기 없다** — 그건 기기의 사실이다."""

    base_slots: int = Field(serialization_alias="baseSlots")
    purchased_slots: int = Field(serialization_alias="purchasedSlots")
    effective_slots: int = Field(serialization_alias="effectiveSlots")
    pack: SlotPackPayload

    model_config = {"populate_by_name": True}

    @staticmethod
    def of(capacity: MirrorCapacity, pack: SlotPack) -> "CapacityPayload":
        return CapacityPayload(
            base_slots=capacity.base_slots,
            purchased_slots=capacity.purchased_slots,
            effective_slots=capacity.effective_slots,
            pack=SlotPackPayload.of(pack),
        )


class PurchaseRequest(BaseModel):
    """**이 둘뿐이다.** 가격도 칸 수도 client가 정할 수 없다.

    `operationId`는 **의도 하나**를 가리킨다. 응답을 잃어 재시도할 때는 같은 값을
    보내야 조각이 두 번 빠지지 않는다. 새 구매에는 새 값을 만든다.
    """

    pack_id: str = Field(alias="packId", max_length=64)
    operation_id: str = Field(alias="operationId", max_length=64)

    model_config = {"populate_by_name": True}


class PurchasePayload(BaseModel):
    """구매 결과. **client가 아무것도 계산하지 않아도 되게 전부 담는다.**

    `applied`는 이번 호출이 실제로 경제를 움직였는가다. 같은 `operationId`가
    다시 오면 `false`이고 나머지 값은 처음 처리한 그대로다 — 오류가 아니다.
    """

    operation_id: str = Field(serialization_alias="operationId")
    pack_id: str = Field(serialization_alias="packId")
    charged_shards: int = Field(serialization_alias="chargedShards")
    slot_delta: int = Field(serialization_alias="slotDelta")
    applied: bool
    balance: int
    base_slots: int = Field(serialization_alias="baseSlots")
    purchased_slots: int = Field(serialization_alias="purchasedSlots")
    effective_slots: int = Field(serialization_alias="effectiveSlots")

    model_config = {"populate_by_name": True}

    @staticmethod
    def of(result: CapacityPurchaseResult) -> "PurchasePayload":
        return PurchasePayload(
            operation_id=result.operation_id,
            pack_id=result.pack_id,
            charged_shards=result.charged_shards,
            slot_delta=result.slot_delta,
            applied=result.applied,
            balance=result.balance,
            base_slots=result.capacity.base_slots,
            purchased_slots=result.capacity.purchased_slots,
            effective_slots=result.capacity.effective_slots,
        )


@router.get("", response_model=CapacityPayload, response_model_by_alias=True)
def my_capacity(
    user: CurrentUser,
    service: Annotated[MirrorCapacityService, Depends(mirror_capacity_service)],
) -> CapacityPayload:
    try:
        capacity = service.capacity(user.id)
    except CapacityStoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from error
    return CapacityPayload.of(capacity, service.pack())


@router.post(
    "/purchases",
    response_model=PurchasePayload,
    response_model_by_alias=True,
    status_code=status.HTTP_200_OK,
)
def purchase_slots(
    body: PurchaseRequest,
    user: CurrentUser,
    service: Annotated[MirrorCapacityService, Depends(mirror_capacity_service)],
) -> PurchasePayload:
    """확장 한 건. 잔액이 모자라면 **아무것도 바뀌지 않는다.**"""
    operation_id = _checked_operation_id(body.operation_id)
    try:
        result = service.purchase(user.id, body.pack_id, operation_id)
    except UnknownPack as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown pack") from error
    except InsufficientShards as error:
        # 정상적인 거절이다. 잔액도 칸도 원장도 그대로다.
        raise HTTPException(status.HTTP_409_CONFLICT, "insufficient shards") from error
    except CapacityStoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from error
    return PurchasePayload.of(result)


def _checked_operation_id(raw: str) -> str:
    """UUID만 받는다. client가 만드는 값이라 모양을 여기서 못박는다 —
    문서 열쇠는 어차피 user와 함께 hash되므로 남의 기록에 닿을 수는 없다.
    """
    try:
        return str(UUID(raw))
    except ValueError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "operationId must be a UUID"
        ) from error
