"""Marketplace listing + 최초 등록비 (B-7C).

가장 중요한 것 하나: **등록비 차감과 게시가 같은 commit인가.**
"수수료만 나가고 게시 실패"가 생기면 사용자가 조각을 잃는다.

나머지는 그 주변이다 — 서버가 비용의 authority인가, 재시도·연타·동시 요청에서
정확히 한 번인가, 내렸다 다시 올릴 때 또 받지 않는가.
"""

from __future__ import annotations

import threading

import pytest

from app.auth.models import User
from app.marketplace.models import (
    ContentType,
    InvalidListing,
    Listing,
    ListingNotFound,
    ListingStatus,
    MarketplacePublishPolicy,
    Snapshot,
    SnapshotNotFound,
)
from app.marketplace.service import MarketplaceService
from app.marketplace.store import InMemoryMarketplaceStore
from app.shards.models import InsufficientShards, ShardReason
from app.shards.service import ShardLedgerService
from app.shards.store import InMemoryShardStore

SELLER = "11111111-2222-4333-8444-555555555555"
OTHER = "99999999-8888-4777-8666-555555555555"


@pytest.fixture
def shard_store() -> InMemoryShardStore:
    return InMemoryShardStore()


@pytest.fixture
def shards(shard_store) -> ShardLedgerService:
    return ShardLedgerService(shard_store)


@pytest.fixture
def store(shard_store) -> InMemoryMarketplaceStore:
    return InMemoryMarketplaceStore(shard_store)


@pytest.fixture
def service(store, shards) -> MarketplaceService:
    return MarketplaceService(store, shards)


def user(user_id: str = SELLER) -> User:
    return User(id=user_id)


def seed(shards: ShardLedgerService, amount: int, who: str = SELLER) -> None:
    if amount:
        shards.credit(who, amount, ShardReason.ADMIN_ADJUSTMENT, external_event_id=f"seed:{who}")


def snapshot(store, kind: ContentType = ContentType.MIRROR, owner: str = SELLER) -> str:
    found = Snapshot(id=f"snap-{kind.value}-{owner[:4]}", seller_user_id=owner, content_type=kind)
    store.snapshots[found.id] = found
    return found.id


def draft(
    service: MarketplaceService,
    store,
    kind: ContentType = ContentType.MIRROR,
    price: int = 0,
    owner: str = SELLER,
) -> Listing:
    return service.create_draft(
        user(owner),
        content_type=kind.value,
        title="내 거울",
        description="설명",
        price_shards=price,
        snapshot_id=snapshot(store, kind, owner),
    )


def fee_entries(shard_store, kind: ContentType):
    reason = MarketplacePublishPolicy.reason(kind)
    return [e for e in shard_store.entries if e.reason is reason]


# MARK: - 서버가 비용의 authority (§1)


def test_server_owns_the_fee():
    assert MarketplacePublishPolicy.fee(ContentType.MIRROR) == 10
    assert MarketplacePublishPolicy.fee(ContentType.STICKER) == 5
    # 옛 20조각 정책은 없다.
    assert 20 not in MarketplacePublishPolicy.FEES.values()


def test_fee_reasons_are_distinct():
    """원장만 보고 거울인지 스티커인지 알 수 있어야 한다."""
    mirror = MarketplacePublishPolicy.reason(ContentType.MIRROR)
    sticker = MarketplacePublishPolicy.reason(ContentType.STICKER)
    assert mirror is ShardReason.MIRROR_PUBLISH_FEE
    assert sticker is ShardReason.STICKER_PUBLISH_FEE
    assert mirror is not sticker
    # 기존 값은 **바꾸지 않았다** — rename하면 과거 원장 파싱이 깨진다.
    assert mirror.value == "mirror_publish_fee"


def test_client_cannot_supply_the_fee():
    """요청에 비용·판매자·상태를 실을 자리가 없다."""
    import inspect

    from app.api.marketplace import DraftRequest

    fields = set(DraftRequest.model_fields)
    for banned in ["fee", "feeInShards", "shardCost", "sellerUserId", "status",
                   "publishFeePaid", "downloadCount", "likeCount"]:
        assert banned not in fields

    signature = inspect.signature(MarketplaceService.create_draft)
    assert "fee" not in signature.parameters
    assert "seller_user_id" not in signature.parameters


