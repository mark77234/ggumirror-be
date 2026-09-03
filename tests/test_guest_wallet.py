"""guest 지갑 (5.1.1(v)).

**로그인 없이 조각을 살 수 있어야 한다.** 그렇다고 client가 만든 UUID를 지갑
주인으로 믿지는 않는다 — 익명 신원도 **서버가 발급**하고, 그 뒤로는 기존 경로
(Apple JWS 검증 · 전역 claim · 원장)를 그대로 지난다.

Apple을 부르지 않는다. 저장소는 전부 in-memory fake다.
"""

from __future__ import annotations

import threading
import uuid

import pytest
from fastapi.testclient import TestClient

from app.auth.models import User, sha256_hex
from app.auth.store import InMemoryAuthStore, StoreUnavailable
from app.core.config import Settings
from app.iap.models import (
    IAPEnvironment,
    InvalidTransaction,
    VerifiedTransaction,
    parse_allowed_environments,
)
from app.iap.service import IAPService
from app.main import create_app
from app.marketplace.store import InMemoryMarketplaceStore
from app.notifications.delivery import InMemoryDeliveryStore
from app.notifications.preferences import InMemoryPreferenceStore
from app.notifications.store import InMemoryNotificationStore
from app.push.store import InMemoryPushStore
from app.shards.models import ShardReason
from app.shards.service import ShardLedgerService
from app.shards.store import InMemoryShardStore
from tests.conftest import CLIENT_ID, apple_claims
from tests.test_marketplace import SELLER, published

PRODUCT_10 = "com.mark77234.ggumirror.shards.10"


class FakeVerifier:
    """B-6A의 fake를 그대로 쓴다 — 여기서 시험하는 것은 서명이 아니라 주인 판정이다."""

    is_configured = True

    def __init__(self, transaction: VerifiedTransaction | None = None) -> None:
        self.transaction = transaction

    def verify(self, signed_transaction: str) -> VerifiedTransaction:
        if self.transaction is None:
            raise InvalidTransaction("signature is not valid")
        return self.transaction


def transaction(
    *,
    transaction_id: str = "2000000123456789",
    app_account_token: str | None = None,
    product_id: str = PRODUCT_10,
) -> VerifiedTransaction:
    return VerifiedTransaction(
        transaction_id=transaction_id,
        product_id=product_id,
        bundle_id=CLIENT_ID,
        environment=IAPEnvironment.PRODUCTION,
        app_account_token=app_account_token,
        transaction_type="Consumable",
    )


@pytest.fixture
def auth_store() -> InMemoryAuthStore:
    return InMemoryAuthStore()


@pytest.fixture
def shard_store() -> InMemoryShardStore:
    return InMemoryShardStore()


@pytest.fixture
def marketplace(shard_store) -> InMemoryMarketplaceStore:
    return InMemoryMarketplaceStore(shard_store)


@pytest.fixture
def verifier() -> FakeVerifier:
    return FakeVerifier()


@pytest.fixture
def client(auth_store, shard_store, marketplace, verifier, apple_key, jwks_of, monkeypatch) -> TestClient:
    from app.auth import jwks as jwks_module

    document = jwks_of(apple_key)
    monkeypatch.setattr(jwks_module, "http_jwks_fetch", lambda *a, **k: lambda: document)

    app = create_app(
        Settings(
            app_env="local",
            apple_client_id=CLIENT_ID,
            iap_allowed_environments="Production",
        ),
        auth_store=auth_store,
        shard_store=shard_store,
        transaction_verifier=verifier,
        marketplace_store=marketplace,
        # 상점 구매는 판매자에게 알림을 남긴다 — 이 store들을 주지 않으면
        # Firestore로 fallback해서 credential이 없는 CI에서 503이 된다.
        push_store=InMemoryPushStore(),
        notification_store=InMemoryNotificationStore(),
        preference_store=InMemoryPreferenceStore(),
        delivery_store=InMemoryDeliveryStore(),
    )
    return TestClient(app)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def guest(client: TestClient) -> tuple[str, str]:
    """(user id, access token). **client가 아무것도 보내지 않는다.**"""
    body = client.post("/auth/guest").json()
    return body["user"]["id"], body["accessToken"]


def sign_in(client: TestClient, apple_key, *, sub: str = "001.guest.subject", token: str | None = None):
    nonce = "nonce-guest"
    identity = apple_key.token(apple_claims(sub=sub, nonce=sha256_hex(nonce)))
    return client.post(
        "/auth/apple",
        json={"identityToken": identity, "nonce": nonce},
        headers=bearer(token) if token else {},
    )


