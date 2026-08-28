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
    InvalidTransition,
    ContentType,
    ModeratedListing,
    ModerationAction,
    ModerationEvent,
    ModerationReason,
    ModerationResult,
    ModerationStatus,
    NotModerated,
    TerminalListing,
    TitleTaken,
    listing_title_key,
    moderation_block_id,
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
from dataclasses import replace

from app.notifications.models import (
    NotificationEvent,
    NotificationType,
    sale_event_id,
    takedown_event_id,
)
from app.shards.models import ShardReason, utcnow
from app.shards.service import ShardLedgerService

LISTINGS = "ggumirror_marketplace_listings"
SNAPSHOTS = "ggumirror_marketplace_snapshots"
#: 구매와 소유를 한 문서로 합쳤다(MVP). 별도 purchases collection을 만들지 않는다.
OWNERSHIP = "ggumirror_marketplace_ownership"
#: 좋아요 관계. **관계가 authority**이고 listing의 `likeCount`는 projection이다.
LIKES = "ggumirror_marketplace_likes"
#: 운영 조치 기록. **경제 원장과 다른 collection이다** — 조각이 움직이지 않는다.
MODERATION_EVENTS = "ggumirror_marketplace_moderation_events"
#: 조치된 원본의 재등록 차단. 문서 자리가 곧 열쇠다.
MODERATION_BLOCKS = "ggumirror_marketplace_moderation_blocks"
#: 상품 이름 자리. 문서 id가 `listing_title_key(제목)`이라 **이름 하나에 문서 하나**다.
#: 그 자체가 uniqueness 보증이고, 게시 transaction 안에서 읽고 쓴다.
#: 거울과 스티커가 **같은 공간**을 쓴다 — 사용자에게는 둘 다 "상품"이다.
LISTING_TITLE_CLAIMS = "ggumirror_listing_title_claims"


def _is_public(listing: Listing) -> bool:
    """공개해도 되는가.

    `published`이면서 **게시 시각이 있어야** 한다. `publishedAt`이 없는 published
    문서는 있을 수 없는 상태(B-7C가 항상 채운다)이므로 **거짓 날짜를 지어내지 않고
    공개에서 제외한다** — 잘못된 기록이 목록에 섞여 정렬을 흔드는 것보다 낫다.
    """
    if listing.moderation_status is ModerationStatus.REMOVED:
        # **운영자가 내렸다.** 여기 한 곳에서 막으면 목록 · 상세 · 구매 · 좋아요가
        # 전부 닫힌다 — 네 경로가 모두 이 함수를 지나기 때문이다. 경로마다 따로
        # 검사하면 나중에 생기는 경로 하나가 조용히 빠진다.
        return False
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

    def list_for_seller(self, seller_user_id: str) -> list[Listing]:
        """**판매자 자신의** listing 전부. `draft` · `published` · `unlisted` 모두다.

        공개 목록(`list_published`)과 다른 것이다 — 그쪽은 `published`만 보여 준다.
        판매자가 자기 상품을 관리하려면 아직 안 올린 것과 내린 것까지 보여야 한다.

        **다른 판매자의 것은 한 건도 섞이지 않는다.**
        """

    def delete(self, listing_id: str, seller_user_id: str) -> Listing:
        """판매자가 **삭제한다.** `deleted`는 끝 상태다 — 다시 올릴 수 없다.

        **아무것도 실제로 지우지 않는다**: snapshot · GCS object · 소유권 · 원장이
        그대로 남는다. 이미 산 사람이 계속 받아야 하기 때문이다.
        등록비도 돌려주지 않는다(경제 mutation 0).

        `unlisted`와 다르다 — 사용자가 삭제를 골랐으면 되살아나지 않아야 한다.
        """

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

    # MARK: - 운영 조치 (Phase E)

    def takedown(
        self, listing_id: str, actor_user_id: str,
        reason: ModerationReason, note: str, shards=None,
    ) -> ModerationResult:
        """운영자가 내린다. **상태 · 차단 · 기록 · 알림 · 보상이 한 commit이다.**

        소유권 · `downloadCount`는 그대로다 — 이미 산 사람의 권리는 운영 조치와
        무관하다. 판매자 지갑에는 보상 조각이 얹힌다(`shards`가 있을 때만).

        이미 내려가 있으면 아무것도 쓰지 않고 `changed=False`다(기록도 보상도 없다).
        """

    def restore(self, listing_id: str, actor_user_id: str) -> ModerationResult:
        """운영자가 다시 공개한다. 차단 해제와 기록이 함께 간다.

        판매자가 **삭제한** 상품은 되살리지 않는다 — `TerminalListing`이다.
        """

    def list_for_admin(self, cursor: str | None, limit: int) -> tuple[list[Listing], str | None]:
        """운영 목록. **전체를 한 번에 읽지 않는다** — cursor로 끊어 읽는다."""

    def moderation_events(self, listing_id: str) -> list[ModerationEvent]:
        """그 상품의 조치 기록. 운영자에게만."""

    def is_source_blocked(self, seller_user_id: str, listing: Listing, source_content_id: str) -> bool:
        """이 원본이 지금 차단돼 있는가."""