# MARK: - draft (§6)


def test_draft_costs_nothing(store, shards, service, shard_store):
    seed(shards, 100)
    listing = draft(service, store)

    assert listing.status is ListingStatus.DRAFT
    assert listing.publish_fee_paid is False
    assert listing.published_at is None
    assert (listing.download_count, listing.like_count) == (0, 0)
    assert shard_store.wallet(SELLER).balance == 100, "만들다 만 것에 돈을 받았다"
    assert shard_store.entries and all(
        e.reason is ShardReason.ADMIN_ADJUSTMENT for e in shard_store.entries
    )


def test_draft_takes_the_seller_from_auth(store, shards, service):
    listing = draft(service, store)
    assert listing.seller_user_id == SELLER


def test_draft_requires_a_server_side_snapshot(service):
    """client가 준 문자열만 믿고 만들지 않는다."""
    with pytest.raises(SnapshotNotFound):
        service.create_draft(
            user(), content_type="mirror", title="제목",
            description="", price_shards=0, snapshot_id="made-up",
        )


def test_draft_rejects_someone_elses_snapshot(store, service):
    other = snapshot(store, owner=OTHER)
    with pytest.raises(SnapshotNotFound):
        service.create_draft(
            user(), content_type="mirror", title="제목",
            description="", price_shards=0, snapshot_id=other,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", ""),
        ("title", "가" * 25),
        ("description", "가" * 201),
        ("price_shards", -1),
        ("price_shards", 1000),
        ("price_shards", True),
        ("content_type", "album"),
    ],
)
def test_draft_validates_input(store, service, field, value):
    kwargs = dict(
        content_type="mirror", title="제목", description="설명",
        price_shards=0, snapshot_id=snapshot(store),
    )
    kwargs[field] = value
    with pytest.raises(InvalidListing):
        service.create_draft(user(), **kwargs)


# MARK: - 최초 게시 (§7)


@pytest.mark.parametrize(
    ("kind", "fee", "start", "left"),
    [(ContentType.MIRROR, 10, 100, 90), (ContentType.STICKER, 5, 100, 95)],
)
def test_first_publish_charges_the_server_fee(
    store, shards, service, shard_store, kind, fee, start, left
):
    seed(shards, start)
    listing = draft(service, store, kind)

    result = service.publish(user(), listing.id)

    assert (result.published, result.fee_charged) == (True, True)
    assert result.fee_shards == fee
    assert result.balance == left
    assert shard_store.wallet(SELLER).balance == left
    assert shard_store.wallet(SELLER).lifetime_spent == fee

    entries = fee_entries(shard_store, kind)
    assert len(entries) == 1
    assert entries[0].delta == -fee

    assert result.listing.status is ListingStatus.PUBLISHED
    assert result.listing.publish_fee_paid is True
    assert result.listing.published_at is not None


def test_free_listing_still_pays_the_publish_fee(store, shards, service, shard_store):
    """무료로 나눠주는 것도 **만드는 값**은 같다."""
    seed(shards, 100)
    listing = draft(service, store, price=0)
    assert listing.price_shards == 0

    service.publish(user(), listing.id)

    assert shard_store.wallet(SELLER).balance == 90


def test_publish_keeps_counters_at_zero(store, shards, service):
    """게시가 다운로드/좋아요를 올리지 않는다."""
    seed(shards, 100)
    listing = draft(service, store)
    result = service.publish(user(), listing.id)
    assert (result.listing.download_count, result.listing.like_count) == (0, 0)


# MARK: - 원자성 (§16)