def buy(client: TestClient, verifier: FakeVerifier, token: str, owner: str, txn: str = "2000000123456789"):
    verifier.transaction = transaction(transaction_id=txn, app_account_token=owner)
    return client.post(
        "/users/me/iap/shards", json={"signedTransaction": "jws"}, headers=bearer(token)
    )


def balance(client: TestClient, token: str) -> int:
    return client.get("/users/me/shards", headers=bearer(token)).json()["balance"]


# MARK: - guest 신원 (§2, §13)


def test_guest_session_needs_no_credentials(client, auth_store):
    """이름 · 이메일 · 전화번호 · Apple 계정을 묻지 않는다. body 자체가 없다."""
    response = client.post("/auth/guest")

    assert response.status_code == 200
    assert response.json()["tokenType"] == "Bearer"
    assert len(auth_store.users) == 1
    assert len(auth_store.identities) == 0


def test_guest_user_id_is_issued_by_the_server(client, auth_store):
    """client가 만든 UUID를 지갑 주인으로 쓰지 않는다."""
    mine = str(uuid.uuid4())

    body = client.post("/auth/guest", json={"userId": mine}).json()

    assert body["user"]["id"] != mine
    assert uuid.UUID(body["user"]["id"]).version == 4
    assert auth_store.users[body["user"]["id"]].is_guest is True


def test_each_guest_session_is_a_different_user(client):
    assert guest(client)[0] != guest(client)[0]


def test_guest_wallet_starts_empty_and_is_readable(client):
    _, token = guest(client)

    response = client.get("/users/me/shards", headers=bearer(token))

    assert response.status_code == 200
    assert response.json()["balance"] == 0


# MARK: - guest 구매 (§3, §4)


def test_guest_buys_shards_without_signing_in(client, verifier, shard_store):
    guest_id, token = guest(client)

    response = buy(client, verifier, token, guest_id)

    assert response.status_code == 200
    assert response.json() == {"credited": True, "amount": 10, "balance": 10}
    assert [e.reason for e in shard_store.entries] == [ShardReason.IAP_PURCHASE]


def test_guest_purchase_is_server_authoritative(client, verifier):
    """catalog가 수량을 정한다 — body에 얹을 자리가 없다."""
    guest_id, token = guest(client)
    verifier.transaction = transaction(app_account_token=guest_id)

    response = client.post(
        "/users/me/iap/shards",
        json={"signedTransaction": "jws", "amount": 999},
        headers=bearer(token),
    )

    assert response.status_code == 422


def test_duplicate_apple_transaction_credits_a_guest_once(client, verifier):
    guest_id, token = guest(client)

    first = buy(client, verifier, token, guest_id)
    second = buy(client, verifier, token, guest_id)

    assert (first.json()["credited"], second.json()["credited"]) == (True, False)
    assert second.json()["balance"] == 10


def test_a_guest_cannot_claim_another_wallets_transaction(client, verifier):
    """appAccountToken이 남의 것이면 거절한다 — guest라고 느슨해지지 않는다."""
    other, _ = guest(client)
    _, token = guest(client)

    response = buy(client, verifier, token, other)

    assert response.status_code == 400
    assert balance(client, token) == 0


def test_a_guest_transaction_without_an_owner_is_rejected(client, verifier):
    _, token = guest(client)

    response = buy(client, verifier, token, None)

    assert response.status_code == 400


def test_guest_spends_what_it_bought(client, verifier, marketplace):
    """산 조각을 실제로 쓸 수 있어야 한다 — 상점 구매도 로그인이 필요 없다."""
    guest_id, token = guest(client)
    buy(client, verifier, token, guest_id)
    published(marketplace, "live", price=5)

    response = client.post("/marketplace/listings/live/purchase", headers=bearer(token))

    assert response.status_code == 200
    assert balance(client, token) == 5


# MARK: - 로그인 (§6, §7)


def test_first_sign_in_adopts_the_guest_wallet(client, verifier, apple_key, auth_store):
    """새 Apple 신원이면 guest가 **그대로 계정이 된다** — 조각이 움직이지 않는다."""
    guest_id, token = guest(client)
    buy(client, verifier, token, guest_id)

    body = sign_in(client, apple_key, token=token).json()

    assert body["user"]["id"] == guest_id
    assert balance(client, body["accessToken"]) == 10
    assert auth_store.users[guest_id].is_guest is False


def test_sign_in_merges_the_guest_wallet_into_an_existing_account(client, verifier, apple_key):
    account = sign_in(client, apple_key).json()
    guest_id, token = guest(client)
    buy(client, verifier, token, guest_id)

    merged = sign_in(client, apple_key, token=token).json()

    assert merged["user"]["id"] == account["user"]["id"] != guest_id
    assert balance(client, merged["accessToken"]) == 10


