"""판매 알림 · 기기 등록 · 알림센터 (Phase F).

가장 중요한 것 셋:

1. **push 실패가 구매를 되돌리지 않는다.** 알림은 최선을 다하는 것이고
   경제는 그것에 매달리지 않는다.
2. **한 판매에 알림 하나.** 재시도·연타가 판매자에게 같은 알림을 다시 보내지 않는다.
3. **A가 B의 판매 알림을 받지 않는다.** 한 기기를 두 계정이 쓸 수 있다.

나머지는 그 주변이다 — token이 로그로 새지 않는가, 총 판매 횟수가 정확한가,
계정을 지우면 token이 남지 않는가.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.auth.models import User, sha256_hex
from app.auth.store import InMemoryAuthStore
from app.core.config import Settings
from app.main import create_app
from app.marketplace.models import ContentType, Listing, Snapshot
from app.marketplace.service import MarketplaceService
from app.marketplace.store import InMemoryMarketplaceStore
from app.notifications.models import (
    NotificationEvent,
    NotificationNotFound,
    NotificationType,
    sale_event_id,
)
from app.notifications.service import NotificationService
from app.notifications.store import InMemoryNotificationStore
from app.push.models import (
    InvalidPushDevice,
    PushDevice,
    PushEnvironment,
    PushMessage,
    PushOutcome,
    checked_token,
    push_device_id,
    token_fingerprint,
)
from app.push.provider import NullPushProvider, TERMINAL_REASONS
from app.push.service import PushService, sale_message
from app.push.store import InMemoryPushStore
from app.shards.models import ShardReason
from app.shards.service import ShardLedgerService
from app.shards.store import InMemoryShardStore
from tests.conftest import CLIENT_ID, apple_claims

SELLER = "11111111-2222-4333-8444-555555555555"
BUYER = "99999999-8888-4777-8666-555555555555"
OTHER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

TOKEN_A = "a1" * 32
TOKEN_B = "b2" * 32


# MARK: - fixture


@pytest.fixture
def shard_store() -> InMemoryShardStore:
    return InMemoryShardStore()


@pytest.fixture
def shards(shard_store) -> ShardLedgerService:
    return ShardLedgerService(shard_store)


@pytest.fixture
def notification_store() -> InMemoryNotificationStore:
    return InMemoryNotificationStore()


@pytest.fixture
def store(shard_store, notification_store) -> InMemoryMarketplaceStore:
    return InMemoryMarketplaceStore(shard_store, notifications=notification_store)


@pytest.fixture
def service(store, shards) -> MarketplaceService:
    return MarketplaceService(store, shards)


@pytest.fixture
def push_store() -> InMemoryPushStore:
    return InMemoryPushStore()


class FakePushProvider:
    """자동 test는 **절대 실제 APNs로 나가지 않는다.**"""

    def __init__(self, *, available: bool = True) -> None:
        self._available = available
        self.sent: list[tuple[str, PushMessage]] = []
        self.terminal_for: set[str] = set()
        self.raises = False
        self.fails = False

    @property
    def is_available(self) -> bool:
        return self._available

    def send(self, device: PushDevice, message: PushMessage) -> PushOutcome:
        self.sent.append((device.id, message))
        if self.raises:
            raise RuntimeError("provider exploded")
        if device.id in self.terminal_for:
            return PushOutcome(device_id=device.id, delivered=False, terminal=True, status=410)
        if self.fails:
            return PushOutcome(device_id=device.id, delivered=False, status=503)
        return PushOutcome(device_id=device.id, delivered=True, status=200)


@pytest.fixture
def provider() -> FakePushProvider:
    return FakePushProvider()


@pytest.fixture
def pushes(push_store, provider) -> PushService:
    return PushService(push_store, provider)


def user(user_id: str) -> User:
    return User(id=user_id)


def seed(shards: ShardLedgerService, who: str, amount: int) -> None:
    shards.credit(who, amount, ShardReason.ADMIN_ADJUSTMENT, external_event_id=f"seed:{who}")


def published(
    service, store, shards, *, kind: ContentType = ContentType.MIRROR,
    price: int = 3, title: str = "먹방거울", source: str = "local-1",
) -> Listing:
    seed(shards, SELLER, 100)
    snapshot = Snapshot(
        id=f"snap-{source}", seller_user_id=SELLER, content_type=kind,
        manifest_checksum="deadbeef", source_content_id=source,
    )
    store.snapshots[snapshot.id] = snapshot
    draft = service.create_draft(
        user(SELLER), content_type=kind.value, title=title,
        description="", price_shards=price, snapshot_id=snapshot.id,
    )
    return service.publish(user(SELLER), draft.id).listing


def economy(shard_store, store) -> dict:
    return {
        "wallets": {x: shard_store.wallets[x].balance for x in shard_store.wallets},
        "ledger": len(shard_store.entries),
        "ownership": sorted(store.ownership_records),
        "downloads": {k: v.download_count for k, v in store.listings.items()},
    }


# MARK: - 판매 이벤트 (§19 · §20 · §51)


@pytest.mark.parametrize("kind", [ContentType.MIRROR, ContentType.STICKER])
def test_purchase_creates_exactly_one_sale_event(
    service, store, shards, notification_store, kind
):
    listing = published(service, store, shards, kind=kind)
    seed(shards, BUYER, 50)

    result = service.purchase(user(BUYER), listing.id)

    assert result.purchased
    events = list(notification_store.events.values())
    assert len(events) == 1
    assert events[0].user_id == SELLER          # 판매자에게 간다
    assert events[0].listing_id == listing.id
    assert events[0].content_type == kind.value
    assert events[0].shard_amount == 3
    assert events[0].title_snapshot == "먹방거울"


def test_sale_event_is_written_in_the_purchase_commit(service, store, shards, notification_store):
    """**소유권과 같은 commit이다.** 밖에서 쓰면 "돈은 오갔는데 판매자는 모르는"
    상태가 생기고, 나중에 그것을 메울 방법이 없다."""
    listing = published(service, store, shards)
    seed(shards, BUYER, 50)
    service.purchase(user(BUYER), listing.id)

    key = sorted(store.ownership_records)[0]
    # 문서 자리가 소유권 열쇠에서 나온다 — 그것이 정확히 한 번의 근거다.
    assert sale_event_id(key) in notification_store.events


def test_repeated_purchase_does_not_duplicate_the_sale_event(
    service, store, shards, notification_store, shard_store
):
    """**BLOCKER guard.** 연타·재시도가 판매자에게 같은 알림을 다시 보내지 않는다."""
    listing = published(service, store, shards)
    seed(shards, BUYER, 50)
    service.purchase(user(BUYER), listing.id)
    after_first = economy(shard_store, store)

    for _ in range(4):
        again = service.purchase(user(BUYER), listing.id)
        assert again.already_owned
        # 새로 팔린 것이 없으므로 보낼 알림도 없다.
        assert again.sale_event is None

    assert len(notification_store.events) == 1
    assert economy(shard_store, store) == after_first


def test_free_acquisition_still_tells_the_seller(service, store, shards, notification_store):
    """무료여도 판매자는 누가 받아 갔는지 알고 싶다. **조각 이야기는 하지 않는다.**"""
    listing = published(service, store, shards, price=0)
    service.purchase(user(BUYER), listing.id)

    event = list(notification_store.events.values())[0]
    assert event.shard_amount == 0
    assert "0조각" not in sale_message(event).body


def test_sale_event_holds_no_buyer_identity(service, store, shards, notification_store):
    """**구매자를 담지 않는다** — 잠금화면에 뜰 수 있는 값이다."""
    listing = published(service, store, shards)
    seed(shards, BUYER, 50)
    service.purchase(user(BUYER), listing.id)

    event = list(notification_store.events.values())[0]
    assert BUYER not in (event.user_id, event.listing_id, event.title_snapshot)
    assert not hasattr(event, "buyer_user_id")
    message = sale_message(event)
    assert BUYER not in message.title and BUYER not in message.body


def test_sale_amount_comes_from_the_transaction(service, store, shards, notification_store):
    """listing 가격을 다시 읽지 않는다 — transaction이 확정한 값이다."""
    listing = published(service, store, shards, price=7)
    seed(shards, BUYER, 50)
    result = service.purchase(user(BUYER), listing.id)

    assert result.sale_event.shard_amount == result.price_paid == 7


def test_sale_title_is_a_snapshot(service, store, shards, notification_store):
    """팔린 그때의 제목이다. 나중에 바뀌어도 기록은 그때를 가리킨다."""
    from dataclasses import replace

    listing = published(service, store, shards, title="먹방거울")
    seed(shards, BUYER, 50)
    service.purchase(user(BUYER), listing.id)
    store.listings[listing.id] = replace(store.listings[listing.id], title="바뀐이름")

    assert list(notification_store.events.values())[0].title_snapshot == "먹방거울"


# MARK: - push 실패 (§25 · §52) — BLOCKER


def test_push_failure_does_not_touch_the_economy(
    service, store, shards, shard_store, notification_store, pushes, provider
):
    """**Phase F에서 가장 중요한 test다.**

    APNs가 실패해도 구매 · 지갑 · 원장 · 소유권 · 알림 기록이 그대로다.
    """
    listing = published(service, store, shards)
    seed(shards, BUYER, 50)
    provider.fails = True

    result = service.purchase(user(BUYER), listing.id)
    sent = pushes.notify_sale(result.sale_event)

    assert sent == 0
    assert result.purchased
    assert store.ownership(listing.id, BUYER) is not None
    assert shard_store.wallets[SELLER].balance == 100 - 10 + 3
    assert shard_store.wallets[BUYER].balance == 50 - 3
    # **기록은 남는다** — 앱을 열면 무엇이 팔렸는지 볼 수 있어야 한다.
    assert len(notification_store.events) == 1


def test_provider_exception_does_not_escape(pushes, provider, push_store):
    """provider가 무엇을 던지든 위로 나가지 않는다 — 부르는 쪽은 이미 commit이 끝났다."""
    push_store.register(_device(SELLER, TOKEN_A))
    provider.raises = True

    assert pushes.notify_sale(_event()) == 0


def test_one_broken_device_does_not_block_the_others(pushes, provider, push_store):
    """아이폰을 바꾼 사람이 알림을 아예 못 받는 일이 없어야 한다."""
    push_store.register(_device(SELLER, TOKEN_A))
    push_store.register(_device(SELLER, TOKEN_B))
    provider.terminal_for = {push_device_id(TOKEN_A)}

    sent = pushes.notify_sale(_event())

    assert sent == 1
    assert len(provider.sent) == 2   # 둘 다 시도했다


def test_unavailable_provider_sends_nothing(push_store):
    """자격 증명이 없으면 아무것도 보내지 않는다. **조용히 성공했다고 하지 않는다.**"""
    provider = FakePushProvider(available=False)
    service = PushService(push_store, provider)
    push_store.register(_device(SELLER, TOKEN_A))

    assert service.notify_sale(_event()) == 0
    assert provider.sent == []


def test_null_provider_never_claims_delivery():
    outcome = NullPushProvider().send(_device(SELLER, TOKEN_A), PushMessage("t", "b"))
    assert not outcome.delivered and not outcome.terminal


# MARK: - token 정리 (§26)


def test_terminal_failure_disables_the_device(pushes, provider, push_store):
    push_store.register(_device(SELLER, TOKEN_A))
    provider.terminal_for = {push_device_id(TOKEN_A)}

    pushes.notify_sale(_event())

    assert push_store.devices(SELLER) == []


def test_temporary_failure_keeps_the_device(pushes, provider, push_store):
    """**5xx에 등록을 지우지 않는다** — 서버가 잠깐 흔들릴 때 사용자의 기기가 사라진다."""
    push_store.register(_device(SELLER, TOKEN_A))
    provider.fails = True

    pushes.notify_sale(_event())

    assert len(push_store.devices(SELLER)) == 1


def test_only_apple_terminal_reasons_are_terminal():
    """무엇이 "끝났다"인지 한 곳에서만 정한다."""
    assert "Unregistered" in TERMINAL_REASONS
    assert "BadDeviceToken" in TERMINAL_REASONS
    # 일시적인 것은 여기 없다.
    for temporary in ("TooManyRequests", "InternalServerError", "ServiceUnavailable"):
        assert temporary not in TERMINAL_REASONS


# MARK: - 기기 등록 (§11 · §12 · §13 · §50)


def test_device_id_is_not_the_raw_token():
    """**raw token을 문서 자리에 쓰지 않는다.**"""
    assert TOKEN_A not in push_device_id(TOKEN_A)
    assert len(push_device_id(TOKEN_A)) == 64


def test_device_id_does_not_include_the_user(pushes, push_store):
    """자리가 token 하나다 — 그래서 재등록이 곧 주인 교체다.

    `(user, token)`으로 자리를 만들었다면 A의 문서가 살아남아 **A가 계속
    B의 판매 알림을 받았을 것이다.**
    """
    a = pushes.register(SELLER, token=TOKEN_A, environment="production")
    b = pushes.register(BUYER, token=TOKEN_A, environment="production")
    assert a.id == b.id


def test_account_switch_rebinds_the_device(pushes, push_store):
    """**BLOCKER guard.** 로그아웃하고 다른 계정으로 들어온 기기."""
    pushes.register(SELLER, token=TOKEN_A, environment="production")
    pushes.register(BUYER, token=TOKEN_A, environment="production")

    assert push_store.devices(SELLER) == []
    assert [x.user_id for x in push_store.devices(BUYER)] == [BUYER]


def test_a_does_not_receive_bs_sales(pushes, push_store, provider):
    """계정을 바꾼 뒤 A의 알림이 그 기기로 가지 않는다."""
    pushes.register(SELLER, token=TOKEN_A, environment="production")
    pushes.register(BUYER, token=TOKEN_A, environment="production")

    pushes.notify_sale(_event(user_id=SELLER))

    assert provider.sent == []


def test_one_user_many_devices(pushes, push_store, provider):
    pushes.register(SELLER, token=TOKEN_A, environment="production")
    pushes.register(SELLER, token=TOKEN_B, environment="sandbox")

    assert pushes.notify_sale(_event()) == 2


def test_reregistering_keeps_the_first_time(pushes, push_store):
    first = pushes.register(SELLER, token=TOKEN_A, environment="production")
    again = pushes.register(SELLER, token=TOKEN_A, environment="production")
    assert again.created_at == first.created_at


def test_unregister_only_removes_my_own(pushes, push_store):
    pushes.register(SELLER, token=TOKEN_A, environment="production")

    assert not pushes.unregister(BUYER, TOKEN_A)      # 남의 등록은 못 지운다
    assert len(push_store.devices(SELLER)) == 1
    assert pushes.unregister(SELLER, TOKEN_A)
    assert push_store.devices(SELLER) == []


def test_environment_must_be_known(pushes):
    """**client 문자열을 그대로 믿지 않는다.**"""
    with pytest.raises(ValueError):
        pushes.register(SELLER, token=TOKEN_A, environment="whatever-i-say")


def test_environment_picks_the_right_host():
    assert PushEnvironment.SANDBOX.host == "api.sandbox.push.apple.com"
    assert PushEnvironment.PRODUCTION.host == "api.push.apple.com"


@pytest.mark.parametrize("bad", ["", "   ", "not-hex!!", "abc", "ff" * 200, "ZZ" * 32])
def test_invalid_tokens_are_rejected(pushes, bad):
    with pytest.raises(InvalidPushDevice):
        pushes.register(SELLER, token=bad, environment="production")


def test_token_fingerprint_is_not_the_token():
    fingerprint = token_fingerprint(TOKEN_A)
    assert TOKEN_A not in fingerprint and len(fingerprint) == 12


# MARK: - 알림센터 (§28 · §31 · §33 · §54)


@pytest.fixture
def center(notification_store, service) -> NotificationService:
    return NotificationService(notification_store, service)


def test_only_the_owner_sees_their_notifications(center, notification_store):
    notification_store.create(_event(user_id=SELLER, event_id="e1"))
    notification_store.create(_event(user_id=BUYER, event_id="e2"))

    mine, _ = center.page(user(SELLER))
    assert [x.id for x in mine] == ["e1"]


def test_notifications_are_paginated(center, notification_store):
    from datetime import UTC, datetime

    for index in range(7):
        notification_store.create(
            _event(user_id=SELLER, event_id=f"e{index}",
                   created_at=datetime(2026, 8, 1 + index, tzinfo=UTC))
        )

    seen, cursor, pages = [], None, 0
    while True:
        page, cursor = center.page(user(SELLER), cursor=cursor, limit=3)
        seen.extend(x.id for x in page)
        pages += 1
        if cursor is None:
            break
        assert pages < 10, "cursor가 끝나지 않는다"

    assert len(seen) == 7 and len(set(seen)) == 7
    assert seen[0] == "e6"   # 최신이 먼저다


def test_marking_read_is_idempotent(center, notification_store):
    notification_store.create(_event(user_id=SELLER, event_id="e1"))

    first = center.mark_read(user(SELLER), "e1")
    again = center.mark_read(user(SELLER), "e1")

    assert first.is_read and again.read_at == first.read_at


def test_cannot_mark_someone_elses_notification(center, notification_store):
    notification_store.create(_event(user_id=SELLER, event_id="e1"))
    with pytest.raises(NotificationNotFound):
        center.mark_read(user(BUYER), "e1")


# MARK: - 판매 현황 (§29 · §30 · §31 · §32)


def test_sale_stats_come_from_the_listing_counter(center, service, store, shards):
    """**새 counter를 만들지 않았다.** `downloadCount`가 이미 정확한 값이다."""
    listing = published(service, store, shards, title="먹방거울")
    seed(shards, BUYER, 50)
    seed(shards, OTHER, 50)
    service.purchase(user(BUYER), listing.id)
    service.purchase(user(OTHER), listing.id)

    stats = center.sale_stats(user(SELLER))
    assert [(x.title, x.sale_count) for x in stats] == [("먹방거울", 2)]


def test_sale_stats_are_a_true_total(center, service, store, shards, notification_store):
    """**알림 목록을 세지 않는다** — 그러면 총계가 몇 장을 불러왔는지에 따라 달라진다.

    알림을 통째로 비워도 판매 횟수는 그대로다. 그것이 두 값이 다른 곳에서
    온다는 증거다.
    """
    listing = published(service, store, shards)
    seed(shards, BUYER, 50)
    service.purchase(user(BUYER), listing.id)

    notification_store.events.clear()

    assert [x.sale_count for x in center.sale_stats(user(SELLER))] == [1]


def test_sale_stats_skip_unsold_listings(center, service, store, shards):
    published(service, store, shards, source="a")
    assert center.sale_stats(user(SELLER)) == []


def test_sale_stats_separate_mirrors_and_stickers(center, service, store, shards):
    mirror = published(service, store, shards, title="먹방거울", source="a")
    sticker = published(
        service, store, shards, kind=ContentType.STICKER, title="하트", source="b"
    )
    seed(shards, BUYER, 50)
    service.purchase(user(BUYER), mirror.id)
    service.purchase(user(BUYER), sticker.id)

    kinds = {x.title: x.content_type for x in center.sale_stats(user(SELLER))}
    assert kinds == {"먹방거울": "mirror", "하트": "sticker"}


def test_sale_stats_are_only_mine(center, service, store, shards):
    listing = published(service, store, shards)
    seed(shards, BUYER, 50)
    service.purchase(user(BUYER), listing.id)

    assert center.sale_stats(user(BUYER)) == []


# MARK: - 다른 기능과의 관계 (§41 · §42 · §53)


def test_existing_buyer_redownload_creates_nothing(
    service, store, shards, notification_store, shard_store
):
    listing = published(service, store, shards)
    seed(shards, BUYER, 50)
    service.purchase(user(BUYER), listing.id)
    before = economy(shard_store, store)

    service.purchase(user(BUYER), listing.id)

    assert len(notification_store.events) == 1
    assert economy(shard_store, store) == before


def test_admin_takedown_creates_no_sale_notification(service, store, shards, notification_store):
    from app.marketplace.models import ModerationReason

    listing = published(service, store, shards)
    service.admin_takedown(user(OTHER), listing.id, reason=ModerationReason.SPAM)

    assert notification_store.events == {}


def test_moderated_listing_produces_no_new_sales(service, store, shards, notification_store):
    from app.marketplace.models import ListingNotFound, ModerationReason

    listing = published(service, store, shards)
    seed(shards, BUYER, 50)
    service.admin_takedown(user(OTHER), listing.id, reason=ModerationReason.SPAM)

    with pytest.raises(ListingNotFound):
        service.purchase(user(BUYER), listing.id)
    assert notification_store.events == {}


def test_builtin_template_purchase_has_no_seller_notification():
    """내장 템플릿에는 판매자가 없다 — **알림 경로가 아예 닿지 않는다.**"""
    import inspect

    from app.catalog import service as catalog_service

    code = inspect.getsource(catalog_service)
    for banned in ["NotificationEvent", "sale_event", "notify_sale", "PushService"]:
        assert banned not in code, f"catalog가 판매 알림을 만든다 ({banned})"


# MARK: - 서버 일정 없음 (§35)


def test_no_server_side_daily_reminder():
    """**매일 알림은 기기가 스스로 띄운다.** 서버 scheduler를 만들지 않았다.

    만들었다면 Cloud Scheduler · 작업 queue · 사용자 시간대 저장이 전부
    따라왔을 것이고, 그 비용을 낼 이유가 없다.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    for path in root.rglob("*.py"):
        code = path.read_text()
        for banned in ["CloudScheduler", "cloudscheduler", "cron", "APScheduler", "daily_reminder"]:
            assert banned not in code, f"{path.name}: 서버 일정({banned})을 들였다"