@pytest.mark.parametrize(
    ("kind", "balance"), [(ContentType.MIRROR, 9), (ContentType.STICKER, 4)]
)
def test_insufficient_balance_leaves_everything_untouched(
    store, shards, service, shard_store, kind, balance
):
    """A · B — 잔액이 모자라면 **listing도 그대로다.**"""
    seed(shards, balance)
    listing = draft(service, store, kind)

    with pytest.raises(InsufficientShards):
        service.publish(user(), listing.id)

    assert store.listings[listing.id].status is ListingStatus.DRAFT
    assert store.listings[listing.id].publish_fee_paid is False
    assert store.listings[listing.id].published_at is None
    assert shard_store.wallet(SELLER).balance == balance
    assert fee_entries(shard_store, kind) == []


def test_listing_write_failure_rolls_back_the_fee(store, shards, service, shard_store):
    """C — 조각을 뺀 뒤 listing 쓰기가 터져도 **차감이 되돌아간다.**"""
    seed(shards, 100)
    listing = draft(service, store)

    class FailingListings(dict):
        def __setitem__(self, key, value):
            raise RuntimeError("listing write failed")

    # commit 시점에 listing 쓰기가 터진다 — 조각은 그 전에 staged된 상태다.
    store.listings = FailingListings(store.listings)

    with pytest.raises(RuntimeError):
        service.publish(user(), listing.id)

    assert shard_store.wallet(SELLER).balance == 100, "수수료만 나갔다"
    assert fee_entries(shard_store, ContentType.MIRROR) == []


# MARK: - 멱등 (§9, §10)


@pytest.mark.parametrize("times", [2, 5])
def test_repeated_publish_charges_once(store, shards, service, shard_store, times):
    """E — 연타 · 네트워크 재시도. 수수료는 한 번뿐이다."""
    seed(shards, 100)
    listing = draft(service, store)

    results = [service.publish(user(), listing.id) for _ in range(times)]

    assert results[0].published is True and results[0].fee_charged is True
    for later in results[1:]:
        assert later.published is False, "이미 올라간 것을 또 올렸다"
        assert later.fee_charged is False
        assert later.balance == 90, "중복 요청도 정상 잔액을 돌려준다"

    assert shard_store.wallet(SELLER).balance == 90
    assert len(fee_entries(shard_store, ContentType.MIRROR)) == 1


def test_publish_idempotency_is_scoped_to_the_listing(store, shards, service, shard_store):
    """다른 listing은 다른 사건이다 — 각각 수수료를 낸다."""
    seed(shards, 100)
    first = draft(service, store)
    second = service.create_draft(
        user(), content_type="mirror", title="두 번째",
        description="", price_shards=0, snapshot_id=snapshot(store),
    )

    service.publish(user(), first.id)
    service.publish(user(), second.id)

    assert shard_store.wallet(SELLER).balance == 80
    assert len(fee_entries(shard_store, ContentType.MIRROR)) == 2


# MARK: - 동시성 (§18)


@pytest.mark.parametrize(
    ("kind", "left"), [(ContentType.MIRROR, 90), (ContentType.STICKER, 95)]
)
def test_concurrent_publish_charges_once(store, shards, service, shard_store, kind, left):
    seed(shards, 100)
    listing = draft(service, store, kind)
    start = threading.Barrier(8)
    published: list[bool] = []

    def run():
        start.wait()
        published.append(service.publish(user(), listing.id).published)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(published) == 1, f"게시가 {sum(published)}번 일어났다"
    assert shard_store.wallet(SELLER).balance == left
    assert len(fee_entries(shard_store, kind)) == 1


# MARK: - 내리기 / 다시 올리기 (§11, §12)


def test_unpublish_moves_no_shards(store, shards, service, shard_store):
    seed(shards, 100)
    listing = draft(service, store)
    published = service.publish(user(), listing.id).listing

    unlisted = service.unpublish(user(), listing.id)

    assert unlisted.status is ListingStatus.UNLISTED
    assert shard_store.wallet(SELLER).balance == 90, "내렸다고 수수료가 돌아왔다"
    assert len(fee_entries(shard_store, ContentType.MIRROR)) == 1
    # 낸 사실 · 최초 게시 시각 · counter는 그대로다.
    assert unlisted.publish_fee_paid is True
    assert unlisted.published_at == published.published_at
    assert (unlisted.download_count, unlisted.like_count) == (0, 0)
    assert unlisted.snapshot_id == published.snapshot_id


