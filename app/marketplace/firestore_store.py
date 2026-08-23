"""Firestore 구현.

**핵심은 transaction 하나다.** 등록비 차감(원장 + 지갑)과 listing 상태 변경이
전부 성공하거나 전부 실패한다 — "수수료만 나가고 게시 실패"가 생길 수 없다.

조각 쪽은 B-7B의 `apply_in_transaction`을 쓴다. 그것은 transaction을 열지도
commit하지도 않으므로, **여기서 연 transaction에 그대로 얹힌다.**
"""

from __future__ import annotations

import logging

from google.api_core import exceptions as gcp_exceptions
from dataclasses import replace

from google.cloud import firestore

from app.auth.store import StoreUnavailable
from app.marketplace.models import (
    InvalidTransition,
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
from app.marketplace.store import LIKES, LISTINGS, OWNERSHIP, SNAPSHOTS, _is_public
from app.shards.models import utcnow
from app.shards.service import ShardLedgerService

logger = logging.getLogger(__name__)


class FirestoreMarketplaceStore:
    def __init__(self, client: firestore.Client) -> None:
        self._db = client

    # MARK: - 읽기

    def create(self, listing: Listing) -> Listing:
        try:
            self._db.collection(LISTINGS).document(listing.id).create(_document(listing))
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("listing_create", error) from error
        return listing

    def listing(self, listing_id: str, seller_user_id: str) -> Listing:
        try:
            snapshot = self._db.collection(LISTINGS).document(listing_id).get()
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("listing_read", error) from error
        return _owned_listing(snapshot, listing_id, seller_user_id)

    def snapshot(self, snapshot_id: str, seller_user_id: str) -> Snapshot:
        try:
            found = self._db.collection(SNAPSHOTS).document(snapshot_id).get()
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("snapshot_read", error) from error
        return _owned_snapshot(found, snapshot_id, seller_user_id)

    def list_published(self) -> list[Listing]:
        """`status == published` **하나로만** 질의한다.

        종류 필터와 정렬을 application에서 하는 이유: 정렬 셋(`publishedAt` ·
        `downloadCount` · `likeCount`)마다 composite index를 만들면 index 세 개를
        지금 production에 요구하게 된다. 초기 상품 수가 작고 pagination도 없으므로
        **index 없이 시작한다.** 규모가 커지면 그때 index와 pagination을 함께 넣는다.

        목록 한 건마다 snapshot을 추가 조회하지 않는다(N+1 금지) —
        공개에 필요한 값은 listing 문서에 이미 다 있다.
        """
        try:
            found = (
                self._db.collection(LISTINGS)
                .where(filter=firestore.FieldFilter("status", "==", ListingStatus.PUBLISHED.value))
                .stream()
            )
            listings = [_listing_from(x.id, x.to_dict() or {}) for x in found]
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("listing_list", error) from error

        skipped = [x for x in listings if not _is_public(x)]
        if skipped:
            # 있을 수 없는 상태다 — 거짓 날짜를 지어내지 않고 조용히 빼고 크게 남긴다.
            logger.error("marketplace_listing_malformed count=%d", len(skipped))
        return [x for x in listings if _is_public(x)]

    def delete(self, listing_id: str, seller_user_id: str) -> Listing:
        """`deleted`로 표시만 한다. **아무것도 실제로 지우지 않는다.**

        snapshot · GCS object · 소유권 · 원장을 건드리지 않는다 — 이미 산 사람이
        계속 받아야 한다. 등록비도 돌려주지 않는다(경제 mutation 0).
        """
        listing_ref = self._db.collection(LISTINGS).document(listing_id)

        @firestore.transactional
        def run(transaction) -> Listing:
            listing = _owned_listing(
                listing_ref.get(transaction=transaction), listing_id, seller_user_id
            )
            if listing.status is ListingStatus.DELETED:
                # 이미 삭제됐다. 다시 써도 같은 결과다(멱등).
                return listing

            now = utcnow()
            transaction.update(
                listing_ref, {"status": ListingStatus.DELETED.value, "updatedAt": now}
            )
            return replace(listing, status=ListingStatus.DELETED, updated_at=now)

        try:
            return run(self._db.transaction())
        except (ListingNotFound, InvalidTransition):
            raise
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("listing_delete", error) from error

    def list_for_seller(self, seller_user_id: str) -> list[Listing]:
        """`sellerUserId == seller_user_id` **하나로만** 질의한다.

        정렬을 query에 넣지 않는다 — `where` + `order_by`는 composite index를
        요구하고, 판매자 한 명의 상품 수는 application에서 정렬해도 되는 규모다
        (`list_published`와 같은 판단이다).

        여기서는 malformed 문서를 **빼지 않는다.** 공개 목록은 거짓 정보를 보여
        주지 않으려고 걸러내지만, 판매자에게는 자기 상품이 이상한 상태라는 것
        자체가 보여야 한다 — 안 보이면 내릴 수도 없다.
        """
        try:
            found = (
                self._db.collection(LISTINGS)
                .where(filter=firestore.FieldFilter("sellerUserId", "==", seller_user_id))
                .stream()
            )
            return [_listing_from(x.id, x.to_dict() or {}) for x in found]
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("listing_seller_list", error) from error

    def get_published(self, listing_id: str) -> Listing:
        try:
            snapshot = self._db.collection(LISTINGS).document(listing_id).get()
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("listing_public_read", error) from error
        if not snapshot.exists:
            raise ListingNotFound(listing_id)
        listing = _listing_from(listing_id, snapshot.to_dict() or {})
        if not _is_public(listing):
            # 판매자 자신이라도 공개 endpoint로는 볼 수 없다.
            raise ListingNotFound(listing_id)
        return listing

    # MARK: - 최초 게시 (한 transaction)

    def publish(
        self, listing_id: str, seller_user_id: str, shards: ShardLedgerService
    ) -> PublishResult:
        listing_ref = self._db.collection(LISTINGS).document(listing_id)

        @firestore.transactional
        def run(transaction) -> PublishResult:
            # ⚠️ **context는 attempt마다 새로 만든다**(B-7B.1).
            # callable 밖에서 만들면 ABORTED 재시도가 이전 시도의 기록을 물려받는다.
            scoped = shards.context(transaction)

            # 읽기는 전부 쓰기보다 먼저. Firestore transaction의 규칙이다.
            listing = _owned_listing(
                listing_ref.get(transaction=transaction), listing_id, seller_user_id
            )
            fee = MarketplacePublishPolicy.fee(listing.content_type)

            if listing.status.is_terminal:

                # **삭제는 끝 상태다.** 되살아나지 않는다.

                raise InvalidTransition(listing.status.value)


            if listing.status is ListingStatus.PUBLISHED:
                # 이미 올라가 있다. **아무것도 쓰지 않는다** — 재시도·연타가 오류가 아니다.
                wallet = shards.wallet(seller_user_id)
                return PublishResult(
                    listing=listing, published=False, fee_charged=False,
                    fee_shards=fee, balance=wallet.balance,
                )

            snapshot_ref = self._db.collection(SNAPSHOTS).document(listing.snapshot_id)
            _owned_snapshot(
                snapshot_ref.get(transaction=transaction), listing.snapshot_id, seller_user_id
            )

            balance = shards.wallet(seller_user_id).balance
            charged = False
            if not listing.publish_fee_paid:
                # 잔액이 모자라면 여기서 `InsufficientShards`가 나가고
                # **listing 상태도 바뀌지 않는다** — 같은 transaction이기 때문이다.
                result = shards.apply_in_transaction(
                    scoped,
                    seller_user_id,
                    -fee,
                    MarketplacePublishPolicy.reason(listing.content_type),
                    listing.id,
                )
                balance = result.wallet.balance
                charged = result.applied

            now = utcnow()
            transaction.update(
                listing_ref,
                {
                    "status": ListingStatus.PUBLISHED.value,
                    "publishFeePaid": True,
                    # **최초 게시 시각을 유지한다.** republish가 덮어쓰지 않는다.
                    "publishedAt": listing.published_at or now,
                    "updatedAt": now,
                },
            )
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
                published_at=listing.published_at or now,
            )
            return PublishResult(
                listing=published, published=True, fee_charged=charged,
                fee_shards=fee, balance=balance,
            )

        try:
            return run(shards.transaction())
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("listing_publish", error) from error

    def unpublish(self, listing_id: str, seller_user_id: str) -> Listing:
        listing_ref = self._db.collection(LISTINGS).document(listing_id)

        @firestore.transactional
        def run(transaction) -> Listing:
            listing = _owned_listing(
                listing_ref.get(transaction=transaction), listing_id, seller_user_id
            )
            if listing.status is not ListingStatus.PUBLISHED:
                return listing

            now = utcnow()
            # **경제를 건드리지 않는다.** 상태와 시각만 바뀐다.
            transaction.update(
                listing_ref, {"status": ListingStatus.UNLISTED.value, "updatedAt": now}
            )
            return Listing(
                id=listing.id,
                seller_user_id=listing.seller_user_id,
                content_type=listing.content_type,
                title=listing.title,
                description=listing.description,
                price_shards=listing.price_shards,
                snapshot_id=listing.snapshot_id,
                status=ListingStatus.UNLISTED,
                publish_fee_paid=listing.publish_fee_paid,
                download_count=listing.download_count,
                like_count=listing.like_count,
                created_at=listing.created_at,
                updated_at=now,
                published_at=listing.published_at,
            )

        try:
            return run(self._db.transaction())
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("listing_unpublish", error) from error

    # MARK: - 획득 (한 transaction)

    def acquire(self, listing_id: str, buyer_user_id: str, shards) -> PurchaseResult:
        """구매자 차감 · 판매자 지급 · 소유권 · counter가 **하나의 commit**이다."""
        listing_ref = self._db.collection(LISTINGS).document(listing_id)
        key = ownership_id(buyer_user_id, listing_id)
        ownership_ref = self._db.collection(OWNERSHIP).document(key)

        @firestore.transactional
        def run(transaction) -> PurchaseResult:
            # ⚠️ context는 attempt마다 새로 만든다(B-7B.1).
            scoped = shards.context(transaction)

            # **marketplace 읽기를 전부 먼저 한다.** 그 뒤 조각 primitive가 자기
            # 문서(원장 · 지갑)를 읽고 쓴다 — 서로 다른 문서라 순서가 섞여도 안전하고,
            # 여기서 읽은 값 이후에 marketplace 문서를 다시 읽지 않는다.
            listing_snapshot = listing_ref.get(transaction=transaction)
            owned_snapshot = ownership_ref.get(transaction=transaction)

            if not listing_snapshot.exists:
                raise ListingNotFound(listing_id)
            listing = _listing_from(listing_id, listing_snapshot.to_dict() or {})
            if not _is_public(listing):
                # 내려간 상품은 살 수 없다 — 이미 산 사람의 권리는 그대로다.
                raise ListingNotFound(listing_id)

            if owned_snapshot.exists:
                # 이미 갖고 있다. **아무것도 쓰지 않는다** — counter도 올리지 않는다.
                owned = _ownership_from(key, owned_snapshot.to_dict() or {})
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
            buyer_entry = seller_entry = None

            if price > 0:
                # 잔액이 모자라면 여기서 끝나고 **소유권도 counter도 바뀌지 않는다.**
                #
                # ⚠️ 원장 event id는 **소유권 id**(= `(구매자, 상품)` hash)다.
                # `listingId`를 쓰면 판매자 쪽 열쇠가 `(판매자, sale, listingId)`가 되어
                # **구매자가 달라도 같은 문서를 겨룬다** — 8명이 사면 판매자가 한 번만
                # 받는다. 동시성 test가 실제로 그것을 잡았다.
                # 구매자 쪽은 어차피 구매자별로 다르지만 **양쪽을 같은 열쇠로** 둬서
                # 한 거래의 두 줄이 같은 사건에서 나왔다는 것이 드러나게 한다.
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
            # `create`다 — 우리가 읽은 뒤 다른 요청이 먼저 자리를 잡았으면 commit이 깨진다.
            transaction.create(ownership_ref, _ownership_document(ownership))
            # counter는 **읽은 값 + 1**이다. listing을 transaction에서 읽었으므로
            # 동시 구매는 충돌로 재시도되고, 그래서 정확히 직렬화된다.
            count = listing.download_count + 1
            transaction.update(listing_ref, {"downloadCount": count})

            return PurchaseResult(
                ownership=ownership, purchased=True, already_owned=False,
                price_paid=price, balance=balance, download_count=count,
            )

        try:
            return run(shards.transaction())
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("listing_acquire", error) from error

    def ownerships(self, user_id: str) -> list[Ownership]:
        try:
            found = (
                self._db.collection(OWNERSHIP)
                .where(filter=firestore.FieldFilter("userId", "==", user_id))
                .stream()
            )
            return [_ownership_from(x.id, x.to_dict() or {}) for x in found]
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("ownership_list", error) from error

    # MARK: - 좋아요 (한 transaction · 조각 경제 무관)

    def like(self, listing_id: str, user_id: str) -> LikeResult:
        """관계 생성과 `likeCount +1`이 **하나의 commit**이다.

        `ShardLedgerService`를 부르지 않는다 — 좋아요는 조각을 주거나 받지 않는다.
        """
        listing_ref = self._db.collection(LISTINGS).document(listing_id)
        key = like_id(user_id, listing_id)
        like_ref = self._db.collection(LIKES).document(key)

        @firestore.transactional
        def run(transaction) -> LikeResult:
            # 읽기를 먼저. **attempt마다 다시 읽는다** — 재시도가 stale count를 쓰지 않는다.
            listing_snapshot = listing_ref.get(transaction=transaction)
            like_snapshot = like_ref.get(transaction=transaction)

            if not listing_snapshot.exists:
                raise ListingNotFound(listing_id)
            listing = _listing_from(listing_id, listing_snapshot.to_dict() or {})
            if not _is_public(listing):
                # 새 좋아요는 공개 상품에만. draft는 애초에 대상이 아니다.
                raise ListingNotFound(listing_id)
            count = _checked_count(listing)

            if like_snapshot.exists:
                # 이미 눌렀다. **아무것도 쓰지 않는다.**
                return LikeResult(
                    listing_id=listing_id, liked=True, changed=False, like_count=count
                )

            if listing.seller_user_id == user_id:
                raise SelfLike(listing_id)

            like = Like(id=key, user_id=user_id, listing_id=listing_id)
            # `create`라 우리가 읽은 뒤 다른 요청이 먼저 자리를 잡았으면 commit이 깨진다.
            transaction.create(like_ref, _like_document(like))
            # listing을 transaction에서 읽었으므로 동시 좋아요는 충돌로 재시도된다.
            transaction.update(listing_ref, {"likeCount": count + 1})
            return LikeResult(
                listing_id=listing_id, liked=True, changed=True, like_count=count + 1
            )

        try:
            return run(self._db.transaction())
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("listing_like", error) from error

    def unlike(self, listing_id: str, user_id: str) -> LikeResult:
        """관계 삭제와 `likeCount -1`이 **하나의 commit**이다.

        `unlisted`여도 취소할 수 있다 — 판매자가 내렸다고 사용자가 자기 좋아요를
        못 지우면 count가 영구히 남는다. 자기 상품 좋아요도 지울 수 있다.
        """
        listing_ref = self._db.collection(LISTINGS).document(listing_id)
        key = like_id(user_id, listing_id)
        like_ref = self._db.collection(LIKES).document(key)

        @firestore.transactional
        def run(transaction) -> LikeResult:
            listing_snapshot = listing_ref.get(transaction=transaction)
            like_snapshot = like_ref.get(transaction=transaction)

            if not listing_snapshot.exists:
                raise ListingNotFound(listing_id)
            listing = _listing_from(listing_id, listing_snapshot.to_dict() or {})
            if listing.status is ListingStatus.DRAFT:
                # draft에 좋아요가 달릴 경로가 없다 — 있으면 malformed다.
                raise ListingNotFound(listing_id)
            count = _checked_count(listing)

            if not like_snapshot.exists:
                return LikeResult(
                    listing_id=listing_id, liked=False, changed=False, like_count=count
                )

            if count == 0:
                # 관계는 있는데 projection이 0이다. **거짓 보정 대신 멈춘다.**
                raise LikeCountInconsistent(listing_id)

            transaction.delete(like_ref)
            transaction.update(listing_ref, {"likeCount": count - 1})
            return LikeResult(
                listing_id=listing_id, liked=False, changed=True, like_count=count - 1
            )

        try:
            return run(self._db.transaction())
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("listing_unlike", error) from error

    def likes(self, user_id: str) -> list[Like]:
        try:
            found = (
                self._db.collection(LIKES)
                .where(filter=firestore.FieldFilter("userId", "==", user_id))
                .stream()
            )
            return [_like_from(x.id, x.to_dict() or {}) for x in found]
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("like_list", error) from error

    # MARK: - snapshot / 전달 (B-7F)

    def create_snapshot(self, snapshot: Snapshot) -> Snapshot:
        """**asset이 전부 올라간 뒤에만** 부른다. `create`라 같은 자리를 덮어쓰지 않는다."""
        try:
            self._db.collection(SNAPSHOTS).document(snapshot.id).create(
                _snapshot_document(snapshot)
            )
        except gcp_exceptions.AlreadyExists as error:
            raise SnapshotNotFound(snapshot.id) from error
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("snapshot_create", error) from error
        return snapshot

    def snapshot_for_delivery(self, snapshot_id: str) -> Snapshot:
        try:
            found = self._db.collection(SNAPSHOTS).document(snapshot_id).get()
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("snapshot_delivery_read", error) from error
        if not found.exists:
            raise SnapshotNotFound(snapshot_id)
        return _snapshot_from(snapshot_id, found.to_dict() or {})

    def ownership(self, listing_id: str, user_id: str) -> Ownership | None:
        key = ownership_id(user_id, listing_id)
        try:
            found = self._db.collection(OWNERSHIP).document(key).get()
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("ownership_read", error) from error
        return _ownership_from(key, found.to_dict() or {}) if found.exists else None

    def any_listing(self, listing_id: str) -> Listing:
        """상태·주인과 무관하게. **템플릿 전달에만** 쓴다."""
        try:
            found = self._db.collection(LISTINGS).document(listing_id).get()
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("listing_any_read", error) from error
        if not found.exists:
            raise ListingNotFound(listing_id)
        return _listing_from(listing_id, found.to_dict() or {})

    # MARK: - 내부

    def _unavailable(self, operation: str, error: Exception) -> StoreUnavailable:
        logger.warning("firestore_failed operation=%s error=%s", operation, type(error).__name__)
        return StoreUnavailable(operation)


