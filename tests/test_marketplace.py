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


def _method_body(source: str, name: str) -> str:
    """`def <name>`부터 **바로 다음 method 정의 직전**까지.

    경계를 손으로 적으면(`source.index("def get_published")`) 그 사이에 method가
    하나 생길 때마다 test가 엉뚱한 코드를 보게 된다 — 실제로 두 번 그랬다.
    다음 `    def `를 찾아 자동으로 끊는다.
    """
    start = source.index(f"def {name}")
    rest = source[start:]
    following = rest.find("\n    def ", 1)
    return rest if following == -1 else rest[:following]


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


def test_gcs_is_confined_to_the_asset_module():
    """§B-7F — GCS를 아는 파일은 `assets.py` 하나다. domain이 bucket을 모른다."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for path in ["app/marketplace/models.py", "app/marketplace/store.py",
                 "app/marketplace/firestore_store.py"]:
        code = _code_only((root / path).read_text()).lower()
        for banned in ["bucket", "google.cloud import storage", "signed_url"]:
            assert banned not in code, f"{path}: GCS를 안다 ({banned})"


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
        "sellerDisplayName",
    }
    assert body["publishedAt"].startswith("2026-08-01")


def test_public_list_items_have_the_same_shape(client, store):
    published(store, "live")
    item = client.get("/marketplace/listings").json()[0]
    assert set(item) == {
        "id", "contentType", "title", "description",
        "priceShards", "downloadCount", "likeCount", "publishedAt",
        # 1.1.0: 판매자를 **이름으로만** 보여 준다. 내부 id는 여전히 나가지 않는다.
        "sellerDisplayName",
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


def test_public_listing_dto_has_no_asset_url(client, store):
    """공개 DTO에 가짜 preview URL을 넣지 않는다 — 미리보기는 별도 endpoint다."""
    published(store, "live")
    body = client.get("/marketplace/listings/live").json()
    for banned in ["previewUrl", "preview_url", "templateUrl", "url", "gs://", "bucket"]:
        assert banned not in body


def test_no_n_plus_one_snapshot_reads():
    """§19 — 목록 한 건마다 snapshot을 추가 조회하지 않는다."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parent.parent / "app/marketplace/firestore_store.py"
    ).read_text()
    body = _code_only(_method_body(source, "list_published"))
    assert "SNAPSHOTS" not in body, "목록에서 snapshot을 읽는다"

    # 판매자 목록도 같은 규칙이다.
    seller = _code_only(_method_body(source, "list_for_seller"))
    assert "SNAPSHOTS" not in seller, "판매자 목록에서 snapshot을 읽는다"


# MARK: - 획득 · 소유권 (B-7E)
#
# 여기서 보는 것: **구매자 차감 · 판매자 지급 · 소유권 · 다운로드 수가 한 commit인가.**
# 하나라도 갈라지면 돈이나 권리가 사라진다.

from app.marketplace.models import Ownership, SelfPurchase, ownership_id   # noqa: E402

BUYER = "22222222-3333-4444-8555-666666666666"


def sale(store, listing_id: str = "for-sale", *, price: int = 30, downloads: int = 0,
         kind: ContentType = ContentType.MIRROR, owner: str = SELLER) -> Listing:
    return published(store, listing_id, kind=kind, price=price, downloads=downloads, owner=owner)


def entries(shard_store, reason: ShardReason):
    return [e for e in shard_store.entries if e.reason is reason]


def purchase_reasons(kind: ContentType = ContentType.MIRROR):
    return (
        MarketplacePublishPolicy.purchase_reason(kind),
        MarketplacePublishPolicy.sale_reason(kind),
    )


# MARK: - 유료 구매 (§8)


@pytest.mark.parametrize("kind", list(ContentType))
def test_paid_purchase_moves_everything_in_one_commit(store, shards, service, shard_store, kind):
    seed(shards, 100, who=BUYER)
    seed(shards, 10)
    listing = sale(store, kind=kind, downloads=7)
    buy_reason, sell_reason = purchase_reasons(kind)

    result = service.purchase(user(BUYER), listing.id)

    assert (result.purchased, result.already_owned) == (True, False)
    assert result.price_paid == 30
    assert result.balance == 70
    assert result.download_count == 8

    buyer_wallet = shard_store.wallet(BUYER)
    seller_wallet = shard_store.wallet(SELLER)
    assert (buyer_wallet.balance, buyer_wallet.lifetime_spent) == (70, 30)
    assert (seller_wallet.balance, seller_wallet.lifetime_earned) == (40, 40)
    # 판매는 "쓴 것"이 아니다.
    assert seller_wallet.lifetime_spent == 0

    assert [e.delta for e in entries(shard_store, buy_reason)] == [-30]
    assert [e.delta for e in entries(shard_store, sell_reason)] == [30]
    assert store.listings[listing.id].download_count == 8


def test_purchase_fee_is_zero_percent(store, shards, service, shard_store):
    """구매자가 낸 만큼 판매자가 정확히 받는다."""
    seed(shards, 100, who=BUYER)
    listing = sale(store, price=77)

    service.purchase(user(BUYER), listing.id)

    assert shard_store.wallet(BUYER).balance == 23
    assert shard_store.wallet(SELLER).balance == 77


def test_ownership_freezes_the_purchase(store, shards, service):
    seed(shards, 100, who=BUYER)
    listing = sale(store)

    owned = service.purchase(user(BUYER), listing.id).ownership

    assert owned.id == ownership_id(BUYER, listing.id)
    assert owned.user_id == BUYER
    assert owned.seller_user_id == SELLER
    assert owned.snapshot_id == listing.snapshot_id
    assert owned.price_paid == 30
    assert owned.buyer_ledger_entry_id is not None
    assert owned.seller_ledger_entry_id is not None
    # raw id를 문서 ID에 노출하지 않는다.
    assert BUYER not in owned.id and listing.id not in owned.id


# MARK: - 무료 획득 (§9)


def test_free_acquisition_creates_ownership_without_ledger(store, shards, service, shard_store):
    seed(shards, 100, who=BUYER)
    listing = sale(store, price=0, downloads=3)
    buy_reason, sell_reason = purchase_reasons()

    result = service.purchase(user(BUYER), listing.id)

    assert result.purchased is True
    assert result.price_paid == 0
    assert result.download_count == 4

    # 지갑도 원장도 건드리지 않는다 — 조각이 움직이지 않았다.
    assert shard_store.wallet(BUYER).balance == 100
    assert shard_store.wallet(BUYER).lifetime_spent == 0
    assert shard_store.wallet(SELLER).balance == 0
    assert entries(shard_store, buy_reason) == []
    assert entries(shard_store, sell_reason) == []

    owned = result.ownership
    assert owned.price_paid == 0
    assert owned.buyer_ledger_entry_id is None
    assert owned.seller_ledger_entry_id is None


# MARK: - 거절 (§7, §10)


def test_insufficient_shards_changes_nothing(store, shards, service, shard_store):
    seed(shards, 29, who=BUYER)
    seed(shards, 10)
    listing = sale(store, downloads=5)
    buy_reason, sell_reason = purchase_reasons()

    with pytest.raises(InsufficientShards):
        service.purchase(user(BUYER), listing.id)

    assert shard_store.wallet(BUYER).balance == 29
    assert shard_store.wallet(SELLER).balance == 10
    assert store.ownership_records == {}
    assert store.listings[listing.id].download_count == 5
    assert entries(shard_store, buy_reason) == []
    assert entries(shard_store, sell_reason) == []


def test_self_purchase_is_refused(store, shards, service, shard_store):
    seed(shards, 100)
    listing = sale(store, downloads=4)

    with pytest.raises(SelfPurchase):
        service.purchase(user(SELLER), listing.id)

    assert shard_store.wallet(SELLER).balance == 100
    assert store.ownership_records == {}
    assert store.listings[listing.id].download_count == 4


@pytest.mark.parametrize("status", [ListingStatus.DRAFT, ListingStatus.UNLISTED])
def test_unavailable_listings_cannot_be_bought(store, shards, service, shard_store, status):
    seed(shards, 100, who=BUYER)
    published(store, "hidden", status=status,
              published_at=None if status is ListingStatus.DRAFT else ...)

    with pytest.raises(ListingNotFound):
        service.purchase(user(BUYER), "hidden")

    assert shard_store.wallet(BUYER).balance == 100
    assert store.ownership_records == {}


# MARK: - 멱등 (§11, §12)


@pytest.mark.parametrize("times", [2, 5])
def test_repeated_purchase_acquires_once(store, shards, service, shard_store, times):
    seed(shards, 100, who=BUYER)
    listing = sale(store, downloads=1)
    buy_reason, sell_reason = purchase_reasons()

    results = [service.purchase(user(BUYER), listing.id) for _ in range(times)]

    assert results[0].purchased is True and results[0].already_owned is False
    for later in results[1:]:
        assert later.purchased is False
        assert later.already_owned is True
        assert later.balance == 70, "중복 요청도 정상 잔액을 돌려준다"
        assert later.download_count == 2, "중복 요청이 counter를 올렸다"

    assert shard_store.wallet(BUYER).balance == 70
    assert shard_store.wallet(SELLER).balance == 30
    assert len(entries(shard_store, buy_reason)) == 1
    assert len(entries(shard_store, sell_reason)) == 1
    assert len(store.ownership_records) == 1
    assert store.listings[listing.id].download_count == 2


def test_ledger_keys_do_not_collide(store, shards, service, shard_store):
    """구매자/판매자는 user도 reason도 다르므로 서로 다른 원장 문서다."""
    seed(shards, 100, who=BUYER)
    listing = sale(store)

    owned = service.purchase(user(BUYER), listing.id).ownership

    assert owned.buyer_ledger_entry_id != owned.seller_ledger_entry_id
    ids = {e.id for e in shard_store.entries}
    assert owned.buyer_ledger_entry_id in ids
    assert owned.seller_ledger_entry_id in ids


def test_different_listings_are_different_purchases(store, shards, service, shard_store):
    seed(shards, 100, who=BUYER)
    first = sale(store, "one")
    second = sale(store, "two")

    service.purchase(user(BUYER), first.id)
    service.purchase(user(BUYER), second.id)

    assert shard_store.wallet(BUYER).balance == 40
    assert len(store.ownership_records) == 2


# MARK: - 동시성 (§14, §15, §16)


def test_same_buyer_concurrency_acquires_once(store, shards, service, shard_store):
    """§14 — 같은 구매자 8 동시 요청. counter가 7 → 8이어야 한다."""
    seed(shards, 100, who=BUYER)
    listing = sale(store, downloads=7)
    start = threading.Barrier(8)
    acquired: list[bool] = []

    def run():
        start.wait()
        acquired.append(service.purchase(user(BUYER), listing.id).purchased)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(acquired) == 1, f"소유권이 {sum(acquired)}번 생겼다"
    assert shard_store.wallet(BUYER).balance == 70
    assert shard_store.wallet(SELLER).balance == 30
    assert len(store.ownership_records) == 1
    assert store.listings[listing.id].download_count == 8, "counter가 여러 번 올랐다"


