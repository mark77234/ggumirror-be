"""이름은 **서버가 정한다.**

두 가지 이름이 겹치면 안 된다:

    사용자 이름      서비스 전체에서 하나
    상품 이름        상점 전체에서 하나 (거울·스티커가 같은 공간)

client가 "찾아보니 없더라"로 정하면, 동시에 같은 이름을 적은 두 사람이 둘 다
통과한다. 그래서 판단은 **transaction 안**에서 한 번만 일어난다.

**이름은 identity가 아니다.** 이 규칙은 이름이 겹치는 것을 막을 뿐이고,
콘텐츠를 찾는 열쇠는 여전히 id다.
"""

from __future__ import annotations

import threading

import pytest

from app.auth.models import User
from app.auth.profile import DisplayNameTaken, display_name_key, normalize_display_name
from app.auth.store import InMemoryAuthStore
from app.marketplace.models import (
    ContentType,
    Listing,
    MarketplacePublishPolicy,
    Snapshot,
    TitleTaken,
    listing_title_key,
)
from app.marketplace.service import MarketplaceService
from app.marketplace.store import InMemoryMarketplaceStore
from datetime import timedelta

from app.shards.models import ShardReason, utcnow
from app.shards.service import ShardLedgerService
from app.shards.store import InMemoryShardStore

ALICE = "11111111-2222-4333-8444-555555555555"
BOB = "99999999-8888-4777-8666-555555555555"


# MARK: - normalization


def test_username_key_folds_whitespace_and_case():
    """`찬찡` · ` 찬찡 `은 같은 이름이고 `Mark` · `MARK`도 같은 이름이다."""
    assert display_name_key(" 찬찡 ") == display_name_key("찬찡")
    assert display_name_key("Mark") == display_name_key("mark") == display_name_key("MARK")
    assert display_name_key("병찬") != display_name_key("찬병")


def test_username_key_folds_hangul_decomposition():
    """자모가 분리돼 들어와도(NFD) 같은 이름으로 센다 — 눈에 같으면 같은 이름이다."""
    composed = "찬찡"
    decomposed = "찬찡"
    assert composed != decomposed
    assert display_name_key(composed) == display_name_key(decomposed)


def test_title_key_uses_the_same_rule():
    """상품 이름도 사용자 이름과 **같은 규칙**이다 — 두 벌을 만들지 않는다."""
    assert listing_title_key(" 핑크 리본 ") == listing_title_key("핑크 리본")
    assert listing_title_key("Pink") == listing_title_key("PINK")


def test_title_key_ignores_content_type():
    """거울과 스티커가 **같은 이름 공간**을 쓴다 — 열쇠가 종류를 받지 않는다."""
    import inspect

    # 인자가 제목 하나뿐이다. 종류를 넣을 자리가 없으므로 공간이 갈라질 수 없다.
    assert list(inspect.signature(listing_title_key).parameters) == ["raw"]


# MARK: - 사용자 이름


@pytest.fixture
def auth():
    store = InMemoryAuthStore()
    for user_id in (ALICE, BOB):
        store.users[user_id] = User(id=user_id)
    return store


def test_a_unique_name_is_accepted(auth):
    updated = auth.set_display_name(ALICE, normalize_display_name("찬찡"), utcnow())
    assert updated.display_name == "찬찡"


def test_the_same_name_is_refused_for_another_user(auth):
    auth.set_display_name(ALICE, "찬찡", utcnow())

    with pytest.raises(DisplayNameTaken):
        auth.set_display_name(BOB, "찬찡", utcnow())

    assert auth.users[BOB].display_name is None


def test_whitespace_and_case_cannot_smuggle_a_duplicate(auth):
    auth.set_display_name(ALICE, "Mark", utcnow())

    for sneaky in (" Mark ", "mark", "MARK"):
        with pytest.raises(DisplayNameTaken):
            auth.set_display_name(BOB, normalize_display_name(sneaky), utcnow())


