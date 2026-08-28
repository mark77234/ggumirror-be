"""Marketplace endpoint (B-7C).

**등록 · 게시 · 내리기까지다.** 목록 조회(B-7D) · 구매(B-7E)는 아직 없다.

body에 **비용 · 판매자 · 상태 · counter를 실을 자리가 없다.** 조각을 움직이는
범용 endpoint도 만들지 않는다 — 등록비는 게시 성공의 부산물이지 별도 요청이 아니다.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, Field

from app.api.deps import current_user, marketplace_service, push_service, store as auth_store_dep
from app.push.service import PushService
from app.auth.models import User
from app.auth.store import AuthStore, StoreUnavailable
from app.marketplace.models import (
    ContentType,
    InvalidListing,
    LikeCountInconsistent,
    MarketplaceSort,
    SelfLike,
    SelfPurchase,
    InvalidTransition,
    ListingNotFound,
    SnapshotNotFound,
    TitleTaken,
)
from app.marketplace.assets import (
    MAX_ASSETS,
    MAX_IMAGE_BYTES,
    MAX_MANIFEST_BYTES,
    AssetError,
    AssetNotFound,
    AssetStorageUnavailable,
    AssetTooLarge,
    checked_package,
)
from app.marketplace.models import ModeratedListing
from app.marketplace.models import SnapshotNotFound as SnapshotMissing
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
    """**판매자 자신에게** 돌려주는 모양. 상태가 들어 있다."""

    id: str
    content_type: str = Field(serialization_alias="contentType")
    title: str
    description: str
    price_shards: int = Field(serialization_alias="priceShards")
    status: str
    download_count: int = Field(serialization_alias="downloadCount")
    like_count: int = Field(serialization_alias="likeCount")
    published_at: str | None = Field(serialization_alias="publishedAt")
    #: 운영자가 내렸는가. **사유는 담지 않는다** — 판매자에게 필요한 것은
    #: "지금 팔리지 않는다"와 "내가 되돌릴 수 없다"이고, 자세한 분류를 주면
    #: 그 분류를 두고 다투게 된다. 필요하면 문의로 받는다.
    moderation_status: str = Field(
        default="active", serialization_alias="moderationStatus"
    )


class PublicListingResponse(BaseModel):
    """**공개 응답. 내부 값을 담지 않는다.**

    Firestore 문서를 그대로 내보내지 않는다 — `sellerUserId`는 내부 user UUID이고,
    `snapshotId` · `publishFeePaid` · `schemaVersion` · `createdAt` · `updatedAt`은
    사는 사람이 알 필요가 없다. 필드를 늘리려면 **왜 공개해야 하는지**부터 답한다.

    **판매자는 이름으로만 나간다**(1.1.0). `sellerUserId`는 여전히 나가지 않는다 —
    누가 올렸는지 보여 주는 데 내부 id가 필요하지 않다. 이름을 정하지 않은
    판매자는 `null`이고, "익명" 같은 가짜 이름을 지어내지 않는다.
    """

    id: str
    content_type: str = Field(serialization_alias="contentType")
    title: str
    description: str
    price_shards: int = Field(serialization_alias="priceShards")
    download_count: int = Field(serialization_alias="downloadCount")
    like_count: int = Field(serialization_alias="likeCount")
    #: 최초 게시 시각. client의 "업로드 날짜"와 같은 값이다.
    published_at: str = Field(serialization_alias="publishedAt")
    #: 판매자가 정한 이름. **없을 수 있다** — 아직 이름을 정하지 않은 판매자와
    #: 1.0.7 시절에 올라온 상품이 그렇다. 그때는 화면이 알아서 처리한다.
    seller_display_name: str | None = Field(
        default=None, serialization_alias="sellerDisplayName"
    )


class PublishResponse(BaseModel):
    """`published`는 **이번 요청이 상태를 바꿨는가**다. `false`도 실패가 아니다."""

    published: bool
    fee_charged: bool = Field(serialization_alias="feeCharged")
    fee_shards: int = Field(serialization_alias="feeShards")
    #: 잔액의 authority는 서버다.
    balance: int
    listing: ListingResponse


def _seller_names(user_ids: set[str], auth_store: AuthStore) -> dict[str, str]:
    """user id → 이름. **이름을 정한 판매자만** 담는다.

    listing 문서에 이름을 복사해 두지 않는 이유: 복사하면 이름을 바꿀 때마다 그
    사람의 모든 상품을 다시 써야 하고, 그 사이 화면에는 옛 이름이 남는다. 예전에
    올라온 상품에는 값 자체가 없어 backfill도 필요해진다. 지금 규모(고유 판매자
    한 자릿수)에서는 문서를 몇 개 더 읽는 쪽이 훨씬 싸고 단순하며, 이름이 언제나
    최신이고 legacy 상품도 그냥 동작한다.

    읽기가 실패해도 목록을 깨뜨리지 않는다 — 이름이 없는 것으로 둔다.
    """
    names: dict[str, str] = {}
    for user_id in user_ids:
        try:
            user = auth_store.user(user_id)
        except StoreUnavailable:
            continue
        if user is not None and user.display_name:
            names[user_id] = user.display_name
    return names


def _public(listing, seller_name: str | None = None) -> PublicListingResponse:
    return PublicListingResponse(
        seller_display_name=seller_name,
        id=listing.id,
        content_type=listing.content_type.value,
        title=listing.title,
        description=listing.description,
        price_shards=listing.price_shards,
        download_count=listing.download_count,
        like_count=listing.like_count,
        # 공개 목록은 게시 시각이 있는 것만 담는다(저장소가 보장한다).
        published_at=listing.published_at.isoformat(),
    )


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
        moderation_status=listing.moderation_status.value,
    )


class _SellerListingResponse(ListingResponse):
    """판매자 자신에게만 주는 모양. `ListingResponse` + 연결 식별자 하나.

    **공개 DTO(`PublicListingResponse`)는 그대로다** — 거기에는 계속
    `sellerUserId` · `snapshotId` · `sourceContentId`가 없다.
    """

    #: 이 상품이 어느 local 콘텐츠(`MyMirror.id` / `StickerProject.id`)에서 나왔는지.
    #: 판매자가 "내 거울 → 판매 중"에서 자기 상품을 찾는 데 쓴다.
    #: 옛 snapshot이라 알 수 없으면 빈 문자열이다 — 거짓 값을 지어내지 않는다.
    source_content_id: str = Field(default="", serialization_alias="sourceContentId")


class SnapshotResponse(BaseModel):
    """올린 내용물. **bucket · object key · gs:// URL을 담지 않는다.**"""

    snapshot_id: str = Field(serialization_alias="snapshotId")
    content_type: str = Field(serialization_alias="contentType")
    asset_count: int = Field(serialization_alias="assetCount")
    total_bytes: int = Field(serialization_alias="totalBytes")
    #: 다운로드 손상 확인용. 서버가 계산한 값이다.
    manifest_checksum: str = Field(serialization_alias="manifestChecksum")