def test_different_buyers_concurrency_counts_each(store, shards, service, shard_store):
    """§15 — 8명이 동시에 획득. lost update가 없어야 한다."""
    buyers = [f"buyer-{i}" for i in range(8)]
    for buyer in buyers:
        seed(shards, 100, who=buyer)
    listing = sale(store, price=10, downloads=0)
    start = threading.Barrier(len(buyers))

    def run(buyer: str):
        start.wait()
        service.purchase(user(buyer), listing.id)

    threads = [threading.Thread(target=run, args=(b,)) for b in buyers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(store.ownership_records) == 8
    for buyer in buyers:
        assert shard_store.wallet(buyer).balance == 90
    assert shard_store.wallet(SELLER).balance == 80, "판매 대금이 유실됐다"
    assert store.listings[listing.id].download_count == 8, "counter가 어긋났다"


def test_balance_limits_concurrent_purchases(store, shards, service, shard_store):
    """§16 — 잔액 30으로 30짜리 둘을 동시에. 정확히 하나만 성공한다."""
    seed(shards, 30, who=BUYER)
    first = sale(store, "one", price=30)
    second = sale(store, "two", price=30)
    start = threading.Barrier(2)
    outcome: list[bool] = []

    def run(listing_id: str):
        start.wait()
        try:
            outcome.append(service.purchase(user(BUYER), listing_id).purchased)
        except InsufficientShards:
            outcome.append(False)

    threads = [threading.Thread(target=run, args=(x,)) for x in (first.id, second.id)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(outcome) == 1
    assert shard_store.wallet(BUYER).balance == 0, "잔액이 음수가 됐다"
    assert len(store.ownership_records) == 1
    assert shard_store.wallet(SELLER).balance == 30


# MARK: - 내리기 경합 (§17, §23)


def test_ownership_survives_unpublish(store, shards, service, shard_store):
    """§23 — 돈을 냈는데 판매자가 내려서 잃으면 안 된다."""
    seed(shards, 100, who=BUYER)
    listing = sale(store)
    service.purchase(user(BUYER), listing.id)

    service.unpublish(user(SELLER), listing.id)

    # 공개 목록에서는 사라지지만 소유권은 남는다.
    assert service.browse() == []
    owned = service.purchases(user(BUYER))
    assert len(owned) == 1
    assert owned[0][0].listing_id == listing.id
    assert owned[0][1].status is ListingStatus.UNLISTED


def test_purchase_after_unpublish_changes_nothing(store, shards, service, shard_store):
    """§17-B — 내려간 뒤 도착한 구매는 경제를 건드리지 않는다."""
    seed(shards, 100, who=BUYER)
    listing = sale(store)
    service.unpublish(user(SELLER), listing.id)

    with pytest.raises(ListingNotFound):
        service.purchase(user(BUYER), listing.id)

    assert shard_store.wallet(BUYER).balance == 100
    assert store.ownership_records == {}


# MARK: - 원자성 (§25)


def test_ownership_failure_rolls_back_the_money(store, shards, service, shard_store):
    """§25-B — 조각이 오간 뒤 소유권 생성이 터지면 **전부 되돌아간다.**"""
    seed(shards, 100, who=BUYER)
    seed(shards, 10)
    listing = sale(store, downloads=2)
    buy_reason, sell_reason = purchase_reasons()

    class FailingOwnership(dict):
        def __setitem__(self, key, value):
            raise RuntimeError("ownership write failed")

    store.ownership_records = FailingOwnership(store.ownership_records)

    with pytest.raises(RuntimeError):
        service.purchase(user(BUYER), listing.id)

    assert shard_store.wallet(BUYER).balance == 100, "구매자만 차감됐다"
    assert shard_store.wallet(SELLER).balance == 10, "판매자만 지급됐다"
    assert entries(shard_store, buy_reason) == []
    assert entries(shard_store, sell_reason) == []
    assert store.listings[listing.id].download_count == 2


def test_counter_failure_rolls_back_everything(store, shards, service, shard_store):
    """§25-C — counter 쓰기가 터지면 소유권과 돈도 되돌아간다."""
    seed(shards, 100, who=BUYER)
    listing = sale(store)

    class FailingListings(dict):
        def __setitem__(self, key, value):
            raise RuntimeError("counter write failed")

    store.listings = FailingListings(store.listings)

    with pytest.raises(RuntimeError):
        service.purchase(user(BUYER), listing.id)

    assert shard_store.wallet(BUYER).balance == 100
    assert shard_store.wallet(SELLER).balance == 0
    assert store.ownership_records == {}


def test_ownership_is_never_overwritten(store, shards, service):
    """§20 — 이미 있는 소유권을 조용히 교체하지 않는다."""
    seed(shards, 100, who=BUYER)
    listing = sale(store)
    first = service.purchase(user(BUYER), listing.id).ownership

    again = service.purchase(user(BUYER), listing.id).ownership

    assert again.created_at == first.created_at, "소유권이 새로 만들어졌다"
    assert again.price_paid == first.price_paid


# MARK: - 판매자 지갑이 없을 때 (§24)


def test_seller_without_a_wallet_still_gets_paid(store, shards, service, shard_store):
    seed(shards, 100, who=BUYER)
    listing = sale(store)
    assert SELLER not in shard_store.wallets

    service.purchase(user(BUYER), listing.id)

    wallet = shard_store.wallet(SELLER)
    assert (wallet.balance, wallet.lifetime_earned) == (30, 30)


# MARK: - 내 구매 목록 (§22)


def test_my_purchases_lists_owned_items(store, shards, service):
    seed(shards, 100, who=BUYER)
    first = sale(store, "one", price=10)
    second = sale(store, "two", price=0)
    service.purchase(user(BUYER), first.id)
    service.purchase(user(BUYER), second.id)

    owned = service.purchases(user(BUYER))

    assert {x[0].listing_id for x in owned} == {"one", "two"}
    assert {x[0].price_paid for x in owned} == {10, 0}


def test_my_purchases_is_scoped_to_the_user(store, shards, service):
    seed(shards, 100, who=BUYER)
    listing = sale(store)
    service.purchase(user(BUYER), listing.id)

    assert service.purchases(user(BUYER)) != []
    assert service.purchases(user(OTHER)) == []


# MARK: - HTTP (§3, §21, §27)


def test_purchase_endpoint_takes_no_body():
    from app.api import marketplace as api

    import inspect

    signature = inspect.signature(api.purchase_listing)
    assert "request" not in signature.parameters, "구매 요청이 body를 받는다"
    for banned in ["price", "amount", "fee", "seller", "snapshot", "downloadCount"]:
        assert banned not in signature.parameters


def test_purchase_response_hides_internal_values(client, store, shards):
    seed(shards, 100, who=BUYER)
    sale(store)

    from app.api.marketplace import PurchaseResponse

    fields = set(PurchaseResponse.model_fields)
    for banned in ["seller_user_id", "sellerUserId", "snapshot_id", "snapshotId"]:
        assert banned not in fields


def test_purchase_and_my_purchases_require_auth(client):
    assert client.post("/marketplace/listings/abc/purchase").status_code == 401
    assert client.get("/users/me/marketplace/purchases").status_code == 401


def test_no_arbitrary_user_ownership_route(client):
    """남의 소유권을 임의 userId로 조회하는 경로를 만들지 않는다."""
    for path in [
        f"/users/{SELLER}/marketplace/purchases",
        "/marketplace/ownership",
        f"/marketplace/ownership/{SELLER}",
    ]:
        assert client.get(path).status_code in {404, 405}


def test_no_like_route_yet(client):
    """§28 — like는 B-7E.1이다."""
    for method in ("get", "post"):
        assert getattr(client, method)("/marketplace/listings/abc/like").status_code in {404, 405}


# MARK: - 읽기 순서 · 소스 불변식 (§19, §13, §26, §29)


def test_marketplace_reads_come_before_writes():
    """§19 — Firestore transaction은 읽기를 쓰기보다 먼저 해야 한다.

    조각 primitive가 자기 문서를 읽고 쓰므로, marketplace 문서 읽기가 그 뒤에 오면
    안 된다. 소스에서 순서를 고정한다.
    """
    from pathlib import Path

    source = (
        Path(__file__).resolve().parent.parent / "app/marketplace/firestore_store.py"
    ).read_text()
    body = source[source.index("def acquire"):source.index("def ownerships")]

    listing_read = body.index("listing_ref.get(transaction=transaction)")
    ownership_read = body.index("ownership_ref.get(transaction=transaction)")
    first_shard_call = body.index("shards.apply_in_transaction")
    ownership_write = body.index("transaction.create(ownership_ref")
    counter_write = body.index("transaction.update(listing_ref")

    assert listing_read < first_shard_call, "listing을 조각 이동 뒤에 읽는다"
    assert ownership_read < first_shard_call, "소유권을 조각 이동 뒤에 읽는다"
    assert first_shard_call < ownership_write < counter_write


def test_counter_and_ownership_share_one_transaction():
    """§13 — counter를 별도 transaction으로 올리지 않는다."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parent.parent / "app/marketplace/firestore_store.py"
    ).read_text()
    body = source[source.index("def acquire"):source.index("def ownerships")]

    assert body.count("@firestore.transactional") == 1
    assert "downloadCount" in body
    assert body.count("shards.transaction()") == 1


def test_purchase_uses_a_fresh_context_each_attempt():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parent.parent / "app/marketplace/firestore_store.py"
    ).read_text()
    body = source[source.index("def acquire"):source.index("def ownerships")]
    assert body.index("@firestore.transactional") < body.index("shards.context(transaction)")


def test_no_counter_or_price_spoofing_in_source():
    """§6 · §26 — client가 가격/counter를 정하는 경로가 없다."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    api = _code_only((root / "app/api/marketplace.py").read_text())
    # 구매 응답 model에만 downloadCount가 있고, 요청 model에는 없다.
    from app.api.marketplace import DraftRequest

    for banned in ["downloadCount", "download_count", "priceShards", "price_shards"]:
        assert banned not in {f for f in DraftRequest.model_fields} or banned in {"price_shards"}
    assert "downloadCount +" not in api and "download_count +" not in api


def test_no_signed_urls_anywhere():
    """§7 — signed URL을 delivery 수단으로 쓰지 않는다. URL 자체가 credential이 된다."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for path in ["app/marketplace/assets.py", "app/marketplace/service.py",
                 "app/api/marketplace.py"]:
        code = _code_only((root / path).read_text())
        for banned in ["generate_signed_url", "signed_url", "sign_blob"]:
            assert banned not in code, f"{path}: signed URL을 만든다 ({banned})"


# MARK: - 좋아요 (B-7E.1)
#
# **관계가 authority이고 `likeCount`는 projection이다.** 둘이 어긋나면 안 된다.
# 조각 경제는 이 경로에 전혀 등장하지 않는다.

from app.marketplace.models import (   # noqa: E402
    Like,
    LikeCountInconsistent,
    SelfLike,
    like_id,
)

LIKER = "33333333-4444-4555-8666-777777777777"


def liked(store, listing_id: str, user_id: str) -> bool:
    return like_id(user_id, listing_id) in store.likes_by_id


def count(store, listing_id: str) -> int:
    return store.listings[listing_id].like_count


# MARK: - 기본 (§3, §4)


@pytest.mark.parametrize("price", [0, 30])
def test_first_like_counts_once(store, service, price):
    listing = sale(store, price=price)

    result = service.like(user(LIKER), listing.id)

    assert (result.liked, result.changed, result.like_count) == (True, True, 1)
    assert liked(store, listing.id, LIKER)
    assert count(store, listing.id) == 1


@pytest.mark.parametrize("times", [2, 8])
def test_repeated_like_changes_nothing(store, service, times):
    listing = sale(store)

    results = [service.like(user(LIKER), listing.id) for _ in range(times)]

    assert results[0].changed is True
    for later in results[1:]:
        assert (later.liked, later.changed, later.like_count) == (True, False, 1)
    assert count(store, listing.id) == 1
    assert len(store.likes_by_id) == 1


def test_unlike_removes_the_relation(store, service):
    listing = sale(store)
    service.like(user(LIKER), listing.id)

    result = service.unlike(user(LIKER), listing.id)

    assert (result.liked, result.changed, result.like_count) == (False, True, 0)
    assert not liked(store, listing.id, LIKER)
    assert count(store, listing.id) == 0


@pytest.mark.parametrize("times", [2, 8])
def test_repeated_unlike_changes_nothing(store, service, times):
    listing = sale(store)
    service.like(user(LIKER), listing.id)

    results = [service.unlike(user(LIKER), listing.id) for _ in range(times)]

    assert results[0].changed is True
    for later in results[1:]:
        assert (later.liked, later.changed, later.like_count) == (False, False, 0)
    assert count(store, listing.id) == 0


def test_unlike_without_a_like_is_a_no_op(store, service):
    listing = sale(store, downloads=0)

    result = service.unlike(user(LIKER), listing.id)

    assert (result.liked, result.changed, result.like_count) == (False, False, 0)
    assert count(store, listing.id) == 0


def test_like_id_hides_raw_values():
    key = like_id("user-abc", "listing-xyz")
    assert "user-abc" not in key and "listing-xyz" not in key
    assert key == like_id("user-abc", "listing-xyz")
    assert key != like_id("listing-xyz", "user-abc")


# MARK: - 상태 정책 (§7, §8)


def test_draft_cannot_be_liked(store, service):
    published(store, "draft-one", status=ListingStatus.DRAFT, published_at=None)

    with pytest.raises(ListingNotFound):
        service.like(user(LIKER), "draft-one")

    assert store.likes_by_id == {}


def test_unlisted_cannot_receive_a_new_like(store, service):
    listing = sale(store)
    service.unpublish(user(SELLER), listing.id)

    with pytest.raises(ListingNotFound):
        service.like(user(LIKER), listing.id)

    assert store.likes_by_id == {}


def test_existing_like_can_be_removed_after_unpublish(store, service):
    """§6 — 못 지우면 count가 영구히 남는다."""
    listing = sale(store)
    service.like(user(LIKER), listing.id)
    service.unpublish(user(SELLER), listing.id)

    result = service.unlike(user(LIKER), listing.id)

    assert (result.changed, result.like_count) == (True, 0)
    assert not liked(store, listing.id, LIKER)


def test_self_like_is_refused(store, service):
    listing = sale(store)

    with pytest.raises(SelfLike):
        service.like(user(SELLER), listing.id)

    assert count(store, listing.id) == 0
    assert store.likes_by_id == {}


def test_self_unlike_cleans_up_a_stray_relation(store, service):
    """§8 — 잘못 존재하는 관계는 지울 수 있어야 한다."""
    listing = sale(store)
    key = like_id(SELLER, listing.id)
    store.likes_by_id[key] = Like(id=key, user_id=SELLER, listing_id=listing.id)
    store.listings[listing.id] = published(
        store, listing.id, price=30, downloads=0
    ).__class__(**{**listing.__dict__, "like_count": 1})

    result = service.unlike(user(SELLER), listing.id)

    assert (result.changed, result.like_count) == (True, 0)
    assert key not in store.likes_by_id


# MARK: - projection 안전 (§15)


def test_negative_like_count_is_not_silently_fixed(store, service):
    listing = sale(store)
    store.listings[listing.id] = type(listing)(**{**listing.__dict__, "like_count": -1})

    with pytest.raises(LikeCountInconsistent):
        service.like(user(LIKER), listing.id)
    with pytest.raises(LikeCountInconsistent):
        service.unlike(user(LIKER), listing.id)


def test_relation_without_count_never_goes_negative(store, service):
    """관계는 있는데 count가 0 — projection이 어긋났다. 음수를 만들지 않는다."""
    listing = sale(store)
    key = like_id(LIKER, listing.id)
    store.likes_by_id[key] = Like(id=key, user_id=LIKER, listing_id=listing.id)
    assert count(store, listing.id) == 0

    with pytest.raises(LikeCountInconsistent):
        service.unlike(user(LIKER), listing.id)

    assert count(store, listing.id) == 0, "음수가 됐다"


# MARK: - 동시성 (§10, §11, §12)


def test_same_user_concurrent_likes_count_once(store, service):
    listing = sale(store)
    start = threading.Barrier(8)
    changes: list[bool] = []

    def run():
        start.wait()
        changes.append(service.like(user(LIKER), listing.id).changed)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(changes) == 1
    assert count(store, listing.id) == 1
    assert len(store.likes_by_id) == 1


def test_same_user_concurrent_unlikes_count_once(store, service):
    listing = sale(store)
    service.like(user(LIKER), listing.id)
    start = threading.Barrier(8)
    changes: list[bool] = []

    def run():
        start.wait()
        changes.append(service.unlike(user(LIKER), listing.id).changed)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(changes) == 1
    assert count(store, listing.id) == 0
    assert store.likes_by_id == {}


def test_different_users_concurrent_likes_count_each(store, service):
    """§11 — lost update 금지."""
    likers = [f"liker-{i}" for i in range(8)]
    listing = sale(store)
    start = threading.Barrier(len(likers))

    def run(who: str):
        start.wait()
        service.like(user(who), listing.id)

    threads = [threading.Thread(target=run, args=(x,)) for x in likers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(store.likes_by_id) == 8
    assert count(store, listing.id) == 8, "count가 어긋났다"


def test_like_and_unlike_race_stays_consistent(store, service):
    """§12 — 관계와 projection이 어긋난 조합은 나올 수 없다."""
    listing = sale(store)
    start = threading.Barrier(2)

    def do_like():
        start.wait()
        service.like(user(LIKER), listing.id)

    def do_unlike():
        start.wait()
        service.unlike(user(LIKER), listing.id)

    for _ in range(20):
        store.likes_by_id.clear()
        store.listings[listing.id] = type(listing)(**{**listing.__dict__, "like_count": 0})
        start = threading.Barrier(2)
        threads = [threading.Thread(target=do_like), threading.Thread(target=do_unlike)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        has_relation = liked(store, listing.id, LIKER)
        current = count(store, listing.id)
        # 관계가 있으면 1, 없으면 0. 다른 조합은 divergence다.
        assert current == (1 if has_relation else 0), (has_relation, current)


# MARK: - 경제 격리 (§9, §19)


def test_like_never_touches_the_shard_economy(store, service, shard_store):
    listing = sale(store)
    seed_before = list(shard_store.entries)

    service.like(user(LIKER), listing.id)
    service.unlike(user(LIKER), listing.id)

    assert shard_store.entries == seed_before, "원장이 바뀌었다"
    assert shard_store.wallets == {}, "지갑이 생겼다"


def test_like_never_touches_download_count(store, service):
    listing = sale(store, downloads=5)

    service.like(user(LIKER), listing.id)
    service.unlike(user(LIKER), listing.id)

    assert store.listings[listing.id].download_count == 5


def test_purchase_and_browse_never_touch_like_count(store, shards, service):
    """§19 — 좋아요는 LIKE/UNLIKE에서만 바뀐다."""
    seed(shards, 100, who=BUYER)
    listing = sale(store)
    service.like(user(LIKER), listing.id)

    service.purchase(user(BUYER), listing.id)
    service.browse()
    service.listing(listing.id)
    service.unpublish(user(SELLER), listing.id)

    assert count(store, listing.id) == 1


def test_like_path_has_no_shard_code():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for path in ["app/marketplace/store.py", "app/marketplace/firestore_store.py"]:
        source = (root / path).read_text()
        body = source[source.index("def like("):source.index("def likes(")]
        # 클래스 중간 조각이라 tokenize할 수 없다 — 주석을 줄 단위로 걷어낸다.
        code = "\n".join(
            line.split("#")[0] for line in body.splitlines()
        )
        # 호출 모양으로 검사한다 — 이름이 설명 문장에 등장하는 것은 문제가 아니다.
        for banned in ["apply_in_transaction(", "shards.", ".credit(", ".debit("]:
            assert banned not in code, f"{path}: 좋아요가 조각을 만진다 ({banned})"


# MARK: - 정렬 회귀 (§18)


def test_likes_do_not_affect_popular_sort(store, service):
    published(store, "downloaded", downloads=10, likes=0)
    published(store, "loved", downloads=1, likes=0)
    service.like(user(LIKER), "loved")
    service.like(user(BUYER), "loved")

    assert ids(service.browse(sort=MarketplaceSort.POPULAR)) == ["downloaded", "loved"]
    assert ids(service.browse(sort=MarketplaceSort.LIKES)) == ["loved", "downloaded"]


# MARK: - 내 좋아요 (§16)


def test_my_likes_lists_listing_ids(store, service):
    first = sale(store, "one")
    second = sale(store, "two")
    service.like(user(LIKER), first.id)
    service.like(user(LIKER), second.id)

    assert service.liked_listing_ids(user(LIKER)) == ["one", "two"]


def test_my_likes_is_scoped_to_the_user(store, service):
    listing = sale(store)
    service.like(user(LIKER), listing.id)

    assert service.liked_listing_ids(user(LIKER)) == [listing.id]
    assert service.liked_listing_ids(user(BUYER)) == []


# MARK: - HTTP (§20, §21)


def test_like_endpoints_require_auth(client):
    assert client.put("/marketplace/listings/abc/like").status_code == 401
    assert client.delete("/marketplace/listings/abc/like").status_code == 401
    assert client.get("/users/me/marketplace/likes").status_code == 401


def test_public_browse_stays_open(client, store):
    published(store, "live")
    assert client.get("/marketplace/listings").status_code == 200


def test_like_response_hides_internal_values():
    from app.api.marketplace import LikeResponse

    fields = set(LikeResponse.model_fields)
    assert fields == {"listing_id", "liked", "changed", "like_count"}
    for banned in ["user_id", "userId", "seller_user_id", "snapshot_id"]:
        assert banned not in fields


def test_no_arbitrary_user_like_route(client):
    for path in [
        f"/users/{SELLER}/marketplace/likes",
        "/marketplace/likes",
        f"/marketplace/likes/{SELLER}",
    ]:
        assert client.get(path).status_code in {404, 405}


def test_like_endpoint_takes_no_body():
    import inspect

    from app.api import marketplace as api

    for handler in (api.like_listing, api.unlike_listing):
        assert "request" not in inspect.signature(handler).parameters


# MARK: - 실제 Firestore 구현 + ABORTED 재시도 (§14)
#
# 지금까지 marketplace test는 in-memory 저장소만 돌았다. 여기서는 **FirestoreMarketplaceStore
# 코드 자체**를 최소 fake db로 돌려 재시도 안전성을 본다 — production Firestore를 부르지 않는다.


class FakeDocument:
    def __init__(self, doc_id: str, data: dict | None) -> None:
        self.id = doc_id
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict | None:
        return dict(self._data) if self._data is not None else None


class FakeReference:
    def __init__(self, store: dict, doc_id: str) -> None:
        self._store = store
        self.id = doc_id

    def get(self, transaction=None) -> FakeDocument:
        return FakeDocument(self.id, self._store.get(self.id))


class FakeCollection:
    def __init__(self, store: dict) -> None:
        self._store = store

    def document(self, doc_id: str) -> FakeReference:
        return FakeReference(self._store, doc_id)

    def where(self, *, filter):   # noqa: A002 — 실제 SDK의 keyword 이름이다
        """`FieldFilter` 하나만 지원한다. 실제 SDK와 같은 속성을 읽는다
        (`field_path` · `op_string` · `value` — 설치본에서 확인했다).

        **정말로 걸러야 한다.** 필터가 통과만 하고 아무것도 안 걸러내면
        판매자 목록 test가 남의 draft 유출을 잡지 못한다.
        """
        assert filter.op_string == "==", f"이 fake는 ==만 안다: {filter.op_string}"
        return FakeQuery(self._store, filter.field_path, filter.value)

    def stream(self):
        return FakeQuery(self._store, None, None).stream()

    def order_by(self, field: str, direction: str = "ASCENDING", **kwargs):
        return FakeQuery(self._store, None, None).order_by(field, direction)


class FakeQuery:
    """`where(...)` 또는 `order_by(...)` **둘 중 하나만** 있는 최소 query."""

    def __init__(self, store: dict, field: str | None, value) -> None:
        self._store = store
        self._field = field
        self._value = value
        self._order: list[tuple[str, bool]] = []
        self._after: str | None = None
        self._limit: int | None = None

    def where(self, *, filter):   # noqa: A002
        raise AssertionError("이 fake는 조건 하나만 지원한다 — composite index가 필요해진다")

    def order_by(self, field: str, direction: str = "ASCENDING", **kwargs):
        """정렬은 **필터가 없을 때만** 허용한다.

        `where` + `order_by`는 composite index를 요구하므로 원래대로 막는다.
        필터 없는 단일 field 정렬은 Firestore가 자동으로 만드는 index로 되므로
        production에 새 index를 요구하지 않는다 — 운영 목록(`list_for_admin`)이 그 경우다.
        """
        assert self._field is None, "where + order_by는 composite index를 요구한다"
        self._order.append((field, str(direction).upper().startswith("DESC")))
        return self

    def start_after(self, anchor):
        self._after = anchor.id
        return self

    def limit(self, count: int):
        self._limit = count
        return self

    def stream(self):
        rows = [
            (doc_id, data)
            for doc_id, data in list(self._store.items())
            if self._field is None or data.get(self._field) == self._value
        ]
        for field, descending in reversed(self._order):
            rows.sort(
                key=lambda row: row[0] if field == "__name__" else row[1].get(field),
                reverse=descending,
            )
        if self._after is not None:
            ids = [x[0] for x in rows]
            rows = rows[ids.index(self._after) + 1:] if self._after in ids else []
        if self._limit is not None:
            rows = rows[: self._limit]
        for doc_id, data in rows:
            yield FakeSnapshot(doc_id, data)


class FakeSnapshot:
    """`stream()`이 내놓는 문서 하나."""

    def __init__(self, doc_id: str, data: dict) -> None:
        self.id = doc_id
        self._data = data
        self.exists = True

    def to_dict(self) -> dict:
        return dict(self._data)


class FakeDatabase:
    """`collection().document().get()`과 transaction만 있는 최소 db."""

    def __init__(self) -> None:
        self.data: dict[str, dict] = {}
        self.transactions: list = []

    def collection(self, name: str) -> FakeCollection:
        return FakeCollection(self.data.setdefault(name, {}))

    def transaction(self):
        tx = FakeFirestoreTransaction(self, aborts=self.aborts)
        self.transactions.append(tx)
        return tx

    aborts = 0


class FakeFirestoreTransaction:
    """실제 `@firestore.transactional`이 부르는 것만 구현한다."""

    _max_attempts = 5
    _read_only = False

    def __init__(self, db: FakeDatabase, aborts: int) -> None:
        self._db = db
        self._aborts = aborts
        self.staged: list = []
        self.commits = 0

    def _clean_up(self) -> None:
        self._write_pbs = []
        self._id = None
        # 이전 attempt의 staged write는 버린다.
        self.staged = []

    def _begin(self, retry_id=None) -> None:
        self._id = b"tx"

    def _commit(self):
        from google.api_core import exceptions

        if self._aborts > 0:
            self._aborts -= 1
            raise exceptions.Aborted("simulated contention")
        for write in self.staged:
            write()
        self.staged = []
        self.commits += 1
        return []

    def _rollback(self) -> None:
        self.staged = []

    # --- store가 부르는 부분 ---

    def create(self, reference, document) -> None:
        def write():
            if reference.id in reference._store:
                raise KeyError(reference.id)
            reference._store[reference.id] = dict(document)

        self.staged.append(write)

    def update(self, reference, changes) -> None:
        self.staged.append(lambda: reference._store[reference.id].update(changes))

    def delete(self, reference) -> None:
        self.staged.append(lambda: reference._store.pop(reference.id, None))

    def set(self, reference, document) -> None:
        self.staged.append(lambda: reference._store.__setitem__(reference.id, dict(document)))


@pytest.fixture
def firestore_like_store():
    from app.marketplace.firestore_store import FirestoreMarketplaceStore
    from app.marketplace.store import LISTINGS

    db = FakeDatabase()
    db.data[LISTINGS] = {
        "live": {
            "sellerUserId": SELLER,
            "contentType": "mirror",
            "title": "제목",
            "description": "",
            "priceShards": 0,
            "snapshotId": "snap",
            "status": "published",
            "publishFeePaid": True,
            "downloadCount": 0,
            "likeCount": 0,
            "createdAt": datetime(2026, 8, 1, tzinfo=timezone.utc),
            "updatedAt": datetime(2026, 8, 1, tzinfo=timezone.utc),
            "publishedAt": datetime(2026, 8, 1, tzinfo=timezone.utc),
            "schemaVersion": 1,
        }
    }
    return FirestoreMarketplaceStore(db), db


@pytest.mark.parametrize("aborts", [0, 1, 3])
def test_firestore_like_survives_aborted_retry(firestore_like_store, aborts):
    from app.marketplace.store import LIKES, LISTINGS

    store, db = firestore_like_store
    db.aborts = aborts

    result = store.like("live", LIKER)

    assert (result.liked, result.changed, result.like_count) == (True, True, 1)
    assert db.data[LISTINGS]["live"]["likeCount"] == 1, "재시도가 count를 두 번 올렸다"
    assert len(db.data.get(LIKES, {})) == 1, "관계가 두 개 생겼다"


@pytest.mark.parametrize("aborts", [0, 1, 3])
def test_firestore_unlike_survives_aborted_retry(firestore_like_store, aborts):
    from app.marketplace.store import LIKES, LISTINGS

    store, db = firestore_like_store
    store.like("live", LIKER)
    db.aborts = aborts

    result = store.unlike("live", LIKER)

    assert (result.liked, result.changed, result.like_count) == (False, True, 0)
    assert db.data[LISTINGS]["live"]["likeCount"] == 0, "재시도가 count를 두 번 내렸다"
    assert db.data.get(LIKES, {}) == {}


def test_firestore_repeated_like_is_idempotent(firestore_like_store):
    from app.marketplace.store import LISTINGS

    store, db = firestore_like_store

    first = store.like("live", LIKER)
    second = store.like("live", LIKER)

    assert (first.changed, second.changed) == (True, False)
    assert db.data[LISTINGS]["live"]["likeCount"] == 1


def test_firestore_self_like_is_refused(firestore_like_store):
    from app.marketplace.store import LISTINGS

    store, db = firestore_like_store

    with pytest.raises(SelfLike):
        store.like("live", SELLER)

    assert db.data[LISTINGS]["live"]["likeCount"] == 0


# MARK: - snapshot asset (B-7F)
#
# 보는 것 셋:
# 1. 같은 snapshotId를 **어느 경로로도 덮어쓸 수 없는가**
# 2. 판매자가 내려도 산 사람이 계속 받는가
# 3. 소유권 없는 사람이 원본을 **어떤 endpoint로도** 못 받는가

import json as _json   # noqa: E402

from app.marketplace.assets import (   # noqa: E402
    MAX_IMAGE_BYTES,
    MAX_MANIFEST_BYTES,
    PNG_MAGIC,
    AssetAlreadyExists,
    AssetError,
    AssetNotFound,
    AssetTooLarge,
    InMemoryMarketplaceAssetStorage,
    asset_key,
    checked_package,
    manifest_key,
    preview_key,
)
from app.marketplace.models import Snapshot   # noqa: E402

ASSET_A = "A0000001-0000-4000-A000-000000000001"
ASSET_B = "B0000002-0000-4000-A000-000000000002"
PNG = PNG_MAGIC + b"pretend-pixels"

# 아래 fixture는 **실제 client `encode(to:)`가 내는 모양**이다(B-7F.1에서 재audit).
# 서버가 상상한 schema가 아니다 — `assetIds` 같은 필드는 client에 없다.

STYLE = {
    "frame": {"red": 1.0, "green": 0.9, "blue": 0.9, "alpha": 1.0},
    "insets": {"top": 0.1, "right": 0.05, "bottom": 0.1, "left": 0.05},
    "doodles": [],
    "frameVisible": True,
}


def photo_sticker(asset_id: str, *, object_id: str = "11111111-1111-4111-8111-111111111111") -> dict:
    """`StickerObject` + `StickerSource.photo`. `id`는 **오브젝트 식별자**이고
    asset이 아니다 — 참조는 `source.assetID`뿐이다."""
    return {
        "id": object_id,
        "source": {"kind": "photo", "assetID": asset_id, "aspectRatio": 1.0},
        "frame": {"x": 0.5, "y": 0.5, "width": 0.2, "height": 0.2},
        "rotation": 0.0, "opacity": 1.0, "zIndex": 0,
        "isLocked": False, "isFlippedHorizontally": False,
    }


def builtin_sticker(name: str = "heart") -> dict:
    """내장 스티커. **asset을 참조하지 않는다.**"""
    return {
        "id": "33333333-3333-4333-8333-333333333333",
        "source": {"kind": "builtIn", "sticker": name},
        "frame": {"x": 0.3, "y": 0.3, "width": 0.1, "height": 0.1},
        "rotation": 0.0, "opacity": 1.0, "zIndex": 1,
        "isLocked": False, "isFlippedHorizontally": False,
    }


def artwork(asset_id: str, *, object_id: str = "44444444-4444-4444-8444-444444444444") -> dict:
    """`ImportedArtworkObject`. `assetID`가 참조다."""
    return {"id": object_id, "assetID": asset_id, "opacity": 1.0, "zIndex": 2}


def text_object(text: str) -> dict:
    """`TextObject`. `text`는 사용자가 쓰는 100자 장식 문구다."""
    return {
        "id": "22222222-2222-4222-8222-222222222222",
        "text": text, "center": {"x": 0.5, "y": 0.8}, "fontSize": 0.088,
        "style": "basic", "alignment": "center",
        "color": {"red": 0, "green": 0, "blue": 0, "alpha": 1},
        "rotation": 0.0, "opacity": 1.0, "zIndex": 0, "isLocked": False,
    }


def mirror_document(*, stickers=None, artworks=None, texts=None, **extra) -> dict:
    """`MyMirror.encode`가 적는 8개 key 그대로."""
    document = {
        "id": "mirror-1", "name": "내 거울", "origin": "made", "style": STYLE,
        "strokes": [],
        "stickers": stickers or [],
        "texts": texts or [],
        "importedArtworks": artworks or [],
    }
    document.update(extra)
    return document


def sticker_document(*, final: str | None = None, stickers=None,
                     artworks=None, **extra) -> dict:
    """`StickerProject.encode`가 적는 모양. `design`은 `MirrorDesign`이다.

    `generationIDs`는 비어 있으면 client가 **적지 않는다** — 여기서도 뺀다.
    """
    document = {
        "id": "sticker-1", "name": "내 스티커",
        "createdAt": 0.0, "updatedAt": 0.0,
        "design": {
            "id": "sticker-1", "name": "내 스티커", "style": STYLE,
            "strokes": [], "stickers": stickers or [], "texts": [],
            "importedArtworks": artworks or [], "canvas": "sticker",
        },
        "origin": "made",
    }
    if final is not None:
        document["finalAssetID"] = final
    document.update(extra)
    return document


def as_bytes(document: dict) -> bytes:
    return _json.dumps(document, ensure_ascii=False).encode()


def manifest_bytes(asset_ids: list[str] | None = None, **extra) -> bytes:
    """참조 asset이 있는 거울 manifest. 참조는 **구조 안에** 들어간다."""
    ids = asset_ids or []
    return as_bytes(mirror_document(stickers=[photo_sticker(x) for x in ids], **extra))


@pytest.fixture
def storage() -> InMemoryMarketplaceAssetStorage:
    return InMemoryMarketplaceAssetStorage()


@pytest.fixture
def asset_service(store, shards, storage) -> MarketplaceService:
    return MarketplaceService(store, shards, assets=storage)


def upload(
    service: MarketplaceService,
    *,
    kind: ContentType = ContentType.MIRROR,
    asset_ids: list[str] | None = None,
    owner: str = SELLER,
) -> Snapshot:
    ids = asset_ids or []
    if kind is ContentType.MIRROR:
        document = mirror_document(stickers=[photo_sticker(x, object_id=_object_id(i))
                                            for i, x in enumerate(ids)])
    else:
        # 스티커는 완성 PNG 하나 + 나머지는 캔버스 안 사진 cutout이다.
        document = sticker_document(
            final=ids[0] if ids else None,
            stickers=[photo_sticker(x, object_id=_object_id(i))
                      for i, x in enumerate(ids[1:])],
        )
    package = checked_package(
        content_type=kind.value,
        manifest=as_bytes(document),
        preview=PNG,
        assets={x: PNG for x in ids},
    )
    return service.create_snapshot(user(owner), content_type=kind.value, package=package)


def _object_id(index: int) -> str:
    """오브젝트 식별자는 서로 달라야 한다 — asset id와는 다른 것이다."""
    return f"1111111{index}-1111-4111-8111-111111111111"


def listing_with_snapshot(store, snapshot: Snapshot, *, price: int = 30,
                          status: ListingStatus = ListingStatus.PUBLISHED) -> Listing:
    listing = published(store, "with-asset", kind=snapshot.content_type, price=price, status=status)
    replaced = type(listing)(**{**listing.__dict__, "snapshot_id": snapshot.id})
    store.listings[listing.id] = replaced
    return replaced


# MARK: - 업로드 (§8, §12, §13)


@pytest.mark.parametrize("kind", list(ContentType))
def test_snapshot_upload_stores_every_object(asset_service, storage, kind):
    snapshot = upload(asset_service, kind=kind, asset_ids=[ASSET_A])

    assert snapshot.content_type is kind
    assert snapshot.seller_user_id == SELLER
    assert snapshot.asset_count == 1
    assert snapshot.total_bytes > 0
    assert snapshot.is_complete

    assert manifest_key(snapshot.id) in storage.objects
    assert preview_key(snapshot.id) in storage.objects
    assert asset_key(snapshot.id, ASSET_A) in storage.objects
    assert storage.objects[preview_key(snapshot.id)].content_type == "image/png"
    assert storage.objects[manifest_key(snapshot.id)].content_type == "application/json"


def test_snapshot_id_is_server_generated(asset_service):
    first = upload(asset_service)
    second = upload(asset_service)
    assert first.id != second.id
    assert len(first.id) == 36


def test_checksum_is_server_computed(asset_service):
    import hashlib

    package = checked_package(content_type="mirror", manifest=manifest_bytes(), preview=PNG, assets={})
    snapshot = asset_service.create_snapshot(
        user(), content_type="mirror", package=package
    )
    assert snapshot.manifest_checksum == hashlib.sha256(manifest_bytes()).hexdigest()


def test_snapshot_binds_to_the_uploader(asset_service, store):
    snapshot = upload(asset_service, owner=SELLER)
    assert store.snapshots[snapshot.id].seller_user_id == SELLER


def test_unknown_content_type_is_refused(asset_service, storage):
    package = checked_package(content_type="mirror", manifest=manifest_bytes(), preview=PNG, assets={})
    with pytest.raises(InvalidListing):
        asset_service.create_snapshot(user(), content_type="album", package=package)
    assert storage.objects == {}, "거절됐는데 object가 올라갔다"


# MARK: - 불변성 (§3, §6)


def test_storage_refuses_to_overwrite(storage):
    storage.put("k", b"first", "image/png")
    with pytest.raises(AssetAlreadyExists):
        storage.put("k", b"second", "image/png")
    assert storage.get("k").data == b"first"


def test_snapshot_objects_never_share_a_key(asset_service, storage):
    first = upload(asset_service, asset_ids=[ASSET_A])
    second = upload(asset_service, asset_ids=[ASSET_A])

    assert manifest_key(first.id) != manifest_key(second.id)
    assert asset_key(first.id, ASSET_A) != asset_key(second.id, ASSET_A)
    assert len(storage.objects) == 6


def test_gcs_storage_uses_create_only_precondition():
    """`if_generation_match=0` — 저장소 수준에서 덮어쓰기를 막는다."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parent.parent / "app/marketplace/assets.py"
    ).read_text()
    body = source[source.index("class GCSMarketplaceAssetStorage"):]
    assert "if_generation_match=0" in body
    assert "PreconditionFailed" in body


def test_no_snapshot_delete_api(client):
    """§29 — 구매자 권리와 묶여 있으므로 삭제 API를 만들지 않는다."""
    for path in ["/marketplace/snapshots/abc", "/marketplace/snapshots"]:
        assert client.delete(path).status_code in {404, 405}


# MARK: - 반쪽 업로드 (§14)


def test_partial_upload_leaves_no_snapshot_document(store, shards, storage):
    """가장 중요 — object가 반쪽만 올라가면 **문서를 만들지 않는다.**

    문서가 없으므로 어떤 listing도 그 snapshot을 참조할 수 없다.
    """
    class FailsOnPreview(InMemoryMarketplaceAssetStorage):
        def put(self, key, data, content_type):
            if key.endswith("preview.png"):
                raise AssetError("upload failed")
            super().put(key, data, content_type)

    failing = FailsOnPreview()
    service = MarketplaceService(store, shards, assets=failing)
    package = checked_package(content_type="mirror", manifest=manifest_bytes(), preview=PNG, assets={})

    with pytest.raises(AssetError):
        service.create_snapshot(user(), content_type="mirror", package=package)

    assert store.snapshots == {}, "반쪽 snapshot이 완료로 기록됐다"
    # 이미 올라간 것은 best-effort로 치운다.
    assert failing.objects == {}, "orphan이 남았다"


def test_incomplete_legacy_snapshot_delivers_nothing(store, shards, storage, service):
    """§26 — B-7C 시절 fixture(asset 없는 문서)로는 미리보기를 만들지 않는다."""
    legacy = Snapshot(id="legacy", seller_user_id=SELLER, content_type=ContentType.MIRROR)
    store.snapshots["legacy"] = legacy
    listing = published(store, "old", price=0)
    store.listings["old"] = type(listing)(**{**listing.__dict__, "snapshot_id": "legacy"})

    asset_service = MarketplaceService(store, shards, assets=storage)
    with pytest.raises(SnapshotNotFound):
        asset_service.preview("old")


# MARK: - 검증 (§10, §11, §25)


def test_oversized_manifest_is_refused():
    with pytest.raises(AssetTooLarge):
        checked_package(content_type="mirror", manifest=b"x" * (MAX_MANIFEST_BYTES + 1), preview=PNG, assets={})


def test_oversized_image_is_refused():
    big = PNG_MAGIC + b"x" * MAX_IMAGE_BYTES
    with pytest.raises(AssetTooLarge):
        checked_package(content_type="mirror", manifest=manifest_bytes(), preview=big, assets={})
    with pytest.raises(AssetTooLarge):
        checked_package(
            content_type="mirror", manifest=manifest_bytes([ASSET_A]),
            preview=PNG, assets={ASSET_A: big},
        )


def test_oversized_snapshot_is_refused():
    from app.marketplace.assets import MAX_SNAPSHOT_BYTES

    ids = [f"A000000{i}-0000-4000-A000-000000000001" for i in range(1, 9)]
    chunk = PNG_MAGIC + b"x" * (MAX_SNAPSHOT_BYTES // 8)
    with pytest.raises(AssetTooLarge):
        checked_package(
            content_type="mirror", manifest=manifest_bytes(ids),
            preview=PNG, assets={x: chunk for x in ids},
        )


@pytest.mark.parametrize("data", [b"notpng", b"", b"GIF89a"])
def test_non_png_images_are_refused(data):
    """확장자를 믿지 않는다 — 실제 바이트를 본다."""
    with pytest.raises((AssetError, AssetTooLarge)):
        checked_package(content_type="mirror", manifest=manifest_bytes(), preview=data, assets={})


@pytest.mark.parametrize("manifest", [b"not json", b"[]", b"\xff\xfe invalid", b""])
def test_malformed_manifest_is_refused(manifest):
    with pytest.raises((AssetError, AssetTooLarge)):
        checked_package(content_type="mirror", manifest=manifest, preview=PNG, assets={})


@pytest.mark.parametrize(
    "poison",
    [
        "../../etc/passwd",
        "..\\\\windows",
        "file:///etc/passwd",
        "http://evil.example/x.png",
        "https://evil.example/x.png",
        "javascript:alert(1)",
        "data:image/png;base64,AAAA",
        "<script>alert(1)</script>",
    ],
)
def test_manifest_cannot_reference_outside_content(poison):
    """§25 — 템플릿은 앱 안의 안전한 local 콘텐츠만 표현한다."""
    with pytest.raises(AssetError):
        checked_package(content_type="mirror", manifest=manifest_bytes(note=poison), preview=PNG, assets={})


@pytest.mark.parametrize("asset_id", ["../evil", "a/b", "not-a-uuid", "", "..%2f"])
def test_asset_ids_must_be_uuids(asset_id):
    """경로 조작이 성립할 문자가 애초에 통과하지 못한다."""
    with pytest.raises(AssetError):
        checked_package(
            content_type="mirror", manifest=manifest_bytes([asset_id]),
            preview=PNG, assets={asset_id: PNG},
        )
    with pytest.raises(AssetError):
        asset_key("snap", asset_id)


def test_referenced_and_uploaded_assets_must_match():
    """빠진 이미지가 있으면 다른 기기에서 복원할 때 조용히 깨진다."""
    with pytest.raises(AssetError):
        checked_package(content_type="mirror", manifest=manifest_bytes([ASSET_A]),
                        preview=PNG, assets={})
    with pytest.raises(AssetError):
        checked_package(content_type="mirror", manifest=manifest_bytes([]),
                        preview=PNG, assets={ASSET_A: PNG})


# MARK: - 공개 미리보기 (§16)


def test_published_preview_is_public(asset_service, store):
    snapshot = upload(asset_service)
    listing_with_snapshot(store, snapshot)

    stored = asset_service.preview("with-asset")

    assert stored.data == PNG
    assert stored.content_type == "image/png"


@pytest.mark.parametrize("status", [ListingStatus.DRAFT, ListingStatus.UNLISTED])
def test_hidden_listing_has_no_public_preview(asset_service, store, status):
    snapshot = upload(asset_service)
    listing = listing_with_snapshot(store, snapshot, status=status)
    if status is ListingStatus.DRAFT:
        store.listings[listing.id] = type(listing)(
            **{**listing.__dict__, "published_at": None}
        )

    with pytest.raises(ListingNotFound):
        asset_service.preview(listing.id)


def test_missing_listing_has_no_preview(asset_service):
    with pytest.raises(ListingNotFound):
        asset_service.preview("nope")


# MARK: - 템플릿 전달 (§17 ~ §20)


def test_seller_can_fetch_the_template(asset_service, store):
    snapshot = upload(asset_service)
    listing_with_snapshot(store, snapshot)

    stored = asset_service.template(user(SELLER), "with-asset")

    assert _json.loads(stored.data)["name"] == "내 거울"


def test_owner_can_fetch_the_template(asset_service, store, shards):
    seed(shards, 100, who=BUYER)
    snapshot = upload(asset_service)
    listing_with_snapshot(store, snapshot)
    asset_service.purchase(user(BUYER), "with-asset")

    stored = asset_service.template(user(BUYER), "with-asset")

    assert _json.loads(stored.data)["id"] == "mirror-1"


def test_free_owner_can_fetch_the_template(asset_service, store):
    """§19 — 무료도 소유권이 생기므로 규칙이 같다."""
    snapshot = upload(asset_service)
    listing_with_snapshot(store, snapshot, price=0)
    asset_service.purchase(user(BUYER), "with-asset")

    assert asset_service.template(user(BUYER), "with-asset").data


def test_stranger_cannot_fetch_the_template(asset_service, store):
    """§17 — 구경만 한 사람은 원본을 받을 수 없다."""
    snapshot = upload(asset_service)
    listing_with_snapshot(store, snapshot)

    # 공개 미리보기는 되지만 원본은 안 된다.
    assert asset_service.preview("with-asset").data
    with pytest.raises(ListingNotFound):
        asset_service.template(user(OTHER), "with-asset")
    with pytest.raises(ListingNotFound):
        asset_service.template_asset(user(OTHER), "with-asset", ASSET_A)


def test_owner_keeps_access_after_unpublish(asset_service, store, shards):
    """§18 — 판매자가 내려도 산 사람은 계속 받는다."""
    seed(shards, 100, who=BUYER)
    snapshot = upload(asset_service, asset_ids=[ASSET_A])
    listing_with_snapshot(store, snapshot)
    asset_service.purchase(user(BUYER), "with-asset")

    asset_service.unpublish(user(SELLER), "with-asset")

    # 공개 미리보기는 사라진다.
    with pytest.raises(ListingNotFound):
        asset_service.preview("with-asset")
    # 하지만 구매자는 그대로 받는다.
    assert asset_service.template(user(BUYER), "with-asset").data
    assert asset_service.template_asset(user(BUYER), "with-asset", ASSET_A).data == PNG


def test_template_asset_requires_a_known_asset(asset_service, store, shards):
    seed(shards, 100, who=BUYER)
    snapshot = upload(asset_service, asset_ids=[ASSET_A])
    listing_with_snapshot(store, snapshot)
    asset_service.purchase(user(BUYER), "with-asset")

    with pytest.raises(AssetNotFound):
        asset_service.template_asset(
            user(BUYER), "with-asset", "B0000002-0000-4000-A000-000000000002"
        )
    with pytest.raises(AssetError):
        asset_service.template_asset(user(BUYER), "with-asset", "../evil")


# MARK: - counter 무영향 (§22, §23)


def test_asset_delivery_never_moves_counters(asset_service, store, shards):
    seed(shards, 100, who=BUYER)
    snapshot = upload(asset_service, asset_ids=[ASSET_A])
    listing_with_snapshot(store, snapshot)
    asset_service.purchase(user(BUYER), "with-asset")
    asset_service.like(user(LIKER), "with-asset")
    before = store.listings["with-asset"]

    for _ in range(10):
        asset_service.preview("with-asset")
        asset_service.template(user(BUYER), "with-asset")
        asset_service.template_asset(user(BUYER), "with-asset", ASSET_A)

    after = store.listings["with-asset"]
    assert after.download_count == before.download_count == 1
    assert after.like_count == before.like_count == 1


# MARK: - listing 연결 (§15)


def test_listing_cannot_use_someone_elses_snapshot(asset_service, store):
    snapshot = upload(asset_service, owner=SELLER)

    with pytest.raises(SnapshotNotFound):
        asset_service.create_draft(
            user(OTHER), content_type="mirror", title="훔친 것",
            description="", price_shards=0, snapshot_id=snapshot.id,
        )


def test_draft_requires_an_uploaded_snapshot(asset_service):
    with pytest.raises(SnapshotNotFound):
        asset_service.create_draft(
            user(), content_type="mirror", title="제목",
            description="", price_shards=0, snapshot_id="made-up",
        )


# MARK: - HTTP (§21, §24)


@pytest.fixture
def asset_client(store, shard_store, storage):
    from fastapi.testclient import TestClient

    from app.core.config import Settings
    from app.main import create_app

    app = create_app(
        Settings(app_env="local"),
        shard_store=shard_store,
        marketplace_store=store,
        marketplace_assets=storage,
    )
    return TestClient(app, raise_server_exceptions=False)


def test_asset_endpoints_require_the_right_auth(asset_client):
    assert asset_client.post("/marketplace/snapshots").status_code == 401
    assert asset_client.get("/marketplace/listings/abc/template").status_code == 401
    assert asset_client.get(
        "/marketplace/listings/abc/template/assets/" + ASSET_A
    ).status_code == 401
    # 미리보기는 공개다 — 없는 상품이라 404.
    assert asset_client.get("/marketplace/listings/abc/preview").status_code == 404


def test_public_preview_streams_bytes(asset_client, asset_service, store):
    snapshot = upload(asset_service)
    listing_with_snapshot(store, snapshot)

    response = asset_client.get("/marketplace/listings/with-asset/preview")

    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-length"] == str(len(PNG))
    assert "immutable" in response.headers["cache-control"]


def test_responses_never_leak_storage_details(asset_client, asset_service, store):
    """§21 — bucket · gs:// · object key · signed URL을 client에 주지 않는다."""
    snapshot = upload(asset_service)
    listing_with_snapshot(store, snapshot)

    preview = asset_client.get("/marketplace/listings/with-asset/preview")
    detail = asset_client.get("/marketplace/listings/with-asset").text

    for banned in ["gs://", "bucket", "marketplace/snapshots", "googleapis.com", "X-Goog"]:
        assert banned not in detail
        assert banned not in str(preview.headers)


def test_snapshot_response_hides_object_keys():
    from app.api.marketplace import SnapshotResponse

    fields = set(SnapshotResponse.model_fields)
    assert fields == {
        "snapshot_id", "content_type", "asset_count", "total_bytes", "manifest_checksum"
    }
    for banned in ["manifest_object", "preview_object", "bucket", "object_key"]:
        assert banned not in fields


def test_no_raw_snapshot_endpoint(asset_client):
    """§24 — snapshotId를 직접 넣어 받는 경로를 만들지 않는다."""
    for path in [
        "/marketplace/snapshots/abc/raw",
        "/marketplace/snapshots/abc",
        "/marketplace/snapshots/abc/manifest",
    ]:
        assert asset_client.get(path).status_code in {404, 405}


def test_anonymous_multipart_upload_is_refused(asset_client):
    """실제 multipart 경로로도 익명 업로드가 통하지 않는다."""
    response = asset_client.post(
        "/marketplace/snapshots",
        data={"contentType": "mirror"},
        files={
            "manifest": ("manifest.json", manifest_bytes(), "application/json"),
            "preview": ("preview.png", PNG, "image/png"),
        },
    )
    assert response.status_code == 401


# MARK: - bucket 격리 (§4, §5)


def test_marketplace_bucket_is_separate_from_the_ai_bucket():
    """§4 — AI 결과 bucket을 재사용하지 않는다.

    그쪽은 7일 lifecycle이다. 판매한 템플릿이 7일 뒤 사라지면 구매자가 산 것을
    잃는다 — 환불로도 되돌릴 수 없다.
    """
    from app.core.config import Settings, load_settings

    import dataclasses

    fields = {f.name: f for f in dataclasses.fields(Settings)}
    assert "marketplace_asset_bucket" in fields
    assert fields["marketplace_asset_bucket"].default == "", "기본값이 있으면 실수로 공유된다"

    shared = load_settings(
        {
            "APP_ENV": "local",
            "AI_RESULT_BUCKET": "ggumirror-ai-results",
            "MARKETPLACE_ASSET_BUCKET": "ggumirror-marketplace-assets",
        }
    )
    assert shared.marketplace_asset_bucket != shared.ai_result_bucket

    # env가 없으면 조용히 AI bucket으로 흐르지 않는다.
    empty = load_settings({"APP_ENV": "local", "AI_RESULT_BUCKET": "ggumirror-ai-results"})
    assert empty.marketplace_asset_bucket == ""


def test_asset_prefix_is_not_shared_with_ai_results():
    """§4 — prefix도 나눈다. 같은 bucket에 섞일 여지를 두지 않는다."""
    from app.marketplace.assets import PREFIX

    assert PREFIX == "marketplace/snapshots"
    assert "ai" not in PREFIX.split("/")
    for key in [manifest_key("s"), preview_key("s"), asset_key("s", ASSET_A)]:
        assert key.startswith("marketplace/snapshots/s/")


def test_missing_bucket_fails_closed(store, shards, shard_store):
    """§5 — bucket이 없으면 조용히 성공하지 않는다. 거짓 preview를 만들지 않는다."""
    from fastapi.testclient import TestClient

    from app.core.config import Settings
    from app.main import create_app
    from app.marketplace.assets import AssetStorageUnavailable

    blind = MarketplaceService(store, shards, assets=None)
    package = checked_package(content_type="mirror", manifest=manifest_bytes(), preview=PNG, assets={})

    with pytest.raises(AssetStorageUnavailable):
        blind.create_snapshot(user(), content_type="mirror", package=package)

    snapshot = Snapshot(id="s", seller_user_id=SELLER, content_type=ContentType.MIRROR,
                        manifest_checksum="x", asset_count=0, total_bytes=1)
    store.snapshots["s"] = snapshot
    listing = published(store, "live")
    store.listings["live"] = type(listing)(**{**listing.__dict__, "snapshot_id": "s"})
    with pytest.raises(AssetStorageUnavailable):
        blind.preview("live")

    # HTTP에서는 503 — 200에 빈 이미지를 주지 않는다.
    app = create_app(
        Settings(app_env="local", marketplace_asset_bucket=""),
        shard_store=shard_store,
        marketplace_store=store,
    )
    with TestClient(app, raise_server_exceptions=False) as http:
        assert http.get("/marketplace/listings/live/preview").status_code == 503


# MARK: - package contract (B-7F.1)
#
# 세 가지를 못 하게 만드는 것이 전부다:
# 1. 스티커를 거울이라고 **label만 바꿔** 등록
# 2. manifest가 참조하는데 **업로드되지 않은** asset
# 3. manifest가 쓰지 않는 asset을 **몰래 끼워 넣기**

from app.marketplace.assets import (   # noqa: E402
    MAX_MANIFEST_DEPTH,
    PROSE_KEYS,
    referenced_asset_ids,
)


def package(content_type: str, document: dict, assets: dict[str, bytes]):
    return checked_package(
        content_type=content_type, manifest=as_bytes(document), preview=PNG, assets=assets
    )


# MARK: - 타입 결합 (§3)


def test_mirror_manifest_is_accepted():
    result = package("mirror", mirror_document(), {})
    assert result.assets == {}


def test_sticker_manifest_is_accepted():
    result = package("sticker", sticker_document(final=ASSET_A), {ASSET_A: PNG})
    assert set(result.assets) == {ASSET_A}


def test_sticker_manifest_cannot_be_labelled_mirror():
    """가장 중요 — `StickerProject` JSON을 `contentType=mirror`로 등록할 수 없다.

    `design`이 있으면 거울이 아니다. label만 바꿔 통과시키면 구매자 앱이
    `MyMirror`로 decode하다 실패하고, snapshot은 불변이라 고칠 수 없다.
    """
    with pytest.raises(AssetError):
        package("mirror", sticker_document(final=ASSET_A), {ASSET_A: PNG})
    # asset이 없어도 마찬가지다 — 크기·PNG가 아니라 **모양**이 문제다.
    with pytest.raises(AssetError):
        package("mirror", sticker_document(), {})


def test_mirror_manifest_cannot_be_labelled_sticker():
    with pytest.raises(AssetError):
        package("sticker", mirror_document(), {})
    with pytest.raises(AssetError):
        package("sticker", mirror_document(stickers=[photo_sticker(ASSET_A)]), {ASSET_A: PNG})


def test_unknown_content_type_is_refused_by_the_validator():
    for label in ["album", "MIRROR", "", "mirror ", "sticker\n"]:
        with pytest.raises(AssetError):
            package(label, mirror_document(), {})


@pytest.mark.parametrize(
    "broken",
    [
        {"id": 1},                       # id가 문자열이 아니다
        {"name": None},                  # name이 없는 것과 같다
        {"style": "pink"},               # style이 object가 아니다
        {"style": None},
    ],
)
def test_mirror_without_the_required_shape_is_refused(broken):
    with pytest.raises(AssetError):
        package("mirror", {**mirror_document(), **broken}, {})


def test_mirror_missing_style_entirely_is_refused():
    document = mirror_document()
    del document["style"]
    with pytest.raises(AssetError):
        package("mirror", document, {})


@pytest.mark.parametrize("broken", [{"design": "x"}, {"design": None}, {"id": []}])
def test_sticker_without_the_required_shape_is_refused(broken):
    with pytest.raises(AssetError):
        package("sticker", {**sticker_document(), **broken}, {})


def test_sticker_design_must_carry_a_style():
    document = sticker_document()
    del document["design"]["style"]
    with pytest.raises(AssetError):
        package("sticker", document, {})


def test_service_rechecks_the_type_binding(store, shards, storage):
    """API를 우회해도 결합이 유지된다.

    검증을 한 호출 경로에만 두면, 다른 경로가 생기는 순간 구매자가 못 읽는
    바이트가 팔린다. snapshot은 불변이라 되돌릴 수 없다.
    """
    service = MarketplaceService(store, shards, assets=storage)
    # mirror로 검증받은 package를 sticker라고 주장한다.
    valid = package("mirror", mirror_document(), {})

    with pytest.raises(InvalidListing):
        service.create_snapshot(user(), content_type="sticker", package=valid)

    assert store.snapshots == {}
    assert storage.objects == {}, "거절됐는데 object가 올라갔다"


# MARK: - 전방 호환 (§4)


def test_unknown_additional_fields_are_accepted():
    """client가 나중에 optional field를 더해도 옛 backend가 깨지지 않는다.

    **key 동일성을 요구하지 않는다** — 핵심 key의 존재/타입만 본다.
    """
    result = package(
        "mirror",
        mirror_document(schemaVersion=3, mood="calm", futureFlags={"glow": True}),
        {},
    )
    assert result.manifest_checksum


def test_future_sticker_fields_are_accepted():
    assert package("sticker", sticker_document(final=ASSET_A, remix=True), {ASSET_A: PNG})


def test_omitted_optional_arrays_are_accepted():
    """client는 빈 `generationIDs`를 적지 않고, v1 거울에는 `importedArtworks`가 없다."""
    document = mirror_document()
    del document["importedArtworks"]
    del document["texts"]
    assert package("mirror", document, {})


def test_present_but_wrong_typed_arrays_are_refused():
    """없는 것은 괜찮지만 **있는데 배열이 아니면** 우리가 아는 포맷이 아니다."""
    for key in ["stickers", "importedArtworks"]:
        with pytest.raises(AssetError):
            package("mirror", mirror_document(**{key: {"a": 1}}), {})


# MARK: - 참조 추출 (§5)


def test_mirror_photo_sticker_reference_is_extracted():
    assert referenced_asset_ids("mirror", mirror_document(
        stickers=[photo_sticker(ASSET_A)])) == {ASSET_A}


def test_mirror_imported_artwork_reference_is_extracted():
    assert referenced_asset_ids("mirror", mirror_document(
        artworks=[artwork(ASSET_B)])) == {ASSET_B}


def test_mirror_extracts_both_kinds():
    assert referenced_asset_ids("mirror", mirror_document(
        stickers=[photo_sticker(ASSET_A)], artworks=[artwork(ASSET_B)],
    )) == {ASSET_A, ASSET_B}


def test_builtin_stickers_reference_nothing():
    """내장 스티커·낙서·글씨·획은 파일을 참조하지 않는다."""
    assert referenced_asset_ids("mirror", mirror_document(
        stickers=[builtin_sticker()], texts=[text_object("안녕")],
    )) == set()


def test_object_ids_are_not_asset_references():
    """`stickers[].id` · `importedArtworks[].id`는 **오브젝트 식별자**다.

    둘 다 UUID라서 구분하지 않으면 존재하지 않는 PNG를 요구하게 된다.
    """
    document = mirror_document(
        stickers=[builtin_sticker()],
        artworks=[artwork(ASSET_B, object_id="99999999-9999-4999-8999-999999999999")],
    )
    references = referenced_asset_ids("mirror", document)
    assert references == {ASSET_B}
    assert "33333333-3333-4333-8333-333333333333" not in references
    assert "99999999-9999-4999-8999-999999999999" not in references


def test_sticker_final_asset_is_a_reference():
    """`finalAssetID` → `UserStickerAssets/<id>.png`. client GC가 이것만 살려둔다."""
    assert referenced_asset_ids("sticker", sticker_document(final=ASSET_A)) == {ASSET_A}


def test_absent_final_asset_is_not_invented():
    """optional이다. **없는 것을 가짜로 만들지 않는다.**"""
    assert referenced_asset_ids("sticker", sticker_document()) == set()
    assert referenced_asset_ids("sticker", sticker_document(finalAssetID=None)) == set()
    assert package("sticker", sticker_document(), {})


def test_sticker_design_references_are_extracted():
    """스티커 캔버스 안의 사진 cutout·외부 디자인도 참조다."""
    document = sticker_document(
        final=ASSET_A, stickers=[photo_sticker(ASSET_B)],
        artworks=[artwork("C0000003-0000-4000-A000-000000000003")],
    )
    assert referenced_asset_ids("sticker", document) == {
        ASSET_A, ASSET_B, "C0000003-0000-4000-A000-000000000003"
    }


def test_generation_ids_are_not_asset_references():
    """AI 생성 기록 id다 — 파일이 아니다. PNG를 요구하면 정상 스티커가 거절된다."""
    document = sticker_document(final=ASSET_A, generationIDs=["gen-1", "gen-2"])
    assert referenced_asset_ids("sticker", document) == {ASSET_A}
    assert package("sticker", document, {ASSET_A: PNG})


def test_photo_sticker_without_an_asset_id_is_refused():
    """client decode에서 `assetID`는 required다 — 없으면 열 수 없는 파일이다."""
    broken = photo_sticker(ASSET_A)
    del broken["source"]["assetID"]
    with pytest.raises(AssetError):
        package("mirror", mirror_document(stickers=[broken]), {})


def test_imported_artwork_without_an_asset_id_is_refused():
    broken = artwork(ASSET_B)
    del broken["assetID"]
    with pytest.raises(AssetError):
        package("mirror", mirror_document(artworks=[broken]), {})


# MARK: - 정확히 같은 집합 (§6)


def test_exact_asset_set_is_accepted():
    result = package(
        "mirror",
        mirror_document(stickers=[photo_sticker(ASSET_A)], artworks=[artwork(ASSET_B)]),
        {ASSET_A: PNG, ASSET_B: PNG},
    )
    assert set(result.assets) == {ASSET_A, ASSET_B}


def test_referenced_but_not_uploaded_is_refused():
    """manifest A,B / upload A → 거절. 구매자 기기에서 조용히 비어 보인다."""
    with pytest.raises(AssetError):
        package(
            "mirror",
            mirror_document(stickers=[photo_sticker(ASSET_A)], artworks=[artwork(ASSET_B)]),
            {ASSET_A: PNG},
        )


def test_uploaded_but_never_referenced_is_refused():
    """manifest A / upload A,B → 거절. 쓰지 않는 이미지를 몰래 넣을 수 없다."""
    with pytest.raises(AssetError):
        package("mirror", mirror_document(stickers=[photo_sticker(ASSET_A)]),
                {ASSET_A: PNG, ASSET_B: PNG})


def test_uploading_an_asset_a_reference_free_manifest_is_refused():
    """manifest 참조 없음 / upload A → 거절."""
    with pytest.raises(AssetError):
        package("mirror", mirror_document(), {ASSET_A: PNG})
    with pytest.raises(AssetError):
        package("sticker", sticker_document(), {ASSET_A: PNG})


def test_asset_free_package_is_accepted():
    """둘 다 비어 있으면 허용이다 — 그린 것만 있는 거울/스티커가 정상이다."""
    assert package("mirror", mirror_document(stickers=[builtin_sticker()]), {}).assets == {}
    assert package("sticker", sticker_document(), {}).assets == {}


def test_a_client_supplied_asset_list_is_not_authority():
    """`assetIds` 같은 목록을 보내도 **manifest 구조가 authority다.**

    (B-7F에서 서버가 발명했던 필드다. client는 그런 것을 적지 않는다.)
    """
    # 목록으로는 참조를 만들 수 없다.
    with pytest.raises(AssetError):
        package("mirror", mirror_document(assetIds=[ASSET_A]), {ASSET_A: PNG})
    # 목록으로 실제 참조를 지울 수도 없다.
    with pytest.raises(AssetError):
        package("mirror", mirror_document(stickers=[photo_sticker(ASSET_A)], assetIds=[]), {})


def test_repeated_reference_to_the_same_asset_is_accepted():
    """§8 — 같은 사진을 두 군데 얹는 것은 정상이다. upload는 **1개**다."""
    document = mirror_document(
        stickers=[
            photo_sticker(ASSET_A, object_id="55555555-5555-4555-8555-555555555555"),
            photo_sticker(ASSET_A, object_id="66666666-6666-4666-8666-666666666666"),
        ],
        artworks=[artwork(ASSET_A)],
    )
    assert referenced_asset_ids("mirror", document) == {ASSET_A}
    assert set(package("mirror", document, {ASSET_A: PNG}).assets) == {ASSET_A}


# MARK: - UUID · 경로 (§7, §10)


@pytest.mark.parametrize(
    "poison",
    [
        "../secret",
        "..\\secret",
        "/Users/ibyeongchan/Desktop/x.png",
        "~/Library/x.png",
        "file:///etc/passwd",
        "https://evil.example/x.png",
        "http://evil.example/x.png",
        "assets/A0000001-0000-4000-A000-000000000001",
        "A0000001-0000-4000-A000-000000000001/../../etc",
        "A0000001-0000-4000-A000-000000000001.png",
        "not-a-uuid",
        "",
        123,
        {"assetID": ASSET_A},
    ],
)
def test_asset_references_must_be_uuid_strings(poison):
    """참조 자리에 경로·URL·비문자열이 오면 거절이다.

    UUID 형식이 아니면 통과하지 못하므로 경로 조작이 **문자 수준에서** 불가능하다.
    """
    broken = photo_sticker(ASSET_A)
    broken["source"]["assetID"] = poison
    with pytest.raises(AssetError):
        package("mirror", mirror_document(stickers=[broken]), {})

    with pytest.raises(AssetError):
        package("sticker", sticker_document(finalAssetID=poison), {})


def test_null_photo_reference_is_refused_but_null_final_asset_is_not():
    """`finalAssetID`는 optional이라 `null`이 정상이다.

    사진 스티커의 `assetID`는 required다 — `null`이면 client가 못 읽는다.
    두 자리를 같은 규칙으로 뭉개지 않는다.
    """
    broken = photo_sticker(ASSET_A)
    broken["source"]["assetID"] = None
    with pytest.raises(AssetError):
        package("mirror", mirror_document(stickers=[broken]), {})

    assert package("sticker", sticker_document(finalAssetID=None), {}).assets == {}


def test_remote_url_as_a_sticker_source_is_refused():
    """client 포맷에 원격 자원 자리가 없다 — 새로 만들어 통과시킬 수 없다."""
    document = mirror_document(stickers=[{
        "id": "77777777-7777-4777-8777-777777777777",
        "source": {"kind": "photo", "url": "https://evil.example/x.png",
                   "assetID": ASSET_A, "aspectRatio": 1.0},
        "frame": {"x": .5, "y": .5, "width": .2, "height": .2},
        "rotation": 0, "opacity": 1, "zIndex": 0,
        "isLocked": False, "isFlippedHorizontally": False,
    }])
    with pytest.raises(AssetError):
        package("mirror", document, {ASSET_A: PNG})


def test_forbidden_strings_in_structural_fields_are_refused():
    for field in [{"origin": "file:///etc/passwd"}, {"style": {**STYLE, "hint": "../../x"}}]:
        with pytest.raises(AssetError):
            package("mirror", mirror_document(**field), {})


def test_forbidden_strings_in_keys_are_refused():
    with pytest.raises(AssetError):
        package("mirror", mirror_document(**{"../evil": "x"}), {})


# MARK: - 검사 정밀도 (§11)


def test_user_text_may_contain_a_url():
    """정상 사용을 막지 않는다.

    거울에 "https://insta.gr/me"라고 적는 것은 흔한 꾸미기다. 그 문자열은 asset을
    가리키지 않고 client는 `Text`로 그릴 뿐이다. B-7F는 이 package를 통째로
    거절했다 — 판매자는 이유를 알 수 없었다.
    """
    document = mirror_document(texts=[text_object("https://insta.gr/me")])
    assert package("mirror", document, {})


def test_user_names_may_contain_a_url():
    assert package("mirror", mirror_document(name="http://내거울"), {})
    assert package("sticker", sticker_document(name="../내스티커"), {})


def test_prose_exemption_is_narrow():
    """면제는 `PROSE_KEYS` 둘뿐이다. 늘어나면 검사가 무력해진다."""
    assert PROSE_KEYS == {"name", "text"}


def test_prose_exemption_does_not_cover_asset_positions():
    """산문 면제가 참조 자리로 새지 않는다."""
    broken = photo_sticker(ASSET_A)
    broken["source"]["assetID"] = "../evil"
    broken["source"]["name"] = "https://ok-in-prose"
    with pytest.raises(AssetError):
        package("mirror", mirror_document(stickers=[broken]), {})


def test_deeply_nested_manifest_is_refused_not_crashed():
    """깊은 중첩으로 parser를 죽이려는 것. **500이 아니라 400이다.**"""
    nested: object = "x"
    for _ in range(MAX_MANIFEST_DEPTH + 5):
        nested = [nested]
    with pytest.raises(AssetError):
        package("mirror", mirror_document(deep=nested), {})

    raw = b'{"id":"a","name":"b","style":{}' + b',"a":[' * 20000
    with pytest.raises(AssetError):
        checked_package(content_type="mirror", manifest=raw, preview=PNG, assets={})


# MARK: - checksum (§12)


def test_checksum_is_over_the_raw_stored_bytes():
    """re-serialize한 값이 아니라 **실제로 저장할 바이트**의 hash다.

    구매자가 받는 바이트와 checksum이 같은 것에서 나와야 무결성 확인이 의미를 갖는다.
    """
    import hashlib

    # 같은 문서를 공백만 다르게 적으면 checksum이 달라야 한다 — raw 기준이라는 증거다.
    document = mirror_document()
    compact = _json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()
    spaced = _json.dumps(document, ensure_ascii=False, indent=2).encode()

    first = checked_package(content_type="mirror", manifest=compact, preview=PNG, assets={})
    second = checked_package(content_type="mirror", manifest=spaced, preview=PNG, assets={})

    assert first.manifest_checksum == hashlib.sha256(compact).hexdigest()
    assert second.manifest_checksum == hashlib.sha256(spaced).hexdigest()
    assert first.manifest_checksum != second.manifest_checksum


def test_stored_manifest_bytes_are_unchanged(asset_service, storage, store):
    """저장한 것이 올린 것과 **바이트 단위로 같다.** 우리가 다시 적지 않는다."""
    raw = _json.dumps(mirror_document(), ensure_ascii=False, indent=2).encode()
    validated = checked_package(content_type="mirror", manifest=raw, preview=PNG, assets={})
    snapshot = asset_service.create_snapshot(user(), content_type="mirror", package=validated)

    assert storage.objects[manifest_key(snapshot.id)].data == raw
    assert store.snapshots[snapshot.id].manifest_checksum == validated.manifest_checksum


# MARK: - HTTP 중복 (§8)


def test_duplicate_multipart_asset_is_refused(asset_client):
    """같은 assetID를 두 번 보내면 거절이다.

    dict로 모으면 **마지막 값이 조용히 이긴다** — 업로더가 보낸 것과 다른 이미지가
    팔리고, snapshot은 불변이라 고칠 수 없다.
    """
    files = [
        ("manifest", ("manifest.json",
                      as_bytes(mirror_document(stickers=[photo_sticker(ASSET_A)])),
                      "application/json")),
        ("preview", ("preview.png", PNG, "image/png")),
        ("assets", (f"{ASSET_A}.png", PNG_MAGIC + b"first", "image/png")),
        ("assets", (f"{ASSET_A}.png", PNG_MAGIC + b"second", "image/png")),
    ]
    response = asset_client.post(
        "/marketplace/snapshots", data={"contentType": "mirror"}, files=files
    )
    # 인증이 먼저다 — 익명은 401. 중복 검사는 아래 단위 test가 고정한다.
    assert response.status_code == 401


def test_upload_endpoint_refuses_duplicates_before_reading_them():
    """중복 검사가 **모으는 자리**에 있어야 한다. dict comprehension이면 놓친다."""
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "app/api/marketplace.py").read_text()
    body = source[source.index('@router.post("/snapshots"'):]
    body = body[: body.index("return SnapshotResponse")]
    assert "duplicate asset" in body, "중복 거절이 없다"
    assert "for item in uploaded" in body, "dict comprehension으로 되돌아갔다"
    assert "removesuffix(\".png\"): await" not in body, "comprehension이 되살아났다"


def test_snapshot_upload_passes_the_content_type_to_the_validator():
    """API가 contentType을 검증기에 넘기지 않으면 결합이 없는 것과 같다."""
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "app/api/marketplace.py").read_text()
    body = source[source.index('@router.post("/snapshots"'):]
    assert "content_type=contentType" in body


