"""Firestore 구현.

**핵심은 transaction 하나다.** 등록비 차감(원장 + 지갑)과 listing 상태 변경이
전부 성공하거나 전부 실패한다 — "수수료만 나가고 게시 실패"가 생길 수 없다.

조각 쪽은 B-7B의 `apply_in_transaction`을 쓴다. 그것은 transaction을 열지도
commit하지도 않으므로, **여기서 연 transaction에 그대로 얹힌다.**
"""

from __future__ import annotations

import logging

from google.api_core import exceptions as gcp_exceptions
from google.cloud import firestore

from app.auth.store import StoreUnavailable
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
from app.marketplace.store import LISTINGS, SNAPSHOTS
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
    return Listing(
        id=listing_id,
        seller_user_id=seller_user_id,
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
    return Snapshot(
        id=snapshot_id,
        seller_user_id=seller_user_id,
        content_type=ContentType(data.get("contentType") or ContentType.MIRROR.value),
        created_at=data.get("createdAt") or utcnow(),
    )