def test_signing_in_again_does_not_duplicate_the_balance(client, verifier, apple_key):
    account = sign_in(client, apple_key).json()
    guest_id, token = guest(client)
    buy(client, verifier, token, guest_id)
    sign_in(client, apple_key, token=token)

    for _ in range(3):
        sign_in(client, apple_key, token=token)

    assert balance(client, account["accessToken"]) == 10


def test_the_guest_session_stops_working_after_the_merge(client, verifier, apple_key):
    """옮긴 뒤에는 그 신원으로 다시 조회 · 구매할 수 없다."""
    account = sign_in(client, apple_key).json()
    guest_id, token = guest(client)
    buy(client, verifier, token, guest_id)

    sign_in(client, apple_key, token=token)

    assert client.get("/users/me/shards", headers=bearer(token)).status_code == 401
    assert account is not None


def test_a_late_guest_purchase_credits_the_claiming_account(client, verifier, apple_key):
    """로그인 직전에 산 결제가 늦게 도착해도 돈을 잃지 않는다."""
    sign_in(client, apple_key)
    guest_id, token = guest(client)
    merged = sign_in(client, apple_key, token=token).json()

    response = buy(client, verifier, merged["accessToken"], guest_id)

    assert response.status_code == 200
    assert balance(client, merged["accessToken"]) == 10


def test_a_late_guest_purchase_does_not_credit_a_stranger(client, verifier, apple_key):
    """넘겨받은 계정이 아닌 사람은 그 guest의 결제를 쓸 수 없다."""
    sign_in(client, apple_key)
    guest_id, token = guest(client)
    sign_in(client, apple_key, token=token)
    stranger = sign_in(client, apple_key, sub="001.other.subject").json()

    response = buy(client, verifier, stranger["accessToken"], guest_id)

    assert response.status_code == 400
    assert balance(client, stranger["accessToken"]) == 0


def test_the_same_purchase_is_not_credited_twice_across_the_merge(client, verifier, apple_key):
    """guest로 지급된 결제를 로그인 뒤 다시 제출해도 조각이 늘지 않는다."""
    sign_in(client, apple_key)
    guest_id, token = guest(client)
    buy(client, verifier, token, guest_id)
    merged = sign_in(client, apple_key, token=token).json()

    again = buy(client, verifier, merged["accessToken"], guest_id)

    assert again.status_code == 409
    assert balance(client, merged["accessToken"]) == 10


# MARK: - 이관 자체 (§7, §13)


def moved(store: InMemoryShardStore, guest_id: str, owner: str) -> int:
    return store.claim_guest_wallet(guest_id, owner)


def test_claim_moves_the_whole_balance_once(shard_store):
    shard_store.apply("g", 10, ShardReason.IAP_PURCHASE, "seed")

    assert moved(shard_store, "g", "a") == 10
    assert (shard_store.wallet("g").balance, shard_store.wallet("a").balance) == (0, 10)


def test_claim_retry_is_a_no_op(shard_store):
    shard_store.apply("g", 10, ShardReason.IAP_PURCHASE, "seed")
    moved(shard_store, "g", "a")

    assert [moved(shard_store, "g", "a") for _ in range(3)] == [0, 0, 0]
    assert shard_store.wallet("a").balance == 10