# MARK: - 판매자 자기 목록 (B-7G.1)
#
# 공개 목록과 **다른 것**이다. 그쪽은 published만 보여 주므로 판매자가 아직 안 올린
# 것과 내린 것을 다시 찾을 방법이 없었다. 앱이 기억해 둔 id에 의존하면 앱을 지웠거나
# 기기를 바꾼 뒤 관리가 끊긴다.
#
# 가장 중요한 것: **남의 draft가 한 건도 새지 않는다.**


def test_seller_sees_every_state(service, store):
    """draft · published · unlisted 모두 돌려준다."""
    published(store, "draft-one", status=ListingStatus.DRAFT, published_at=None)
    published(store, "live-one", status=ListingStatus.PUBLISHED)
    published(store, "pulled-one", status=ListingStatus.UNLISTED)

    mine = service.seller_listings(user(SELLER))

    assert {x.id for x in mine} == {"draft-one", "live-one", "pulled-one"}
    assert {x.status for x in mine} == {
        ListingStatus.DRAFT, ListingStatus.PUBLISHED, ListingStatus.UNLISTED
    }


def test_seller_never_sees_another_seller(service, store):
    """가장 중요 — 남의 상품이 섞이면 draft 내용과 가격 전략이 유출된다."""
    published(store, "mine", owner=SELLER, status=ListingStatus.DRAFT, published_at=None)
    published(store, "theirs", owner=OTHER, status=ListingStatus.DRAFT, published_at=None)
    published(store, "theirs-live", owner=OTHER)

    mine = service.seller_listings(user(SELLER))
    theirs = service.seller_listings(user(OTHER))

    assert [x.id for x in mine] == ["mine"]
    assert {x.id for x in theirs} == {"theirs", "theirs-live"}
    assert all(x.seller_user_id == SELLER for x in mine)


