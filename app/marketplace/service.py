"""Marketplace 서비스.

**client가 정할 수 있는 것과 서버가 정하는 것을 여기서 가른다.**
등록 비용 · 판매자 · 상태 · counter · 게시 시각은 전부 서버 몫이다.
"""

from __future__ import annotations

import json
import logging

from app.auth.models import User
from app.marketplace.models import (
    ContentType,
    MarketplaceSort,
    LikeResult,
    Ownership,
    Snapshot,
    SnapshotNotFound,
    PurchaseResult,
    InvalidListing,
    Listing,
    ListingNotFound,
    ListingStatus,
    MarketplacePublishPolicy,
    PublishResult,
    checked_price,
    normalized_description,
    normalized_title,
)
from app.marketplace.assets import (
    AssetError,
    AssetNotFound,
    AssetStorageUnavailable,
    referenced_asset_ids,
    MarketplaceAssetStorage,
    SnapshotPackage,
    asset_key,
    manifest_key,
    preview_key,
)
from app.marketplace.store import MarketplaceStore
from app.shards.service import ShardLedgerService

logger = logging.getLogger(__name__)


class MarketplaceService:
    def __init__(
        self,
        store: MarketplaceStore,
        shards: ShardLedgerService,
        assets: MarketplaceAssetStorage | None = None,
    ) -> None:
        self._store = store
        self._shards = shards
        self._assets = assets

    def create_draft(
        self,
        user: User,
        *,
        content_type: str,
        title: str,
        description: str,
        price_shards: int,
        snapshot_id: str,
    ) -> Listing:
        """draft를 만든다. **수수료를 받지 않는다** — 만들다 만 것에 돈을 받지 않는다.

        `sellerUserId` · `status` · `publishFeePaid` · counter를 client가 정하는
        자리가 없다. 인자에 그런 값이 아예 없다.
        """
        try:
            kind = ContentType(content_type)
        except ValueError as error:
            raise InvalidListing("contentType") from error

        # 서버에 없는 내용물은 draft로도 만들지 않는다 — 매달린 draft를 남기지 않는다.
        self._store.snapshot(snapshot_id, user.id)

        listing = Listing(
            id=Listing.new_id(),
            seller_user_id=user.id,
            content_type=kind,
            title=normalized_title(title),
            description=normalized_description(description),
            price_shards=checked_price(price_shards),
            snapshot_id=snapshot_id,
            status=ListingStatus.DRAFT,
            publish_fee_paid=False,
            download_count=0,
            like_count=0,
            published_at=None,
        )
        created = self._store.create(listing)
        logger.info(
            "marketplace_draft_created type=%s price=%d", kind.value, created.price_shards
        )
        return created

    def publish(self, user: User, listing_id: str) -> PublishResult:
        """**최초 게시. 등록비와 상태 변경이 한 commit이다**(저장소가 보장한다).

        이미 게시돼 있으면 `published=False`로 조용히 끝난다 — 오류가 아니다.
        """
        result = self._store.publish(listing_id, user.id, self._shards)
        logger.info(
            "marketplace_publish type=%s published=%s fee_charged=%s fee=%d balance=%d",
            result.listing.content_type.value, result.published,
            result.fee_charged, result.fee_shards, result.balance,
        )
        return result

    def unpublish(self, user: User, listing_id: str) -> Listing:
        """목록에서 내린다. **경제 mutation 0** — 낸 등록비는 돌아오지 않는다."""
        listing = self._store.unpublish(listing_id, user.id)
        logger.info("marketplace_unpublish status=%s", listing.status.value)
        return listing

    # MARK: - 공개 조회 (B-7D)

    def browse(
        self, *, content_type: ContentType | None = None, sort: MarketplaceSort | None = None
    ) -> list[Listing]:
        """공개 목록. **`published`만** 보이고 정렬은 여기서 한다.

        정렬을 저장소가 아니라 service가 하는 이유: 같은 요청이 저장소에 따라 다른
        순서를 내놓으면 안 된다. Firestore 질의는 `status == published` 하나뿐이고
        종류 필터도 여기서 건다 — 정렬마다 composite index를 요구하지 않는다.
        """
        listings = self._store.list_published()
        if content_type is not None:
            listings = [x for x in listings if x.content_type is content_type]
        return (sort or MarketplaceSort.default()).sorted(listings)

    def listing(self, listing_id: str) -> Listing:
        """공개 상세. draft · unlisted · 없는 것은 모두 `ListingNotFound`다."""
        return self._store.get_published(listing_id)

    def seller_listings(self, user: User) -> list[Listing]:
        """**판매자 자신의** listing 전부 — `draft` · `published` · `unlisted`.

        판매자가 자기 상품을 다시 찾는 **authority**다. 앱이 기억해 둔 id에
        의존하면 앱을 지웠거나 기기를 바꾼 뒤 자기 상품을 내릴 수 없다.

        정렬은 `updatedAt` 내림차순이다 — 방금 만진 것이 위로 온다. 같으면 id로
        안정화한다(값이 같을 때 순서가 흔들리면 목록이 이유 없이 재배열돼 보인다).
        `updatedAt`은 이미 있는 field다 — 정렬 때문에 schema를 늘리지 않는다.
        """
        listings = self._store.list_for_seller(user.id)
        return sorted(listings, key=lambda x: (x.updated_at, x.id), reverse=True)

    # MARK: - 획득 (B-7E)

    def purchase(self, user: User, listing_id: str) -> PurchaseResult:
        """상품을 획득한다. **경제 전체가 한 commit이다**(저장소가 보장한다).

        요청에 가격 · 판매자 · 수량을 실을 자리가 없다 — 값은 transaction 안에서
        읽은 listing이 정한다. 이미 갖고 있으면 `already_owned=True`로 조용히 끝난다.
        """
        result = self._store.acquire(listing_id, user.id, self._shards)
        logger.info(
            "marketplace_purchase purchased=%s already_owned=%s price=%d balance=%d downloads=%d",
            result.purchased, result.already_owned,
            result.price_paid, result.balance, result.download_count,
        )
        return result

    def purchases(self, user: User) -> list[tuple[Ownership, Listing | None]]:
        """내가 가진 것. **내려간 상품도 남는다** — 돈을 냈으면 계속 쓸 수 있어야 한다.

        화면에 필요한 listing metadata를 함께 붙인다. 소유권 하나마다 listing을
        조회하므로 N+1이지만 **"내가 산 것"은 사용자당 수십 건 규모**다 —
        많아지면 그때 listing 일괄 조회나 소유권에 표시용 값 복사를 검토한다.
        """
        owned = sorted(self._store.ownerships(user.id), key=lambda x: x.created_at, reverse=True)
        pairs: list[tuple[Ownership, Listing | None]] = []
        for ownership in owned:
            try:
                # 내려간 상품도 보여야 하므로 **공개 조회가 아니라 판매자 무관 조회**가 필요하다.
                listing = self._store.listing(ownership.listing_id, ownership.seller_user_id)
            except ListingNotFound:
                listing = None
            pairs.append((ownership, listing))
        return pairs

    # MARK: - 좋아요 (B-7E.1)

    def like(self, user: User, listing_id: str) -> LikeResult:
        """좋아요. **조각 경제와 무관하다** — 이 경로에 원장도 지갑도 없다."""
        result = self._store.like(listing_id, user.id)
        logger.info(
            "marketplace_like changed=%s likes=%d", result.changed, result.like_count
        )
        return result

    def unlike(self, user: User, listing_id: str) -> LikeResult:
        result = self._store.unlike(listing_id, user.id)
        logger.info(
            "marketplace_unlike changed=%s likes=%d", result.changed, result.like_count
        )
        return result

    def liked_listing_ids(self, user: User) -> list[str]:
        """내가 좋아요한 상품 id. **관계만** 돌려준다 — 내부 userId를 담지 않는다."""
        return sorted(x.listing_id for x in self._store.likes(user.id))

    # MARK: - snapshot 업로드 (B-7F)

    def create_snapshot(
        self, user: User, *, content_type: str, package: SnapshotPackage
    ) -> Snapshot:
        """asset을 먼저 올리고 **성공한 뒤에만** Firestore 문서를 만든다.

        순서가 핵심이다 — 반쪽 업로드로 끝나면 문서가 없으므로 listing이 그것을
        참조할 수 없다. 남은 GCS object는 orphan이고 **best-effort로 치운다**
        (실패해도 경제와 권리는 안전하다).

        `snapshotId` · `sellerUserId` · checksum · object key를 **서버가 만든다.**
        """
        try:
            kind = ContentType(content_type)
        except ValueError as error:
            raise InvalidListing("contentType") from error
        if self._assets is None:
            raise AssetStorageUnavailable("marketplace asset bucket is not configured")

        # **API가 이미 검증했지만 여기서 다시 확인한다.** 저장된 manifest가 선언한
        # contentType과 맞지 않으면 구매자 앱이 못 읽는 바이트를 판 것이 되고,
        # snapshot은 불변이라 나중에 고칠 수 없다. 검증을 한 호출 경로에만
        # 의존하지 않는다 — 우회하면 손해가 구매자에게 간다.
        try:
            referenced = referenced_asset_ids(kind.value, json.loads(package.manifest))
        except (AssetError, ValueError) as error:
            # **`InvalidListing`으로 바꾼다.** 그대로 두면 API가 `AssetError`를
            # 저장소 장애(503)로 분류해서, 잘못된 package를 보낸 판매자에게
            # "서버 문제"라고 거짓말한다.
            raise InvalidListing("manifest does not match contentType") from error
        if referenced != set(package.assets):
            raise InvalidListing("package assets do not match the manifest")

        snapshot_id = Snapshot.new_id()
        keys = [
            (manifest_key(snapshot_id), package.manifest, "application/json"),
            (preview_key(snapshot_id), package.preview, "image/png"),
        ] + [
            (asset_key(snapshot_id, asset_id), data, "image/png")
            for asset_id, data in sorted(package.assets.items())
        ]

        written: list[str] = []
        try:
            for key, data, content in keys:
                self._assets.put(key, data, content)
                written.append(key)
        except Exception:
            for key in written:
                self._assets.delete(key)
            raise

        snapshot = self._store.create_snapshot(
            Snapshot(
                id=snapshot_id,
                seller_user_id=user.id,
                content_type=kind,
                manifest_checksum=package.manifest_checksum,
                asset_count=len(package.assets),
                total_bytes=package.total_bytes,
            )
        )
        logger.info(
            "marketplace_snapshot_created type=%s assets=%d bytes=%d snapshot=%s",
            kind.value, snapshot.asset_count, snapshot.total_bytes,
            snapshot.manifest_checksum[:12],
        )
        return snapshot

    # MARK: - 전달 (B-7F)

    def preview(self, listing_id: str):
        """공개 미리보기. **`published`만** — draft · unlisted는 없는 것처럼 404다."""
        listing = self._store.get_published(listing_id)
        snapshot = self._complete_snapshot(listing.snapshot_id)
        return self._read(preview_key(snapshot.id))

    def seller_preview(self, user: User, listing_id: str):
        """**판매자 자신의** 미리보기. `draft` · `published` · `unlisted` 모두.

        공개 미리보기(`preview`)와 다른 것이다 — 그쪽은 `published`만 보여 주고
        그 정책은 그대로 둔다. 판매자가 자기 상품을 관리하는 화면에서는 아직 올리지
        않은 것과 내린 것도 생김새가 보여야 한다(숫자만 보이면 어느 상품인지 모른다).

        **판매자 본인만이다.** 남의 draft를 미리보기로 엿볼 수 없다. 없는 것과 권한
        없는 것을 구분해 알려주지 않는다 — 존재 여부 자체가 정보다.

        저장소 접근은 `preview`와 **같은 reader**를 쓴다. 새 storage 경로를 만들지 않는다.
        """
        listing = self._store.any_listing(listing_id)
        if listing.seller_user_id != user.id:
            raise ListingNotFound(listing_id)
        snapshot = self._complete_snapshot(listing.snapshot_id)
        return self._read(preview_key(snapshot.id))

    def template(self, user: User, listing_id: str):
        """원본 템플릿. **판매자 또는 소유자만.**

        `published`를 요구하지 않는다 — 판매자가 내려도 산 사람은 받아야 한다.
        구경만 한 사람은 어떤 경로로도 받을 수 없다.
        """
        listing = self._store.any_listing(listing_id)
        is_seller = listing.seller_user_id == user.id
        if not is_seller and self._store.ownership(listing_id, user.id) is None:
            # 없는 것과 권한 없는 것을 구분해 알려주지 않는다.
            raise ListingNotFound(listing_id)

        snapshot = self._complete_snapshot(listing.snapshot_id)
        return self._read(manifest_key(snapshot.id))

    def template_asset(self, user: User, listing_id: str, asset_id: str):
        """템플릿이 참조하는 이미지 하나. 권한 규칙은 `template`과 같다."""
        listing = self._store.any_listing(listing_id)
        if listing.seller_user_id != user.id and self._store.ownership(listing_id, user.id) is None:
            raise ListingNotFound(listing_id)

        snapshot = self._complete_snapshot(listing.snapshot_id)
        return self._read(asset_key(snapshot.id, asset_id))

    def _complete_snapshot(self, snapshot_id: str) -> Snapshot:
        """**불완전한 snapshot으로는 아무것도 내보내지 않는다.**

        B-7C 시절 fixture처럼 asset 없이 만들어진 문서가 있을 수 있다 —
        거짓 미리보기를 만들지 않고 없는 것으로 취급한다.
        """
        snapshot = self._store.snapshot_for_delivery(snapshot_id)
        if not snapshot.is_complete:
            logger.error("marketplace_snapshot_incomplete")
            raise SnapshotNotFound(snapshot_id)
        return snapshot

    def _read(self, key: str):
        if self._assets is None:
            # 404가 아니다 — 상품은 있고 우리 설정이 없다.
            raise AssetStorageUnavailable("marketplace asset bucket is not configured")
        return self._assets.get(key)

    @staticmethod
    def fee(content_type: ContentType) -> int:
        return MarketplacePublishPolicy.fee(content_type)