class InMemoryMarketplaceStore:
    """test / local용. 실제 Firestore transaction의 **의미**를 흉내 낸다.

    조각 차감과 listing 쓰기가 **같은 lock 안에서** 일어나므로, 중간에 실패하면
    둘 다 반영되지 않는다.
    """

    def __init__(self, shard_store, notifications=None) -> None:
        self.listings: dict[str, Listing] = {}
        self.snapshots: dict[str, Snapshot] = {}
        self.ownership_records: dict[str, Ownership] = {}
        self.likes_by_id: dict[str, Like] = {}
        self.moderation_event_log: list[ModerationEvent] = []
        #: `listing_title_key` → listing id. **상품 이름 하나에 상품 하나다.**
        self.title_claims: dict[str, str] = {}
        #: block id → 그 차단을 만든 listing id. 복구할 때 주인을 확인한다.
        self.moderation_blocks: dict[str, str] = {}
        self._shard_store = shard_store
        # 판매 알림을 **구매와 같은 lock 안에서** 쓴다(Firestore transaction과 같은 뜻).
        self._notifications = notifications
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

    def list_for_seller(self, seller_user_id: str) -> list[Listing]:
        return [x for x in self.listings.values() if x.seller_user_id == seller_user_id]

    def delete(self, listing_id: str, seller_user_id: str) -> Listing:
        found = self.listings.get(listing_id)
        if found is None or found.seller_user_id != seller_user_id:
            raise ListingNotFound(listing_id)
        if found.status is ListingStatus.DELETED:
            return found
        updated = replace(found, status=ListingStatus.DELETED, updated_at=utcnow())
        self.listings[listing_id] = updated
        # **이름을 놓아 준다.** 삭제한 상품이 이름을 계속 쥐고 있으면
        # 아무도 그 이름을 다시 쓸 수 없다.
        self._release_title(found)
        return updated

    def _release_title(self, listing: Listing) -> None:
        key = listing_title_key(listing.title)
        if self.title_claims.get(key) == listing.id:
            self.title_claims.pop(key, None)

    # MARK: - 쓰기

    def publish(
        self, listing_id: str, seller_user_id: str, shards: ShardLedgerService
    ) -> PublishResult:
        with self._lock:
            listing = self.listing(listing_id, seller_user_id)
            fee = MarketplacePublishPolicy.fee(listing.content_type)

            if listing.status.is_terminal:
                # **삭제는 끝 상태다.** 사용자가 삭제를 골랐으면 되살아나지 않는다.
                raise InvalidTransition(listing.status.value)

            if listing.is_moderated:
                # **운영자가 내린 것을 판매자가 다시 올릴 수 없다.**
                # 조용히 성공시키면 판매자는 올라간 줄 알고, 실제로는 목록에 없다.
                raise ModeratedListing(listing_id)

            if listing.status is ListingStatus.PUBLISHED:
                # 이미 올라가 있다. 재시도 · 연타는 오류가 아니다.
                return PublishResult(
                    listing=listing, published=False, fee_charged=False,
                    fee_shards=fee, balance=shards.wallet(seller_user_id).balance,
                )

            # 내용물이 서버에 있는지 확인한다 — client가 준 문자열만 믿지 않는다.
            snapshot = self.snapshot(listing.snapshot_id, seller_user_id)

            # 조치된 원본을 지우고 새 listing으로 다시 올리는 우회를 막는다.
            if snapshot.source_content_id and moderation_block_id(
                seller_user_id, listing.content_type.value, snapshot.source_content_id
            ) in self.moderation_blocks:
                raise ModeratedListing(listing_id)

            # **이름을 먼저 잡는다.** 조각을 빼기 전에 확인해야, 이름이 겹쳐
            # 실패했을 때 사용자가 등록비만 잃는 일이 없다.
            key = listing_title_key(listing.title)
            owner = self.title_claims.get(key)
            if owner is not None and owner != listing.id:
                raise TitleTaken(listing.title)

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

                published = replace(
                    listing,
                    status=ListingStatus.PUBLISHED,
                    publish_fee_paid=True,
                    updated_at=now,
                    # **최초 게시 시각을 유지한다.** republish가 덮어쓰지 않는다.
                    published_at=listing.published_at or now,
                )
                def write_listing():
                    previous = self.listings.get(listing.id)
                    self.listings[listing.id] = published
                    return lambda: self.listings.__setitem__(listing.id, previous)

                def claim_title():
                    self.title_claims[key] = listing.id
                    return lambda: self.title_claims.pop(key, None)

                tx.add(write_listing)
                tx.add(claim_title)

            return PublishResult(
                listing=published, published=True, fee_charged=charged,
                fee_shards=fee, balance=balance,
            )

    def unpublish(self, listing_id: str, seller_user_id: str) -> Listing:
        with self._lock:
            listing = self.listing(listing_id, seller_user_id)
            if listing.status is not ListingStatus.PUBLISHED:
                return listing   # 이미 내려가 있거나 아직 안 올렸다. 조용히 끝낸다.

            # 나머지는 **그대로 둔다** — 내렸다고 낸 돈도 counter도 돌아오지 않는다.
            unlisted = replace(listing, status=ListingStatus.UNLISTED, updated_at=utcnow())
            self.listings[listing.id] = unlisted
            # 상점에서 내려갔으므로 이름도 놓아 준다. 다시 올릴 때 다시 잡는다.
            self._release_title(listing)
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
                # **판매 알림도 같은 commit이다.** 밖에서 쓰면 "돈은 오갔는데
                # 판매자는 모르는" 상태가 생기고, 그것을 나중에 메울 방법이 없다.
                sale = _sale_event(listing, key, price)

                def write_notification():
                    if self._notifications is None:
                        return lambda: None
                    self._notifications.create(sale)
                    return lambda: None

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
                tx.add(write_notification)

            return PurchaseResult(
                ownership=ownership, purchased=True, already_owned=False,
                price_paid=price, balance=balance,
                download_count=counted.download_count,
                sale_event=sale,
            )

    def ownerships(self, user_id: str) -> list[Ownership]:
        return [x for x in self.ownership_records.values() if x.user_id == user_id]

    # MARK: - 내부

    @staticmethod
    def _with_download_count(listing: Listing, count: int) -> Listing:
        # **field를 하나씩 옮겨 적지 않는다.** 그렇게 쓰면 나중에 늘어난 field가
        # 조용히 사라진다 — 운영 조치가 구매 한 번으로 풀리는 식이다.
        return replace(listing, download_count=count)

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

    # MARK: - 운영 조치 (Phase E)

    def takedown(
        self, listing_id: str, actor_user_id: str,
        reason: ModerationReason, note: str, shards=None,
    ) -> ModerationResult:
        with self._lock:
            listing = self.any_listing(listing_id)
            if listing.is_moderated:
                # 이미 내려가 있다. **기록을 남기지 않는다** — 연타가 감사 기록을 채우면
                # 진짜 조치가 언제였는지 알 수 없게 된다. 보상도 여기서 끝난다.
                return ModerationResult(listing=listing, changed=False)

            now = utcnow()
            removed = replace(
                listing,
                moderation_status=ModerationStatus.REMOVED,
                moderation_reason=reason,
                moderated_at=now,
                moderated_by=actor_user_id,
                updated_at=now,
            )
            with self._shard_store.transaction() as tx:
                paid = self._compensate(tx, removed, shards)
                self.listings[listing_id] = removed
                self._block_source(removed)
                self._record(removed, ModerationAction.TAKEDOWN, actor_user_id, reason, note, now)
                # **판매자가 이유를 알아야 한다.** 상태만 바꾸고 말하지 않으면
                # 판매자는 자기 상품이 왜 사라졌는지 알 방법이 없다.
                if self._notifications is not None:
                    self._notifications.create(_takedown_event(removed, reason, paid))
            return ModerationResult(listing=removed, changed=True, compensation=paid)

    def _compensate(self, tx, listing: Listing, shards) -> int:
        """판매자에게 보상 조각을 얹는다. **열쇠는 listing id 하나다.**

        `changed=False`가 이미 연타를 막지만, 그것만으로는 부족하다 —
        운영자가 내렸다 되살렸다를 반복하면 그때마다 새 조치이고, 열쇠가 조치마다
        다르면 조각이 그만큼 발행된다. **한 상품이 내려간 것에 대한 보상은 한 번**이라
        정하면 그 통로가 아예 없다. 조치 기록(`moderation_event_log`)은 그대로
        전부 남으므로 몇 번 내려갔는지는 여전히 알 수 있다.
        """
        if shards is None or MarketplacePublishPolicy.MODERATION_COMPENSATION <= 0:
            return 0
        result = shards.apply_in_transaction(
            shards.context(tx),
            listing.seller_user_id,
            MarketplacePublishPolicy.MODERATION_COMPENSATION,
            ShardReason.MARKETPLACE_MODERATION_COMPENSATION,
            listing.id,
        )
        return MarketplacePublishPolicy.MODERATION_COMPENSATION if result.applied else 0

    def restore(self, listing_id: str, actor_user_id: str) -> ModerationResult:
        with self._lock:
            listing = self.any_listing(listing_id)
            if listing.status.is_terminal:
                # 판매자가 삭제했다. **운영자 복구가 사용자의 삭제를 뒤집지 않는다.**
                raise TerminalListing(listing_id)
            if not listing.is_moderated:
                raise NotModerated(listing_id)

            now = utcnow()
            restored = replace(
                listing,
                moderation_status=ModerationStatus.ACTIVE,
                moderation_reason=None,
                moderated_at=now,
                moderated_by=actor_user_id,
                updated_at=now,
            )
            self.listings[listing_id] = restored
            self._unblock_source(restored)
            self._record(restored, ModerationAction.RESTORE, actor_user_id, None, "", now)
            return ModerationResult(listing=restored, changed=True)

    def list_for_admin(self, cursor: str | None, limit: int) -> tuple[list[Listing], str | None]:
        with self._lock:
            ordered = sorted(
                self.listings.values(), key=lambda x: (x.created_at, x.id), reverse=True
            )
        if cursor is not None:
            ids = [x.id for x in ordered]
            start = ids.index(cursor) + 1 if cursor in ids else len(ids)
            ordered = ordered[start:]
        page = ordered[:limit]
        # 다음 자리가 남아 있을 때만 cursor를 준다 — 빈 page를 한 번 더 받지 않는다.
        return page, (page[-1].id if page and len(ordered) > limit else None)

    def moderation_events(self, listing_id: str) -> list[ModerationEvent]:
        return [x for x in self.moderation_event_log if x.listing_id == listing_id]

    def is_source_blocked(self, seller_user_id: str, listing: Listing, source_content_id: str) -> bool:
        if not source_content_id:
            return False
        key = moderation_block_id(seller_user_id, listing.content_type.value, source_content_id)
        return key in self.moderation_blocks

    def _block_source(self, listing: Listing) -> None:
        source = self.snapshots.get(listing.snapshot_id)
        if source is None or not source.source_content_id:
            return   # 옛 snapshot이라 원본을 모른다. 거짓 열쇠를 만들지 않는다.
        key = moderation_block_id(
            listing.seller_user_id, listing.content_type.value, source.source_content_id
        )
        self.moderation_blocks[key] = listing.id

    def _unblock_source(self, listing: Listing) -> None:
        source = self.snapshots.get(listing.snapshot_id)
        if source is None or not source.source_content_id:
            return
        key = moderation_block_id(
            listing.seller_user_id, listing.content_type.value, source.source_content_id
        )
        # **다른 listing이 만든 차단은 풀지 않는다** — 같은 원본에서 두 번 조치했다면
        # 하나를 복구했다고 나머지 조치까지 사라지면 안 된다.
        if self.moderation_blocks.get(key) == listing.id:
            del self.moderation_blocks[key]

    def _record(self, listing, action, actor_user_id, reason, note, now) -> None:
        self.moderation_event_log.append(
            ModerationEvent(
                id=ModerationEvent.new_id(),
                listing_id=listing.id,
                content_type=listing.content_type,
                action=action,
                actor_user_id=actor_user_id,
                reason=reason,
                note=note,
                created_at=now,
            )
        )


