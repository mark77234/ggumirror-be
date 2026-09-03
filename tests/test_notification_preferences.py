"""알림 설정 · 모아 보기 · 추천 (I-11 ~ I-15).

지키는 것 셋:

1. **기존 사용자가 판매 알림을 잃지 않는다.** 설정 문서가 없는 것이 기본 상태다.
2. **홍보성 알림을 몰래 켜지 않는다.** 받고 싶은 사람이 켠다.
3. **같은 발송이 두 번 가지 않는다.** scheduler는 같은 일을 두 번 부른다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.auth.models import User
from app.marketplace.models import ContentType, ModerationReason, Snapshot
from app.marketplace.service import MarketplaceService
from app.marketplace.store import InMemoryMarketplaceStore
from app.notifications.delivery import InMemoryDeliveryStore, delivery_id
from app.notifications.digest import MirrorDigestService
from app.notifications.models import NotificationEvent, NotificationType
from app.notifications.preferences import (
    DigestFrequency,
    InMemoryPreferenceStore,
    NotificationPreferenceService,
    NotificationPreferences,
)
from app.notifications.store import InMemoryNotificationStore
from app.push.models import PushDevice, PushEnvironment, push_device_id
from app.push.service import PushService, message_for
from app.push.store import InMemoryPushStore
from app.shards.models import ShardReason
from app.shards.service import ShardLedgerService
from app.shards.store import InMemoryShardStore

ALICE = "11111111-2222-4333-8444-555555555555"
BOB = "99999999-8888-4777-8666-555555555555"
SELLER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

MORNING = datetime(2026, 8, 20, 3, tzinfo=UTC)      # KST 12:00


@pytest.fixture
def preference_store() -> InMemoryPreferenceStore:
    return InMemoryPreferenceStore()


@pytest.fixture
def preferences(preference_store) -> NotificationPreferenceService:
    return NotificationPreferenceService(preference_store)


@pytest.fixture
def deliveries() -> InMemoryDeliveryStore:
    return InMemoryDeliveryStore()


@pytest.fixture
def notifications() -> InMemoryNotificationStore:
    return InMemoryNotificationStore()


class FakeProvider:
    is_available = True

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.failing_devices: set[str] = set()

    def send(self, device, message):
        from app.push.models import PushOutcome

        if device.id in self.failing_devices:
            raise RuntimeError("apns exploded")
        self.sent.append((device.user_id, message.title))
        return PushOutcome(device_id=device.id, delivered=True, status=200)


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def push_store() -> InMemoryPushStore:
    return InMemoryPushStore()


@pytest.fixture
def pushes(push_store, provider, preferences) -> PushService:
    return PushService(push_store, provider, preferences=preferences)


def register(push_store, user_id: str) -> None:
    token = f"{abs(hash(user_id)) % 10**12:024x}" * 2
    push_store.register(
        PushDevice(
            id=push_device_id(token), user_id=user_id, token=token,
            environment=PushEnvironment.PRODUCTION,
        )
    )


def event(user_id: str = SELLER, kind: NotificationType = NotificationType.MARKETPLACE_SALE):
    return NotificationEvent(
        id=NotificationEvent.new_id(), user_id=user_id, type=kind,
        listing_id="L1", content_type="mirror", title_snapshot="먹방거울", shard_amount=3,
    )


# MARK: - 기본값 (§12)


def test_sales_defaults_to_on(preferences):
    """**기존 사용자가 판매 알림을 잃지 않는다.** 문서가 없는 것이 지금의 상태다."""
    assert preferences.preferences(ALICE).sales_enabled is True


def test_digest_defaults_to_off(preferences):
    """새로 생긴 콘텐츠 알림이다. 켜고 싶은 사람이 켠다."""
    assert preferences.preferences(ALICE).digest_frequency is DigestFrequency.OFF


def test_recommendation_defaults_to_off(preferences):
    """홍보성 알림을 기존 계정에 몰래 켜지 않는다."""
    assert preferences.preferences(ALICE).recommendation_enabled is False


def test_reading_does_not_create_a_document(preferences, preference_store):
    """읽기가 쓰기를 일으키지 않는다 — migration이 없는 이유다."""
    preferences.preferences(ALICE)
    assert preference_store.saved == {}


def test_unknown_frequency_is_off():
    """모르는 값일 때 더 보내는 쪽으로 기울지 않는다."""
    assert DigestFrequency.of("hourly") is DigestFrequency.OFF
    assert DigestFrequency.of(None) is DigestFrequency.OFF


# MARK: - 수정 (§13)


def test_update_changes_only_what_was_sent(preferences):
    """보내지 않은 값은 그대로다 — 오래된 화면이 새 설정을 되돌리지 않는다."""
    preferences.update(ALICE, digest_frequency="daily")
    preferences.update(ALICE, recommendation_enabled=True)

    saved = preferences.preferences(ALICE)
    assert saved.digest_frequency is DigestFrequency.DAILY
    assert saved.recommendation_enabled is True
    assert saved.sales_enabled is True   # 건드리지 않았다


def test_preferences_are_isolated_between_accounts(preferences):
    preferences.update(ALICE, sales_enabled=False, digest_frequency="weekly")

    other = preferences.preferences(BOB)
    assert other.sales_enabled is True
    assert other.digest_frequency is DigestFrequency.OFF


def test_deleting_an_account_removes_preferences(preferences, preference_store):
    preferences.update(ALICE, digest_frequency="daily")
    preferences.delete(ALICE)
    assert preference_store.saved == {}
    # 기본값으로 돌아간다 — 탈퇴한 사람이 대상으로 남지 않는다.
    assert preferences.preferences(ALICE).digest_frequency is DigestFrequency.OFF


# MARK: - 판매 알림 gate (§15)


def test_sales_enabled_sends_the_push(pushes, push_store, provider):
    register(push_store, SELLER)
    assert pushes.notify_sale(event()) == 1
    assert len(provider.sent) == 1


def test_sales_disabled_skips_the_push(pushes, push_store, provider, preferences):
    register(push_store, SELLER)
    preferences.update(SELLER, sales_enabled=False)

    assert pushes.notify_sale(event()) == 0
    assert provider.sent == []


def test_the_sale_record_is_kept_even_when_push_is_off(
    preferences, notifications, push_store, provider
):
    """**끈 것은 전달이지 사실이 아니다.** 앱을 열면 무엇이 팔렸는지 보인다."""
    preferences.update(SELLER, sales_enabled=False)
    sale = event()
    notifications.create(sale)

    assert len(notifications.events) == 1
    mine, _ = notifications.page(SELLER, None, 10)
    assert [x.id for x in mine] == [sale.id]


def test_preference_never_touches_the_economy():
    """설정이 구매 · 소유권 · 판매자 지급에 닿지 않는다."""
    import inspect

    from app.push import service as push_service

    code = inspect.getsource(push_service)
    for banned in ["ShardLedger", "ownership", "wallet", "debit", "credit"]:
        assert banned not in code, banned


def test_unreadable_preferences_do_not_lose_the_sale_push(push_store, provider):
    """설정을 못 읽었다고 판매 알림을 잃지 않는다 — 기존 동작이 안전한 쪽이다."""

    class Broken:
        def preferences(self, user_id):
            raise RuntimeError("firestore down")

    service = PushService(push_store, provider, preferences=Broken())
    register(push_store, SELLER)
    assert service.notify_sale(event()) == 1


# MARK: - 모아 보기 (§16 · §17)


@pytest.fixture
def marketplace():
    shard_store = InMemoryShardStore()
    shards = ShardLedgerService(shard_store)
    store = InMemoryMarketplaceStore(shard_store)
    return MarketplaceService(store, shards), store, shards


def publish_mirror(bundle, *, title=None, kind=ContentType.MIRROR, source="a"):
    # 상품 이름은 상점 전체에서 하나뿐이다 — 원본이 다르면 이름도 달라야 한다.
    title = title or f"새 거울 {source}"
    service, store, shards = bundle
    shards.credit(SELLER, 100, ShardReason.ADMIN_ADJUSTMENT, external_event_id=f"seed:{source}")
    snapshot = Snapshot(
        id=f"snap-{source}", seller_user_id=SELLER, content_type=kind,
        manifest_checksum="deadbeef", source_content_id=source,
    )
    store.snapshots[snapshot.id] = snapshot
    draft = service.create_draft(
        User(id=SELLER), content_type=kind.value, title=title,
        description="", price_shards=0, snapshot_id=snapshot.id,
    )
    return service.publish(User(id=SELLER), draft.id).listing


@pytest.fixture
def digest(marketplace, notifications, deliveries, preferences, pushes):
    return MirrorDigestService(marketplace[0], notifications, deliveries, preferences, pushes)


def test_daily_reaches_only_daily_subscribers(digest, marketplace, preferences, provider, push_store):
    publish_mirror(marketplace)
    for who, freq in [(ALICE, "daily"), (BOB, "weekly")]:
        preferences.update(who, digest_frequency=freq)
        register(push_store, who)

    outcome = digest.run_daily([ALICE, BOB], now=MORNING)

    assert outcome.sent == 1
    assert [x[0] for x in provider.sent] == [ALICE]


def test_weekly_reaches_only_weekly_subscribers(digest, marketplace, preferences, provider, push_store):
    publish_mirror(marketplace)
    for who, freq in [(ALICE, "daily"), (BOB, "weekly")]:
        preferences.update(who, digest_frequency=freq)
        register(push_store, who)

    outcome = digest.run_weekly([ALICE, BOB], now=MORNING)

    assert outcome.sent == 1
    assert [x[0] for x in provider.sent] == [BOB]


def test_off_receives_nothing(digest, marketplace, provider, push_store):
    publish_mirror(marketplace)
    register(push_store, ALICE)

    assert digest.run_daily([ALICE], now=MORNING).sent == 0
    assert provider.sent == []


def test_only_public_mirrors_are_counted(digest, marketplace):
    """**공개 판정을 다시 만들지 않았다** — marketplace가 그대로 authority다."""
    service, store, _ = marketplace
    live = publish_mirror(marketplace, source="live")
    hidden = publish_mirror(marketplace, source="hidden")
    service.admin_takedown(User(id=BOB), hidden.id, reason=ModerationReason.SPAM)
    deleted = publish_mirror(marketplace, source="gone")
    service.delete_listing(User(id=SELLER), deleted.id)
    publish_mirror(marketplace, kind=ContentType.STICKER, source="sticker")

    # 살아 있는 거울 하나만 센다 — 내려간 것 · 지운 것 · 스티커는 빠진다.
    assert digest.new_mirrors(since=datetime(2026, 1, 1, tzinfo=UTC)) == 1
    assert live.id


def test_zero_new_mirrors_sends_nothing(digest, preferences, provider, push_store, notifications):
    preferences.update(ALICE, digest_frequency="daily")
    register(push_store, ALICE)

    outcome = digest.run_daily([ALICE], now=MORNING)

    assert outcome.sent == 0
    assert outcome.skipped_empty == 1
    assert provider.sent == []
    assert notifications.events == {}


def test_duplicate_daily_run_sends_once(digest, marketplace, preferences, provider, push_store, notifications):
    """**scheduler는 같은 일을 두 번 부른다.** 자리를 선점해서 막는다."""
    publish_mirror(marketplace)
    preferences.update(ALICE, digest_frequency="daily")
    register(push_store, ALICE)

    first = digest.run_daily([ALICE], now=MORNING)
    second = digest.run_daily([ALICE], now=MORNING)

    assert first.sent == 1
    assert second.sent == 0
    assert second.skipped_duplicate == 1
    assert len(provider.sent) == 1
    assert len(notifications.events) == 1


def test_duplicate_weekly_run_sends_once(digest, marketplace, preferences, provider, push_store):
    publish_mirror(marketplace)
    preferences.update(ALICE, digest_frequency="weekly")
    register(push_store, ALICE)

    digest.run_weekly([ALICE], now=MORNING)
    digest.run_weekly([ALICE], now=MORNING)

    assert len(provider.sent) == 1


def test_the_day_key_is_seoul(deliveries):
    """조각 출석과 **같은 달력**이다 — 기능마다 하루가 다르면 안 된다."""
    late = datetime(2026, 8, 20, 16, tzinfo=UTC)     # KST 8/21 01:00
    early = datetime(2026, 8, 20, 3, tzinfo=UTC)     # KST 8/20 12:00
    from app.shards.attendance import attendance_date

    assert attendance_date(late) != attendance_date(early)
    assert delivery_id(ALICE, "d", attendance_date(late)) != delivery_id(
        ALICE, "d", attendance_date(early)
    )


def test_digest_is_recorded_in_the_center(digest, marketplace, preferences, push_store, notifications):
    publish_mirror(marketplace)
    preferences.update(ALICE, digest_frequency="daily")
    register(push_store, ALICE)

    digest.run_daily([ALICE], now=MORNING)

    stored = list(notifications.events.values())
    assert len(stored) == 1
    assert stored[0].type is NotificationType.MIRROR_DIGEST
    # 상품 하나에 매이지 않는다.
    assert stored[0].listing_id == ""
    assert stored[0].headline


# MARK: - 추천 (§18)


def test_recommendation_is_opt_in(digest, provider, push_store):
    register(push_store, ALICE)
    assert digest.run_recommendation([ALICE], now=MORNING).sent == 0
    assert provider.sent == []


def test_recommendation_is_not_stored_in_the_center(
    digest, preferences, push_store, notifications, provider
):
    """**홍보성 알림이 쌓이면 판매 소식이 묻힌다.** push만 보낸다."""
    preferences.update(ALICE, recommendation_enabled=True)
    register(push_store, ALICE)

    outcome = digest.run_recommendation([ALICE], now=MORNING)

    assert outcome.sent == 1
    assert len(provider.sent) == 1
    assert notifications.events == {}


def test_recommendation_at_most_once_a_week(digest, preferences, push_store, provider):
    preferences.update(ALICE, recommendation_enabled=True)
    register(push_store, ALICE)

    digest.run_recommendation([ALICE], now=MORNING)
    digest.run_recommendation([ALICE], now=MORNING + timedelta(days=1))

    assert len(provider.sent) == 1


def test_recommendation_says_nothing_it_did_not_measure(digest, preferences, push_store, provider):
    preferences.update(ALICE, recommendation_enabled=True)
    register(push_store, ALICE)
    digest.run_recommendation([ALICE], now=MORNING)

    text = provider.sent[0][1]
    for unmeasured in ["인기 1위", "가장 인기", "1만"]:
        assert unmeasured not in text


# MARK: - 전달 격리 (§21)


def test_one_broken_device_does_not_abort_the_batch(
    digest, marketplace, preferences, push_store, provider
):
    publish_mirror(marketplace)
    for who in (ALICE, BOB):
        preferences.update(who, digest_frequency="daily")
        register(push_store, who)
    # ALICE의 기기가 터진다.
    provider.failing_devices = {push_store.devices(ALICE)[0].id}

    outcome = digest.run_daily([ALICE, BOB], now=MORNING)

    # BOB은 그대로 받는다.
    assert [x[0] for x in provider.sent] == [BOB]
    assert outcome.sent == 2   # 둘 다 시도했다


# MARK: - 모양 (§8 · §23)


def test_unknown_type_does_not_raise():
    """**모르는 종류 하나가 페이지 전체를 깨뜨리지 않는다.**"""
    assert NotificationType.of("something_from_2027") is NotificationType.UNKNOWN
    assert NotificationType.of(None) is NotificationType.MARKETPLACE_SALE


def test_legacy_sale_document_decodes():
    """옛 판매 문서에는 새 field가 하나도 없다."""
    from app.notifications.firestore_store import _event_from

    decoded = _event_from("e1", {
        "userId": SELLER, "type": "marketplace_sale", "listingId": "L1",
        "contentType": "mirror", "titleSnapshot": "먹방거울", "shardAmount": 3,
    })
    assert decoded.type is NotificationType.MARKETPLACE_SALE
    assert decoded.headline == ""
    assert decoded.shard_amount == 3


def test_digest_document_decodes():
    from app.notifications.firestore_store import _event_from

    decoded = _event_from("e2", {
        "userId": ALICE, "type": "mirror_digest",
        "headline": "새로운 거울이 올라왔어요", "body": "오늘 새 거울 7개",
    })
    assert decoded.type is NotificationType.MIRROR_DIGEST
    assert decoded.listing_id == ""
    assert decoded.shard_amount == 0


def test_unknown_document_decodes_without_raising():
    from app.notifications.firestore_store import _event_from

    decoded = _event_from("e3", {"userId": ALICE, "type": "future_thing_2030"})
    assert decoded.type is NotificationType.UNKNOWN


def test_mixed_page_paginates(notifications):
    """판매와 모아 보기가 섞인 페이지도 경계에서 빠지거나 겹치지 않는다."""
    for index in range(7):
        kind = (
            NotificationType.MARKETPLACE_SALE if index % 2 == 0
            else NotificationType.MIRROR_DIGEST
        )
        notifications.create(NotificationEvent(
            id=f"e{index}", user_id=ALICE, type=kind,
            created_at=datetime(2026, 8, 1 + index, tzinfo=UTC),
        ))

    seen, cursor, pages = [], None, 0
    while True:
        page, cursor = notifications.page(ALICE, cursor, 3)
        seen.extend(x.id for x in page)
        pages += 1
        if cursor is None:
            break
        assert pages < 10

    assert len(seen) == 7 and len(set(seen)) == 7
    assert seen[0] == "e6"   # 최신이 먼저


def test_push_copy_matches_the_type():
    sale = message_for(event())
    digest_message = message_for(NotificationEvent(
        id="d", user_id=ALICE, type=NotificationType.MIRROR_DIGEST,
        headline="새로운 거울이 올라왔어요 🪞", body="오늘 새 거울 7개를 구경해보세요.",
    ))
    assert "판매" in sale.title
    assert digest_message.title == "새로운 거울이 올라왔어요 🪞"
    assert digest_message.kind == "mirror_digest"


def test_deletion_map_includes_notification_state():
    """**계정 삭제가 알림 설정과 발송 기록까지 지운다.**

    service 함수를 직접 부르는 test만으로는 부족하다 — 실제 삭제는 `main.py`가
    건네주는 collection 목록을 돈다. 거기서 빠지면 코드는 멀쩡한데 문서만 남고,
    탈퇴한 사람이 정기 발송 대상으로 계속 남는다.
    """
    import inspect

    from app import main
    from app.notifications.delivery import DIGEST_DELIVERIES
    from app.notifications.preferences import NOTIFICATION_PREFERENCES
    from app.notifications.store import NOTIFICATIONS
    from app.push.store import PUSH_DEVICES

    source = inspect.getsource(main)
    for name in ("NOTIFICATION_PREFERENCES", "DIGEST_DELIVERIES",
                 "NOTIFICATIONS", "PUSH_DEVICES"):
        assert f'"{name.lower()}"' in source.lower() or name in source, name

    # 이름이 실제로 삭제 대상 목록에 들어가는지 본다.
    start = source.index("AccountDeletionService(")
    block = source[start:source.index("        )", start)]
    for key in ("notification_preferences", "notification_deliveries",
                "notifications", "push_devices"):
        assert f'"{key}"' in block, f"삭제 목록에 {key}가 없다"

    # 상수 자체도 확인 — 이름만 맞고 값이 다르면 엉뚱한 collection을 지운다.
    assert NOTIFICATION_PREFERENCES == "ggumirror_notification_preferences"
    assert DIGEST_DELIVERIES == "ggumirror_notification_deliveries"
    assert NOTIFICATIONS == "ggumirror_user_notifications"
    assert PUSH_DEVICES == "ggumirror_push_devices"


def test_deletion_walks_both_key_shapes():
    """설정은 **문서 이름이 user id**이고 발송 기록은 **field**다. 둘 다 돈다."""
    import inspect

    from app.auth import deletion

    code = inspect.getsource(deletion)
    id_keyed = code.index('for name in ("users", "wallets"')
    field_keyed = code.index('for name in ("sessions"')
    assert "notification_preferences" in code[id_keyed:field_keyed]
    assert "notification_deliveries" in code[field_keyed:field_keyed + 600]
