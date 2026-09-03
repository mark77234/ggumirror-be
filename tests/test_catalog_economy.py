"""내장 템플릿 유료화.

**값의 authority는 서버 표 하나다.** client가 보낸 가격을 쓰지 않는다.

가장 위험한 자리는 값이 아니라 **예전 무료 경로**다. 값이 생긴 뒤에도
`acquire`와 `reconcile`이 기록을 만들 수 있으면, 구버전 앱이나 손으로 만든
요청으로 유료 템플릿을 전부 공짜로 가져갈 수 있다. 그 두 구멍을 여기서 막는다.
"""

from __future__ import annotations

import pytest

from app.auth.models import User
from app.catalog.models import (
    ARTWORK_TEMPLATE_IDS,
    BASIC_TEMPLATE_IDS,
    CATALOG_TEMPLATE_PRICES,
    MAX_TEMPLATE_PRICE,
    MIN_TEMPLATE_PRICE,
    TEMPLATE_IDS,
    PurchaseRequired,
    UnknownTemplate,
    is_free,
    template_price,
)
from app.catalog.service import CatalogService
from app.catalog.store import InMemoryCatalogStore
from app.shards.models import InsufficientShards, ShardReason
from app.shards.service import ShardLedgerService
from app.shards.store import InMemoryShardStore
from tests.test_catalog import client  # noqa: F401

ALICE = "11111111-2222-4333-8444-555555555555"
BOB = "99999999-8888-4777-8666-555555555555"

CHEAP = "art-pink-ribbon"    # 1조각
PRICEY = "art-angel-heart"   # 3조각
FREE = "basic-mint"          # 0조각


def user(user_id: str) -> User:
    return User(id=user_id)


@pytest.fixture
def shards():
    return ShardLedgerService(InMemoryShardStore())


@pytest.fixture
def store():
    return InMemoryCatalogStore()


@pytest.fixture
def service(store, shards):
    return CatalogService(store, shards)


def fund(shards, user_id: str, amount: int) -> None:
    shards.credit(user_id, amount, ShardReason.ADMIN_ADJUSTMENT, external_event_id=f"seed-{user_id}")


# MARK: - 값 표


def test_every_known_template_has_a_price():
    # 표에 없는 것이 있으면 그 템플릿은 살 수도 받을 수도 없게 된다.
    assert set(CATALOG_TEMPLATE_PRICES) == set(TEMPLATE_IDS)


def test_prices_are_within_range():
    for template_id, price in CATALOG_TEMPLATE_PRICES.items():
        assert MIN_TEMPLATE_PRICE <= price <= MAX_TEMPLATE_PRICE, template_id


def test_artwork_templates_are_priced_one_or_three():
    prices = {CATALOG_TEMPLATE_PRICES[x] for x in ARTWORK_TEMPLATE_IDS}
    assert prices == {1, 3}


def test_basic_mirrors_stay_free():
    # 앱이 기본값으로 쓰는 거울이다. 값을 매기면 처음 켠 사람이 거울을 못 쓴다.
    for template_id in BASIC_TEMPLATE_IDS:
        assert CATALOG_TEMPLATE_PRICES[template_id] == 0, template_id
        assert is_free(template_id)


def test_price_distribution_matches_the_old_catalog():
    # 예전 값을 그대로 옮긴 결정적 매핑이다(0 → 1, 4 → 3). 8 + 16 + 8.
    counts = {}
    for price in CATALOG_TEMPLATE_PRICES.values():
        counts[price] = counts.get(price, 0) + 1
    assert counts == {0: 8, 1: 8, 3: 16}


def test_unknown_template_has_no_price():
    with pytest.raises(UnknownTemplate):
        template_price("art-does-not-exist")


# MARK: - 구매


def test_buying_a_one_shard_template(service, shards, store):
    fund(shards, ALICE, 5)
    result = service.purchase(user(ALICE), CHEAP)

    assert result.first_acquisition is True
    assert shards.wallet(ALICE).balance == 4
    assert result.download_count == 1


def test_buying_a_three_shard_template(service, shards):
    fund(shards, ALICE, 5)
    service.purchase(user(ALICE), PRICEY)
    assert shards.wallet(ALICE).balance == 2


def test_purchase_writes_the_right_ledger_reason(service, shards):
    fund(shards, ALICE, 5)
    service.purchase(user(ALICE), CHEAP)
    # 같은 사건 id로 그 이유의 기록이 남았는지 공개 API로 본다.
    from app.catalog.models import acquisition_id

    key = acquisition_id(ALICE, CHEAP)
    assert shards.has_event(ALICE, ShardReason.CATALOG_TEMPLATE_PURCHASE, key)
    # Marketplace의 판매 짝과 섞지 않는다 — 내장 템플릿에는 파는 사람이 없다.
    assert not shards.has_event(ALICE, ShardReason.MIRROR_PURCHASE, key)
    assert not shards.has_event(ALICE, ShardReason.MIRROR_SALE, key)