def _sale_event(listing: Listing, ownership_key: str, price: int) -> NotificationEvent:
    """판매 알림 하나. **문서 자리가 소유권에서 나오므로 두 번 생기지 않는다.**

    제목은 **팔린 그때의 값**을 복사한다 — 나중에 판매자가 제목을 바꿔도 기록은
    그때 팔린 것을 가리켜야 한다. 구매자는 담지 않는다.
    """
    return NotificationEvent(
        id=sale_event_id(ownership_key),
        user_id=listing.seller_user_id,
        type=NotificationType.MARKETPLACE_SALE,
        listing_id=listing.id,
        content_type=listing.content_type.value,
        title_snapshot=listing.title,
        shard_amount=price,
    )


#: 사용자에게 보여 줄 조치 사유. **운영자 내부 메모(`note`)는 나가지 않는다.**
#:
#: 모든 사유를 "부적절한 내용" 하나로 덮지 않는다 — 판매자가 무엇을 고쳐야 하는지
#: 알 수 없게 된다. 서버가 아는 사유를 그대로 말한다.
TAKEDOWN_REASON_LABELS: dict[ModerationReason, str] = {
    ModerationReason.INAPPROPRIATE: "부적절한 내용",
    ModerationReason.SPAM: "스팸/도배",
    ModerationReason.COPYRIGHT: "권리 침해",
    ModerationReason.OTHER: "상점 운영 정책",
}