def test_keeping_your_own_name_is_not_a_duplicate(auth):
    now = utcnow()
    auth.set_display_name(ALICE, "찬찡", now)
    # 30일 규칙은 그대로다 — 그것과 별개로 자기 자리를 자기가 막지 않는다.
    later = now + timedelta(days=31)
    updated = auth.set_display_name(ALICE, "찬찡", later)
    assert updated.display_name == "찬찡"


def test_renaming_releases_the_old_name(auth):
    now = utcnow()
    auth.set_display_name(ALICE, "옛이름", now)
    auth.set_display_name(ALICE, "새이름", now + timedelta(days=31))

    # 놓아 준 이름은 다른 사람이 쓸 수 있다.
    auth.set_display_name(BOB, "옛이름", utcnow())
    assert auth.users[BOB].display_name == "옛이름"


def test_a_failed_rename_keeps_the_old_claim(auth):
    now = utcnow()
    auth.set_display_name(ALICE, "찬찡", now)
    auth.set_display_name(BOB, "병찬", now)

    later = now + timedelta(days=31)
    with pytest.raises(DisplayNameTaken):
        auth.set_display_name(BOB, "찬찡", later)

    # **예전 이름을 잃지 않는다.** 실패가 사용자에게서 이름을 빼앗으면 안 된다.
    assert auth.users[BOB].display_name == "병찬"
    # 그리고 BOB의 자리는 여전히 BOB의 것이다.
    with pytest.raises(DisplayNameTaken):
        auth.set_display_name(ALICE, "병찬", later)


def test_apple_seed_never_steals_a_taken_name(auth):
    """Apple이 준 이름이 이미 쓰이고 있으면 **비워 둔다** — 로그인은 계속된다."""
    auth.set_display_name(ALICE, "찬찡", utcnow())

    seeded = auth.seed_display_name(BOB, "찬찡")

    assert seeded.display_name is None