def test_no_seller_is_credited(service, shards):
    fund(shards, ALICE, 5)
    service.purchase(user(ALICE), CHEAP)
    # 아무도 받지 않는다. BOB 지갑은 존재하지도 않는다.
    assert shards.wallet(BOB).balance == 0


def test_free_template_costs_nothing(service, shards):
    fund(shards, ALICE, 5)
    result = service.purchase(user(ALICE), FREE)
    assert result.first_acquisition is True
    assert shards.wallet(ALICE).balance == 5


def test_insufficient_balance_changes_nothing(service, shards, store):
    fund(shards, ALICE, 2)
    with pytest.raises(InsufficientShards):
        service.purchase(user(ALICE), PRICEY)  # 3조각
    assert shards.wallet(ALICE).balance == 2
    assert store.acquisitions == {}
    assert store.counts == {}


def test_unknown_template_cannot_be_bought(service, shards):
    fund(shards, ALICE, 5)
    with pytest.raises(UnknownTemplate):
        service.purchase(user(ALICE), "art-does-not-exist")
    assert shards.wallet(ALICE).balance == 5


# MARK: - 멱등


def test_buying_twice_charges_once(service, shards, store):
    fund(shards, ALICE, 5)
    first = service.purchase(user(ALICE), PRICEY)
    second = service.purchase(user(ALICE), PRICEY)

    assert first.first_acquisition is True
    assert second.first_acquisition is False
    # **한 번만 빠진다.**
    assert shards.wallet(ALICE).balance == 2
    assert store.counts[PRICEY] == 1


def test_repeat_purchase_does_not_raise(service, shards):
    fund(shards, ALICE, 5)
    service.purchase(user(ALICE), CHEAP)
    # 이미 가진 것을 다시 사도 실패가 아니다 — 값을 내지 않을 뿐이다.
    assert service.purchase(user(ALICE), CHEAP).first_acquisition is False


def test_download_count_moves_only_on_first_acquisition(service, shards, store):
    fund(shards, ALICE, 9)
    service.purchase(user(ALICE), CHEAP)
    service.purchase(user(ALICE), CHEAP)
    service.stats([CHEAP])
    assert store.counts[CHEAP] == 1


def test_two_users_are_charged_separately(service, shards, store):
    fund(shards, ALICE, 5)
    fund(shards, BOB, 5)
    service.purchase(user(ALICE), CHEAP)
    service.purchase(user(BOB), CHEAP)
    assert shards.wallet(ALICE).balance == 4
    assert shards.wallet(BOB).balance == 4
    assert store.counts[CHEAP] == 2


# MARK: - 예전 무료 경로 (release-critical)


def test_legacy_acquire_cannot_take_a_paid_template(service, shards, store):
    """**구버전 앱으로 유료 템플릿을 공짜로 가져갈 수 없다.**"""
    fund(shards, ALICE, 5)
    with pytest.raises(PurchaseRequired):
        service.acquire(user(ALICE), PRICEY)
    assert store.acquisitions == {}
    # 조각을 대신 빼지도 않는다 — 사용자는 결제 화면을 본 적이 없다.
    assert shards.wallet(ALICE).balance == 5


def test_legacy_acquire_still_works_for_free_templates(service, store):
    assert service.acquire(user(ALICE), FREE).first_acquisition is True


def test_legacy_acquire_returns_owned_paid_template(service, shards):
    """이미 산 것은 예전 경로로도 그대로 돌려준다 — 구버전 앱이 깨지지 않는다."""
    fund(shards, ALICE, 5)
    service.purchase(user(ALICE), CHEAP)
    result = service.acquire(user(ALICE), CHEAP)
    assert result.first_acquisition is False
    assert shards.wallet(ALICE).balance == 4  # 다시 빠지지 않는다


def test_reconcile_cannot_take_paid_templates(service, shards, store):
    """**id를 32개 보내도 유료 템플릿이 생기지 않는다.**"""
    fund(shards, ALICE, 100)
    service.reconcile(user(ALICE), sorted(TEMPLATE_IDS))

    created = {store.acquisitions[k].template_id for k in store.acquisitions}
    # 값이 없는 것만 생겼다.
    assert created == set(BASIC_TEMPLATE_IDS)
    assert not (created & ARTWORK_TEMPLATE_IDS)
    # 조각도 그대로다.
    assert shards.wallet(ALICE).balance == 100