async def _read_capped(upload: UploadFile, limit: int, label: str) -> bytes:
    """**`Content-Length`를 믿지 않는다** — 실제로 읽은 바이트로 판단한다.

    상한보다 한 바이트 더 읽어 초과를 확인한다. 통째로 메모리에 담기 전에 끊는다.
    """
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"{label} is too large")
    return data


@router.post("/snapshots", status_code=status.HTTP_201_CREATED)
async def create_snapshot(
    user: Annotated[User, Depends(current_user)],
    service: Annotated[MarketplaceService, Depends(marketplace_service)],
    contentType: Annotated[str, Form()],
    manifest: Annotated[UploadFile, File()],
    preview: Annotated[UploadFile, File()],
    assets: Annotated[list[UploadFile] | None, File()] = None,
) -> SnapshotResponse:
    """내용물을 올린다. **`snapshotId` · 판매자 · checksum · object key는 서버가 만든다.**

    asset을 전부 올린 뒤에만 snapshot 문서가 생긴다 — 반쪽 업로드는 문서가 없으므로
    어떤 listing도 참조할 수 없다.

    파일 이름은 **assetID(UUID)만** 허용한다. 경로를 담을 자리가 없다.
    """
    uploaded = assets or []
    if len(uploaded) > MAX_ASSETS:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "too many assets")

    # 확장자를 떼고 남은 것이 UUID여야 한다 — 검증은 `checked_package`가 한다.
    #
    # **dict comprehension으로 모으지 않는다.** 같은 assetID가 두 번 오면 마지막 값이
    # 조용히 이기고, 업로더는 자기가 보낸 것과 다른 이미지가 팔리는 것을 모른다.
    # 여기서 거절한다.
    parts: dict[str, bytes] = {}
    for item in uploaded:
        asset_id = (item.filename or "").removesuffix(".png")
        if asset_id in parts:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "duplicate asset")
        parts[asset_id] = await _read_capped(item, MAX_IMAGE_BYTES, "asset")

    try:
        package = checked_package(
            content_type=contentType,
            manifest=await _read_capped(manifest, MAX_MANIFEST_BYTES, "manifest"),
            preview=await _read_capped(preview, MAX_IMAGE_BYTES, "preview"),
            assets=parts,
        )
    except AssetTooLarge as error:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "package is too large") from error
    except AssetError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "package is not valid") from error

    try:
        snapshot = service.create_snapshot(user, content_type=contentType, package=package)
    except InvalidListing as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "contentType is not valid") from error
    except (AssetError, SnapshotMissing) as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error

    return SnapshotResponse(
        snapshot_id=snapshot.id,
        content_type=snapshot.content_type.value,
        asset_count=snapshot.asset_count,
        total_bytes=snapshot.total_bytes,
        manifest_checksum=snapshot.manifest_checksum,
    )