# MARK: - 문서 변환


def _document(listing: Listing) -> dict:
    return {
        "sellerUserId": listing.seller_user_id,
        "contentType": listing.content_type.value,
        "title": listing.title,
        "description": listing.description,
        "priceShards": listing.price_shards,
        "snapshotId": listing.snapshot_id,
        "status": listing.status.value,
        "publishFeePaid": listing.publish_fee_paid,
        "downloadCount": listing.download_count,
        "likeCount": listing.like_count,
        "createdAt": listing.created_at,
        "updatedAt": listing.updated_at,
        "publishedAt": listing.published_at,
        "schemaVersion": listing.schema_version,
    }


def _owned_listing(snapshot, listing_id: str, seller_user_id: str) -> Listing:
    """**남의 것과 없는 것을 구분해 알려주지 않는다** — 존재 사실도 정보다."""
    if not snapshot.exists:
        raise ListingNotFound(listing_id)
    data = snapshot.to_dict() or {}
    if data.get("sellerUserId") != seller_user_id:
        raise ListingNotFound(listing_id)
    return _listing_from(listing_id, data)


def _listing_from(listing_id: str, data: dict) -> Listing:
    return Listing(
        id=listing_id,
        seller_user_id=str(data.get("sellerUserId") or ""),
        content_type=ContentType(data.get("contentType") or ContentType.MIRROR.value),
        title=str(data.get("title") or ""),
        description=str(data.get("description") or ""),
        price_shards=int(data.get("priceShards") or 0),
        snapshot_id=str(data.get("snapshotId") or ""),
        status=ListingStatus(data.get("status") or ListingStatus.DRAFT.value),
        publish_fee_paid=bool(data.get("publishFeePaid")),
        download_count=int(data.get("downloadCount") or 0),
        like_count=int(data.get("likeCount") or 0),
        created_at=data.get("createdAt") or utcnow(),
        updated_at=data.get("updatedAt") or utcnow(),
        published_at=data.get("publishedAt"),
        schema_version=int(data.get("schemaVersion") or 1),
    )