def test_reconcile_keeps_already_owned_paid_templates(service, shards, store):
    fund(shards, ALICE, 5)
    service.purchase(user(ALICE), CHEAP)
    service.reconcile(user(ALICE), [CHEAP])
    # 이미 가진 것은 그대로 남고 수도 오르지 않는다.
    assert store.counts[CHEAP] == 1
    assert shards.wallet(ALICE).balance == 4


def test_reconcile_never_debits(service, shards):
    fund(shards, ALICE, 10)
    service.reconcile(user(ALICE), sorted(TEMPLATE_IDS))
    assert shards.wallet(ALICE).balance == 10


# MARK: - 물려받기(grandfathering)


def test_existing_acquisition_is_never_re_billed(service, shards, store):
    """값이 생기기 전에 받은 사람은 다시 내지 않는다."""
    # 값이 없던 시절에 받은 기록이 이미 있다고 하자.
    store.acquire(ALICE, PRICEY)
    fund(shards, ALICE, 5)

    result = service.purchase(user(ALICE), PRICEY)
    assert result.first_acquisition is False
    assert shards.wallet(ALICE).balance == 5   # 한 조각도 빠지지 않는다
    assert store.counts[PRICEY] == 1           # 수도 오르지 않는다


def test_grandfathered_item_is_usable_through_the_legacy_path(service, store):
    store.acquire(ALICE, PRICEY)
    assert service.acquire(user(ALICE), PRICEY).first_acquisition is False


# MARK: - API


def test_purchase_requires_authentication(client):
    assert client.post(f"/catalog/templates/{CHEAP}/purchases").status_code == 401


def test_purchase_takes_no_body_fields(client):
    """가격도 사용자도 client가 정하는 자리가 없다."""
    from app.api.catalog import purchase_template
    import inspect

    parameters = set(inspect.signature(purchase_template).parameters)
    assert parameters == {"template_id", "user", "service"}
    for forbidden in ("price", "amount", "user_id", "shards"):
        assert forbidden not in parameters


def test_owned_list_starts_empty(service):
    assert service.owned_template_ids(user(ALICE)) == []


def test_owned_list_reflects_purchases(service, shards):
    fund(shards, ALICE, 5)
    service.purchase(user(ALICE), CHEAP)
    assert service.owned_template_ids(user(ALICE)) == [CHEAP]
    # 다른 사람 것이 섞이지 않는다.
    assert service.owned_template_ids(user(BOB)) == []


def test_owned_endpoint_requires_authentication(client):
    assert client.get("/catalog/templates/mine").status_code == 401


# MARK: - 1.0.7 → 1.1.0 전환 (release-critical)


def _authenticated(catalog_store):
    """1.0.7 앱이 쓰던 그대로의 요청을 보낼 수 있는 client."""
    from datetime import UTC, datetime, timedelta

    from fastapi.testclient import TestClient

    from app.auth.models import Session, User as AuthUser, sha256_hex
    from app.auth.store import InMemoryAuthStore
    from app.core.config import Settings
    from app.main import create_app

    auth = InMemoryAuthStore()
    who = AuthUser(id=ALICE, created_at=datetime.now(UTC))
    auth.users[who.id] = who
    token = "legacy-client-token"
    auth.sessions[sha256_hex(token)] = Session(
        token_hash=sha256_hex(token),
        user_id=who.id,
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    app = create_app(
        Settings(app_env="local"), auth_store=auth, catalog_store=catalog_store
    )
    return TestClient(app, raise_server_exceptions=False), {
        "Authorization": f"Bearer {token}"
    }


def test_legacy_acquire_of_a_paid_template_is_not_a_server_error(store):
    """**구버전 앱의 정상 동작이 500으로 기록되면 안 된다.**

    1.0.7은 손그림이 전부 공짜였을 때 만들어졌고, 지금도 이 경로를 부른다.
    값이 붙은 템플릿에서 여기가 터지면 정상적인 구버전 사용 하나하나가
    서버 장애처럼 쌓여 **진짜 장애가 묻힌다.**
    """
    client, headers = _authenticated(store)

    response = client.post(f"/catalog/templates/{PRICEY}/acquire", headers=headers)

    assert response.status_code == 402
    assert response.status_code < 500
    # 공짜로 주지도 않는다.
    assert store.acquisitions == {}


def test_legacy_acquire_of_a_free_template_still_works(store):
    """무료 템플릿은 예전 그대로다 — 전환이 구버전을 깨뜨리지 않는다."""
    client, headers = _authenticated(store)

    response = client.post(f"/catalog/templates/{FREE}/acquire", headers=headers)

    assert response.status_code == 200
    assert response.json()["firstAcquisition"] is True