def test_seller_with_nothing_gets_an_empty_list(service, store):
    """가짜 상품을 만들지 않는다."""
    published(store, "theirs", owner=OTHER)
    assert service.seller_listings(user(SELLER)) == []


def test_seller_listings_are_sorted_by_recent_change(service, store):
    """방금 만진 것이 위로. 값이 같으면 id로 안정화한다."""
    first = published(store, "aaa", status=ListingStatus.DRAFT, published_at=None)
    second = published(store, "bbb", status=ListingStatus.DRAFT, published_at=None)
    store.listings["aaa"] = type(first)(
        **{**first.__dict__, "updated_at": datetime(2026, 8, 1, tzinfo=timezone.utc)}
    )
    store.listings["bbb"] = type(second)(
        **{**second.__dict__, "updated_at": datetime(2026, 8, 9, tzinfo=timezone.utc)}
    )

    assert [x.id for x in service.seller_listings(user(SELLER))] == ["bbb", "aaa"]


def test_seller_listing_sort_is_deterministic(service, store):
    """같은 시각이면 순서가 흔들리지 않는다."""
    same = datetime(2026, 8, 5, tzinfo=timezone.utc)
    for listing_id in ["zzz", "aaa", "mmm"]:
        listing = published(store, listing_id, status=ListingStatus.DRAFT, published_at=None)
        store.listings[listing_id] = type(listing)(**{**listing.__dict__, "updated_at": same})

    order = [x.id for x in service.seller_listings(user(SELLER))]
    assert order == ["zzz", "mmm", "aaa"], order