# MARK: - 로그 (§25)


def test_logs_never_contain_the_raw_token(pushes, provider, push_store, caplog):
    push_store.register(_device(SELLER, TOKEN_A))
    provider.terminal_for = {push_device_id(TOKEN_A)}

    with caplog.at_level(logging.DEBUG):
        pushes.notify_sale(_event())

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert TOKEN_A not in text
    assert SELLER not in text


def test_private_key_is_never_logged(caplog):
    """**private key는 어디에도 찍히지 않는다.**"""
    from app.push.provider import APNsPushProvider

    secret = "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----"
    with caplog.at_level(logging.DEBUG):
        provider = APNsPushProvider(
            key_id="K", team_id="T", private_key=secret, bundle_id="com.example",
        )
        assert provider.is_available

    assert secret not in caplog.text
    assert "not-a-real-key" not in caplog.text


# MARK: - helper


def _device(user_id: str, token: str) -> PushDevice:
    return PushDevice(
        id=push_device_id(token), user_id=user_id, token=token,
        environment=PushEnvironment.PRODUCTION,
    )


def _event(
    *, user_id: str = SELLER, event_id: str = "event-1", created_at=None
) -> NotificationEvent:
    extra = {"created_at": created_at} if created_at else {}
    return NotificationEvent(
        id=event_id, user_id=user_id, type=NotificationType.MARKETPLACE_SALE,
        listing_id="L1", content_type="mirror", title_snapshot="먹방거울",
        shard_amount=3, **extra,
    )