def test_republish_is_free(store, shards, service, shard_store):
    seed(shards, 100)
    listing = draft(service, store)
    first = service.publish(user(), listing.id).listing
    service.unpublish(user(), listing.id)

    again = service.publish(user(), listing.id)

    assert again.published is True
    assert again.fee_charged is False, "다시 올리는 데 또 받았다"
    assert shard_store.wallet(SELLER).balance == 90
    assert len(fee_entries(shard_store, ContentType.MIRROR)) == 1
    # **최초 업로드 날짜를 유지한다.**
    assert again.listing.published_at == first.published_at


def test_republish_keeps_the_first_upload_date_across_cycles(store, shards, service):
    seed(shards, 100)
    listing = draft(service, store)
    first = service.publish(user(), listing.id).listing

    for _ in range(3):
        service.unpublish(user(), listing.id)
        result = service.publish(user(), listing.id)
        assert result.listing.published_at == first.published_at


# MARK: - 상태 전이 (§13)


def test_unpublishing_a_draft_changes_nothing(store, shards, service, shard_store):
    seed(shards, 100)
    listing = draft(service, store)

    result = service.unpublish(user(), listing.id)

    assert result.status is ListingStatus.DRAFT
    assert shard_store.wallet(SELLER).balance == 100


def test_unpublishing_twice_is_a_no_op(store, shards, service):
    seed(shards, 100)
    listing = draft(service, store)
    service.publish(user(), listing.id)
    service.unpublish(user(), listing.id)

    assert service.unpublish(user(), listing.id).status is ListingStatus.UNLISTED


# MARK: - 권한 (§20)


def test_other_sellers_cannot_touch_the_listing(store, shards, service):
    seed(shards, 100)
    listing = draft(service, store)

    # 없는 것과 남의 것을 구분해 알려주지 않는다.
    with pytest.raises(ListingNotFound):
        service.publish(user(OTHER), listing.id)
    with pytest.raises(ListingNotFound):
        service.unpublish(user(OTHER), listing.id)


def test_unknown_listing_is_not_found(service):
    with pytest.raises(ListingNotFound):
        service.publish(user(), "made-up")


def test_other_sellers_wallet_is_never_touched(store, shards, service, shard_store):
    seed(shards, 100)
    seed(shards, 50, who=OTHER)
    listing = draft(service, store)

    with pytest.raises(ListingNotFound):
        service.publish(user(OTHER), listing.id)

    assert shard_store.wallet(OTHER).balance == 50
    assert shard_store.wallet(SELLER).balance == 100


# MARK: - 소스 불변식


def _code_only(source: str) -> str:
    import io
    import tokenize

    return "".join(
        t.string
        for t in tokenize.generate_tokens(io.StringIO(source).readline)
        if t.type not in (tokenize.COMMENT, tokenize.STRING)
    )


def test_no_generic_shard_endpoint_and_no_counter_writes():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for path in ["app/api/marketplace.py", "app/marketplace/service.py",
                 "app/marketplace/store.py", "app/marketplace/firestore_store.py"]:
        code = _code_only((root / path).read_text())
        for banned in ["downloadCount +", "download_count +", "likeCount +", "like_count +"]:
            assert banned not in code, f"{path}: 앱이 counter를 올린다 ({banned})"
        for banned in ["def transfer", "shards/transfer"]:
            assert banned not in code, f"{path}: 범용 이체가 생겼다"


def test_publish_uses_a_fresh_context_each_attempt():
    """B-7B.1 — context를 callable **안에서** 만들어야 재시도가 안전하다."""
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "app/marketplace/firestore_store.py").read_text()
    decorator = source.index("@firestore.transactional")
    context_line = source.index("shards.context(transaction)")
    assert decorator < context_line, "context를 transactional callable 밖에서 만들었다"