def test_seller_listings_query_needs_no_composite_index():
    """`where` + `order_by`를 함께 걸지 않는다 — composite index를 요구하게 된다."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parent.parent / "app/marketplace/firestore_store.py"
    ).read_text()
    raw = _method_body(source, "list_for_seller")
    assert 'FieldFilter("sellerUserId", "==", seller_user_id)' in raw
    # **docstring을 걷어낸 코드만** 본다 — 설명에 적은 단어를 잡으면 안 된다.
    body = _code_only("def " + raw.split("def ", 1)[1])
    assert "order_by" not in body, "query에 정렬을 넣었다"
    # 질의 조건이 하나뿐이다.
    assert body.count("FieldFilter") == 1


# MARK: - HTTP


def test_my_listings_needs_auth(client):
    """§7 — 로그인 없이는 남의 draft를 볼 수 없다."""
    assert client.get("/users/me/marketplace/listings").status_code == 401


def test_no_arbitrary_user_listing_path(client):
    """임의 userId로 남의 draft를 조회하는 경로를 만들지 않았다."""
    for path in [
        f"/users/{SELLER}/marketplace/listings",
        "/marketplace/sellers/x/listings",
        f"/marketplace/listings?sellerUserId={SELLER}",
    ]:
        response = client.get(path)
        # 마지막 것은 200이지만 **공개 목록**이고 seller 필터가 없다.
        if response.status_code == 200:
            assert response.json() == [], "query로 판매자 목록을 얻을 수 있다"
        else:
            assert response.status_code in {401, 404, 405}


def test_my_listings_response_is_the_seller_dto(client, store):
    """판매자 응답에는 `status`가 있다. 공개 DTO에는 없다."""
    from app.api.marketplace import ListingResponse, PublicListingResponse

    assert "status" in ListingResponse.model_fields
    assert "status" not in PublicListingResponse.model_fields
    # 판매자 DTO에도 내부 식별자는 없다.
    for banned in ["seller_user_id", "sellerUserId", "snapshot_id", "snapshotId"]:
        assert banned not in ListingResponse.model_fields
    del store


def test_public_listing_contract_is_unchanged(client, store):
    """§5 — 공개 DTO 보안 계약을 건드리지 않았다."""
    from app.api.marketplace import PublicListingResponse

    assert set(PublicListingResponse.model_fields) == {
        "id", "content_type", "title", "description",
        "price_shards", "download_count", "like_count", "published_at",
        "seller_display_name",
    }

    published(store, "live")
    body = client.get("/marketplace/listings").json()
    assert [x["id"] for x in body] == ["live"]
    text = client.get("/marketplace/listings").text
    for banned in ["sellerUserId", "snapshotId", "status", "bucket", "gs://"]:
        assert banned not in text


def test_public_browse_still_hides_drafts(client, store):
    """판매자 목록이 생겼어도 공개 목록은 그대로다."""
    published(store, "draft-one", status=ListingStatus.DRAFT, published_at=None)
    published(store, "pulled-one", status=ListingStatus.UNLISTED)
    published(store, "live-one")

    assert [x["id"] for x in client.get("/marketplace/listings").json()] == ["live-one"]


def test_seller_listings_do_not_move_counters(service, store, shards):
    """조회가 경제나 카운터를 건드리지 않는다."""
    seed(shards, 100, who=BUYER)
    listing = published(store, "live", price=10)
    service.purchase(user(BUYER), "live")
    service.like(user(LIKER), "live")
    before = store.listings["live"]

    for _ in range(5):
        service.seller_listings(user(SELLER))

    after = store.listings["live"]
    assert after.download_count == before.download_count == 1
    assert after.like_count == before.like_count == 1
    assert shards.wallet(SELLER).balance == 10
    del listing


# MARK: - 실제 Firestore store로 판매자 목록 (B-7G.1)


@pytest.fixture
def seller_listings_store():
    """`FirestoreMarketplaceStore`를 **실제로** 돌린다. production Firestore는 부르지 않는다."""
    from app.marketplace.firestore_store import FirestoreMarketplaceStore
    from app.marketplace.store import LISTINGS

    def document(seller: str, status: str, day: int) -> dict:
        return {
            "sellerUserId": seller,
            "contentType": "mirror",
            "title": "제목",
            "description": "",
            "priceShards": 0,
            "snapshotId": "snap",
            "status": status,
            "publishFeePaid": status != "draft",
            "downloadCount": 0,
            "likeCount": 0,
            "createdAt": datetime(2026, 8, day, tzinfo=timezone.utc),
            "updatedAt": datetime(2026, 8, day, tzinfo=timezone.utc),
            "publishedAt": (
                datetime(2026, 8, day, tzinfo=timezone.utc) if status != "draft" else None
            ),
            "schemaVersion": 1,
        }

    db = FakeDatabase()
    db.data[LISTINGS] = {
        "mine-draft": document(SELLER, "draft", 1),
        "mine-live": document(SELLER, "published", 2),
        "mine-pulled": document(SELLER, "unlisted", 3),
        "theirs-draft": document(OTHER, "draft", 4),
        "theirs-live": document(OTHER, "published", 5),
    }
    return FirestoreMarketplaceStore(db)


def test_real_store_returns_only_my_listings(seller_listings_store):
    """실제 store 코드가 **정말로** 판매자로 걸러낸다."""
    mine = seller_listings_store.list_for_seller(SELLER)

    assert {x.id for x in mine} == {"mine-draft", "mine-live", "mine-pulled"}
    assert all(x.seller_user_id == SELLER for x in mine)


def test_real_store_leaks_no_other_seller(seller_listings_store):
    ids = {x.id for x in seller_listings_store.list_for_seller(SELLER)}
    assert "theirs-draft" not in ids
    assert "theirs-live" not in ids


def test_real_store_returns_all_three_states(seller_listings_store):
    states = {x.status.value for x in seller_listings_store.list_for_seller(SELLER)}
    assert states == {"draft", "published", "unlisted"}


def test_real_store_empty_for_a_seller_with_nothing(seller_listings_store):
    assert seller_listings_store.list_for_seller("00000000-0000-4000-8000-000000000000") == []


def test_real_public_list_still_only_published(seller_listings_store):
    """공개 목록은 그대로다 — 판매자 목록이 생겼어도 draft가 새지 않는다."""
    public = seller_listings_store.list_published()
    assert {x.id for x in public} == {"mine-live", "theirs-live"}


# MARK: - 판매자 전용 미리보기 (B-7H hotfix)
#
# 판매자 관리 화면에 숫자만 보이고 생김새가 없어 어느 상품인지 알 수 없었다.
# draft · unlisted도 **판매자 본인에게는** 보여야 한다.
#
# 가장 중요한 것: **공개 정책은 그대로다.** draft/unlisted가 공개 endpoint로
# 새면 사기 전에 원본을 보여 주는 것이 된다.


def test_seller_sees_preview_in_every_state(asset_service, store):
    """draft · published · unlisted 모두 판매자에게는 보인다."""
    for state, listing_id in [
        (ListingStatus.DRAFT, "mine-draft"),
        (ListingStatus.PUBLISHED, "mine-live"),
        (ListingStatus.UNLISTED, "mine-pulled"),
    ]:
        snapshot = upload(asset_service, owner=SELLER)
        listing = published(
            store, listing_id, status=state,
            published_at=None if state is ListingStatus.DRAFT else ...,
        )
        store.listings[listing_id] = type(listing)(
            **{**listing.__dict__, "snapshot_id": snapshot.id}
        )

        stored = asset_service.seller_preview(user(SELLER), listing_id)

        assert stored.data == PNG, state
        assert stored.content_type == "image/png"


def test_seller_preview_refuses_another_seller(asset_service, store):
    """가장 중요 — 남의 draft를 미리보기로 엿볼 수 없다."""
    snapshot = upload(asset_service, owner=SELLER)
    listing = published(store, "theirs", owner=OTHER, status=ListingStatus.DRAFT,
                        published_at=None)
    store.listings["theirs"] = type(listing)(
        **{**listing.__dict__, "snapshot_id": snapshot.id}
    )

    with pytest.raises(ListingNotFound):
        asset_service.seller_preview(user(SELLER), "theirs")


def test_seller_preview_of_missing_listing(asset_service):
    with pytest.raises(ListingNotFound):
        asset_service.seller_preview(user(SELLER), "nope")


def test_seller_preview_does_not_move_counters(asset_service, store):
    snapshot = upload(asset_service)
    listing = listing_with_snapshot(store, snapshot)
    before = store.listings[listing.id]

    for _ in range(5):
        asset_service.seller_preview(user(SELLER), listing.id)

    after = store.listings[listing.id]
    assert after.download_count == before.download_count
    assert after.like_count == before.like_count


def test_public_preview_policy_is_unchanged(asset_service, store):
    """**회귀 금지** — 공개 미리보기는 여전히 published만이다."""
    for state in [ListingStatus.DRAFT, ListingStatus.UNLISTED]:
        snapshot = upload(asset_service)
        listing = published(
            store, f"hidden-{state.value}", status=state,
            published_at=None if state is ListingStatus.DRAFT else ...,
        )
        store.listings[listing.id] = type(listing)(
            **{**listing.__dict__, "snapshot_id": snapshot.id}
        )

        # 판매자 자신은 본다.
        assert asset_service.seller_preview(user(SELLER), listing.id).data == PNG
        # 공개로는 안 보인다.
        with pytest.raises(ListingNotFound):
            asset_service.preview(listing.id)


def test_public_published_preview_still_works(asset_service, store):
    snapshot = upload(asset_service)
    listing_with_snapshot(store, snapshot)
    assert asset_service.preview("with-asset").data == PNG


def test_seller_preview_needs_auth(asset_client):
    """§8 — 로그인 없이는 남의 draft 미리보기를 얻을 수 없다."""
    assert asset_client.get(
        "/users/me/marketplace/listings/abc/preview"
    ).status_code == 401


def test_seller_preview_leaks_no_storage_detail(asset_client, asset_service, store):
    """bucket · gs:// · object key · signed URL이 응답에 없다."""
    snapshot = upload(asset_service)
    listing_with_snapshot(store, snapshot)

    # 인증이 없으므로 401이지만, 헤더에 저장소 정보가 실릴 자리가 없음을 함께 본다.
    response = asset_client.get("/users/me/marketplace/listings/with-asset/preview")
    for banned in ["gs://", "bucket", "marketplace/snapshots", "X-Goog", "googleapis.com"]:
        assert banned not in str(response.headers)
        assert banned not in response.text