# MARK: - API (§46 · §50 · §54)


@pytest.fixture
def client(
    store, shard_store, push_store, notification_store, apple_key, jwks_of, monkeypatch
) -> TestClient:
    from app.auth import jwks as jwks_module

    document = jwks_of(apple_key)
    monkeypatch.setattr(jwks_module, "http_jwks_fetch", lambda *a, **k: lambda: document)

    app = create_app(
        Settings(app_env="local", apple_client_id=CLIENT_ID),
        auth_store=InMemoryAuthStore(),
        shard_store=shard_store,
        marketplace_store=store,
        push_store=push_store,
        notification_store=notification_store,
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


NOTIFICATION_PATHS = [
    ("GET", "/users/me/notifications", None),
    ("PATCH", "/users/me/notifications/any/read", None),
    ("GET", "/users/me/sale-stats", None),
    ("PUT", "/users/me/push-devices", {"token": TOKEN_A, "environment": "production"}),
    ("DELETE", "/users/me/push-devices", {"token": TOKEN_A, "environment": "production"}),
]


@pytest.mark.parametrize("method,path,body", NOTIFICATION_PATHS)
def test_endpoints_require_auth(client, method, path, body):
    assert client.request(method, path, json=body).status_code == 401


def test_register_endpoint_never_echoes_the_token(client, apple_key):
    """**token을 응답으로 되돌려주지 않는다** — 알 필요가 없고 응답 로그에 남는다."""
    token = sign_in(client, apple_key)
    response = client.put(
        "/users/me/push-devices",
        json={"token": TOKEN_A, "environment": "production"},
        headers=auth(token),
    )
    assert response.status_code == 204
    assert TOKEN_A not in response.text


def test_register_rejects_an_unknown_environment(client, apple_key):
    token = sign_in(client, apple_key)
    assert client.put(
        "/users/me/push-devices",
        json={"token": TOKEN_A, "environment": "whatever"},
        headers=auth(token),
    ).status_code == 422


def test_register_rejects_extra_authority_fields(client, apple_key):
    """`userId`를 실을 자리가 없다 — 남의 기기로 등록할 수 없다."""
    token = sign_in(client, apple_key)
    assert client.put(
        "/users/me/push-devices",
        json={"token": TOKEN_A, "environment": "production", "userId": SELLER},
        headers=auth(token),
    ).status_code == 422


def test_register_rejects_a_malformed_token(client, apple_key):
    token = sign_in(client, apple_key)
    assert client.put(
        "/users/me/push-devices",
        json={"token": "not-a-token", "environment": "production"},
        headers=auth(token),
    ).status_code == 400


def test_account_switch_rebinds_over_http(client, apple_key, push_store):
    """**BLOCKER guard, 실제 endpoint로.** 한 기기를 두 계정이 쓴다."""
    first = sign_in(client, apple_key, subject="001.aaa.1")
    a = client.get("/users/me", headers=auth(first)).json()["id"]
    client.put(
        "/users/me/push-devices",
        json={"token": TOKEN_A, "environment": "production"},
        headers=auth(first),
    )

    second = sign_in(client, apple_key, subject="001.bbb.2")
    b = client.get("/users/me", headers=auth(second)).json()["id"]
    client.put(
        "/users/me/push-devices",
        json={"token": TOKEN_A, "environment": "production"},
        headers=auth(second),
    )

    assert push_store.devices(a) == []
    assert [x.user_id for x in push_store.devices(b)] == [b]


def test_unregister_over_http(client, apple_key, push_store):
    token = sign_in(client, apple_key)
    who = client.get("/users/me", headers=auth(token)).json()["id"]
    body = {"token": TOKEN_A, "environment": "production"}
    client.put("/users/me/push-devices", json=body, headers=auth(token))

    assert client.request(
        "DELETE", "/users/me/push-devices", json=body, headers=auth(token)
    ).status_code == 204
    assert push_store.devices(who) == []


def test_notifications_are_account_isolated(client, apple_key, notification_store):
    """**A의 알림이 B에게 보이지 않는다.**"""
    first = sign_in(client, apple_key, subject="001.aaa.1")
    a = client.get("/users/me", headers=auth(first)).json()["id"]
    notification_store.create(_event(user_id=a, event_id="mine"))

    second = sign_in(client, apple_key, subject="001.bbb.2")
    body = client.get("/users/me/notifications", headers=auth(second)).json()

    assert body["notifications"] == []
    assert "mine" not in client.get(
        "/users/me/notifications", headers=auth(second)
    ).text


def test_cannot_read_someone_elses_notification(client, apple_key, notification_store):
    first = sign_in(client, apple_key, subject="001.aaa.1")
    a = client.get("/users/me", headers=auth(first)).json()["id"]
    notification_store.create(_event(user_id=a, event_id="mine"))

    second = sign_in(client, apple_key, subject="001.bbb.2")
    assert client.patch(
        "/users/me/notifications/mine/read", headers=auth(second)
    ).status_code == 404


def test_notification_response_has_no_buyer(client, apple_key, notification_store):
    token = sign_in(client, apple_key)
    who = client.get("/users/me", headers=auth(token)).json()["id"]
    notification_store.create(_event(user_id=who))

    body = client.get("/users/me/notifications", headers=auth(token)).text
    for banned in ("buyer", "userId", "sub", "email"):
        assert banned not in body


def test_purchase_over_http_leaves_a_notification(
    client, apple_key, store, shards, notification_store
):
    """구매 → 판매자 알림 하나. **push 자격 증명이 없어도 기록은 남는다.**"""
    listing = published(MarketplaceService(store, shards), store, shards)
    buyer_token = sign_in(client, apple_key, subject="001.buyer.1")
    buyer = client.get("/users/me", headers=auth(buyer_token)).json()["id"]
    seed(shards, buyer, 50)

    response = client.post(
        f"/marketplace/listings/{listing.id}/purchase", headers=auth(buyer_token)
    )

    assert response.status_code == 200
    assert response.json()["purchased"] is True
    events = list(notification_store.events.values())
    assert len(events) == 1 and events[0].user_id == SELLER


def test_repeated_purchase_over_http_does_not_duplicate(
    client, apple_key, store, shards, notification_store
):
    listing = published(MarketplaceService(store, shards), store, shards)
    buyer_token = sign_in(client, apple_key, subject="001.buyer.1")
    buyer = client.get("/users/me", headers=auth(buyer_token)).json()["id"]
    seed(shards, buyer, 50)

    for _ in range(3):
        client.post(f"/marketplace/listings/{listing.id}/purchase", headers=auth(buyer_token))

    assert len(notification_store.events) == 1


def test_sale_stats_over_http(client, apple_key, store, shards):
    """판매자가 자기 상품이 몇 번 팔렸는지 본다."""
    from app.auth.models import User as _User

    market = MarketplaceService(store, shards)
    seller_token = sign_in(client, apple_key, subject="001.seller.1")
    seller = client.get("/users/me", headers=auth(seller_token)).json()["id"]

    seed(shards, seller, 100)
    snapshot = Snapshot(
        id="snap-http", seller_user_id=seller, content_type=ContentType.MIRROR,
        manifest_checksum="deadbeef", source_content_id="local-http",
    )
    store.snapshots[snapshot.id] = snapshot
    draft = market.create_draft(
        _User(id=seller), content_type="mirror", title="먹방거울",
        description="", price_shards=3, snapshot_id=snapshot.id,
    )
    market.publish(_User(id=seller), draft.id)

    buyer_token = sign_in(client, apple_key, subject="001.buyer.1")
    buyer = client.get("/users/me", headers=auth(buyer_token)).json()["id"]
    seed(shards, buyer, 50)
    client.post(f"/marketplace/listings/{draft.id}/purchase", headers=auth(buyer_token))

    stats = client.get("/users/me/sale-stats", headers=auth(seller_token)).json()
    assert stats == [
        {
            "listingId": draft.id, "contentType": "mirror", "title": "먹방거울",
            "saleCount": 1, "priceShards": 3,
        }
    ]
    # 구매자에게는 판매 현황이 없다.
    assert client.get("/users/me/sale-stats", headers=auth(buyer_token)).json() == []


def test_mark_read_over_http(client, apple_key, notification_store):
    token = sign_in(client, apple_key)
    who = client.get("/users/me", headers=auth(token)).json()["id"]
    notification_store.create(_event(user_id=who, event_id="e1"))

    body = client.patch("/users/me/notifications/e1/read", headers=auth(token)).json()
    assert body["read"] is True
    assert client.get("/users/me/notifications", headers=auth(token)).json()[
        "notifications"
    ][0]["read"] is True


def test_purchase_survives_a_broken_push_service(client, apple_key, store, shards, monkeypatch):
    """**BLOCKER guard.** push가 통째로 터져도 구매 응답은 성공이다."""
    listing = published(MarketplaceService(store, shards), store, shards)
    buyer_token = sign_in(client, apple_key, subject="001.buyer.1")
    buyer = client.get("/users/me", headers=auth(buyer_token)).json()["id"]
    seed(shards, buyer, 50)

    def explode(event):
        raise RuntimeError("push is down")

    monkeypatch.setattr(client.app.state.push_service(), "notify_sale", explode)

    response = client.post(
        f"/marketplace/listings/{listing.id}/purchase", headers=auth(buyer_token)
    )

    assert response.status_code == 200
    assert response.json()["purchased"] is True
    assert store.ownership(listing.id, buyer) is not None


def test_no_userid_parameter_anywhere(client):
    """**주인은 언제나 session의 사용자다.** 경로에 userId를 받는 자리가 없다."""
    paths = [getattr(x, "path", "") for x in client.app.routes]
    notification_paths = [x for x in paths if "notification" in x or "push-device" in x or "sale-stat" in x]
    assert notification_paths
    for path in notification_paths:
        assert "{user" not in path and "{seller" not in path


def test_logs_have_no_token_over_http(client, apple_key, caplog):
    token = sign_in(client, apple_key)
    with caplog.at_level(logging.INFO):
        client.put(
            "/users/me/push-devices",
            json={"token": TOKEN_A, "environment": "production"},
            headers=auth(token),
        )

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert TOKEN_A not in text
    assert token not in text
    assert "Bearer" not in text
