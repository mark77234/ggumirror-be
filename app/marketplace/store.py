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
    Like,
    LikeCountInconsistent,
    LikeResult,
    Listing,
    Ownership,
    ListingNotFound,
    ListingStatus,
    MarketplacePublishPolicy,
    PublishResult,
    PurchaseResult,
    SelfLike,
    SelfPurchase,
    Snapshot,
    SnapshotNotFound,
    like_id,
    ownership_id,
)
from app.shards.models import utcnow
from app.shards.service import ShardLedgerService

LISTINGS = "ggumirror_marketplace_listings"
SNAPSHOTS = "ggumirror_marketplace_snapshots"
#: 구매와 소유를 한 문서로 합쳤다(MVP). 별도 purchases collection을 만들지 않는다.
OWNERSHIP = "ggumirror_marketplace_ownership"
#: 좋아요 관계. **관계가 authority**이고 listing의 `likeCount`는 projection이다.
LIKES = "ggumirror_marketplace_likes"


def _is_public(listing: Listing) -> bool:
    """공개해도 되는가.

    `published`이면서 **게시 시각이 있어야** 한다. `publishedAt`이 없는 published
    문서는 있을 수 없는 상태(B-7C가 항상 채운다)이므로 **거짓 날짜를 지어내지 않고
    공개에서 제외한다** — 잘못된 기록이 목록에 섞여 정렬을 흔드는 것보다 낫다.
    """
    return listing.status is ListingStatus.PUBLISHED and listing.published_at is not None


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

    def list_published(self) -> list[Listing]:
        """공개 목록. **`published`만** 돌려준다 — draft/unlisted는 없는 것처럼 다룬다.

        정렬과 종류 필터는 **service가 한다.** 저장소마다 정렬이 달라지면
        같은 요청이 다른 순서를 내놓는다.
        """

    def get_published(self, listing_id: str) -> Listing:
        """공개 상세. `published`가 아니면 `ListingNotFound` —
        **판매자 자신이라도** 공개 endpoint로는 draft를 볼 수 없다."""

    def acquire(self, listing_id: str, buyer_user_id: str, shards) -> PurchaseResult:
        """**상품 하나를 획득한다. 전부 한 commit이다.**

        한 transaction 안에서:

        1. **읽기를 먼저** — listing · 소유권 (그 뒤에 조각 primitive가 자기 읽기를 한다)
        2. 검증 — published인가 · 내 상품이 아닌가 · 이미 갖고 있지 않은가 · 잔액이 되는가
        3. 유료면 **구매자 차감 + 판매자 지급**(같은 금액, 수수료 0%)
        4. 소유권 `create` — 문서 ID가 `(구매자, 상품)` hash라 중복이 구조적으로 막힌다
        5. `downloadCount + 1`

        **"돈만 나가고 소유권 실패"도, "소유권만 생기고 지급 실패"도 없다.**
        counter를 별도 transaction으로 올리지 않는다 — 죽으면 수가 어긋난다.

        이미 갖고 있으면 아무것도 쓰지 않고 `already_owned=True`로 끝난다.
        무료(`priceShards == 0`)면 지갑도 원장도 건드리지 않고 소유권과 counter만 생긴다.
        """

    def ownerships(self, user_id: str) -> list[Ownership]:
        """내가 가진 것 전부. **내린 상품도 남는다** — 돈을 냈으면 계속 쓸 수 있어야 한다."""

    def like(self, listing_id: str, user_id: str) -> LikeResult:
        """좋아요. **관계 생성과 `likeCount +1`이 한 commit이다.**

        `published`만 가능하고 자기 상품은 안 된다. 이미 눌렀으면 아무것도 쓰지 않고
        `changed=False`로 끝난다 — 연타가 오류가 아니다.

        **조각 경제를 건드리지 않는다.**
        """

    def unlike(self, listing_id: str, user_id: str) -> LikeResult:
        """좋아요 취소. **관계 삭제와 `likeCount -1`이 한 commit이다.**

        `unlisted`여도 취소할 수 있다 — 판매자가 내렸다고 사용자가 자기 좋아요를
        못 지우면 count가 영구히 남는다. 자기 상품 좋아요도 지울 수 있다(정리 동작).

        `likeCount`가 음수가 되지 않는다. 관계가 있는데 count가 0이면
        `LikeCountInconsistent`다 — **조용히 보정하지 않는다.**
        """

    def likes(self, user_id: str) -> list[Like]:
        """내가 좋아요한 것 전부."""

    def create_snapshot(self, snapshot: Snapshot) -> Snapshot:
        """**asset이 전부 올라간 뒤에만** 부른다 — 이 문서가 완결의 증거다."""

    def snapshot_for_delivery(self, snapshot_id: str) -> Snapshot:
        """전달용 조회. 주인을 묻지 않는다 — 권한은 listing/소유권이 판단한다."""

    def ownership(self, listing_id: str, user_id: str) -> Ownership | None:
        """그 사람이 그 상품을 갖고 있는가. 템플릿 접근 판단에 쓴다."""

    def any_listing(self, listing_id: str) -> Listing:
        """상태·주인과 무관하게 listing 하나. **템플릿 전달에만** 쓴다 —
        판매자가 내려도 기존 구매자는 받아야 하기 때문이다."""