def test_seller_preview_reuses_the_public_reader():
    """새 storage 경로를 만들지 않았다 — 같은 key builder와 reader를 쓴다."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parent.parent / "app/marketplace/service.py"
    ).read_text()
    body = _method_body(source, "seller_preview")
    assert "preview_key(snapshot.id)" in body
    assert "self._read(" in body
    # 자기만의 bucket 접근을 만들지 않았다.
    for banned in ["storage.Client", "bucket(", "generate_signed_url", "gs://"]:
        assert banned not in body, banned


def test_no_signed_url_in_seller_preview_endpoint():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parent.parent / "app/api/marketplace.py"
    ).read_text()
    raw = source[source.index("def my_listing_preview"):]
    raw = raw[: raw.index("@purchases_router.get(\"/likes\")")]
    # **docstring을 걷어낸 코드만** 본다 — 설명에 적은 단어를 잡으면 안 된다.
    body = _code_only("def " + raw.split("def ", 1)[1])
    for banned in ["signed_url", "generate_signed_url", "gs://", "bucket"]:
        assert banned not in body, banned
    # 판매자 전용이라 공용 캐시에 두지 않는다.
    assert 'private' in raw and "immutable" in raw


# MARK: - 삭제 (Marketplace UX hardening)
#
# 사용자는 "내리기"가 아니라 삭제를 원한다. 하지만 **아무것도 실제로 지우지 않는다** —
# 이미 산 사람이 계속 받아야 하기 때문이다. `deleted`는 tombstone이고 끝 상태다.


def test_seller_can_delete(service, store):
    published(store, "live")

    listing = service.delete_listing(user(SELLER), "live")

    assert listing.status is ListingStatus.DELETED
    assert store.listings["live"].status is ListingStatus.DELETED


def test_delete_is_terminal_and_cannot_be_republished(service, store, shards):
    """가장 중요 — 삭제를 골랐으면 되살아나지 않는다."""
    from app.marketplace.models import InvalidTransition

    seed(shards, 100)
    published(store, "live")
    service.delete_listing(user(SELLER), "live")

    with pytest.raises(InvalidTransition):
        service.publish(user(SELLER), "live")

    assert store.listings["live"].status is ListingStatus.DELETED


def test_delete_refunds_nothing(service, store, shards):
    """등록비를 돌려주지 않는다. 상점에 올라가 있던 값은 이미 제공됐다."""
    seed(shards, 100)
    listing = draft(service, store)
    service.publish(user(SELLER), listing.id)
    after_publish = shards.wallet(SELLER).balance
    assert after_publish == 90, "등록비가 빠지지 않았다"

    service.delete_listing(user(SELLER), listing.id)

    assert shards.wallet(SELLER).balance == after_publish


def test_delete_touches_no_economy(service, store, shards):
    seed(shards, 100)
    published(store, "live")
    before = len(shards.entries(SELLER)) if hasattr(shards, "entries") else None

    service.delete_listing(user(SELLER), "live")

    assert shards.wallet(SELLER).balance == 100
    del before


def test_delete_refuses_another_seller(service, store):
    published(store, "theirs", owner=OTHER)

    with pytest.raises(ListingNotFound):
        service.delete_listing(user(SELLER), "theirs")

    assert store.listings["theirs"].status is ListingStatus.PUBLISHED


def test_delete_is_idempotent(service, store):
    published(store, "live")
    service.delete_listing(user(SELLER), "live")

    again = service.delete_listing(user(SELLER), "live")

    assert again.status is ListingStatus.DELETED


def test_deleted_disappears_from_public_browse(service, store):
    published(store, "live")
    published(store, "other")
    assert {x.id for x in service.browse()} == {"live", "other"}

    service.delete_listing(user(SELLER), "live")

    assert {x.id for x in service.browse()} == {"other"}


def test_deleted_public_detail_is_gone(service, store):
    published(store, "live")
    service.delete_listing(user(SELLER), "live")

    with pytest.raises(ListingNotFound):
        service.listing("live")


def test_deleted_public_preview_is_gone(asset_service, store):
    snapshot = upload(asset_service)
    listing_with_snapshot(store, snapshot)
    assert asset_service.preview("with-asset").data == PNG

    asset_service.delete_listing(user(SELLER), "with-asset")

    with pytest.raises(ListingNotFound):
        asset_service.preview("with-asset")


def test_deleted_still_appears_in_seller_list(service, store):
    """판매자에게는 남는다 — 무엇을 지웠는지 알 수 있어야 한다."""
    published(store, "live")
    service.delete_listing(user(SELLER), "live")

    mine = service.seller_listings(user(SELLER))

    assert [x.id for x in mine] == ["live"]
    assert mine[0].status is ListingStatus.DELETED


def test_delete_keeps_the_snapshot(asset_service, store, storage):
    """snapshot도 GCS object도 **지우지 않는다.**"""
    snapshot = upload(asset_service, asset_ids=[ASSET_A])
    listing_with_snapshot(store, snapshot)
    before = dict(storage.objects)

    asset_service.delete_listing(user(SELLER), "with-asset")

    assert store.snapshots[snapshot.id] is not None
    assert set(storage.objects) == set(before), "GCS object가 사라졌다"


def test_buyer_keeps_template_access_after_delete(asset_service, store, shards):
    """**핵심 관문** — 산 사람은 삭제 뒤에도 계속 받는다."""
    seed(shards, 100, who=BUYER)
    snapshot = upload(asset_service, asset_ids=[ASSET_A])
    listing_with_snapshot(store, snapshot)
    asset_service.purchase(user(BUYER), "with-asset")

    asset_service.delete_listing(user(SELLER), "with-asset")

    # 공개로는 사라졌지만
    with pytest.raises(ListingNotFound):
        asset_service.preview("with-asset")
    # 구매자는 그대로다.
    assert asset_service.template(user(BUYER), "with-asset").data
    assert asset_service.template_asset(user(BUYER), "with-asset", ASSET_A).data == PNG


def test_delete_keeps_ownership_and_counts(asset_service, store, shards):
    seed(shards, 100, who=BUYER)
    snapshot = upload(asset_service)
    listing_with_snapshot(store, snapshot)
    asset_service.purchase(user(BUYER), "with-asset")
    asset_service.like(user(LIKER), "with-asset")

    asset_service.delete_listing(user(SELLER), "with-asset")

    listing = store.listings["with-asset"]
    assert listing.download_count == 1, "다운로드 기록이 사라졌다"
    assert listing.like_count == 1, "좋아요 기록이 사라졌다"
    assert store.ownership("with-asset", BUYER) is not None


def test_delete_does_not_hard_delete_anything():
    """소스에 실제 삭제가 없다 — tombstone뿐이다."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    body = _method_body(
        (root / "app/marketplace/firestore_store.py").read_text(), "delete"
    )
    code = _code_only(body)
    for banned in [".delete()", "SNAPSHOTS", "OWNERSHIP", "LIKES", "bucket"]:
        assert banned not in code, f"삭제가 {banned}를 건드린다"
    assert "ListingStatus.DELETED.value" in code


