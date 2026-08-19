"""Marketplace 서비스.

**client가 정할 수 있는 것과 서버가 정하는 것을 여기서 가른다.**
등록 비용 · 판매자 · 상태 · counter · 게시 시각은 전부 서버 몫이다.
"""

from __future__ import annotations

import logging

from app.auth.models import User
from app.marketplace.models import (
    ContentType,
    MarketplaceSort,
    Ownership,
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
from app.marketplace.store import MarketplaceStore
from app.shards.service import ShardLedgerService

logger = logging.getLogger(__name__)


class MarketplaceService:
    def __init__(self, store: MarketplaceStore, shards: ShardLedgerService) -> None:
        self._store = store
        self._shards = shards

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

    @staticmethod
    def fee(content_type: ContentType) -> int:
        return MarketplacePublishPolicy.fee(content_type)