def test_no_gcs_yet():
    """§23 — 실제 asset 저장은 B-7F다."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for path in ["app/marketplace/models.py", "app/marketplace/store.py",
                 "app/marketplace/firestore_store.py", "app/marketplace/service.py"]:
        code = _code_only((root / path).read_text())
        for banned in ["storage", "bucket", "signed_url", "generate_signed_url"]:
            assert banned not in code.lower(), f"{path}: GCS를 들였다 ({banned})"


# MARK: - 공개 조회 (B-7D)
#
# 여기서 보는 것 셋:
# 1. draft/unlisted가 **없는 것처럼** 보이는가
# 2. "인기"가 오직 downloadCount인가
# 3. 공개 응답에 내부 값(seller UUID · snapshotId)이 새지 않는가

from datetime import datetime, timedelta, timezone   # noqa: E402

from app.marketplace.models import MarketplaceSort   # noqa: E402


def published(
    store,
    listing_id: str,
    *,
    kind: ContentType = ContentType.MIRROR,
    downloads: int = 0,
    likes: int = 0,
    day: int = 1,
    price: int = 0,
    owner: str = SELLER,
    status: ListingStatus = ListingStatus.PUBLISHED,
    published_at: datetime | None = ...,
) -> Listing:
    """fixture — 저장소에 직접 넣는다(게시 경로는 위에서 이미 검증했다)."""
    when = (
        datetime(2026, 8, day, tzinfo=timezone.utc)
        if published_at is ...
        else published_at
    )
    listing = Listing(
        id=listing_id,
        seller_user_id=owner,
        content_type=kind,
        title=listing_id,
        description="설명",
        price_shards=price,
        snapshot_id=f"snap-{listing_id}",
        status=status,
        publish_fee_paid=status is not ListingStatus.DRAFT,
        download_count=downloads,
        like_count=likes,
        published_at=when,
    )
    store.listings[listing_id] = listing
    return listing


def ids(listings) -> list[str]:
    return [x.id for x in listings]


# MARK: - 공개 범위 (§13)


def test_browse_shows_only_published(store, service):
    published(store, "live")
    published(store, "draft-one", status=ListingStatus.DRAFT, published_at=None)
    published(store, "taken-down", status=ListingStatus.UNLISTED)

    assert ids(service.browse()) == ["live"]


def test_detail_hides_draft_and_unlisted(store, service):
    published(store, "live")
    published(store, "draft-one", status=ListingStatus.DRAFT, published_at=None)
    published(store, "taken-down", status=ListingStatus.UNLISTED)

    assert service.listing("live").id == "live"
    for hidden in ("draft-one", "taken-down", "missing"):
        with pytest.raises(ListingNotFound):
            service.listing(hidden)


def test_seller_cannot_see_own_draft_through_the_public_path(store, service):
    """공개 조회와 판매자 관리를 한 endpoint에 섞지 않는다."""
    published(store, "mine", status=ListingStatus.DRAFT, published_at=None)
    with pytest.raises(ListingNotFound):
        service.listing("mine")


def test_published_without_a_date_is_not_public(store, service):
    """있을 수 없는 상태다 — **거짓 날짜를 지어내지 않고** 공개에서 뺀다."""
    published(store, "broken", published_at=None)
    assert service.browse() == []
    with pytest.raises(ListingNotFound):
        service.listing("broken")


# MARK: - 종류 필터 (§3)


def test_browse_filters_by_content_type(store, service):
    published(store, "mirror-one", kind=ContentType.MIRROR)
    published(store, "sticker-one", kind=ContentType.STICKER)

    assert ids(service.browse(content_type=ContentType.MIRROR)) == ["mirror-one"]
    assert ids(service.browse(content_type=ContentType.STICKER)) == ["sticker-one"]
    assert len(service.browse()) == 2


def test_empty_sticker_store_returns_nothing(store, service):
    """§8 — 가짜 스티커 상품을 만들지 않는다."""
    published(store, "mirror-only", kind=ContentType.MIRROR)
    assert service.browse(content_type=ContentType.STICKER) == []


# MARK: - 정렬 (§14)


def test_latest_sorts_by_published_date(store, service):
    published(store, "old", day=1)
    published(store, "newest", day=9)
    published(store, "middle", day=5)

    assert ids(service.browse(sort=MarketplaceSort.LATEST)) == ["newest", "middle", "old"]


def test_default_sort_is_latest(store, service):
    published(store, "old", day=1)
    published(store, "new", day=9)
    assert ids(service.browse()) == ids(service.browse(sort=MarketplaceSort.LATEST))
    assert MarketplaceSort.default() is MarketplaceSort.LATEST


def test_popular_ignores_likes_entirely(store, service):
    """§5 — 좋아요가 999여도 다운로드가 적으면 뒤로 간다."""
    published(store, "a", downloads=10, likes=0)
    published(store, "b", downloads=9, likes=999)

    assert ids(service.browse(sort=MarketplaceSort.POPULAR)) == ["a", "b"]


def test_likes_sorts_by_like_count(store, service):
    published(store, "a", likes=10, downloads=0)
    published(store, "b", likes=9, downloads=999)

    assert ids(service.browse(sort=MarketplaceSort.LIKES)) == ["a", "b"]


def test_popular_tie_breaks_by_date_then_id(store, service):
    published(store, "older", downloads=5, day=1)
    published(store, "newer", downloads=5, day=8)
    assert ids(service.browse(sort=MarketplaceSort.POPULAR)) == ["newer", "older"]


def test_likes_tie_breaks_by_downloads(store, service):
    published(store, "a", likes=7, downloads=8)
    published(store, "b", likes=7, downloads=1)
    assert ids(service.browse(sort=MarketplaceSort.LIKES)) == ["a", "b"]


@pytest.mark.parametrize("sort", list(MarketplaceSort))
def test_sorting_is_deterministic_when_everything_ties(store, service, sort):
    for listing_id in ("c", "a", "b"):
        published(store, listing_id)

    assert ids(service.browse(sort=sort)) == ["a", "b", "c"]
    assert ids(service.browse(sort=sort)) == ids(service.browse(sort=sort))


def test_no_weighted_popularity_in_source():
    from pathlib import Path

    code = _code_only(
        (Path(__file__).resolve().parent.parent / "app/marketplace/models.py").read_text()
    )
    for banned in ["score", "weight", "engagement", "like_count +", "+ like_count"]:
        assert banned not in code, f"인기 순에 가중치가 들어갔다 ({banned})"


# MARK: - 값 (§7, §12, §15)


def test_free_and_zero_count_listings_are_visible(store, service):
    published(store, "free", price=0, downloads=0, likes=0)

    found = service.listing("free")
    assert (found.price_shards, found.download_count, found.like_count) == (0, 0, 0)
    assert ids(service.browse()) == ["free"]


def test_browse_never_mutates_counters(store, service):
    published(store, "one", downloads=3, likes=4)

    for _ in range(5):
        service.browse()
        service.browse(sort=MarketplaceSort.POPULAR)
        service.listing("one")

    stored = store.listings["one"]
    assert (stored.download_count, stored.like_count) == (3, 4), "조회가 counter를 올렸다"


# MARK: - HTTP (§2, §11, §16)


@pytest.fixture
def client(store, shard_store):
    from fastapi.testclient import TestClient

    from app.core.config import Settings
    from app.main import create_app

    app = create_app(
        Settings(app_env="local"), shard_store=shard_store, marketplace_store=store
    )
    return TestClient(app, raise_server_exceptions=False)


def test_public_browse_needs_no_auth(client, store):
    published(store, "live")

    response = client.get("/marketplace/listings")

    assert response.status_code == 200
    assert [x["id"] for x in response.json()] == ["live"]


def test_public_detail_needs_no_auth(client, store):
    published(store, "live")
    assert client.get("/marketplace/listings/live").status_code == 200


@pytest.mark.parametrize("hidden", ["draft-one", "taken-down", "missing"])
def test_public_detail_404s_for_hidden_listings(client, store, hidden):
    published(store, "draft-one", status=ListingStatus.DRAFT, published_at=None)
    published(store, "taken-down", status=ListingStatus.UNLISTED)

    assert client.get(f"/marketplace/listings/{hidden}").status_code == 404


def test_public_response_never_leaks_internal_values(client, store):
    """§11 — Firestore 문서를 그대로 내보내지 않는다."""
    published(store, "live", downloads=2, likes=3, price=120)

    body = client.get("/marketplace/listings/live").json()
    raw = client.get("/marketplace/listings/live").text

    for banned in [
        "sellerUserId", "seller_user_id",
        "snapshotId", "snapshot_id",
        "publishFeePaid", "publish_fee_paid",
        "schemaVersion", "schema_version",
        "createdAt", "created_at", "updatedAt", "updated_at",
        "status",
    ]:
        assert banned not in body, f"{banned}가 공개 응답에 있다"
    # 내부 user UUID 값 자체도 새지 않는다.
    assert SELLER not in raw
    assert "snap-live" not in raw

    assert set(body) == {
        "id", "contentType", "title", "description",
        "priceShards", "downloadCount", "likeCount", "publishedAt",
    }
    assert body["publishedAt"].startswith("2026-08-01")


def test_public_list_items_have_the_same_shape(client, store):
    published(store, "live")
    item = client.get("/marketplace/listings").json()[0]
    assert set(item) == {
        "id", "contentType", "title", "description",
        "priceShards", "downloadCount", "likeCount", "publishedAt",
    }


@pytest.mark.parametrize("sort", ["latest", "popular", "likes"])
def test_http_sort_values(client, store, sort):
    published(store, "a", downloads=10, likes=1, day=1)
    published(store, "b", downloads=1, likes=10, day=9)

    response = client.get("/marketplace/listings", params={"sort": sort})

    assert response.status_code == 200
    expected = {"latest": ["b", "a"], "popular": ["a", "b"], "likes": ["b", "a"]}[sort]
    assert [x["id"] for x in response.json()] == expected


@pytest.mark.parametrize(
    ("params", "expected"),
    [({"sort": "trending"}, 422), ({"contentType": "album"}, 422)],
)
def test_http_rejects_unknown_enums(client, params, expected):
    assert client.get("/marketplace/listings", params=params).status_code == expected


def test_http_content_type_filter(client, store):
    published(store, "mirror-one", kind=ContentType.MIRROR)
    published(store, "sticker-one", kind=ContentType.STICKER)

    for kind, expected in (("mirror", ["mirror-one"]), ("sticker", ["sticker-one"])):
        response = client.get("/marketplace/listings", params={"contentType": kind})
        assert [x["id"] for x in response.json()] == expected


def test_mutations_still_require_auth(client):
    """§16 — browse를 열면서 mutation 인증을 약화하지 않았다."""
    assert client.post("/marketplace/listings", json={
        "contentType": "mirror", "title": "x", "priceShards": 0, "snapshotId": "s",
    }).status_code == 401
    assert client.post("/marketplace/listings/abc/publish").status_code == 401
    assert client.post("/marketplace/listings/abc/unpublish").status_code == 401


def test_no_asset_or_purchase_routes(client):
    """§18 · purchase는 B-7E다 — 가짜 preview URL도 만들지 않는다."""
    for path in [
        "/marketplace/listings/abc/preview",
        "/marketplace/listings/abc/template",
        "/marketplace/listings/abc/image",
        "/marketplace/listings/abc/purchase",
        "/marketplace/listings/abc/like",
    ]:
        assert client.get(path).status_code in {404, 405}
        assert client.post(path).status_code in {404, 405}


def test_no_n_plus_one_snapshot_reads():
    """§19 — 목록 한 건마다 snapshot을 추가 조회하지 않는다."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parent.parent / "app/marketplace/firestore_store.py"
    ).read_text()
    start = source.index("def list_published")
    end = source.index("def get_published")
    body = _code_only(source[start:end])
    assert "SNAPSHOTS" not in body, "목록에서 snapshot을 읽는다"
