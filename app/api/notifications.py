"""알림센터 · 기기 등록 endpoint (Phase F).

**userId를 요청으로 받지 않는다.** 주인은 언제나 session의 사용자다 —
받는 순간 남의 알림을 읽는 경로가 생긴다.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, marketplace_service, notification_service, push_service
from app.auth.store import StoreUnavailable
from app.notifications.models import (
    MAX_PAGE,
    PAGE_SIZE,
    NotificationNotFound,
)
from app.notifications.service import NotificationService
from app.push.models import InvalidPushDevice, PushEnvironment
from app.push.service import PushService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users/me", tags=["notifications"])


class NotificationResponse(BaseModel):
    """알림 하나. **구매자를 담지 않는다** — id도 이름도 없다."""

    id: str
    type: str
    listing_id: str = Field(serialization_alias="listingId")
    content_type: str = Field(serialization_alias="contentType")
    title: str
    shard_amount: int = Field(serialization_alias="shardAmount")
    created_at: str = Field(serialization_alias="createdAt")
    read: bool


class NotificationPage(BaseModel):
    notifications: list[NotificationResponse]
    #: 있으면 다음 장이 남아 있다는 뜻이다.
    cursor: str | None = None


class SaleStatResponse(BaseModel):
    listing_id: str = Field(serialization_alias="listingId")
    content_type: str = Field(serialization_alias="contentType")
    title: str
    #: **총 판매 횟수다.** 알림 목록을 몇 장 불러왔는지와 무관하다.
    sale_count: int = Field(serialization_alias="saleCount")
    price_shards: int = Field(serialization_alias="priceShards")


class PushDeviceRequest(BaseModel):
    """**여기 없는 값은 서버가 정한다.** `userId`를 받을 자리가 없다."""

    model_config = {"extra": "forbid"}

    #: APNs token. **경로가 아니라 본문으로 받는다** — URL은 로그·중계에 남는다.
    token: str
    #: 어느 APNs로 보낼지. 서버가 아는 값만 받는다.
    environment: PushEnvironment


def _notification(event) -> NotificationResponse:
    return NotificationResponse(
        id=event.id,
        type=event.type.value,
        listing_id=event.listing_id,
        content_type=event.content_type,
        title=event.title_snapshot,
        shard_amount=event.shard_amount,
        created_at=event.created_at.isoformat(),
        read=event.is_read,
    )


@router.get("/notifications")
def my_notifications(
    user: CurrentUser,
    service: Annotated[NotificationService, Depends(notification_service)],
    cursor: str | None = None,
    limit: int = Query(default=PAGE_SIZE, ge=1, le=MAX_PAGE),
) -> NotificationPage:
    """내 알림. 최신이 먼저다. **전체를 한 번에 읽지 않는다.**"""
    try:
        events, next_cursor = service.page(user, cursor=cursor, limit=limit)
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error
    return NotificationPage(
        notifications=[_notification(x) for x in events], cursor=next_cursor
    )


@router.patch("/notifications/{notification_id}/read")
def mark_notification_read(
    notification_id: str,
    user: CurrentUser,
    service: Annotated[NotificationService, Depends(notification_service)],
) -> NotificationResponse:
    """읽음으로 바꾼다. 이미 읽었으면 그대로다 — 다시 눌러도 오류가 아니다."""
    try:
        return _notification(service.mark_read(user, notification_id))
    except NotificationNotFound as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "notification not found") from error
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error


@router.get("/sale-stats")
def my_sale_stats(
    user: CurrentUser,
    service: Annotated[NotificationService, Depends(notification_service)],
    market: Annotated[object, Depends(marketplace_service)],
) -> list[SaleStatResponse]:
    """내 상품이 각각 몇 번 팔렸는가.

    알림 기록을 세지 않는다 — 그것은 페이지로 끊어 읽으므로 총계가 아니게 된다.
    `downloadCount`가 구매와 같은 transaction에서 오르는 정확한 값이다.
    """
    try:
        stats = service.sale_stats(user)
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error
    return [
        SaleStatResponse(
            listing_id=x.listing_id, content_type=x.content_type, title=x.title,
            sale_count=x.sale_count, price_shards=x.price_shards,
        )
        for x in stats
    ]


@router.put("/push-devices", status_code=status.HTTP_204_NO_CONTENT)
def register_push_device(
    request: PushDeviceRequest,
    user: CurrentUser,
    service: Annotated[PushService, Depends(push_service)],
) -> Response:
    """이 기기로 알림을 받겠다.

    **같은 token을 다시 등록하면 주인이 바뀐다.** 로그아웃하고 다른 계정으로
    들어온 기기가 이전 사람의 판매 알림을 계속 받으면 안 된다.
    """
    try:
        service.register(user.id, token=request.token, environment=request.environment.value)
    except InvalidPushDevice as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "device is not valid") from error
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error
    # 등록 결과에 token을 되돌려주지 않는다 — 알 필요가 없고, 응답 로그에 남는다.
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/push-devices", status_code=status.HTTP_204_NO_CONTENT)
def unregister_push_device(
    request: PushDeviceRequest,
    user: CurrentUser,
    service: Annotated[PushService, Depends(push_service)],
) -> Response:
    """이 기기로 그만 받겠다(로그아웃). **내 등록만 지운다.**"""
    try:
        service.unregister(user.id, request.token)
    except InvalidPushDevice as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "device is not valid") from error
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)
