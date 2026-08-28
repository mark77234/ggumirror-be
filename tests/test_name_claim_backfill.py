"""**규칙보다 먼저 있던 이름들.**

새 uniqueness 규칙은 claim collection **하나만** 본다 — `set_display_name`도
`publish`도 사용자 문서나 listing을 다시 훑지 않는다. 빠르고, 동시성이 정확하고,
composite index가 필요 없다. 대신 대가가 하나 있다:

    규칙이 생기기 전에 만들어진 이름은 **자리를 잡고 있지 않다.**

그래서 `찬찡`이라는 기존 사용자가 있어도 새 사용자가 `찬찡`을 그대로 가져갈 수 있다.
이 파일은 그 구멍이 실제로 있다는 것을 먼저 증명하고(그래서 backfill이 필요하다),
자리를 등록한 뒤에는 막힌다는 것을 증명한다.

`scripts/backfill_name_claims.py`가 하는 일은 **이미 존재하는 이름을 그 값 그대로**
index에 넣는 것뿐이다. 이름을 바꾸지 않고, 겹치면 승자를 고르지 않고 멈춘다.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.auth.models import User
from app.auth.profile import DisplayNameTaken, display_name_key
from app.auth.store import InMemoryAuthStore
from app.marketplace.models import ContentType, Snapshot, TitleTaken, listing_title_key
from app.marketplace.service import MarketplaceService
from app.marketplace.store import InMemoryMarketplaceStore
from app.shards.models import ShardReason, utcnow
from app.shards.service import ShardLedgerService
from app.shards.store import InMemoryShardStore

from scripts.backfill_name_claims import Row, audit

LEGACY = "11111111-2222-4333-8444-555555555555"
NEWCOMER = "99999999-8888-4777-8666-555555555555"


# MARK: - 사용자 이름


@pytest.fixture
def auth():
    """**규칙보다 먼저 있던 사용자.** 이름은 있는데 claim 문서가 없다.

    production에서 실제로 이 모양이었다 — `byeongchan` 하나가 자리 없이 있었다.
    """
    store = InMemoryAuthStore()
    store.users[LEGACY] = User(id=LEGACY, display_name="찬찡")
    store.users[NEWCOMER] = User(id=NEWCOMER)
    assert store.name_claims == {}
    return store


def backfill_username(store: InMemoryAuthStore) -> int:
    """script가 Firestore에 하는 일과 **같은 일**: 기존 값을 그대로 등록한다."""
    rows = [
        Row(display_name_key(user.display_name), user_id, user.display_name)
        for user_id, user in store.users.items()
        if user.display_name
    ]
    result = audit(rows, dict(store.name_claims))
    assert result.is_safe, "겹치는 이름이 있으면 script는 아무것도 쓰지 않는다"
    for row in result.missing:
        store.name_claims.setdefault(row.key, row.owner_id)
    return len(result.missing)


def test_legacy_username_is_unprotected_without_a_claim(auth):
    """**이것이 backfill이 필요한 이유다.**

    claim이 없으면 규칙이 볼 것이 없어서, 기존 이름을 새 사용자가 그대로 가져간다.
    이 테스트가 실패하기 시작하면(= 다른 방어가 생기면) backfill을 다시 판단한다.
    """
    auth.set_display_name(NEWCOMER, "찬찡", utcnow())

    assert auth.users[NEWCOMER].display_name == "찬찡"
    assert auth.users[LEGACY].display_name == "찬찡"  # 두 사람이 같은 이름이다


def test_backfilled_username_refuses_a_duplicate(auth):
    assert backfill_username(auth) == 1

    with pytest.raises(DisplayNameTaken):
        auth.set_display_name(NEWCOMER, "찬찡", utcnow())
    assert auth.users[NEWCOMER].display_name is None
    # 기존 사용자의 이름은 **그대로다.** backfill은 값을 바꾸지 않는다.
    assert auth.users[LEGACY].display_name == "찬찡"


def test_backfilled_username_folds_case_and_spacing(auth):
    backfill_username(auth)

    for sneaky in (" 찬찡 ", "찬찡"):
        with pytest.raises(DisplayNameTaken):
            auth.set_display_name(NEWCOMER, sneaky.strip(), utcnow())


def test_the_legacy_owner_can_still_save_their_own_name(auth):
    """**자기 자리를 자기가 막지 않는다.** 등록된 주인이 그 사용자이기 때문이다."""
    backfill_username(auth)

    updated = auth.set_display_name(LEGACY, "찬찡", utcnow() + timedelta(days=31))
    assert updated.display_name == "찬찡"


def test_a_new_unique_username_still_works(auth):
    backfill_username(auth)

    assert auth.set_display_name(NEWCOMER, "병찬", utcnow()).display_name == "병찬"


def test_username_backfill_is_idempotent(auth):
    assert backfill_username(auth) == 1
    before = dict(auth.name_claims)

    # 두 번째 실행은 만들 것이 없다.
    assert backfill_username(auth) == 0
    assert auth.name_claims == before


# MARK: - 상품 이름


@pytest.fixture
def shard_store():
    return InMemoryShardStore()


@pytest.fixture
def shards(shard_store):
    return ShardLedgerService(shard_store)


@pytest.fixture
def store(shard_store):
    return InMemoryMarketplaceStore(shard_store)


@pytest.fixture
def service(store, shards):
    return MarketplaceService(store, shards)


def fund(shards, user_id: str, amount: int) -> None:
    shards.credit(
        user_id, amount, ShardReason.ADMIN_ADJUSTMENT, external_event_id=f"seed:{user_id}:{amount}"
    )


def draft(service, store, *, title: str, kind=ContentType.MIRROR, owner=LEGACY, source="s"):
    found = Snapshot(id=f"snap-{owner[:4]}-{source}", seller_user_id=owner, content_type=kind)
    store.snapshots[found.id] = found
    return service.create_draft(
        User(id=owner), content_type=kind.value, title=title,
        description="", price_shards=0, snapshot_id=found.id,
    )


@pytest.fixture
def legacy_listing(service, store, shards):
    """**규칙보다 먼저 올라간 상품.** published인데 이름 자리가 없다."""
    fund(shards, LEGACY, 100)
    listing = draft(service, store, title="핑크 거울")
    service.publish(User(id=LEGACY), listing.id)
    # 규칙 이전 상태를 만든다 — 게시는 됐고 자리는 없다.
    store.title_claims.clear()
    return listing


def backfill_titles(store: InMemoryMarketplaceStore) -> int:
    rows = [
        Row(listing_title_key(listing.title), listing.id, listing.title)
        for listing in store.listings.values()
        if listing.status.value == "published"
    ]
    result = audit(rows, dict(store.title_claims))
    assert result.is_safe
    for row in result.missing:
        store.title_claims.setdefault(row.key, row.owner_id)
    return len(result.missing)


def test_legacy_title_is_unprotected_without_a_claim(legacy_listing, service, store, shards):
    """**이것이 backfill이 필요한 이유다** — 상품 쪽도 같은 모양이다."""
    fund(shards, NEWCOMER, 100)
    second = draft(service, store, title="핑크 거울", owner=NEWCOMER, source="b")

    assert service.publish(User(id=NEWCOMER), second.id).published is True


def test_backfilled_title_refuses_a_duplicate(legacy_listing, service, store, shards):
    assert backfill_titles(store) == 1
    fund(shards, NEWCOMER, 100)
    second = draft(service, store, title="핑크 거울", owner=NEWCOMER, source="b")

    with pytest.raises(TitleTaken):
        service.publish(User(id=NEWCOMER), second.id)
    # 이름이 겹쳐 막힌 등록은 **등록비를 가져가지 않는다.**
    assert shards.wallet(NEWCOMER).balance == 100


def test_backfilled_title_refuses_a_sticker_with_the_same_name(
    legacy_listing, service, store, shards
):
    """**거울과 스티커가 같은 이름 공간을 쓴다.** 종류를 바꿔도 통과하지 않는다."""
    backfill_titles(store)
    fund(shards, NEWCOMER, 100)
    sticker = draft(
        service, store, title="핑크 거울", kind=ContentType.STICKER, owner=NEWCOMER, source="b"
    )

    with pytest.raises(TitleTaken):
        service.publish(User(id=NEWCOMER), sticker.id)


def test_the_legacy_listing_keeps_its_own_title(legacy_listing, service, store):
    """자기 자리다 — 다시 올려도 자기 이름에 막히지 않는다."""
    backfill_titles(store)

    service.unpublish(User(id=LEGACY), legacy_listing.id)
    assert service.publish(User(id=LEGACY), legacy_listing.id).published is True


def test_a_new_unique_title_still_publishes(legacy_listing, service, store, shards):
    backfill_titles(store)
    fund(shards, NEWCOMER, 100)
    other = draft(service, store, title="파란 거울", owner=NEWCOMER, source="b")

    assert service.publish(User(id=NEWCOMER), other.id).published is True


def test_title_backfill_is_idempotent(legacy_listing, store):
    assert backfill_titles(store) == 1
    before = dict(store.title_claims)

    assert backfill_titles(store) == 0
    assert store.title_claims == before


# MARK: - script 자체


def test_audit_stops_on_a_conflict():
    """**승자를 고르지 않는다.** 겹치는 것이 하나라도 있으면 아무것도 쓰지 않는다."""
    rows = [Row("찬찡", "user-a", "찬찡"), Row("찬찡", "user-b", " 찬찡 ")]

    result = audit(rows, {})

    assert result.is_safe is False
    assert result.missing == []          # 겹친 것은 만들 목록에 들어가지 않는다
    assert [key for key, _ in result.conflicts] == ["찬찡"]


def test_audit_refuses_to_take_someone_elses_claim():
    """자리는 있는데 주인이 다르다 — 덮어쓰지 않고 멈춘다."""
    result = audit([Row("찬찡", "user-a", "찬찡")], {"찬찡": "user-b"})

    assert result.is_safe is False
    assert result.missing == []
    assert result.mismatched == [(Row("찬찡", "user-a", "찬찡"), "user-b")]


def test_audit_is_a_no_op_when_everything_is_registered():
    result = audit([Row("찬찡", "user-a", "찬찡")], {"찬찡": "user-a"})

    assert result.is_safe is True
    assert result.missing == []


def test_backfill_never_rewrites_the_stored_name():
    """등록하는 것은 **열쇠**이고, 사람이 보는 이름은 그대로 남는다."""
    rows = [Row(display_name_key(" Mark "), "user-a", " Mark ")]

    result = audit(rows, {})

    assert [row.key for row in result.missing] == ["mark"]
    assert [row.display for row in result.missing] == [" Mark "]
