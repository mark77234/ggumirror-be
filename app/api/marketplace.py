"""Marketplace endpoint (B-7C).

**등록 · 게시 · 내리기까지다.** 목록 조회(B-7D) · 구매(B-7E)는 아직 없다.

body에 **비용 · 판매자 · 상태 · counter를 실을 자리가 없다.** 조각을 움직이는
범용 endpoint도 만들지 않는다 — 등록비는 게시 성공의 부산물이지 별도 요청이 아니다.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import current_user, marketplace_service
from app.auth.models import User
from app.auth.store import StoreUnavailable
from app.marketplace.models import (
    InvalidListing,
    InvalidTransition,
    ListingNotFound,
    SnapshotNotFound,
)
from app.marketplace.service import MarketplaceService
from app.shards.models import InsufficientShards

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/marketplace", tags=["marketplace"])


class DraftRequest(BaseModel):
    """client가 정할 수 있는 것 전부. **여기 없는 값은 서버가 정한다.**"""

    model_config = {"extra": "forbid"}

    content_type: str = Field(alias="contentType")
    title: str
    description: str = ""
    price_shards: int = Field(alias="priceShards")
    snapshot_id: str = Field(alias="snapshotId")


class ListingResponse(BaseModel):
    id: str
    content_type: str = Field(serialization_alias="contentType")
    title: str
    description: str
    price_shards: int = Field(serialization_alias="priceShards")
    status: str
    download_count: int = Field(serialization_alias="downloadCount")
    like_count: int = Field(serialization_alias="likeCount")
    published_at: str | None = Field(serialization_alias="publishedAt")


class PublishResponse(BaseModel):
    """`published`는 **이번 요청이 상태를 바꿨는가**다. `false`도 실패가 아니다."""

    published: bool
    fee_charged: bool = Field(serialization_alias="feeCharged")
    fee_shards: int = Field(serialization_alias="feeShards")
    #: 잔액의 authority는 서버다.
    balance: int
    listing: ListingResponse


def _listing(listing) -> ListingResponse:
    return ListingResponse(
        id=listing.id,
        content_type=listing.content_type.value,
        title=listing.title,
        description=listing.description,
        price_shards=listing.price_shards,
        status=listing.status.value,
        download_count=listing.download_count,
        like_count=listing.like_count,
        published_at=listing.published_at.isoformat() if listing.published_at else None,
    )


@router.post("/listings", status_code=status.HTTP_201_CREATED)
def create_listing(
    request: DraftRequest,
    user: Annotated[User, Depends(current_user)],
    service: Annotated[MarketplaceService, Depends(marketplace_service)],
) -> ListingResponse:
    try:
        listing = service.create_draft(
            user,
            content_type=request.content_type,
            title=request.title,
            description=request.description,
            price_shards=request.price_shards,
            snapshot_id=request.snapshot_id,
        )
    except SnapshotNotFound as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "snapshot not found") from error
    except InvalidListing as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "listing is not valid") from error
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error
    return _listing(listing)


@router.post("/listings/{listing_id}/publish")
def publish_listing(
    listing_id: str,
    user: Annotated[User, Depends(current_user)],
    service: Annotated[MarketplaceService, Depends(marketplace_service)],
) -> PublishResponse:
    """**등록비와 게시가 한 commit이다.** 잔액이 모자라면 아무것도 바뀌지 않는다.

    같은 요청을 다시 보내도 안전하다 — `published=false`로 돌아온다(오류가 아니다).
    """
    try:
        result = service.publish(user, listing_id)
    except (ListingNotFound, SnapshotNotFound) as error:
        # 남의 listing과 없는 listing을 구분해 알려주지 않는다.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "listing not found") from error
    except InsufficientShards as error:
        raise HTTPException(status.HTTP_409_CONFLICT, "not enough shards") from error
    except InvalidTransition as error:
        raise HTTPException(status.HTTP_409_CONFLICT, "listing cannot be published") from error
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error

    return PublishResponse(
        published=result.published,
        fee_charged=result.fee_charged,
        fee_shards=result.fee_shards,
        balance=result.balance,
        listing=_listing(result.listing),
    )


@router.post("/listings/{listing_id}/unpublish")
def unpublish_listing(
    listing_id: str,
    user: Annotated[User, Depends(current_user)],
    service: Annotated[MarketplaceService, Depends(marketplace_service)],
) -> ListingResponse:
    """목록에서 내린다. **조각이 움직이지 않는다.**"""
    try:
        listing = service.unpublish(user, listing_id)
    except ListingNotFound as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "listing not found") from error
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error
    return _listing(listing)