def _streamed(stored, *, cache: str) -> Response:
    """저장된 바이트를 그대로 흘려보낸다. **signed URL을 만들지 않는다.**"""
    return Response(
        content=stored.data,
        media_type=stored.content_type,
        headers={"Content-Length": str(len(stored.data)), "Cache-Control": cache},
    )


@router.get("/listings/{listing_id}/preview")
def listing_preview(
    listing_id: str,
    service: Annotated[MarketplaceService, Depends(marketplace_service)],
) -> Response:
    """**공개다.** `published`만 — draft · unlisted · 없는 것 모두 404.

    내부 GCS object key를 응답에 담지 않는다. 우리가 읽어서 흘려보낸다.
    """
    try:
        stored = service.preview(listing_id)
    except AssetStorageUnavailable as error:
        # 상품은 있고 우리 bucket 설정이 없다 — 404로 뭉개면 오진을 부른다.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error
    except (ListingNotFound, SnapshotMissing, AssetNotFound) as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "preview not found") from error
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error
    # snapshot은 불변이라 오래 캐시해도 안전하다.
    return _streamed(stored, cache="public, max-age=86400, immutable")


@router.get("/listings/{listing_id}/template")
def listing_template(
    listing_id: str,
    user: Annotated[User, Depends(current_user)],
    service: Annotated[MarketplaceService, Depends(marketplace_service)],
) -> Response:
    """원본 템플릿. **판매자 또는 구매자만.**

    `published`를 요구하지 않는다 — 판매자가 내려도 산 사람은 받아야 한다.
    """
    try:
        stored = service.template(user, listing_id)
    except AssetStorageUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error
    except (ListingNotFound, SnapshotMissing, AssetNotFound) as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "template not found") from error
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error
    return _streamed(stored, cache="private, max-age=3600, immutable")


@router.get("/listings/{listing_id}/template/assets/{asset_id}")
def listing_template_asset(
    listing_id: str,
    asset_id: str,
    user: Annotated[User, Depends(current_user)],
    service: Annotated[MarketplaceService, Depends(marketplace_service)],
) -> Response:
    """템플릿이 참조하는 이미지 하나. 권한은 템플릿과 같다."""
    try:
        stored = service.template_asset(user, listing_id, asset_id)
    except AssetStorageUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error
    except (ListingNotFound, SnapshotMissing, AssetNotFound, AssetError) as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "asset not found") from error
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error
    return _streamed(stored, cache="private, max-age=3600, immutable")