def _owned_snapshot(found, snapshot_id: str, seller_user_id: str) -> Snapshot:
    if not found.exists:
        raise SnapshotNotFound(snapshot_id)
    data = found.to_dict() or {}
    if data.get("sellerUserId") != seller_user_id:
        raise SnapshotNotFound(snapshot_id)
    return _snapshot_from(snapshot_id, data)


def _snapshot_document(snapshot: Snapshot) -> dict:
    return {
        "sellerUserId": snapshot.seller_user_id,
        "contentType": snapshot.content_type.value,
        "manifestChecksum": snapshot.manifest_checksum,
        "assetCount": snapshot.asset_count,
        "totalBytes": snapshot.total_bytes,
        # 판매자가 "내 거울 → 판매 중"에서 자기 상품을 찾는 연결.
        # **공개 응답에는 넣지 않는다.**
        "sourceContentId": snapshot.source_content_id,
        "createdAt": snapshot.created_at,
        "schemaVersion": snapshot.schema_version,
    }


def _snapshot_from(snapshot_id: str, data: dict) -> Snapshot:
    return Snapshot(
        id=snapshot_id,
        seller_user_id=str(data.get("sellerUserId") or ""),
        content_type=ContentType(data.get("contentType") or ContentType.MIRROR.value),
        manifest_checksum=str(data.get("manifestChecksum") or ""),
        asset_count=int(data.get("assetCount") or 0),
        total_bytes=int(data.get("totalBytes") or 0),
        # 옛 문서에는 없다. 그때는 저장된 manifest에서 읽는다(service의 fallback).
        source_content_id=str(data.get("sourceContentId") or ""),
        created_at=data.get("createdAt") or utcnow(),
        schema_version=int(data.get("schemaVersion") or 1),
    )