def test_concurrent_claims_move_the_balance_exactly_once(shard_store):
    shard_store.apply("g", 10, ShardReason.IAP_PURCHASE, "seed")
    results: list[int] = []

    threads = [
        threading.Thread(target=lambda: results.append(moved(shard_store, "g", "a")))
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [0] * 7 + [10]
    assert shard_store.wallet("a").balance == 10


def test_an_already_claimed_guest_gives_a_second_account_nothing(shard_store):
    shard_store.apply("g", 10, ShardReason.IAP_PURCHASE, "seed")
    moved(shard_store, "g", "a")

    assert moved(shard_store, "g", "b") == 0
    assert (shard_store.wallet("a").balance, shard_store.wallet("b").balance) == (10, 0)


def test_an_empty_guest_wallet_writes_nothing(shard_store):
    assert moved(shard_store, "g", "a") == 0
    assert shard_store.entries == []


def test_claiming_yourself_is_refused(shard_store):
    shard_store.apply("a", 10, ShardReason.IAP_PURCHASE, "seed")

    assert moved(shard_store, "a", "a") == 0
    assert shard_store.wallet("a").balance == 10


def test_a_ledger_pair_records_both_sides(shard_store):
    shard_store.apply("g", 10, ShardReason.IAP_PURCHASE, "seed")
    moved(shard_store, "g", "a")

    pair = [e for e in shard_store.entries if e.reason == ShardReason.GUEST_CLAIM]
    assert sorted(e.delta for e in pair) == [-10, 10]


def test_an_account_cannot_be_relabelled_as_a_guest(auth_store):
    """계정을 guest 취급해 남의 지갑을 가져가는 경로가 없다."""
    account, _ = auth_store.user_for_identity("apple", "001.subject")

    with pytest.raises(StoreUnavailable):
        auth_store.link_identity("apple", "001.other", account.id)


def test_a_claimed_guest_is_recorded_write_once(auth_store):
    first = auth_store.create_guest_user()

    auth_store.mark_guest_claimed(first.id, "a")
    auth_store.mark_guest_claimed(first.id, "b")

    assert auth_store.user(first.id).claimed_by_user_id == "a"


def test_a_guest_owner_only_helps_the_account_that_claimed_it(shard_store, auth_store):
    """`_check_owner`의 우회로가 아니다 — 서버가 적어 둔 주인만 통과한다."""
    ghost = auth_store.create_guest_user()
    auth_store.mark_guest_claimed(ghost.id, "11111111-2222-4333-8444-555555555555")
    service = IAPService(
        verifier=FakeVerifier(transaction(app_account_token=ghost.id)),
        shards=ShardLedgerService(shard_store),
        bundle_id=CLIENT_ID,
        allowed_environments=parse_allowed_environments("Production"),
        users=auth_store,
    )

    with pytest.raises(Exception) as error:
        service.credit(User(id="99999999-8888-4777-8666-555555555555"), "jws")

    assert "appAccountToken" in str(error.value)


# MARK: - 계정이 필요한 것은 그대로 (§5)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/marketplace/listings"),
        ("post", "/marketplace/listings/live/publish"),
        ("post", "/marketplace/listings/live/unpublish"),
        ("get", "/users/me/marketplace/listings"),
        ("delete", "/users/me/marketplace/listings/live"),
        ("get", "/users/me/marketplace/listings/live/preview"),
    ],
)
def test_seller_paths_still_need_an_apple_account(client, method, path):
    _, token = guest(client)

    response = getattr(client, method)(
        path,
        headers=bearer(token),
        **({"json": {
            "contentType": "mirror",
            "title": "t",
            "priceShards": 0,
            "snapshotId": "s",
        }} if method == "post" and path.endswith("/listings") else {}),
    )

    assert response.status_code == 403
    assert "Apple" in response.json()["detail"]


def test_a_guest_session_renews_for_the_same_wallet(client, verifier):
    """30일이 지나 지갑을 잃으면 안 된다 — 같은 사용자에게 새 session을 준다."""
    user_id, token = guest(client)
    buy(client, verifier, token, user_id, "tx-renew")

    renewed = client.post("/auth/guest", headers=bearer(token))

    assert renewed.status_code == 200
    assert renewed.json()["user"]["id"] == user_id
    fresh = renewed.json()["accessToken"]
    assert fresh != token
    assert balance(client, fresh) == 10
    # 옛 token은 그대로 살아 있다 — 응답을 잃은 client가 돌아올 수 있어야 한다.
    assert balance(client, token) == 10


def test_an_account_session_does_not_become_a_guest(client, apple_key):
    """계정 token으로 불러도 그 계정이 guest가 되지 않는다 — 새 guest가 나온다."""
    account = sign_in(client, apple_key).json()

    response = client.post("/auth/guest", headers=bearer(account["accessToken"]))

    assert response.status_code == 200
    assert response.json()["user"]["id"] != account["user"]["id"]


@pytest.mark.parametrize(
    "path", ["/users/me/attendance", "/users/me/rewarded-ads/context"]
)
def test_free_shard_paths_still_need_an_apple_account(client, path):
    """**공짜 조각은 guest에게 주지 않는다.**

    guest 신원은 요청 하나로 무한히 만들 수 있고 지갑은 로그인할 때 계정으로 넘어간다 —
    열어 두면 "새 guest → 출석 → 넘기기"로 조각을 찍어낼 수 있다.
    """
    _, token = guest(client)

    response = client.post(path, headers=bearer(token))

    assert response.status_code == 403
    assert "Apple" in response.json()["detail"]


def test_an_account_still_claims_attendance(client, apple_key):
    """막은 것은 guest뿐이다 — 계정의 출석은 그대로 동작한다."""
    session = sign_in(client, apple_key).json()

    response = client.post(
        "/users/me/attendance", headers=bearer(session["accessToken"])
    )

    assert response.status_code == 200
    assert response.json()["claimed"] is True


def test_a_guest_cannot_take_a_display_name(client):
    _, token = guest(client)

    response = client.patch(
        "/users/me/profile", json={"displayName": "pink"}, headers=bearer(token)
    )

    assert response.status_code == 403


def test_signing_in_still_works_without_a_guest_session(client, apple_key):
    """로그인 경로는 그대로다 — guest token이 없어도 된다."""
    assert sign_in(client, apple_key).status_code == 200