class InMemoryMarketplaceStore:
    """test / local용. 실제 Firestore transaction의 **의미**를 흉내 낸다.

    조각 차감과 listing 쓰기가 **같은 lock 안에서** 일어나므로, 중간에 실패하면
    둘 다 반영되지 않는다.
    """

    def __init__(self, shard_store) -> None:
        self.listings: dict[str, Listing] = {}
        self.snapshots: dict[str, Snapshot] = {}
        self.ownership_records: dict[str, Ownership] = {}
        self.likes_by_id: dict[str, Like] = {}
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

    def list_published(self) -> list[Listing]:
        return [x for x in self.listings.values() if _is_public(x)]

    def get_published(self, listing_id: str) -> Listing:
        found = self.listings.get(listing_id)
        if found is None or not _is_public(found):
            raise ListingNotFound(listing_id)
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

    # MARK: - 획득 (한 transaction)

    def acquire(self, listing_id: str, buyer_user_id: str, shards) -> PurchaseResult:
        with self._lock:
            # 읽기를 먼저 한다 — Firestore 구현과 같은 순서다.
            listing = self.get_published(listing_id)
            key = ownership_id(buyer_user_id, listing_id)
            owned = self.ownership_records.get(key)

            if owned is not None:
                # 이미 갖고 있다. **아무것도 쓰지 않는다** — counter도 올리지 않는다.
                return PurchaseResult(
                    ownership=owned, purchased=False, already_owned=True,
                    price_paid=owned.price_paid,
                    balance=shards.wallet(buyer_user_id).balance,
                    download_count=listing.download_count,
                )

            if listing.seller_user_id == buyer_user_id:
                raise SelfPurchase(listing_id)

            price = listing.price_shards
            balance = shards.wallet(buyer_user_id).balance

            with self._shard_store.transaction() as tx:
                buyer_entry = seller_entry = None
                if price > 0:
                    scoped = shards.context(tx)
                    debit = shards.apply_in_transaction(
                        scoped, buyer_user_id, -price,
                        MarketplacePublishPolicy.purchase_reason(listing.content_type),
                        key,
                    )
                    credit = shards.apply_in_transaction(
                        scoped, listing.seller_user_id, price,
                        MarketplacePublishPolicy.sale_reason(listing.content_type),
                        key,
                    )
                    balance = debit.wallet.balance
                    buyer_entry, seller_entry = debit.entry_id, credit.entry_id

                ownership = Ownership(
                    id=key,
                    user_id=buyer_user_id,
                    listing_id=listing_id,
                    seller_user_id=listing.seller_user_id,
                    snapshot_id=listing.snapshot_id,
                    price_paid=price,
                    buyer_ledger_entry_id=buyer_entry,
                    seller_ledger_entry_id=seller_entry,
                )
                counted = self._with_download_count(listing, listing.download_count + 1)

                def write_ownership():
                    if key in self.ownership_records:
                        # `create` 의미다 — 조용히 덮어쓰지 않는다.
                        raise KeyError(key)
                    self.ownership_records[key] = ownership
                    return lambda: self.ownership_records.pop(key, None)

                def write_counter():
                    previous = self.listings.get(listing_id)
                    self.listings[listing_id] = counted
                    return lambda: self.listings.__setitem__(listing_id, previous)

                tx.add(write_ownership)
                tx.add(write_counter)

            return PurchaseResult(
                ownership=ownership, purchased=True, already_owned=False,
                price_paid=price, balance=balance,
                download_count=counted.download_count,
            )

    def ownerships(self, user_id: str) -> list[Ownership]:
        return [x for x in self.ownership_records.values() if x.user_id == user_id]

    # MARK: - 내부

    @staticmethod
    def _with_download_count(listing: Listing, count: int) -> Listing:
        return Listing(
            id=listing.id,
            seller_user_id=listing.seller_user_id,
            content_type=listing.content_type,
            title=listing.title,
            description=listing.description,
            price_shards=listing.price_shards,
            snapshot_id=listing.snapshot_id,
            status=listing.status,
            publish_fee_paid=listing.publish_fee_paid,
            download_count=count,
            like_count=listing.like_count,
            created_at=listing.created_at,
            updated_at=listing.updated_at,
            published_at=listing.published_at,
        )

    # MARK: - 좋아요 (한 transaction · 조각 경제 무관)

    def like(self, listing_id: str, user_id: str) -> LikeResult:
        with self._lock:
            listing = self.get_published(listing_id)
            key = like_id(user_id, listing_id)

            if key in self.likes_by_id:
                # 이미 눌렀다. **아무것도 쓰지 않는다.**
                return LikeResult(
                    listing_id=listing_id, liked=True, changed=False,
                    like_count=_checked_like_count(listing),
                )

            if listing.seller_user_id == user_id:
                raise SelfLike(listing_id)

            count = _checked_like_count(listing) + 1
            self.likes_by_id[key] = Like(id=key, user_id=user_id, listing_id=listing_id)
            self.listings[listing_id] = _with_like_count(listing, count)
            return LikeResult(
                listing_id=listing_id, liked=True, changed=True, like_count=count
            )

    def unlike(self, listing_id: str, user_id: str) -> LikeResult:
        with self._lock:
            listing = self.listings.get(listing_id)
            if listing is None or listing.status is ListingStatus.DRAFT:
                # draft에 좋아요가 달릴 경로가 없다 — 있으면 malformed다.
                raise ListingNotFound(listing_id)

            key = like_id(user_id, listing_id)
            current = _checked_like_count(listing)

            if key not in self.likes_by_id:
                return LikeResult(
                    listing_id=listing_id, liked=False, changed=False, like_count=current
                )

            if current == 0:
                # 관계는 있는데 projection이 0이다. **거짓 보정 대신 멈춘다.**
                raise LikeCountInconsistent(listing_id)

            del self.likes_by_id[key]
            self.listings[listing_id] = _with_like_count(listing, current - 1)
            return LikeResult(
                listing_id=listing_id, liked=False, changed=True, like_count=current - 1
            )

    def likes(self, user_id: str) -> list[Like]:
        return [x for x in self.likes_by_id.values() if x.user_id == user_id]

    # MARK: - snapshot / 전달 (B-7F)

    def create_snapshot(self, snapshot: Snapshot) -> Snapshot:
        with self._lock:
            if snapshot.id in self.snapshots:
                raise SnapshotNotFound(snapshot.id)   # 같은 자리를 덮어쓰지 않는다
            self.snapshots[snapshot.id] = snapshot
            return snapshot

    def snapshot_for_delivery(self, snapshot_id: str) -> Snapshot:
        found = self.snapshots.get(snapshot_id)
        if found is None:
            raise SnapshotNotFound(snapshot_id)
        return found

    def ownership(self, listing_id: str, user_id: str) -> Ownership | None:
        return self.ownership_records.get(ownership_id(user_id, listing_id))

    def any_listing(self, listing_id: str) -> Listing:
        found = self.listings.get(listing_id)
        if found is None:
            raise ListingNotFound(listing_id)
        return found


def _checked_like_count(listing: Listing) -> int:
    """음수 projection을 **조용히 0으로 만들지 않는다.**"""
    if listing.like_count < 0:
        raise LikeCountInconsistent(listing.id)
    return listing.like_count


def _with_like_count(listing: Listing, count: int) -> Listing:
    return Listing(
        id=listing.id,
        seller_user_id=listing.seller_user_id,
        content_type=listing.content_type,
        title=listing.title,
        description=listing.description,
        price_shards=listing.price_shards,
        snapshot_id=listing.snapshot_id,
        status=listing.status,
        publish_fee_paid=listing.publish_fee_paid,
        download_count=listing.download_count,
        like_count=count,
        created_at=listing.created_at,
        updated_at=listing.updated_at,
        published_at=listing.published_at,
    )