def _ownership_document(ownership: Ownership) -> dict:
    return {
        "userId": ownership.user_id,
        "listingId": ownership.listing_id,
        "sellerUserId": ownership.seller_user_id,
        "snapshotId": ownership.snapshot_id,
        "pricePaid": ownership.price_paid,
        "buyerLedgerEntryId": ownership.buyer_ledger_entry_id,
        "sellerLedgerEntryId": ownership.seller_ledger_entry_id,
        "createdAt": ownership.created_at,
        "schemaVersion": ownership.schema_version,
    }


def _ownership_from(ownership_id_value: str, data: dict) -> Ownership:
    return Ownership(
        id=ownership_id_value,
        user_id=str(data.get("userId") or ""),
        listing_id=str(data.get("listingId") or ""),
        seller_user_id=str(data.get("sellerUserId") or ""),
        snapshot_id=str(data.get("snapshotId") or ""),
        price_paid=int(data.get("pricePaid") or 0),
        buyer_ledger_entry_id=data.get("buyerLedgerEntryId"),
        seller_ledger_entry_id=data.get("sellerLedgerEntryId"),
        created_at=data.get("createdAt") or utcnow(),
        schema_version=int(data.get("schemaVersion") or 1),
    )


def _checked_count(listing: Listing) -> int:
    """음수 projection을 **조용히 0으로 만들지 않는다.**"""
    if listing.like_count < 0:
        raise LikeCountInconsistent(listing.id)
    return listing.like_count


def _like_document(like: Like) -> dict:
    return {
        "userId": like.user_id,
        "listingId": like.listing_id,
        "createdAt": like.created_at,
        "schemaVersion": like.schema_version,
    }


def _like_from(like_id_value: str, data: dict) -> Like:
    return Like(
        id=like_id_value,
        user_id=str(data.get("userId") or ""),
        listing_id=str(data.get("listingId") or ""),
        created_at=data.get("createdAt") or utcnow(),
        schema_version=int(data.get("schemaVersion") or 1),
    )