# MARK: - HTTP


def test_delete_needs_auth(client):
    assert client.delete("/users/me/marketplace/listings/abc").status_code == 401


def test_no_arbitrary_delete_path(client):
    """임의 userId로 남의 상품을 지우는 경로를 만들지 않았다."""
    for path in [
        f"/users/{SELLER}/marketplace/listings/abc",
        "/marketplace/listings/abc",
    ]:
        assert client.delete(path).status_code in {401, 404, 405}


# MARK: - source content id


def test_new_snapshot_records_the_source_content(asset_service, store):
    """manifest top-level `id`를 그대로 쓴다 — 새 식별자를 만들지 않는다."""
    package = checked_package(
        content_type="mirror",
        manifest=as_bytes(mirror_document(id="art-mint-flower")),
        preview=PNG, assets={},
    )
    snapshot = asset_service.create_snapshot(
        user(), content_type="mirror", package=package
    )

    assert store.snapshots[snapshot.id].source_content_id == "art-mint-flower"


def test_sticker_snapshot_records_its_project_id(asset_service, store):
    package = checked_package(
        content_type="sticker",
        manifest=as_bytes(sticker_document(id="sticker-42")),
        preview=PNG, assets={},
    )
    snapshot = asset_service.create_snapshot(
        user(), content_type="sticker", package=package
    )

    assert store.snapshots[snapshot.id].source_content_id == "sticker-42"