@router.get("/listings")
def browse_listings(
    service: Annotated[MarketplaceService, Depends(marketplace_service)],
    auth_store: Annotated[AuthStore, Depends(auth_store_dep)],
    contentType: ContentType | None = None,
    sort: MarketplaceSort = MarketplaceSort.LATEST,
) -> list[PublicListingResponse]:
    """**공개다 — 로그인 없이 볼 수 있다.**

    상점 구경에 로그인 벽을 세우지 않는다(Core Product Policy). 상품이 없으면
    빈 배열이다 — 가짜 상품을 만들지 않는다.

    조회가 `downloadCount` · `likeCount`를 올리지 않는다. 그건 B-7E와 like phase다.
    """
    try:
        listings = service.browse(content_type=contentType, sort=sort)
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error
    # **판매자 이름은 한 번에 모아 읽는다.** 상품마다 조회하면 목록 하나에 N번
    # 읽게 되고, 같은 판매자의 상품이 여러 개일 때 같은 문서를 반복해서 읽는다.
    names = _seller_names({x.seller_user_id for x in listings}, auth_store)
    return [_public(x, names.get(x.seller_user_id)) for x in listings]


@router.get("/listings/{listing_id}")
def listing_detail(
    listing_id: str,
    service: Annotated[MarketplaceService, Depends(marketplace_service)],
    auth_store: Annotated[AuthStore, Depends(auth_store_dep)],
) -> PublicListingResponse:
    """공개 상세. **draft · unlisted · 없는 것 모두 404**다.

    판매자 자신이라도 이 경로로는 자기 draft를 볼 수 없다 — 공개 조회와
    판매자 관리를 한 endpoint에 섞지 않는다.
    """
    try:
        listing = service.listing(listing_id)
        names = _seller_names({listing.seller_user_id}, auth_store)
        return _public(listing, names.get(listing.seller_user_id))
    except ListingNotFound as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "listing not found") from error
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error


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
    except TitleTaken as error:
        # **generic 오류로 숨기지 않는다.** 다음에 무엇을 할지(다른 이름) 알아야 한다.
        # 등록비는 빠지지 않았다 — 이름 확인이 차감보다 먼저다.
        raise HTTPException(
            status.HTTP_409_CONFLICT, "listing title is already taken"
        ) from error
    except ModeratedListing as error:
        # **조용히 성공시키지 않는다.** 성공했다고 답하면 판매자는 올라간 줄 알고,
        # 실제로는 목록에 없다 — 그게 더 나쁜 거짓말이다.
        raise HTTPException(
            status.HTTP_409_CONFLICT, "listing was removed by an operator"
        ) from error
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


class PurchaseResponse(BaseModel):
    """획득 결과.

    `purchased`는 **이번 요청이 소유권을 만들었는가**다. `alreadyOwned`가 true여도
    실패가 아니다 — 재시도·연타는 정상 동작이다.

    내부 값(`sellerUserId` · `snapshotId`)을 담지 않는다. 템플릿을 내려줄 때
    서버가 소유권을 직접 조회하면 되므로 client가 알 필요가 없다.
    """

    purchased: bool
    already_owned: bool = Field(serialization_alias="alreadyOwned")
    price_paid: int = Field(serialization_alias="pricePaid")
    #: 잔액의 authority는 서버다.
    balance: int
    download_count: int = Field(serialization_alias="downloadCount")
    listing_id: str = Field(serialization_alias="listingId")
    acquired_at: str = Field(serialization_alias="acquiredAt")


class PurchasedListingResponse(BaseModel):
    """"내가 산 것" 한 줄. 상품이 내려가도 남는다."""

    listing_id: str = Field(serialization_alias="listingId")
    price_paid: int = Field(serialization_alias="pricePaid")
    acquired_at: str = Field(serialization_alias="acquiredAt")
    #: 상품이 내려갔거나 사라지면 `null` — 소유권 자체는 유지된다.
    listing: PublicListingResponse | None


