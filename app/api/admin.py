"""운영자 전용 endpoint (Phase E).

**여기 있는 모든 경로는 `AdminUser`를 지난다.** 화면이 메뉴를 숨기는 것은 편의일
뿐이고, 강제로 요청을 보내도 여기서 막힌다.

새 관리자 웹을 만들지 않았다. 새 hosting · 새 로그인 · 새 세션이 필요 없고,
이미 있는 iOS 인증을 그대로 쓰는 쪽이 훨씬 싸다. 나중에 데스크톱 화면이 정말
필요해지면 **이 API 위에** 붙이면 된다.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from app.api.deps import AdminUser, marketplace_service
from app.api.marketplace import _seller_names, _streamed
from app.auth.store import AuthStore, StoreUnavailable
from app.api.deps import store as auth_store_dep
from app.marketplace.assets import AssetNotFound, AssetStorageUnavailable
from app.marketplace.models import (
    ContentType,
    InvalidListing,
    ListingNotFound,
    ModerationReason,
    ModerationStatus,
    NotModerated,
    SnapshotNotFound as SnapshotMissing,
    TerminalListing,
)
from app.marketplace.service import ADMIN_PAGE_SIZE, ADMIN_MAX_PAGE, MarketplaceService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


class AdminStatusResponse(BaseModel):
    """`/admin/me`. **`/users/me`에 섞지 않는다** — 일반 응답에 권한을 실어 보내면
    그 값이 client 안에서 돌아다니게 되고, 화면이 그것을 믿기 시작한다."""

    is_admin: bool = Field(serialization_alias="isAdmin")


class AdminListingResponse(BaseModel):
    """운영 화면에 필요한 최소한.

    **`sellerUserId`를 담지 않는다.** 화면에는 이름이면 충분하고, 내부 id는
    Apple subject · 세션 · 지갑으로 이어지는 열쇠다. `snapshotId` ·
    `sourceContentId` · `moderatedBy`도 화면에서 쓸 일이 없어 빼둔다.
    """

    id: str
    content_type: str = Field(serialization_alias="contentType")
    title: str
    description: str
    price_shards: int = Field(serialization_alias="priceShards")
    status: str
    moderation_status: str = Field(serialization_alias="moderationStatus")
    moderation_reason: str | None = Field(serialization_alias="moderationReason")
    download_count: int = Field(serialization_alias="downloadCount")
    like_count: int = Field(serialization_alias="likeCount")
    created_at: str = Field(serialization_alias="createdAt")
    published_at: str | None = Field(serialization_alias="publishedAt")
    seller_display_name: str | None = Field(
        default=None, serialization_alias="sellerDisplayName"
    )


class AdminListingPage(BaseModel):
    """`cursor`가 있으면 다음 page가 남아 있다는 뜻이다.

    필터를 걸면 `listings`가 요청한 수보다 적을 수 있다 — 저장소에서 읽은 page
    안에서 거르기 때문이다. 그때도 `cursor`를 따라가면 나머지가 나온다.
    """

    listings: list[AdminListingResponse]
    cursor: str | None = None


class TakedownRequest(BaseModel):
    """**여기 없는 값은 서버가 정한다.**

    `sellerId` · `status` · 지갑 · counter를 받을 자리가 없다 — 운영자라도
    상품의 주인이나 경제를 요청 body로 바꿀 수 없다.
    """

    model_config = {"extra": "forbid"}

    reason: ModerationReason
    #: 운영자 내부 메모. 판매자에게도 구매자에게도 나가지 않는다.
    note: str = ""


def _admin(listing, seller_name: str | None) -> AdminListingResponse:
    return AdminListingResponse(
        id=listing.id,
        content_type=listing.content_type.value,
        title=listing.title,
        description=listing.description,
        price_shards=listing.price_shards,
        status=listing.status.value,
        moderation_status=listing.moderation_status.value,
        moderation_reason=(
            listing.moderation_reason.value if listing.moderation_reason else None
        ),
        download_count=listing.download_count,
        like_count=listing.like_count,
        created_at=listing.created_at.isoformat(),
        published_at=listing.published_at.isoformat() if listing.published_at else None,
        seller_display_name=seller_name,
    )


@router.get("/me")
def admin_status(user: AdminUser) -> AdminStatusResponse:
    """운영자면 200, 아니면 403이다.

    `isAdmin: false`를 200으로 돌려주지 않는다 — 이 경로 자체가 운영자 전용이고,
    권한 판단이 **한 곳**(`AdminUser`)에서만 일어나야 나중에 갈라지지 않는다.
    화면은 403을 "메뉴 없음"으로 읽는다.
    """
    return AdminStatusResponse(is_admin=True)


@router.get("/marketplace/listings")
def admin_listings(
    admin: AdminUser,
    service: Annotated[MarketplaceService, Depends(marketplace_service)],
    auth_store: Annotated[AuthStore, Depends(auth_store_dep)],
    contentType: ContentType | None = None,
    moderationStatus: ModerationStatus | None = None,
    cursor: str | None = None,
    limit: int = Query(default=ADMIN_PAGE_SIZE, ge=1, le=ADMIN_MAX_PAGE),
) -> AdminListingPage:
    """운영 목록. **모든 상태가 보인다** — 안 보이면 내릴 수도 없다."""
    try:
        listings, next_cursor = service.admin_listings(
            content_type=contentType,
            moderation_status=moderationStatus,
            cursor=cursor,
            limit=limit,
        )
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error
    # 공개 목록과 **같은 일괄 조회**를 쓴다 — 상품마다 읽지 않는다.
    names = _seller_names({x.seller_user_id for x in listings}, auth_store)
    return AdminListingPage(
        listings=[_admin(x, names.get(x.seller_user_id)) for x in listings],
        cursor=next_cursor,
    )


@router.get("/marketplace/listings/{listing_id}")
def admin_listing_detail(
    listing_id: str,
    admin: AdminUser,
    service: Annotated[MarketplaceService, Depends(marketplace_service)],
    auth_store: Annotated[AuthStore, Depends(auth_store_dep)],
) -> AdminListingResponse:
    try:
        listing = service.admin_listing(listing_id)
    except ListingNotFound as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "listing not found") from error
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error
    names = _seller_names({listing.seller_user_id}, auth_store)
    return _admin(listing, names.get(listing.seller_user_id))


@router.get("/marketplace/listings/{listing_id}/preview")
def admin_listing_preview(
    listing_id: str,
    admin: AdminUser,
    service: Annotated[MarketplaceService, Depends(marketplace_service)],
) -> Response:
    """운영 미리보기. **내려간 상품도 보인다.**

    그림을 못 보면 내릴지 되돌릴지 판단할 수 없다. 공개 미리보기와 **같은 GCS
    object**를 읽는다 — 운영용 사본을 따로 만들지 않는다.
    """
    try:
        stored = service.admin_preview(listing_id)
    except AssetStorageUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error
    except (ListingNotFound, SnapshotMissing, AssetNotFound) as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "preview not found") from error
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error
    # 운영 화면은 조치 뒤 바로 다시 본다. 오래 캐시하지 않는다.
    return _streamed(stored, cache="private, max-age=60")


@router.post("/marketplace/listings/{listing_id}/takedown")
def takedown_listing(
    listing_id: str,
    request: TakedownRequest,
    admin: AdminUser,
    service: Annotated[MarketplaceService, Depends(marketplace_service)],
    auth_store: Annotated[AuthStore, Depends(auth_store_dep)],
) -> AdminListingResponse:
    """상점에서 내린다.

    이미 내려가 있으면 **아무것도 쓰지 않고** 현재 상태를 돌려준다 — 실패가
    아니다. 연타가 기록을 채우면 진짜 조치가 언제였는지 알 수 없게 된다.
    """
    try:
        result = service.admin_takedown(
            admin, listing_id, reason=request.reason, note=request.note
        )
    except ListingNotFound as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "listing not found") from error
    except InvalidListing as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "note is not valid") from error
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error
    names = _seller_names({result.listing.seller_user_id}, auth_store)
    return _admin(result.listing, names.get(result.listing.seller_user_id))


@router.post("/marketplace/listings/{listing_id}/restore")
def restore_listing(
    listing_id: str,
    admin: AdminUser,
    service: Annotated[MarketplaceService, Depends(marketplace_service)],
    auth_store: Annotated[AuthStore, Depends(auth_store_dep)],
) -> AdminListingResponse:
    """다시 공개한다. **판매자가 삭제한 상품은 되살리지 않는다.**"""
    try:
        result = service.admin_restore(admin, listing_id)
    except TerminalListing as error:
        # 사용자의 삭제가 운영자 조치보다 우선한다. 409로 분명히 알린다.
        raise HTTPException(status.HTTP_409_CONFLICT, "listing was deleted by its seller") from error
    except NotModerated as error:
        raise HTTPException(status.HTTP_409_CONFLICT, "listing is not moderated") from error
    except ListingNotFound as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "listing not found") from error
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error
    names = _seller_names({result.listing.seller_user_id}, auth_store)
    return _admin(result.listing, names.get(result.listing.seller_user_id))