def test_concurrent_same_name_has_exactly_one_winner(auth):
    """동시에 같은 이름을 적어도 한 명만 얻는다."""
    for index in range(20):
        auth.users[f"user-{index}"] = User(id=f"user-{index}")
    start = threading.Barrier(20)
    winners: list[str] = []
    lock = threading.Lock()

    def run(user_id: str) -> None:
        start.wait()
        try:
            auth.set_display_name(user_id, "찬찡", utcnow())
        except DisplayNameTaken:
            return
        with lock:
            winners.append(user_id)

    threads = [threading.Thread(target=run, args=(f"user-{i}",)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(winners) == 1, f"{len(winners)}명이 같은 이름을 얻었다"


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


def draft(service, store, *, title: str, kind=ContentType.MIRROR, owner=ALICE, source="s"):
    found = Snapshot(id=f"snap-{owner[:4]}-{source}", seller_user_id=owner, content_type=kind)
    store.snapshots[found.id] = found
    return service.create_draft(
        User(id=owner), content_type=kind.value, title=title,
        description="", price_shards=0, snapshot_id=found.id,
    )


def balance(shards, user_id: str) -> int:
    return shards.wallet(user_id).balance


def test_a_unique_title_publishes(service, store, shards):
    fund(shards, ALICE, 100)
    listing = draft(service, store, title="핑크 리본")

    result = service.publish(User(id=ALICE), listing.id)

    assert result.published is True
    assert result.fee_shards == 10
    assert balance(shards, ALICE) == 90


def test_a_second_mirror_cannot_take_the_same_title(service, store, shards):
    fund(shards, ALICE, 100)
    first = draft(service, store, title="핑크 리본", source="a")
    service.publish(User(id=ALICE), first.id)

    fund(shards, BOB, 100)
    second = draft(service, store, title="핑크 리본", owner=BOB, source="b")
    with pytest.raises(TitleTaken):
        service.publish(User(id=BOB), second.id)


def test_a_sticker_cannot_take_a_mirror_title(service, store, shards):
    """**종류가 달라도 같은 이름은 안 된다** — 상점에서는 둘 다 "상품"이다."""
    fund(shards, ALICE, 100)
    mirror = draft(service, store, title="핑크 리본", source="a")
    service.publish(User(id=ALICE), mirror.id)

    sticker = draft(
        service, store, title="핑크 리본", kind=ContentType.STICKER, owner=BOB, source="b"
    )
    fund(shards, BOB, 100)
    with pytest.raises(TitleTaken):
        service.publish(User(id=BOB), sticker.id)


def test_whitespace_and_case_cannot_smuggle_a_duplicate_title(service, store, shards):
    fund(shards, ALICE, 100)
    first = draft(service, store, title="Pink", source="a")
    service.publish(User(id=ALICE), first.id)

    fund(shards, BOB, 200)
    for index, sneaky in enumerate((" Pink ", "pink", "PINK")):
        second = draft(service, store, title=sneaky, owner=BOB, source=f"b{index}")
        with pytest.raises(TitleTaken):
            service.publish(User(id=BOB), second.id)


def test_a_refused_title_costs_nothing(service, store, shards):
    """**등록비는 이름 확인 뒤에 빠진다.** 겹쳐서 실패했는데 돈만 나가면 안 된다."""
    fund(shards, ALICE, 100)
    first = draft(service, store, title="핑크 리본", source="a")
    service.publish(User(id=ALICE), first.id)

    fund(shards, BOB, 100)
    before = balance(shards, BOB)
    second = draft(service, store, title="핑크 리본", owner=BOB, source="b")

    with pytest.raises(TitleTaken):
        service.publish(User(id=BOB), second.id)

    assert balance(shards, BOB) == before
    # listing도 그대로 draft다 — 서버 상태가 하나도 바뀌지 않았다.
    assert store.listings[second.id].status.value == "draft"
    assert store.listings[second.id].publish_fee_paid is False


def test_republishing_the_same_listing_is_not_a_duplicate(service, store, shards):
    """자기 자리를 자기가 막지 않는다 — 재시도는 오류가 아니다."""
    fund(shards, ALICE, 100)
    listing = draft(service, store, title="핑크 리본")

    first = service.publish(User(id=ALICE), listing.id)
    second = service.publish(User(id=ALICE), listing.id)

    assert (first.published, second.published) == (True, False)
    # 등록비는 한 번만 빠진다.
    assert balance(shards, ALICE) == 90


def test_deleting_releases_the_title(service, store, shards):
    fund(shards, ALICE, 100)
    first = draft(service, store, title="핑크 리본", source="a")
    service.publish(User(id=ALICE), first.id)
    service.delete_listing(User(id=ALICE), first.id)

    # 놓아 준 이름은 다른 사람이 쓸 수 있다.
    fund(shards, BOB, 100)
    second = draft(service, store, title="핑크 리본", owner=BOB, source="b")
    assert service.publish(User(id=BOB), second.id).published is True


def test_concurrent_publish_has_exactly_one_winner(store, shards, service):
    """동시에 같은 이름을 올려도 정확히 한 명만 성공한다."""
    sellers = [f"seller-{index}" for index in range(8)]
    drafts = []
    for index, seller in enumerate(sellers):
        fund(shards, seller, 100)
        drafts.append(draft(service, store, title="핑크 리본", owner=seller, source=f"s{index}"))

    start = threading.Barrier(len(sellers))
    published: list[str] = []
    lock = threading.Lock()

    def run(seller: str, listing_id: str) -> None:
        start.wait()
        try:
            service.publish(User(id=seller), listing_id)
        except TitleTaken:
            return
        with lock:
            published.append(seller)

    threads = [
        threading.Thread(target=run, args=(seller, listing.id))
        for seller, listing in zip(sellers, drafts)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(published) == 1, f"{len(published)}개가 같은 이름으로 올라갔다"
    # 진 사람들은 등록비를 내지 않았다.
    losers = [seller for seller in sellers if seller not in published]
    assert all(balance(shards, seller) == 100 for seller in losers)


def test_the_fee_is_unchanged_by_the_new_rule():
    """이름 규칙이 등록비를 건드리지 않는다."""
    assert MarketplacePublishPolicy.fee(ContentType.MIRROR) == 10
    assert MarketplacePublishPolicy.fee(ContentType.STICKER) == 10


# MARK: - 조치 알림


def test_takedown_notifies_the_seller_once(store, shards, service):
    """운영자가 내리면 **판매자에게 한 번** 알린다.

    상태만 바꾸고 말하지 않으면 판매자는 자기 상품이 왜 사라졌는지 알 수 없다.
    """
    from app.marketplace.models import ModerationReason
    from app.notifications.models import NotificationType
    from app.notifications.store import InMemoryNotificationStore

    notifications = InMemoryNotificationStore()
    marketplace = InMemoryMarketplaceStore(shards._store, notifications=notifications)
    moderated = MarketplaceService(marketplace, shards)

    fund(shards, ALICE, 100)
    listing = draft(moderated, marketplace, title="핑크 리본")
    moderated.publish(User(id=ALICE), listing.id)

    moderated.admin_takedown(
        User(id=BOB), listing.id, reason=ModerationReason.INAPPROPRIATE
    )

    events = list(notifications.events.values())
    assert len(events) == 1
    event = events[0]
    assert event.type is NotificationType.MARKETPLACE_TAKEDOWN
    # **판매자에게만** 간다.
    assert event.user_id == ALICE
    assert event.listing_id == listing.id
    assert event.content_type == "mirror"
    assert event.title_snapshot == "핑크 리본"
    assert "내려갔어요" in event.headline
    # 사유를 말한다 — 모든 조치를 "부적절한 내용" 하나로 덮지 않는다.
    assert "부적절한 내용" in event.body
    assert "거울" in event.body


def test_takedown_reason_is_not_flattened(store, shards, service):
    """사유마다 다른 말을 한다."""
    from app.marketplace.models import ModerationReason
    from app.marketplace.store import _takedown_event

    listing = Listing(
        id="l-1", seller_user_id=ALICE, content_type=ContentType.STICKER,
        title="퉁퉁퉁", description="", price_shards=0, snapshot_id="s-1",
    )
    bodies = {
        reason: _takedown_event(listing, reason).body
        for reason in ModerationReason
    }
    # 넷이 서로 다른 말이다.
    assert len(set(bodies.values())) == len(ModerationReason)
    # 스티커에는 스티커라고 말한다.
    assert all("스티커" in body for body in bodies.values())


def test_retrying_a_takedown_does_not_notify_twice(store, shards, service):
    """이미 내려가 있으면 아무것도 쓰지 않는다 — 알림도 늘지 않는다."""
    from app.marketplace.models import ModerationReason
    from app.notifications.store import InMemoryNotificationStore

    notifications = InMemoryNotificationStore()
    marketplace = InMemoryMarketplaceStore(shards._store, notifications=notifications)
    moderated = MarketplaceService(marketplace, shards)

    fund(shards, ALICE, 100)
    listing = draft(moderated, marketplace, title="핑크 리본")
    moderated.publish(User(id=ALICE), listing.id)

    for _ in range(3):
        moderated.admin_takedown(User(id=BOB), listing.id, reason=ModerationReason.SPAM)

    assert len(notifications.events) == 1


def test_takedown_notice_id_comes_from_the_listing():
    """문서 자리가 listing id에서 나온다 — 재시도가 알림을 두 번 만들 수 없다."""
    from app.notifications.models import takedown_event_id

    assert takedown_event_id("l-1") == takedown_event_id("l-1")
    assert takedown_event_id("l-1") != takedown_event_id("l-2")
    # raw id를 문서 ID에 노출하지 않는다(원장 · 소유권과 같은 규칙).
    assert "l-1" not in takedown_event_id("l-1")