def test_seller_listing_exposes_the_source_content(asset_service, store):
    snapshot = upload(asset_service)
    listing = listing_with_snapshot(store, snapshot)

    assert asset_service.source_content(listing) == "mirror-1"


def test_legacy_snapshot_falls_back_to_the_stored_manifest(asset_service, store, storage):
    """옛 문서에는 `sourceContentId`가 없다. **저장된 manifest에서 읽는다.**

    production Firestore를 다시 쓰지 않는다 — 응답에만 담는다.
    """
    snapshot = upload(asset_service)
    listing = listing_with_snapshot(store, snapshot)
    # 옛 문서를 흉내 낸다: 값을 지운다.
    legacy = type(snapshot)(**{**store.snapshots[snapshot.id].__dict__, "source_content_id": ""})
    store.snapshots[snapshot.id] = legacy
    assert store.snapshots[snapshot.id].source_content_id == ""

    found = asset_service.source_content(listing)

    assert found == "mirror-1", "저장된 manifest에서 읽지 못했다"
    # **문서를 고치지 않았다.**
    assert store.snapshots[snapshot.id].source_content_id == ""
    del storage


def test_unknown_source_content_is_empty_not_invented(service, store):
    """알 수 없으면 빈 문자열이다. 거짓 값을 지어내지 않는다."""
    listing = published(store, "live")
    # snapshot이 없다.
    assert service.source_content(listing) == ""


def test_source_content_is_not_in_the_public_dto(client, store):
    from app.api.marketplace import PublicListingResponse

    assert "source_content_id" not in PublicListingResponse.model_fields
    published(store, "live")
    text = client.get("/marketplace/listings").text
    for banned in ["sourceContentId", "source_content_id"]:
        assert banned not in text


def test_seller_dto_carries_the_source_content():
    from app.api.marketplace import _SellerListingResponse

    assert "source_content_id" in _SellerListingResponse.model_fields
    # 판매자 DTO에도 내부 식별자는 없다.
    for banned in ["seller_user_id", "snapshot_id"]:
        assert banned not in _SellerListingResponse.model_fields


# MARK: - 가격순 정렬 (Marketplace UX Hardening)


def _sortable(listing_id: str, *, price: int, downloads: int, likes: int, published: str):
    from datetime import datetime, timezone
    from app.marketplace.models import Listing, ListingStatus

    return Listing(
        id=listing_id, seller_user_id="seller", content_type="mirror",
        title=listing_id, description="", price_shards=price,
        snapshot_id=f"snap-{listing_id}", status=ListingStatus.PUBLISHED,
        publish_fee_paid=True, download_count=downloads, like_count=likes,
        published_at=datetime.fromisoformat(published).replace(tzinfo=timezone.utc),
    )


def _fixture_listings():
    # 명세 §42의 fixture 그대로.
    return [
        _sortable("A", price=5, downloads=10, likes=1, published="2026-08-03T00:00:00"),
        _sortable("B", price=0, downloads=20, likes=0, published="2026-08-01T00:00:00"),
        _sortable("C", price=1, downloads=5, likes=8, published="2026-08-02T00:00:00"),
    ]


def test_price_sort_is_cheapest_first():
    from app.marketplace.models import MarketplaceSort

    order = [x.id for x in MarketplaceSort.PRICE.sorted(_fixture_listings())]
    assert order == ["B", "C", "A"]


def test_every_sort_is_deterministic():
    from app.marketplace.models import MarketplaceSort

    expected = {
        MarketplaceSort.LATEST: ["A", "C", "B"],
        MarketplaceSort.POPULAR: ["B", "A", "C"],
        MarketplaceSort.LIKES: ["C", "A", "B"],
        MarketplaceSort.PRICE: ["B", "C", "A"],
    }
    for sort, order in expected.items():
        assert [x.id for x in sort.sorted(_fixture_listings())] == order, sort


def test_price_ties_break_by_newest_then_id():
    from app.marketplace.models import MarketplaceSort

    same = [
        _sortable("z", price=1, downloads=0, likes=0, published="2026-08-01T00:00:00"),
        _sortable("a", price=1, downloads=0, likes=0, published="2026-08-01T00:00:00"),
        _sortable("m", price=1, downloads=0, likes=0, published="2026-08-05T00:00:00"),
    ]
    assert [x.id for x in MarketplaceSort.PRICE.sorted(same)] == ["m", "a", "z"]