@router.post("/listings/{listing_id}/purchase")
def purchase_listing(
    listing_id: str,
    user: Annotated[User, Depends(current_user)],
    service: Annotated[MarketplaceService, Depends(marketplace_service)],
    pushes: Annotated[PushService, Depends(push_service)],
) -> PurchaseResponse:
    """**body가 없다.** 가격 · 판매자 · 수량을 client가 정하는 자리를 만들지 않는다.

    구매자 차감 · 판매자 지급 · 소유권 · 다운로드 수 · **판매 알림 기록**이
    한 commit이다. push는 그 commit이 끝난 **뒤에** 최선을 다해 보낸다 —
    APNs를 transaction 안에서 부르면 network 지연이 잠금을 붙들고, 실패가
    구매를 되돌린다.
    """
    try:
        result = service.purchase(user, listing_id)
    except ListingNotFound as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "listing not found") from error
    except SelfPurchase as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "cannot buy your own listing") from error
    except InsufficientShards as error:
        raise HTTPException(status.HTTP_409_CONFLICT, "not enough shards") from error
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error

    # **commit 뒤다. 여기서 무엇이 실패해도 구매는 이미 끝났다.**
    # 이미 갖고 있던 경우에는 `sale_event`가 없다 — 새로 팔린 것이 없으므로
    # 알림도 없다(연타가 판매자에게 같은 알림을 여러 번 보내지 않는다).
    if result.sale_event is not None:
        try:
            pushes.notify_sale(result.sale_event)
        except Exception:   # noqa: BLE001 — 전송 실패가 구매 응답을 실패로 만들지 않는다
            logger.warning("sale_push_failed")

    return PurchaseResponse(
        purchased=result.purchased,
        already_owned=result.already_owned,
        price_paid=result.price_paid,
        balance=result.balance,
        download_count=result.download_count,
        listing_id=result.ownership.listing_id,
        acquired_at=result.ownership.created_at.isoformat(),
    )


class LikeResponse(BaseModel):
    """좋아요 결과. `changed`는 **이번 요청이 관계를 바꿨는가**다.

    내부 값(`userId` · `sellerUserId` · `snapshotId`)을 담지 않는다.
    """

    listing_id: str = Field(serialization_alias="listingId")
    liked: bool
    changed: bool
    like_count: int = Field(serialization_alias="likeCount")


def _like(result) -> LikeResponse:
    return LikeResponse(
        listing_id=result.listing_id,
        liked=result.liked,
        changed=result.changed,
        like_count=result.like_count,
    )


@router.put("/listings/{listing_id}/like")
def like_listing(
    listing_id: str,
    user: Annotated[User, Depends(current_user)],
    service: Annotated[MarketplaceService, Depends(marketplace_service)],
) -> LikeResponse:
    """**body가 없다.** 같은 요청을 반복해도 `changed=false`로 끝난다(오류가 아니다).

    조각을 주거나 받지 않는다.
    """
    try:
        return _like(service.like(user, listing_id))
    except ListingNotFound as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "listing not found") from error
    except SelfLike as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "cannot like your own listing") from error
    except LikeCountInconsistent as error:
        logger.error("marketplace_like_count_inconsistent")
        raise HTTPException(status.HTTP_409_CONFLICT, "like count is inconsistent") from error
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error


@router.delete("/listings/{listing_id}/like")
def unlike_listing(
    listing_id: str,
    user: Annotated[User, Depends(current_user)],
    service: Annotated[MarketplaceService, Depends(marketplace_service)],
) -> LikeResponse:
    """내려간 상품도 취소할 수 있다 — 못 지우면 count가 영구히 남는다."""
    try:
        return _like(service.unlike(user, listing_id))
    except ListingNotFound as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "listing not found") from error
    except LikeCountInconsistent as error:
        logger.error("marketplace_like_count_inconsistent")
        raise HTTPException(status.HTTP_409_CONFLICT, "like count is inconsistent") from error
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error


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


purchases_router = APIRouter(prefix="/users/me/marketplace", tags=["marketplace"])


@purchases_router.get("/purchases")
def my_purchases(
    user: Annotated[User, Depends(current_user)],
    service: Annotated[MarketplaceService, Depends(marketplace_service)],
) -> list[PurchasedListingResponse]:
    """내가 산 것. **`/users/me/...`뿐이다** — 임의 userId로 남의 소유권을 조회하는
    경로를 만들지 않는다.

    상품이 내려갔어도 목록에 남는다(돈을 냈으면 계속 쓸 수 있어야 한다).
    """
    try:
        owned = service.purchases(user)
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error

    return [
        PurchasedListingResponse(
            listing_id=ownership.listing_id,
            price_paid=ownership.price_paid,
            acquired_at=ownership.created_at.isoformat(),
            listing=_public(listing) if listing and listing.published_at else None,
        )
        for ownership, listing in owned
    ]


