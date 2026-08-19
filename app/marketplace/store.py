"""Marketplace 저장소.

`AuthStore` · `ShardStore`와 같은 방식이다 — Protocol 하나 + Firestore 구현 하나 +
test fake 하나. 계층을 더 쌓지 않는다.

**핵심은 `publish`가 transaction을 소유한다는 것이다.** 등록비 차감과 상태 변경이
하나의 commit이어야 하므로, 조각 원장을 부르는 쪽이 아니라 **listing을 쓰는 쪽**이
transaction을 열어야 한다. 그래서 저장소가 `ShardLedgerService`를 인자로 받는다.
"""

from __future__ import annotations

import threading
from typing import Protocol

from app.marketplace.models import (
    ContentType,
    Listing,
    ListingNotFound,
    ListingStatus,
    MarketplacePublishPolicy,
    PublishResult,
    Snapshot,
    SnapshotNotFound,
)
from app.shards.models import utcnow
from app.shards.service import ShardLedgerService

LISTINGS = "ggumirror_marketplace_listings"
SNAPSHOTS = "ggumirror_marketplace_snapshots"


class MarketplaceStore(Protocol):
    def create(self, listing: Listing) -> Listing:
        """draft를 만든다. **수수료를 받지 않는다** — 만들다 만 것에 돈을 받지 않는다."""

    def listing(self, listing_id: str, seller_user_id: str) -> Listing:
        """**내 listing만** 돌려준다. 남의 것이면 `ListingNotFound`."""

    def snapshot(self, snapshot_id: str, seller_user_id: str) -> Snapshot:
        """내가 올린 내용물인지 확인한다."""

    def publish(self, listing_id: str, seller_user_id: str, shards: ShardLedgerService) -> PublishResult:
        """**최초 게시. 등록비 차감과 상태 변경이 하나의 commit이다.**

        한 transaction 안에서:

        1. listing · snapshot · (원장 · 지갑)을 읽는다
        2. 내 것인가 · draft인가 · 아직 안 냈는가 · 잔액이 되는가
        3. 조각 차감 + 원장 + `status=published` + `publishFeePaid` + `publishedAt`

        **전부 되거나 전부 안 된다.** "수수료만 나가고 게시 실패"가 구조적으로 없다.

        이미 게시돼 있으면 `published=False, fee_charged=False`로 조용히 끝난다 —
        재시도 · 연타가 오류가 아니다. republish(`unlisted → published`)도 무료다.
        """

    def unpublish(self, listing_id: str, seller_user_id: str) -> Listing:
        """`published → unlisted`. **경제 mutation 0**이고 수수료도 되돌리지 않는다."""


class InMemoryMarketplaceStore:
    """test / local용. 실제 Firestore transaction의 **의미**를 흉내 낸다.

    조각 차감과 listing 쓰기가 **같은 lock 안에서** 일어나므로, 중간에 실패하면
    둘 다 반영되지 않는다.
    """

    def __init__(self, shard_store) -> None:
        self.listings: dict[str, Listing] = {}
        self.snapshots: dict[str, Snapshot] = {}
        self._shard_store = shard_store
        self._lock = threading.RLock()

    # MARK: - 읽기

    def create(self, listing: Listing) -> Listing:
        with self._lock:
            self.listings[listing.id] = listing
            return listing

    def listing(self, listing_id: str, seller_user_id: str) -> Listing:
        found = self.listings.get(listing_id)
        if found is None or found.seller_user_id != seller_user_id:
            raise ListingNotFound(listing_id)
        return found

    def snapshot(self, snapshot_id: str, seller_user_id: str) -> Snapshot:
        found = self.snapshots.get(snapshot_id)
        if found is None or found.seller_user_id != seller_user_id:
            raise SnapshotNotFound(snapshot_id)
        return found

    # MARK: - 쓰기

    def publish(
        self, listing_id: str, seller_user_id: str, shards: ShardLedgerService
    ) -> PublishResult:
        with self._lock:
            listing = self.listing(listing_id, seller_user_id)
            fee = MarketplacePublishPolicy.fee(listing.content_type)

            if listing.status is ListingStatus.PUBLISHED:
                # 이미 올라가 있다. 재시도 · 연타는 오류가 아니다.
                return PublishResult(
                    listing=listing, published=False, fee_charged=False,
                    fee_shards=fee, balance=shards.wallet(seller_user_id).balance,
                )

            # 내용물이 서버에 있는지 확인한다 — client가 준 문자열만 믿지 않는다.
            self.snapshot(listing.snapshot_id, seller_user_id)

            now = utcnow()
            charged = not listing.publish_fee_paid

            with self._shard_store.transaction() as tx:
                balance = shards.wallet(seller_user_id).balance
                if charged:
                    result = shards.apply_in_transaction(
                        shards.context(tx),
                        seller_user_id,
                        -fee,
                        MarketplacePublishPolicy.reason(listing.content_type),
                        listing.id,
                    )
                    balance = result.wallet.balance
                    charged = result.applied

                published = Listing(
                    id=listing.id,
                    seller_user_id=listing.seller_user_id,
                    content_type=listing.content_type,
                    title=listing.title,
                    description=listing.description,
                    price_shards=listing.price_shards,
                    snapshot_id=listing.snapshot_id,
                    status=ListingStatus.PUBLISHED,
                    publish_fee_paid=True,
                    download_count=listing.download_count,
                    like_count=listing.like_count,
                    created_at=listing.created_at,
                    updated_at=now,
                    # **최초 게시 시각을 유지한다.** republish가 덮어쓰지 않는다.
                    published_at=listing.published_at or now,
                )
                def write_listing():
                    previous = self.listings.get(listing.id)
                    self.listings[listing.id] = published
                    return lambda: self.listings.__setitem__(listing.id, previous)

                tx.add(write_listing)

            return PublishResult(
                listing=published, published=True, fee_charged=charged,
                fee_shards=fee, balance=balance,
            )

    def unpublish(self, listing_id: str, seller_user_id: str) -> Listing:
        with self._lock:
            listing = self.listing(listing_id, seller_user_id)
            if listing.status is not ListingStatus.PUBLISHED:
                return listing   # 이미 내려가 있거나 아직 안 올렸다. 조용히 끝낸다.

            unlisted = Listing(
                id=listing.id,
                seller_user_id=listing.seller_user_id,
                content_type=listing.content_type,
                title=listing.title,
                description=listing.description,
                price_shards=listing.price_shards,
                snapshot_id=listing.snapshot_id,
                status=ListingStatus.UNLISTED,
                # 아래 전부 **그대로 둔다** — 내렸다고 낸 돈이 돌아오지 않는다.
                publish_fee_paid=listing.publish_fee_paid,
                download_count=listing.download_count,
                like_count=listing.like_count,
                created_at=listing.created_at,
                updated_at=utcnow(),
                published_at=listing.published_at,
            )
            self.listings[listing.id] = unlisted
            return unlisted