def _takedown_event(
    listing: Listing, reason: ModerationReason, compensation: int = 0
) -> NotificationEvent:
    """조치 알림 하나. **판매자에게만** 간다.

    제목은 **내려간 그때의 값**을 복사한다(판매 알림과 같은 규칙) — 나중에 제목이
    바뀌어도 기록은 그때 내려간 것을 가리켜야 한다.

    조각 이야기는 **실제로 지급됐을 때만** 적는다(`compensation > 0`). 지급되지
    않았는데 "지급됐어요"라고 하면 지갑을 열어 본 판매자가 우리를 못 믿게 된다.
    """
    noun = "스티커" if listing.content_type is ContentType.STICKER else "거울"
    label = TAKEDOWN_REASON_LABELS.get(reason)
    body = f"상점 운영 정책에 따라 등록한 {noun}이 내려갔어요."
    if label:
        body = f"{body}\n사유: {label}"
    if compensation > 0:
        body = f"{body}\n{compensation}조각이 지급됐어요."
    return NotificationEvent(
        id=takedown_event_id(listing.id),
        user_id=listing.seller_user_id,
        type=NotificationType.MARKETPLACE_TAKEDOWN,
        listing_id=listing.id,
        content_type=listing.content_type.value,
        title_snapshot=listing.title,
        headline="등록한 상품이 상점에서 내려갔어요",
        body=body,
    )


def _checked_like_count(listing: Listing) -> int:
    """음수 projection을 **조용히 0으로 만들지 않는다.**"""
    if listing.like_count < 0:
        raise LikeCountInconsistent(listing.id)
    return listing.like_count


def _with_like_count(listing: Listing, count: int) -> Listing:
    """`_with_download_count`와 같은 이유로 `replace`다."""
    return replace(listing, like_count=count)