@purchases_router.get("/listings")
def my_listings(
    user: Annotated[User, Depends(current_user)],
    service: Annotated[MarketplaceService, Depends(marketplace_service)],
) -> list[_SellerListingResponse]:
    """**내가 올린 것 전부** — `draft` · `published` · `unlisted`.

    공개 목록(`GET /marketplace/listings`)과 다른 것이다. 그쪽은 `published`만
    보여 주므로, 판매자가 아직 안 올린 것과 내린 것을 다시 찾을 방법이 없었다.
    앱이 기억해 둔 id에 의존하면 앱을 지웠거나 기기를 바꾼 뒤 관리가 끊긴다.

    `/users/me/...`뿐이다 — 임의 userId로 **남의 draft를 조회하는 경로를 만들지
    않는다.** 판매자 판단은 session의 user이고 요청 본문이나 query로 받지 않는다.

    응답은 판매자 전용 `ListingResponse`(`status` 포함)다. **공개 DTO는 그대로
    둔다** — 거기에는 계속 `sellerUserId` · `snapshotId`가 없다.
    """
    try:
        listings = service.seller_listings(user)
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error
    return [
        _SellerListingResponse(
            **_listing(x).model_dump(),
            source_content_id=service.source_content(x),
        )
        for x in listings
    ]


@purchases_router.delete("/listings/{listing_id}")
def delete_my_listing(
    listing_id: str,
    user: Annotated[User, Depends(current_user)],
    service: Annotated[MarketplaceService, Depends(marketplace_service)],
) -> ListingResponse:
    """상품을 **삭제한다.** 판매자 본인만.

    `deleted`는 **끝 상태**다 — 다시 올릴 수 없다. `unlisted`(잠시 내림)와
    구분하는 이유: 사용자가 "삭제"를 골랐는데 되살아나는 상품처럼 행동하면 안 된다.

    **아무것도 실제로 지우지 않는다.** snapshot · GCS object · 소유권 · 원장이
    그대로 남아서 **이미 산 사람은 계속 받는다.** 등록비도 돌려주지 않는다 —
    상점에 올라가 있던 값은 이미 제공됐다.
    """
    try:
        listing = service.delete_listing(user, listing_id)
    except ListingNotFound as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "listing not found") from error
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error
    return _listing(listing)


@purchases_router.get("/listings/{listing_id}/preview")
def my_listing_preview(
    listing_id: str,
    user: Annotated[User, Depends(current_user)],
    service: Annotated[MarketplaceService, Depends(marketplace_service)],
) -> Response:
    """**내가 올린 상품의 미리보기.** `draft` · `published` · `unlisted` 모두.

    공개 미리보기(`GET /marketplace/listings/{id}/preview`)는 `published`만
    보여 주고 **그 정책은 그대로다.** 판매자 관리 화면에서만 아직 올리지 않은 것과
    내린 것의 생김새가 필요하다 — 숫자만 보이면 어느 상품인지 알 수 없다.

    판매자 본인만이고, 남의 것이면 404다(존재 여부를 알려주지 않는다).
    signed URL을 만들지 않고 bucket 경로도 응답에 담지 않는다 — 우리가 읽어 흘려보낸다.
    """
    try:
        stored = service.seller_preview(user, listing_id)
    except AssetStorageUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error
    except (ListingNotFound, SnapshotMissing, AssetNotFound) as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "preview not found") from error
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error
    # snapshot은 불변이지만 **판매자 전용**이라 공용 캐시에 두지 않는다.
    return _streamed(stored, cache="private, max-age=3600, immutable")


@purchases_router.get("/likes")
def my_likes(
    user: Annotated[User, Depends(current_user)],
    service: Annotated[MarketplaceService, Depends(marketplace_service)],
) -> list[str]:
    """내가 좋아요한 상품 id 목록.

    공개 목록에 `likedByMe`를 넣으려고 optional auth를 만들지 않는다 —
    client가 공개 목록과 이 목록을 합친다. 내부 userId는 응답에 없다.
    """
    try:
        return service.liked_listing_ids(user)
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error
