"""Marketplace 서비스.

**client가 정할 수 있는 것과 서버가 정하는 것을 여기서 가른다.**
등록 비용 · 판매자 · 상태 · counter · 게시 시각은 전부 서버 몫이다.
"""

from __future__ import annotations

import logging

from app.auth.models import User
from app.marketplace.models import (
    ContentType,
    InvalidListing,
    Listing,
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

    @staticmethod
    def fee(content_type: ContentType) -> int:
        return MarketplacePublishPolicy.fee(content_type)
