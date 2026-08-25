"""운영자 상품 내리기 / 복구 (Phase E).

가장 중요한 것 둘:

1. **권한을 client가 정하지 않는다.** 화면을 강제로 열어도 서버에서 막힌다.
2. **이미 산 사람이 손해를 보지 않는다.** 내리는 것은 새 유통을 막는 일이지
   구매자의 파일을 부수는 일이 아니다.

나머지는 그 주변이다 — 판매자가 우회할 수 없는가, 경제가 그대로인가,
사용자가 삭제한 것을 운영자가 되살리지 않는가, 연타가 기록을 오염시키지 않는가.
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.auth.models import User, sha256_hex
from app.auth.store import InMemoryAuthStore
from app.core.config import Settings
from app.main import create_app
from app.marketplace.models import (
    ContentType,
    InvalidListing,
    Listing,
    ListingNotFound,
    ListingStatus,
    ModeratedListing,
    ModerationAction,
    ModerationReason,
    ModerationStatus,
    NotModerated,
    Snapshot,
    TerminalListing,
)
from app.marketplace.service import MarketplaceService
from app.marketplace.store import InMemoryMarketplaceStore
from app.shards.models import ShardReason
from app.shards.service import ShardLedgerService
from app.shards.store import InMemoryShardStore
from tests.conftest import CLIENT_ID, apple_claims

SELLER = "11111111-2222-4333-8444-555555555555"
BUYER = "99999999-8888-4777-8666-555555555555"
#: 운영자. **실제 계정 id를 쓰지 않는다** — repo에 운영자 신원을 남기지 않는다.
OPERATOR = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


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


def user(user_id: str) -> User:
    return User(id=user_id)


def seed(shards: ShardLedgerService, who: str, amount: int) -> None:
    shards.credit(who, amount, ShardReason.ADMIN_ADJUSTMENT, external_event_id=f"seed:{who}")


def snapshot(store, kind: ContentType, owner: str = SELLER, source: str = "local-1") -> str:
    found = Snapshot(
        id=f"snap-{kind.value}-{source}",
        seller_user_id=owner,
        content_type=kind,
        manifest_checksum="deadbeef",
        source_content_id=source,
    )
    store.snapshots[found.id] = found
    return found.id


def published(
    service, store, shards, *,
    kind: ContentType = ContentType.MIRROR, price: int = 0, source: str = "local-1",
) -> Listing:
    seed(shards, SELLER, 100)
    draft = service.create_draft(
        user(SELLER),
        content_type=kind.value,
        title="내 거울",
        description="설명",
        price_shards=price,
        snapshot_id=snapshot(store, kind, SELLER, source),
    )
    return service.publish(user(SELLER), draft.id).listing


def economy(shard_store, store) -> dict:
    """조치 전후를 통째로 비교하는 스냅샷."""
    return {
        "wallets": {x: shard_store.wallets[x].balance for x in shard_store.wallets},
        "ledger": len(shard_store.entries),
        "ownership": sorted(store.ownership_records),
        "downloads": {k: v.download_count for k, v in store.listings.items()},
        "likes": {k: v.like_count for k, v in store.listings.items()},
    }


# MARK: - 조치 모델 (§6 · §7)


def test_legacy_listings_are_active():
    """**옛 문서에는 이 값이 없다.** 없으면 `active`다 — migration하지 않는다."""
    assert ModerationStatus.of(None) is ModerationStatus.ACTIVE
    assert ModerationStatus.of("") is ModerationStatus.ACTIVE
    assert Listing(
        id="x", seller_user_id=SELLER, content_type=ContentType.MIRROR,
        title="t", description="", price_shards=0, snapshot_id="s",
    ).moderation_status is ModerationStatus.ACTIVE


def test_unknown_moderation_value_hides_the_listing():
    """모르는 값이면 감춘다. **공개하는 쪽보다 감추는 쪽이 안전하다.**"""
    assert ModerationStatus.of("quarantined-by-some-future-phase") is ModerationStatus.REMOVED


def test_moderation_is_a_separate_axis_from_seller_status():
    """한 field로 합치지 않았다 — 합치면 판매자가 다시 올리는 것만으로 풀린다."""
    listing = Listing(
        id="x", seller_user_id=SELLER, content_type=ContentType.MIRROR,
        title="t", description="", price_shards=0, snapshot_id="s",
        status=ListingStatus.PUBLISHED,
        moderation_status=ModerationStatus.REMOVED,
    )
    assert listing.status is ListingStatus.PUBLISHED   # 판매자 축은 그대로
    assert not listing.is_visible                      # 공개는 되지 않는다


# MARK: - 내리기 (§8 · §22 · §42)


@pytest.mark.parametrize("kind", [ContentType.MIRROR, ContentType.STICKER])
def test_takedown_hides_from_the_public_feed(service, store, shards, kind):
    listing = published(service, store, shards, kind=kind)
    assert [x.id for x in service.browse()] == [listing.id]

    service.admin_takedown(user(OPERATOR), listing.id, reason=ModerationReason.SPAM)

    assert service.browse() == []
    with pytest.raises(ListingNotFound):
        service.listing(listing.id)


@pytest.mark.parametrize("kind", [ContentType.MIRROR, ContentType.STICKER])
def test_takedown_blocks_new_purchase(service, store, shards, kind):
    """**목록에서 감추는 것만으로는 부족하다.** id를 아는 사람이 직접 부를 수 있다."""
    listing = published(service, store, shards, kind=kind, price=5)
    seed(shards, BUYER, 50)
    service.admin_takedown(user(OPERATOR), listing.id, reason=ModerationReason.INAPPROPRIATE)

    with pytest.raises(ListingNotFound):
        service.purchase(user(BUYER), listing.id)


def test_takedown_blocks_new_like(service, store, shards):
    listing = published(service, store, shards)
    service.admin_takedown(user(OPERATOR), listing.id, reason=ModerationReason.SPAM)

    with pytest.raises(ListingNotFound):
        service.like(user(BUYER), listing.id)


def test_takedown_hides_from_old_clients_too(service, store, shards):
    """1.0.7 앱은 이 field를 모른다. **목록을 서버가 만들기 때문에** 그래도 안 보인다.

    조치를 client 필터로 구현했다면 구버전 앱에는 그대로 노출됐을 것이다.
    """
    listing = published(service, store, shards)
    service.admin_takedown(user(OPERATOR), listing.id, reason=ModerationReason.COPYRIGHT)

    # 구버전이 쓰는 것과 **같은 목록 · 같은 상세**다. 버전 분기가 없다.
    assert service.browse() == []
    with pytest.raises(ListingNotFound):
        service.listing(listing.id)


def test_takedown_records_the_reason(service, store, shards):
    listing = published(service, store, shards)
    result = service.admin_takedown(
        user(OPERATOR), listing.id, reason=ModerationReason.COPYRIGHT
    )
    assert result.changed
    assert result.listing.moderation_reason is ModerationReason.COPYRIGHT
    assert result.listing.moderated_by == OPERATOR
    assert result.listing.moderated_at is not None


# MARK: - 이미 산 사람 (§22 · §24) — BLOCKER


@pytest.mark.parametrize("kind", [ContentType.MIRROR, ContentType.STICKER])
def test_existing_buyer_keeps_everything(service, store, shards, shard_store, kind):
    """**Phase E 전체에서 가장 중요한 test다.**

    A가 올리고 B가 샀다. 운영자가 내린다. B는 아무것도 잃지 않는다.
    """
    listing = published(service, store, shards, kind=kind, price=5)
    seed(shards, BUYER, 50)
    service.purchase(user(BUYER), listing.id)
    before = economy(shard_store, store)

    service.admin_takedown(user(OPERATOR), listing.id, reason=ModerationReason.INAPPROPRIATE)

    # 소유권이 그대로다.
    assert store.ownership(listing.id, BUYER) is not None
    assert [o.listing_id for o, _ in service.purchases(user(BUYER))] == [listing.id]
    # snapshot 문서도 GCS object도 지우지 않는다.
    assert store.snapshots[listing.snapshot_id].is_complete
    # 경제가 한 자리도 움직이지 않았다.
    assert economy(shard_store, store) == before


def test_moderated_listing_still_reaches_its_buyer(service, store, shards):
    """구매자의 전달 경로는 **공개 조회를 지나지 않는다** — 그래서 조치와 무관하다."""
    listing = published(service, store, shards, price=5)
    seed(shards, BUYER, 50)
    service.purchase(user(BUYER), listing.id)
    service.admin_takedown(user(OPERATOR), listing.id, reason=ModerationReason.SPAM)

    # 이 경로가 쓰는 것은 `any_listing` + 소유권이다.
    assert store.any_listing(listing.id).id == listing.id
    assert store.ownership(listing.id, BUYER) is not None


def test_takedown_does_not_touch_the_economy(service, store, shards, shard_store):
    """지갑 · 원장 · counter가 그대로다. **이 경로에 조각 service가 들어오지 않는다.**"""
    listing = published(service, store, shards, price=5)
    seed(shards, BUYER, 50)
    service.purchase(user(BUYER), listing.id)
    service.like(user(BUYER), listing.id)
    before = economy(shard_store, store)

    service.admin_takedown(user(OPERATOR), listing.id, reason=ModerationReason.SPAM)
    service.admin_restore(user(OPERATOR), listing.id)

    assert economy(shard_store, store) == before


def test_unlike_does_not_clear_a_takedown(service, store, shards):
    """좋아요 취소는 내려간 상품에도 허용된다 — **그때 조치가 지워지면 안 된다.**

    `likeCount`를 쓰면서 field를 하나씩 옮겨 적으면 조치가 조용히 사라진다.
    """
    listing = published(service, store, shards)
    service.like(user(BUYER), listing.id)
    service.admin_takedown(user(OPERATOR), listing.id, reason=ModerationReason.SPAM)

    service.unlike(user(BUYER), listing.id)

    assert store.any_listing(listing.id).is_moderated
    assert service.browse() == []


def test_purchase_does_not_clear_a_takedown(service, store, shards):
    """구매 counter를 올리는 경로도 같은 위험이 있다.

    구매 자체는 막히지만, 조치 **전에** 산 사람이 남긴 counter가 조치를 지우지 않는지
    본다 — 순서를 뒤집어 확인한다.
    """
    listing = published(service, store, shards, price=0)
    service.purchase(user(BUYER), listing.id)
    service.admin_takedown(user(OPERATOR), listing.id, reason=ModerationReason.SPAM)

    assert store.any_listing(listing.id).is_moderated
    assert store.any_listing(listing.id).download_count == 1


# MARK: - 복구 (§10 · §43)


def test_restore_makes_it_visible_again(service, store, shards):
    listing = published(service, store, shards)
    service.admin_takedown(user(OPERATOR), listing.id, reason=ModerationReason.OTHER)

    result = service.admin_restore(user(OPERATOR), listing.id)

    assert result.changed
    assert result.listing.moderation_reason is None
    assert [x.id for x in service.browse()] == [listing.id]


def test_restore_rejects_a_seller_deleted_listing(service, store, shards):
    """**사용자의 삭제가 운영자 조치보다 우선한다.**

    되살릴 수 있으면 "삭제"가 삭제가 아니게 된다.
    """
    listing = published(service, store, shards)
    service.admin_takedown(user(OPERATOR), listing.id, reason=ModerationReason.SPAM)
    service.delete_listing(user(SELLER), listing.id)

    with pytest.raises(TerminalListing):
        service.admin_restore(user(OPERATOR), listing.id)


def test_restore_rejects_a_listing_that_is_not_moderated(service, store, shards):
    listing = published(service, store, shards)
    with pytest.raises(NotModerated):
        service.admin_restore(user(OPERATOR), listing.id)


def test_restore_does_not_republish_an_unlisted_listing(service, store, shards):
    """판매자가 내려 둔 것을 운영자 복구가 대신 올려주지 않는다.

    복구는 **운영자 조치만** 되돌린다. 판매자의 뜻은 판매자가 정한다.
    """
    listing = published(service, store, shards)
    service.admin_takedown(user(OPERATOR), listing.id, reason=ModerationReason.SPAM)
    service.unpublish(user(SELLER), listing.id)

    restored = service.admin_restore(user(OPERATOR), listing.id).listing

    assert restored.status is ListingStatus.UNLISTED
    assert service.browse() == []


# MARK: - 판매자 우회 (§11 · §12 · §44)


def test_seller_cannot_republish_a_moderated_listing(service, store, shards):
    """판매자가 내렸다 다시 올려도 조치가 풀리지 않는다."""
    listing = published(service, store, shards)
    service.admin_takedown(user(OPERATOR), listing.id, reason=ModerationReason.SPAM)
    service.unpublish(user(SELLER), listing.id)

    with pytest.raises(ModeratedListing):
        service.publish(user(SELLER), listing.id)
    assert service.browse() == []


def test_seller_cannot_relist_the_same_source(service, store, shards):
    """**지웠다 새로 올리는 우회를 막는다.**

    같은 원본(`sourceContentId`)에서 나온 새 listing도 막힌다 — 안 그러면
    "삭제하고 다시 올리기" 한 번으로 조치가 무의미해진다.
    """
    listing = published(service, store, shards, source="local-1")
    service.admin_takedown(user(OPERATOR), listing.id, reason=ModerationReason.COPYRIGHT)
    service.delete_listing(user(SELLER), listing.id)

    again = service.create_draft(
        user(SELLER),
        content_type=ContentType.MIRROR.value,
        title="내 거울 2",
        description="",
        price_shards=0,
        snapshot_id=snapshot(store, ContentType.MIRROR, SELLER, "local-1"),
    )
    with pytest.raises(ModeratedListing):
        service.publish(user(SELLER), again.id)


def test_a_different_source_is_not_blocked(service, store, shards):
    """**조치되지 않은 다른 작품까지 막지 않는다.** 차단은 그 원본에만 걸린다."""
    listing = published(service, store, shards, source="local-1")
    service.admin_takedown(user(OPERATOR), listing.id, reason=ModerationReason.SPAM)

    other = service.create_draft(
        user(SELLER),
        content_type=ContentType.MIRROR.value,
        title="다른 거울",
        description="",
        price_shards=0,
        snapshot_id=snapshot(store, ContentType.MIRROR, SELLER, "local-2"),
    )
    assert service.publish(user(SELLER), other.id).published


def test_restore_reopens_the_normal_path(service, store, shards):
    listing = published(service, store, shards, source="local-1")
    service.admin_takedown(user(OPERATOR), listing.id, reason=ModerationReason.OTHER)
    service.admin_restore(user(OPERATOR), listing.id)

    again = service.create_draft(
        user(SELLER),
        content_type=ContentType.MIRROR.value,
        title="내 거울 2",
        description="",
        price_shards=0,
        snapshot_id=snapshot(store, ContentType.MIRROR, SELLER, "local-1"),
    )
    assert service.publish(user(SELLER), again.id).published


# MARK: - 기록 (§28 · §29)


def test_every_action_is_recorded(service, store, shards):
    listing = published(service, store, shards)
    service.admin_takedown(
        user(OPERATOR), listing.id, reason=ModerationReason.SPAM, note="내부 메모"
    )
    service.admin_restore(user(OPERATOR), listing.id)

    events = service.admin_events(listing.id)
    assert [x.action for x in events] == [ModerationAction.TAKEDOWN, ModerationAction.RESTORE]
    assert events[0].reason is ModerationReason.SPAM
    assert events[0].note == "내부 메모"
    # **이름이 아니라 internal id다** — 이름은 바뀌고 계정은 지워진다.
    assert all(x.actor_user_id == OPERATOR for x in events)


def test_repeated_takedown_writes_nothing(service, store, shards):
    """**연타가 기록을 채우면 진짜 조치가 언제였는지 알 수 없게 된다.**"""
    listing = published(service, store, shards)
    first = service.admin_takedown(user(OPERATOR), listing.id, reason=ModerationReason.SPAM)
    second = service.admin_takedown(
        user(OPERATOR), listing.id, reason=ModerationReason.COPYRIGHT
    )

    assert first.changed and not second.changed
    assert len(service.admin_events(listing.id)) == 1
    # 사유도 덮어쓰지 않는다 — 처음 판단이 기록이다.
    assert store.any_listing(listing.id).moderation_reason is ModerationReason.SPAM


def test_concurrent_takedowns_change_state_once(service, store, shards):
    listing = published(service, store, shards)
    results, errors = [], []

    def run():
        try:
            results.append(
                service.admin_takedown(user(OPERATOR), listing.id, reason=ModerationReason.SPAM)
            )
        except Exception as error:   # noqa: BLE001 — 실패도 기록해서 보고한다
            errors.append(error)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert sum(1 for x in results if x.changed) == 1
    assert len(service.admin_events(listing.id)) == 1


def test_note_is_bounded(service, store, shards):
    listing = published(service, store, shards)
    with pytest.raises(InvalidListing):
        service.admin_takedown(
            user(OPERATOR), listing.id, reason=ModerationReason.OTHER, note="가" * 201
        )


def test_moderation_events_are_not_the_shard_ledger(shard_store, service, store, shards):
    """조치 기록이 **조각 원장에 섞이지 않는다.** 섞이면 잔액 감사에서 잡음이 된다."""
    listing = published(service, store, shards)
    before = len(shard_store.entries)
    service.admin_takedown(user(OPERATOR), listing.id, reason=ModerationReason.SPAM)
    assert len(shard_store.entries) == before


# MARK: - 목록 (§13 · §14 · §39)


def test_admin_sees_every_state(service, store, shards):
    """**공개 목록과 정반대 요구다** — 안 보이면 내릴 수도 없다."""
    live = published(service, store, shards, source="a")
    hidden = published(service, store, shards, kind=ContentType.STICKER, source="b")
    service.admin_takedown(user(OPERATOR), hidden.id, reason=ModerationReason.SPAM)
    draft = service.create_draft(
        user(SELLER), content_type=ContentType.MIRROR.value, title="초안",
        description="", price_shards=0,
        snapshot_id=snapshot(store, ContentType.MIRROR, SELLER, "c"),
    )

    listings, _ = service.admin_listings()
    assert {x.id for x in listings} == {live.id, hidden.id, draft.id}


def test_admin_filters_by_content_type(service, store, shards):
    mirror = published(service, store, shards, source="a")
    published(service, store, shards, kind=ContentType.STICKER, source="b")

    listings, _ = service.admin_listings(content_type=ContentType.MIRROR)
    assert [x.id for x in listings] == [mirror.id]


def test_admin_filters_by_moderation_status(service, store, shards):
    live = published(service, store, shards, source="a")
    hidden = published(service, store, shards, source="b")
    service.admin_takedown(user(OPERATOR), hidden.id, reason=ModerationReason.SPAM)

    removed, _ = service.admin_listings(moderation_status=ModerationStatus.REMOVED)
    active, _ = service.admin_listings(moderation_status=ModerationStatus.ACTIVE)
    assert [x.id for x in removed] == [hidden.id]
    assert [x.id for x in active] == [live.id]


def test_admin_list_is_paginated(service, store, shards):
    """**전체를 한 번에 읽지 않는다.**"""
    seed(shards, SELLER, 500)
    for index in range(7):
        service.create_draft(
            user(SELLER), content_type=ContentType.MIRROR.value, title=f"{index}",
            description="", price_shards=0,
            snapshot_id=snapshot(store, ContentType.MIRROR, SELLER, f"src-{index}"),
        )

    seen, cursor, pages = [], None, 0
    while True:
        page, cursor = service.admin_listings(cursor=cursor, limit=3)
        seen.extend(x.id for x in page)
        pages += 1
        if cursor is None:
            break
        assert pages < 10, "cursor가 끝나지 않는다"

    assert len(seen) == 7
    assert len(set(seen)) == 7   # 같은 것을 두 번 주지 않는다


def test_admin_page_size_is_capped(service, store, shards):
    """client가 요청해도 무제한으로 읽지 않는다."""
    from app.marketplace.service import ADMIN_MAX_PAGE

    calls = []
    original = store.list_for_admin
    store.list_for_admin = lambda cursor, limit: (calls.append(limit), original(cursor, limit))[1]
    service.admin_listings(limit=100_000)
    assert calls == [ADMIN_MAX_PAGE]


# MARK: - 미리보기 (§16)


def test_admin_preview_reuses_the_public_object(service, store, shards):
    """**운영용 GCS 사본을 만들지 않는다** — 같은 key를 같은 reader로 읽는다."""
    import inspect

    source = inspect.getsource(MarketplaceService.admin_preview)
    assert "preview_key(snapshot.id)" in source
    assert "self._read(" in source


# MARK: - 계정 삭제 (§26 · §46)


def test_account_deletion_still_wins(service, store, shards):
    """조치된 상품의 판매자가 계정을 지워도 **A2b 규칙이 그대로다.**

    실제 삭제 service는 Firestore 전용이라 여기서는 그것이 하는 일을 그대로
    흉내 낸다 — 판매자 익명화 + 끝 상태. 조치가 그것을 되돌리지 못하는지 본다.
    """
    from dataclasses import replace
    from app.auth.deletion import DELETED_OWNER

    listing = published(service, store, shards, price=5)
    seed(shards, BUYER, 50)
    service.purchase(user(BUYER), listing.id)
    service.admin_takedown(user(OPERATOR), listing.id, reason=ModerationReason.SPAM)

    store.listings[listing.id] = replace(
        store.listings[listing.id],
        status=ListingStatus.DELETED,
        seller_user_id=DELETED_OWNER,
    )

    # 판매자 신원이 되살아나지 않는다.
    assert store.any_listing(listing.id).seller_user_id == DELETED_OWNER
    # 운영자 복구가 삭제를 뒤집지 않는다.
    with pytest.raises(TerminalListing):
        service.admin_restore(user(OPERATOR), listing.id)
    # 구매자는 그대로 갖고 있다.
    assert store.ownership(listing.id, BUYER) is not None


def test_moderation_event_holds_no_seller_identity(service, store, shards):
    """기록에 판매자 신원이 없다 — 계정을 지워도 여기서 되살아나지 않는다."""
    listing = published(service, store, shards)
    service.admin_takedown(user(OPERATOR), listing.id, reason=ModerationReason.SPAM)

    event = service.admin_events(listing.id)[0]
    assert SELLER not in (event.actor_user_id, event.listing_id, event.note)
    assert not hasattr(event, "seller_user_id")


# MARK: - API 권한 (§41)


@pytest.fixture
def auth_store() -> InMemoryAuthStore:
    return InMemoryAuthStore()


@pytest.fixture
def client(auth_store, store, shard_store, apple_key, jwks_of, monkeypatch) -> TestClient:
    from app.auth import jwks as jwks_module

    document = jwks_of(apple_key)
    monkeypatch.setattr(jwks_module, "http_jwks_fetch", lambda *a, **k: lambda: document)

    app = create_app(
        Settings(app_env="local", apple_client_id=CLIENT_ID),
        auth_store=auth_store,
        shard_store=shard_store,
        marketplace_store=store,
    )
    return TestClient(app, raise_server_exceptions=False)


def sign_in(client: TestClient, apple_key, subject: str = "001234.abcdef0123456789.1234") -> str:
    nonce = f"nonce-{subject}"
    token = apple_key.token(apple_claims(sub=subject, nonce=sha256_hex(nonce)))
    return client.post(
        "/auth/apple", json={"identityToken": token, "nonce": nonce}
    ).json()["accessToken"]


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


ADMIN_PATHS = [
    ("GET", "/admin/me", None),
    ("GET", "/admin/marketplace/listings", None),
    ("GET", "/admin/marketplace/listings/any", None),
    ("GET", "/admin/marketplace/listings/any/preview", None),
    ("POST", "/admin/marketplace/listings/any/takedown", {"reason": "spam"}),
    ("POST", "/admin/marketplace/listings/any/restore", None),
]


@pytest.mark.parametrize("method,path,body", ADMIN_PATHS)
def test_admin_endpoints_reject_anonymous(client, method, path, body):
    assert client.request(method, path, json=body).status_code == 401


@pytest.mark.parametrize("method,path,body", ADMIN_PATHS)
def test_admin_endpoints_reject_a_normal_user(client, apple_key, method, path, body):
    """로그인만으로는 되지 않는다. **403이지 404가 아니다** — 경로는 있고 권한이 없다."""
    token = sign_in(client, apple_key)
    assert client.request(method, path, json=body, headers=auth(token)).status_code == 403


def test_admin_endpoint_allows_an_allowlisted_user(client, auth_store, apple_key):
    token = sign_in(client, apple_key)
    who = client.get("/users/me", headers=auth(token)).json()["id"]
    auth_store.admins[who] = True

    response = client.get("/admin/me", headers=auth(token))
    assert response.status_code == 200
    assert response.json() == {"isAdmin": True}


def test_disabled_admin_is_forbidden(client, auth_store, apple_key):
    """문서를 지우지 않고 끌 수 있어야 기록이 남는다."""
    token = sign_in(client, apple_key)
    who = client.get("/users/me", headers=auth(token)).json()["id"]
    auth_store.admins[who] = False

    assert client.get("/admin/me", headers=auth(token)).status_code == 403


def test_client_cannot_claim_to_be_admin(client, apple_key):
    """**client가 보낸 어떤 값도 권한이 되지 않는다.**"""
    token = sign_in(client, apple_key)
    for payload in (
        {"isAdmin": True},
        {"role": "admin"},
        {"reason": "spam", "isAdmin": True},
    ):
        assert client.post(
            "/admin/marketplace/listings/any/takedown", json=payload, headers=auth(token)
        ).status_code == 403
    # header로도 안 된다.
    assert client.get(
        "/admin/me", headers={**auth(token), "X-Admin": "true", "X-Role": "admin"}
    ).status_code == 403


def test_takedown_body_cannot_carry_authority(client, auth_store, apple_key):
    """운영자라도 body로 주인·상태·경제를 정할 수 없다 — **받을 자리가 없다.**"""
    token = sign_in(client, apple_key)
    who = client.get("/users/me", headers=auth(token)).json()["id"]
    auth_store.admins[who] = True

    response = client.post(
        "/admin/marketplace/listings/any/takedown",
        json={"reason": "spam", "sellerId": BUYER, "status": "deleted", "balance": 9999},
        headers=auth(token),
    )
    # 422다 — 모르는 field를 조용히 무시하지 않는다.
    assert response.status_code == 422


def test_admin_response_has_no_internal_identity(client, auth_store, apple_key):
    """§15 — 내부 id · Apple subject · 인증 metadata가 나가지 않는다."""
    token = sign_in(client, apple_key)
    who = client.get("/users/me", headers=auth(token)).json()["id"]
    auth_store.admins[who] = True

    body = client.get("/admin/marketplace/listings", headers=auth(token)).text
    for leak in ("sellerUserId", "snapshotId", "moderatedBy", "sub", "identityToken", "email"):
        assert leak not in body


def test_admin_status_is_not_in_the_public_profile(client, auth_store, apple_key):
    """`/users/me`에 권한을 섞지 않는다 — 섞으면 화면이 그 값을 믿기 시작한다."""
    token = sign_in(client, apple_key)
    who = client.get("/users/me", headers=auth(token)).json()["id"]
    auth_store.admins[who] = True

    body = client.get("/users/me", headers=auth(token)).json()
    assert "isAdmin" not in body and "role" not in body


def test_no_endpoint_writes_the_admin_allowlist(client):
    """**운영자 등록/해제 API를 만들지 않았다.** 사람이 Firestore에서 직접 한다."""
    paths = [getattr(x, "path", "") for x in client.app.routes]
    assert not [x for x in paths if "admin" in x and ("users" in x or x.endswith("/admins"))]

    import inspect
    import app.api.admin as admin_module

    source = inspect.getsource(admin_module)
    for writer in ("is_admin =", "admins[", "ADMIN_USERS", "set_admin"):
        assert writer not in source


def test_takedown_and_restore_round_trip_over_http(client, auth_store, apple_key, store, shards):
    """실제 endpoint로 한 바퀴. 조치 → 목록에서 사라짐 → 복구 → 다시 보임."""
    token = sign_in(client, apple_key)
    who = client.get("/users/me", headers=auth(token)).json()["id"]
    auth_store.admins[who] = True

    service = MarketplaceService(store, shards)
    listing = published(service, store, shards)

    assert len(client.get("/marketplace/listings").json()) == 1

    down = client.post(
        f"/admin/marketplace/listings/{listing.id}/takedown",
        json={"reason": "inappropriate_content"},
        headers=auth(token),
    )
    assert down.status_code == 200
    assert down.json()["moderationStatus"] == "removed"
    assert down.json()["moderationReason"] == "inappropriate_content"
    assert client.get("/marketplace/listings").json() == []
    assert client.get(f"/marketplace/listings/{listing.id}").status_code == 404

    up = client.post(
        f"/admin/marketplace/listings/{listing.id}/restore", headers=auth(token)
    )
    assert up.status_code == 200
    assert up.json()["moderationStatus"] == "active"
    assert len(client.get("/marketplace/listings").json()) == 1


def test_restore_of_a_deleted_listing_is_a_conflict(client, auth_store, apple_key, store, shards):
    token = sign_in(client, apple_key)
    who = client.get("/users/me", headers=auth(token)).json()["id"]
    auth_store.admins[who] = True

    service = MarketplaceService(store, shards)
    listing = published(service, store, shards)
    service.admin_takedown(user(OPERATOR), listing.id, reason=ModerationReason.SPAM)
    service.delete_listing(user(SELLER), listing.id)

    response = client.post(
        f"/admin/marketplace/listings/{listing.id}/restore", headers=auth(token)
    )
    assert response.status_code == 409


def test_unknown_reason_is_rejected(client, auth_store, apple_key, store, shards):
    """분류를 client가 늘리지 못한다."""
    token = sign_in(client, apple_key)
    who = client.get("/users/me", headers=auth(token)).json()["id"]
    auth_store.admins[who] = True

    listing = published(MarketplaceService(store, shards), store, shards)
    assert client.post(
        f"/admin/marketplace/listings/{listing.id}/takedown",
        json={"reason": "because-i-said-so"},
        headers=auth(token),
    ).status_code == 422


def test_seller_sees_that_it_was_stopped(client, auth_store, apple_key, store, shards):
    """§25 — 판매자에게는 **상태만** 알린다. 사유 분류는 주지 않는다."""
    token = sign_in(client, apple_key)
    who = client.get("/users/me", headers=auth(token)).json()["id"]
    auth_store.admins[who] = True

    service = MarketplaceService(store, shards)
    seed(shards, who, 100)
    draft = service.create_draft(
        user(who), content_type=ContentType.MIRROR.value, title="내 거울",
        description="", price_shards=0,
        snapshot_id=snapshot(store, ContentType.MIRROR, who, "mine"),
    )
    service.publish(user(who), draft.id)
    service.admin_takedown(user(OPERATOR), draft.id, reason=ModerationReason.COPYRIGHT)

    mine = client.get("/users/me/marketplace/listings", headers=auth(token)).json()
    assert mine[0]["moderationStatus"] == "removed"
    assert "moderationReason" not in mine[0]

    # 판매자에게 "다시 판매" 버튼을 주지 않는다 — 서버가 거절한다.
    assert client.post(
        f"/marketplace/listings/{draft.id}/publish", headers=auth(token)
    ).status_code == 409


def test_logs_have_no_admin_secrets(client, auth_store, apple_key, store, shards, caplog):
    import logging

    token = sign_in(client, apple_key)
    who = client.get("/users/me", headers=auth(token)).json()["id"]
    auth_store.admins[who] = True
    listing = published(MarketplaceService(store, shards), store, shards)

    with caplog.at_level(logging.INFO):
        client.post(
            f"/admin/marketplace/listings/{listing.id}/takedown",
            json={"reason": "spam", "note": "내부 메모"},
            headers=auth(token),
        )

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert token not in text
    assert "Bearer" not in text
    # 내부 메모도 판매자 id도 로그로 새지 않는다.
    assert "내부 메모" not in text
    assert SELLER not in text


# MARK: - Firestore 구현 (§7 · §30)
#
# 여기까지는 in-memory 저장소였다. **아래는 `FirestoreMarketplaceStore` 코드 자체를**
# 최소 fake db로 돌린다 — production Firestore를 부르지 않는다.


@pytest.fixture
def fs():
    from datetime import datetime, timezone
    from app.marketplace.firestore_store import FirestoreMarketplaceStore
    from app.marketplace.store import LISTINGS, SNAPSHOTS
    from tests.test_marketplace import FakeDatabase

    db = FakeDatabase()
    when = datetime(2026, 8, 1, tzinfo=timezone.utc)
    db.data[LISTINGS] = {
        # **1.0.7 시절 문서다 — moderation field가 하나도 없다.**
        "legacy": {
            "sellerUserId": SELLER, "contentType": "mirror", "title": "예전 상품",
            "description": "", "priceShards": 0, "snapshotId": "snap",
            "status": "published", "publishFeePaid": True,
            "downloadCount": 3, "likeCount": 2,
            "createdAt": when, "updatedAt": when, "publishedAt": when,
            "schemaVersion": 1,
        }
    }
    db.data[SNAPSHOTS] = {
        "snap": {
            "sellerUserId": SELLER, "contentType": "mirror",
            "manifestChecksum": "deadbeef", "sourceContentId": "local-1",
            "createdAt": when, "schemaVersion": 1,
        }
    }
    return FirestoreMarketplaceStore(db), db


def test_firestore_legacy_document_is_public(fs):
    """**field가 없는 문서가 그대로 보인다.** migration을 요구하지 않는다."""
    store, _ = fs
    assert store.get_published("legacy").moderation_status is ModerationStatus.ACTIVE
    assert [x.id for x in store.list_published()] == ["legacy"]


def test_firestore_takedown_hides_and_blocks(fs):
    from app.marketplace.store import LISTINGS, MODERATION_BLOCKS, MODERATION_EVENTS

    store, db = fs
    result = store.takedown("legacy", OPERATOR, ModerationReason.SPAM, "메모")

    assert result.changed
    assert db.data[LISTINGS]["legacy"]["moderationStatus"] == "removed"
    assert db.data[LISTINGS]["legacy"]["moderationReason"] == "spam"
    # 상태 · 차단 · 기록이 **한 commit**이다.
    assert len(db.data[MODERATION_BLOCKS]) == 1
    assert len(db.data[MODERATION_EVENTS]) == 1
    assert db.transactions[-1].commits == 1

    assert store.list_published() == []
    with pytest.raises(ListingNotFound):
        store.get_published("legacy")


def test_firestore_takedown_does_not_touch_the_economy(fs):
    from app.marketplace.store import LISTINGS

    store, db = fs
    before = dict(db.data[LISTINGS]["legacy"])
    store.takedown("legacy", OPERATOR, ModerationReason.SPAM, "")
    after = db.data[LISTINGS]["legacy"]

    assert after["downloadCount"] == before["downloadCount"]
    assert after["likeCount"] == before["likeCount"]
    assert after["priceShards"] == before["priceShards"]
    assert after["publishFeePaid"] == before["publishFeePaid"]
    # 지갑 · 원장 collection이 아예 생기지 않았다.
    assert not [x for x in db.data if "wallet" in x or "ledger" in x]


def test_firestore_repeated_takedown_writes_nothing(fs):
    from app.marketplace.store import MODERATION_EVENTS

    store, db = fs
    store.takedown("legacy", OPERATOR, ModerationReason.SPAM, "")
    commits = db.transactions[-1].commits
    second = store.takedown("legacy", OPERATOR, ModerationReason.OTHER, "")

    assert not second.changed
    assert len(db.data[MODERATION_EVENTS]) == 1
    assert db.transactions[-1].commits == commits   # 새 transaction이 쓰지 않았다


@pytest.mark.parametrize("aborts", [0, 1, 3])
def test_firestore_takedown_survives_aborted_retry(fs, aborts):
    """재시도가 기록을 두 번 남기지 않는다 — attempt마다 staged write를 버린다."""
    from app.marketplace.store import LISTINGS, MODERATION_EVENTS

    store, db = fs
    db.aborts = aborts
    store.takedown("legacy", OPERATOR, ModerationReason.SPAM, "")

    assert db.data[LISTINGS]["legacy"]["moderationStatus"] == "removed"
    assert len(db.data[MODERATION_EVENTS]) == 1


def test_firestore_restore_clears_the_block(fs):
    from app.marketplace.store import LISTINGS, MODERATION_BLOCKS

    store, db = fs
    store.takedown("legacy", OPERATOR, ModerationReason.SPAM, "")
    store.restore("legacy", OPERATOR)

    assert db.data[LISTINGS]["legacy"]["moderationStatus"] == "active"
    assert db.data[LISTINGS]["legacy"]["moderationReason"] is None
    assert db.data[MODERATION_BLOCKS] == {}
    assert [x.id for x in store.list_published()] == ["legacy"]


def test_firestore_restore_rejects_a_deleted_listing(fs):
    from app.marketplace.store import LISTINGS

    store, db = fs
    store.takedown("legacy", OPERATOR, ModerationReason.SPAM, "")
    db.data[LISTINGS]["legacy"]["status"] = "deleted"

    with pytest.raises(TerminalListing):
        store.restore("legacy", OPERATOR)


def test_firestore_restore_keeps_another_listings_block(fs):
    """같은 원본으로 두 번 조치했다면 하나를 복구해도 나머지가 풀리지 않는다."""
    from app.marketplace.store import MODERATION_BLOCKS

    store, db = fs
    store.takedown("legacy", OPERATOR, ModerationReason.SPAM, "")
    # 다른 listing이 같은 자리를 차지했다고 두고 복구한다.
    key = next(iter(db.data[MODERATION_BLOCKS]))
    db.data[MODERATION_BLOCKS][key]["listingId"] = "some-other-listing"

    store.restore("legacy", OPERATOR)
    assert key in db.data[MODERATION_BLOCKS]


def test_firestore_source_block_survives_a_new_listing(fs):
    store, db = fs
    store.takedown("legacy", OPERATOR, ModerationReason.COPYRIGHT, "")
    assert store.is_source_blocked(SELLER, store.any_listing("legacy"), "local-1")
    # 다른 원본은 막히지 않는다.
    assert not store.is_source_blocked(SELLER, store.any_listing("legacy"), "local-2")


def test_firestore_admin_list_is_paginated(fs):
    from datetime import datetime, timezone
    from app.marketplace.store import LISTINGS

    store, db = fs
    base = db.data[LISTINGS]["legacy"]
    for index in range(6):
        db.data[LISTINGS][f"item-{index}"] = {
            **base, "createdAt": datetime(2026, 8, 2 + index, tzinfo=timezone.utc)
        }

    seen, cursor, pages = [], None, 0
    while True:
        page, cursor = store.list_for_admin(cursor, 3)
        seen.extend(x.id for x in page)
        pages += 1
        if cursor is None:
            break
        assert pages < 10, "cursor가 끝나지 않는다"

    assert len(seen) == 7 and len(set(seen)) == 7
    # 최신이 먼저다.
    assert seen[0] == "item-5"


def test_firestore_admin_list_needs_no_composite_index(fs):
    """`where` + `order_by`를 쓰지 않는다 — production에 새 index를 요구하지 않는다.

    fake db가 그 조합에서 실패하므로, 여기가 통과한다는 것이 곧 증거다.
    """
    store, _ = fs
    assert store.list_for_admin(None, 5)[0]


def test_firestore_events_are_readable(fs):
    store, _ = fs
    store.takedown("legacy", OPERATOR, ModerationReason.SPAM, "메모")
    store.restore("legacy", OPERATOR)

    events = store.moderation_events("legacy")
    assert [x.action for x in events] == [ModerationAction.TAKEDOWN, ModerationAction.RESTORE]
    assert events[0].actor_user_id == OPERATOR


class StubShards:
    """`publish`가 조각을 만지기 **전에** 거절하는지 보기 위한 최소 stub.

    조각을 쓰려고 하면 곧바로 실패한다 — 조치된 상품에 등록비가 나갔다면
    여기서 터진다.
    """

    def __init__(self, db) -> None:
        self._db = db

    def transaction(self):
        return self._db.transaction()

    def context(self, transaction):
        return self

    def wallet(self, user_id):
        raise AssertionError("조치된 상품인데 지갑을 읽었다")

    def apply_in_transaction(self, *args, **kwargs):
        raise AssertionError("조치된 상품인데 조각을 움직였다")


def test_firestore_publish_rejects_a_moderated_listing(fs):
    from app.marketplace.store import LISTINGS

    store, db = fs
    store.takedown("legacy", OPERATOR, ModerationReason.SPAM, "")
    db.data[LISTINGS]["legacy"]["status"] = "unlisted"

    with pytest.raises(ModeratedListing):
        store.publish("legacy", SELLER, StubShards(db))


def test_firestore_publish_rejects_a_blocked_source(fs):
    """조치된 원본으로 만든 **새 listing**도 막힌다."""
    from datetime import datetime, timezone
    from app.marketplace.store import LISTINGS

    store, db = fs
    store.takedown("legacy", OPERATOR, ModerationReason.COPYRIGHT, "")
    db.data[LISTINGS]["again"] = {
        **db.data[LISTINGS]["legacy"],
        "status": "draft", "publishFeePaid": True, "publishedAt": None,
        "moderationStatus": "active", "moderationReason": None,
        "createdAt": datetime(2026, 8, 9, tzinfo=timezone.utc),
    }

    with pytest.raises(ModeratedListing):
        store.publish("again", SELLER, StubShards(db))


def test_firestore_document_round_trips_moderation(fs):
    """새로 쓰는 문서에 조치 field가 들어가고 그대로 읽힌다."""
    from app.marketplace.firestore_store import _document, _listing_from

    store, _ = fs
    store.takedown("legacy", OPERATOR, ModerationReason.COPYRIGHT, "")
    listing = store.any_listing("legacy")

    again = _listing_from(listing.id, _document(listing))
    assert again.moderation_status is ModerationStatus.REMOVED
    assert again.moderation_reason is ModerationReason.COPYRIGHT
    assert again.moderated_by == OPERATOR
