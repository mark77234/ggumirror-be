"""조각 IAP (B-6A).

**Apple을 부르지 않는다.** 검증기는 seam이고 여기서는 가짜를 주입한다 —
production 경로에는 가짜를 켤 설정이 없다(`test_no_fake_verifier_in_production`).

가장 중요한 두 가지를 고정한다:
1. `appAccountToken`이 Apple transaction을 **우리 사용자에 묶는다**
2. 같은 `transactionId`는 **전역에서 한 번만** 쓰인다 (다른 user namespace로도 못 쓴다)
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth.models import User, sha256_hex
from app.auth.store import InMemoryAuthStore
from app.core.config import Settings
from app.iap.models import (
    AccountTokenMismatch,
    EnvironmentNotAllowed,
    IAPEnvironment,
    IAPUnavailable,
    InvalidTransaction,
    SHARD_PRODUCTS,
    TransactionAlreadyClaimed,
    UnknownProduct,
    VerifiedTransaction,
    parse_allowed_environments,
    transaction_claim_id,
)
from app.iap.service import TRANSACTIONS, IAPService
from app.iap.verifier import UnconfiguredVerifier, build_verifier
from app.main import create_app
from app.shards.models import ShardReason
from app.shards.service import ShardLedgerService
from app.shards.store import InMemoryShardStore
from tests.conftest import apple_claims

CLIENT_ID = "com.mark77234.ggumirror"
USER = "063cd7cb-fd94-4055-b6d8-2e4866879ed9"
OTHER_USER = "11111111-2222-4333-8444-555555555555"
PRODUCT_10 = "com.mark77234.ggumirror.shards.10"
PRODUCT_50 = "com.mark77234.ggumirror.shards.50"
PRODUCT_100 = "com.mark77234.ggumirror.shards.100"


class FakeVerifier:
    """서명 검증을 흉내 내지 않는다 — **이미 검증된 결과**를 그대로 돌려준다.

    B-6A가 시험하는 것은 서명 알고리즘이 아니라 그 **뒤의 정책**이다.
    실제 검증은 B-6B에서 Apple 공식 라이브러리가 한다.
    """

    is_configured = True

    def __init__(self, transaction: VerifiedTransaction | None = None) -> None:
        self.transaction = transaction
        self.calls: list[str] = []

    def verify(self, signed_transaction: str) -> VerifiedTransaction:
        self.calls.append(signed_transaction)
        if self.transaction is None:
            raise InvalidTransaction("signature is not valid")
        return self.transaction


def transaction(
    *,
    transaction_id: str = "2000000123456789",
    product_id: str = PRODUCT_10,
    app_account_token: str | None = USER,
    environment: str = IAPEnvironment.PRODUCTION,
    bundle_id: str = CLIENT_ID,
    transaction_type: str = "Consumable",
) -> VerifiedTransaction:
    return VerifiedTransaction(
        transaction_id=transaction_id,
        product_id=product_id,
        bundle_id=bundle_id,
        environment=environment,
        app_account_token=app_account_token,
        transaction_type=transaction_type,
    )


@pytest.fixture
def store() -> InMemoryShardStore:
    return InMemoryShardStore()


def build(store: InMemoryShardStore, verifier, environments: str = "Production") -> IAPService:
    return IAPService(
        verifier=verifier,
        shards=ShardLedgerService(store),
        bundle_id=CLIENT_ID,
        allowed_environments=parse_allowed_environments(environments),
    )


def user(user_id: str = USER) -> User:
    return User(id=user_id)


# MARK: - 정상 지급


@pytest.mark.parametrize(
    ("product", "amount"),
    [(PRODUCT_10, 10), (PRODUCT_50, 50), (PRODUCT_100, 100)],
)
def test_each_tier_credits_its_catalog_amount(store, product, amount):
    service = build(store, FakeVerifier(transaction(product_id=product)))

    result = service.credit(user(), "jws")

    assert result.credited is True
    assert result.amount == amount
    assert result.balance == amount
    entries = [e for e in store.entries if e.reason is ShardReason.IAP_PURCHASE]
    assert len(entries) == 1
    assert entries[0].delta == amount


def test_catalog_is_exactly_three_tiers():
    """티어는 10 / 50 / 100이다. 문서의 10/30/70/160은 outdated다."""
    assert sorted(SHARD_PRODUCTS.values()) == [10, 50, 100]
    assert set(SHARD_PRODUCTS) == {PRODUCT_10, PRODUCT_50, PRODUCT_100}


def test_amount_comes_from_server_not_client(store):
    """client가 수량을 정할 수 없다 — JWS의 productId만 본다."""
    service = build(store, FakeVerifier(transaction(product_id=PRODUCT_10)))
    result = service.credit(user(), "jws")
    assert result.amount == 10  # 요청 어디에도 수량을 실을 자리가 없다


# MARK: - appAccountToken (security change 1)


def test_missing_account_token_is_rejected(store):
    service = build(store, FakeVerifier(transaction(app_account_token=None)))

    with pytest.raises(AccountTokenMismatch):
        service.credit(user(), "jws")

    assert store.entries == []
    assert store.claims == {}


def test_account_token_of_another_user_is_rejected(store):
    """남의 결제 JWS로 내 지갑을 채울 수 없다."""
    service = build(store, FakeVerifier(transaction(app_account_token=OTHER_USER)))

    with pytest.raises(AccountTokenMismatch):
        service.credit(user(USER), "jws")

    assert store.entries == []
    assert ShardLedgerService(store).wallet(USER).balance == 0


def test_account_token_casing_does_not_matter(store):
    """Apple은 소문자로 주지만 표기가 흔들려도 같은 UUID면 같은 사람이다."""
    service = build(store, FakeVerifier(transaction(app_account_token=USER.upper())))
    assert service.credit(user(USER), "jws").credited is True


def test_malformed_account_token_is_rejected(store):
    for bad in ["", "not-a-uuid", "0", "null"]:
        store_ = InMemoryShardStore()
        service = build(store_, FakeVerifier(transaction(app_account_token=bad)))
        with pytest.raises(AccountTokenMismatch):
            service.credit(user(), "jws")
        assert store_.entries == []


# MARK: - 전역 transaction claim (security change 2)


def test_duplicate_from_same_user_credits_exactly_once(store):
    service = build(store, FakeVerifier(transaction()))

    first = service.credit(user(), "jws")
    second = service.credit(user(), "jws")

    assert first.credited is True
    assert second.credited is False
    assert second.balance == first.balance == 10
    assert len([e for e in store.entries if e.reason is ShardReason.IAP_PURCHASE]) == 1


def test_same_transaction_from_another_user_is_rejected(store):
    """★ 원장 멱등만으로는 막히지 않는다 — 열쇠에 user_id가 들어가기 때문이다."""
    shared = transaction()
    build(store, FakeVerifier(shared)).credit(user(USER), "jws")
    before = list(store.entries)

    # 두 번째 사용자가 같은 transaction을 자기 token으로 들고 온다.
    hijacked = transaction(app_account_token=OTHER_USER)
    service = build(store, FakeVerifier(hijacked))

    with pytest.raises(TransactionAlreadyClaimed):
        service.credit(user(OTHER_USER), "jws")

    # 두 번째 지갑은 **한 번도 움직이지 않았다.**
    assert ShardLedgerService(store).wallet(OTHER_USER).balance == 0
    assert store.entries == before
    assert ShardLedgerService(store).wallet(USER).balance == 10


def test_claim_id_is_global_and_hides_the_raw_transaction_id():
    raw = "2000000123456789"
    key = transaction_claim_id(raw)
    assert raw not in key
    assert len(key) == 64
    # user가 섞이지 않는다 — 그래서 누가 들고 와도 같은 자리를 겨룬다.
    assert transaction_claim_id(raw) == transaction_claim_id(raw)


def test_claim_stores_only_safe_metadata(store):
    build(store, FakeVerifier(transaction())).credit(user(), "jws")

    (claim,) = [doc for (collection, _), doc in store.claims.items() if collection == TRANSACTIONS]
    assert claim["productId"] == PRODUCT_10
    assert claim["amount"] == 10
    assert claim["environment"] == IAPEnvironment.PRODUCTION
    assert claim["userId"] == USER
    assert claim["ledgerEntryId"]
    # raw transaction id를 넣지 않는다.
    assert "2000000123456789" not in str(claim)


# MARK: - atomicity


def test_claim_ledger_and_wallet_move_together(store):
    build(store, FakeVerifier(transaction())).credit(user(), "jws")

    assert len(store.claims) == 1
    assert len([e for e in store.entries if e.reason is ShardReason.IAP_PURCHASE]) == 1
    assert ShardLedgerService(store).wallet(USER).balance == 10
    # 원장이 계산한 잔액과 projection이 어긋나지 않는다.
    assert store.entries[-1].balance_after == 10


def test_rejected_transaction_writes_nothing_anywhere(store):
    service = build(store, FakeVerifier(transaction(product_id="com.example.unknown")))

    with pytest.raises(UnknownProduct):
        service.credit(user(), "jws")

    assert store.claims == {}
    assert store.entries == []
    assert store.wallets == {}


def test_concurrent_duplicates_have_exactly_one_winner(store):
    """동시에 같은 transaction이 들어와도 원장에 적는 것은 하나뿐이다."""
    service = build(store, FakeVerifier(transaction()))
    results: list[bool] = []
    lock = threading.Lock()

    def submit() -> None:
        try:
            outcome = service.credit(user(), "jws")
        except TransactionAlreadyClaimed:
            outcome = None
        with lock:
            results.append(outcome.credited if outcome else False)

    threads = [threading.Thread(target=submit) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(True) == 1, results
    assert ShardLedgerService(store).wallet(USER).balance == 10
    assert len([e for e in store.entries if e.reason is ShardReason.IAP_PURCHASE]) == 1


# MARK: - 형식 / 환경


def test_unknown_product_is_rejected(store):
    service = build(store, FakeVerifier(transaction(product_id="com.mark77234.ggumirror.shards.9999")))
    with pytest.raises(UnknownProduct):
        service.credit(user(), "jws")


def test_other_bundle_id_is_rejected(store):
    service = build(store, FakeVerifier(transaction(bundle_id="com.someone.else")))
    with pytest.raises(InvalidTransaction):
        service.credit(user(), "jws")
    assert store.entries == []


def test_non_consumable_is_rejected(store):
    service = build(store, FakeVerifier(transaction(transaction_type="Auto-Renewable Subscription")))
    with pytest.raises(InvalidTransaction):
        service.credit(user(), "jws")


def test_sandbox_is_rejected_unless_allowed(store):
    service = build(store, FakeVerifier(transaction(environment="Sandbox")), environments="Production")
    with pytest.raises(EnvironmentNotAllowed):
        service.credit(user(), "jws")
    assert store.entries == []


def test_sandbox_is_accepted_when_configured(store):
    service = build(
        store, FakeVerifier(transaction(environment="Sandbox")), environments="Production,Sandbox"
    )
    assert service.credit(user(), "jws").credited is True


def test_empty_environment_config_allows_nothing(store):
    assert parse_allowed_environments("") == frozenset()
    service = build(store, FakeVerifier(transaction()), environments="")
    with pytest.raises(EnvironmentNotAllowed):
        service.credit(user(), "jws")


def test_xcode_environment_can_never_be_allowed():
    """Xcode StoreKit Testing 서명은 로컬에서 만들어진다 — 설정으로도 켤 수 없다."""
    for raw in ["Xcode", "Production,Xcode", "Xcode,Sandbox", "xcode"]:
        assert IAPEnvironment.XCODE not in parse_allowed_environments(raw)


def test_xcode_transaction_is_rejected_even_with_everything_allowed(store):
    service = build(
        store, FakeVerifier(transaction(environment="Xcode")), environments="Production,Sandbox,Xcode"
    )
    with pytest.raises(EnvironmentNotAllowed):
        service.credit(user(), "jws")
    assert store.entries == []


def test_invalid_signature_is_rejected(store):
    service = build(store, FakeVerifier(None))
    with pytest.raises(InvalidTransaction):
        service.credit(user(), "jws")


def test_empty_signed_transaction_is_rejected(store):
    service = build(store, FakeVerifier(transaction()))
    for raw in ["", "   "]:
        with pytest.raises(InvalidTransaction):
            service.credit(user(), raw)


# MARK: - fail closed


def test_unconfigured_verifier_refuses_to_credit(store):
    service = build(store, UnconfiguredVerifier())
    with pytest.raises(IAPUnavailable):
        service.credit(user(), "jws")
    assert store.entries == []


def test_production_verifier_is_fail_closed():
    """B-6A는 seam만 만든다 — 추측한 검증 로직으로 지급하지 않는다."""
    assert build_verifier().is_configured is False


def test_service_is_unavailable_without_verifier_or_environment(store):
    assert build(store, UnconfiguredVerifier()).is_available is False
    assert build(store, FakeVerifier(transaction()), environments="").is_available is False
    assert build(store, FakeVerifier(transaction())).is_available is True


def test_no_fake_verifier_in_production():
    """가짜 검증기를 **설정으로** 켤 수 있는 경로를 만들지 않았다."""
    source = Path(__file__).resolve().parent.parent
    for path in ["app/iap/verifier.py", "app/iap/service.py", "app/main.py", "app/core/config.py"]:
        text = (source / path).read_text(encoding="utf-8")
        assert "FakeVerifier" not in text
        assert "IAP_FAKE" not in text
        assert "allow_unverified" not in text


# MARK: - 로그


def test_logs_never_contain_raw_transaction_id_or_user(store, caplog):
    service = build(store, FakeVerifier(transaction()))
    with caplog.at_level(logging.DEBUG):
        service.credit(user(), "jws")

    assert "2000000123456789" not in caplog.text
    assert USER not in caplog.text
    assert "jws" not in caplog.text


# MARK: - HTTP


@pytest.fixture
def client(store, apple_key, jwks_of, monkeypatch) -> TestClient:
    from app.auth import jwks as jwks_module

    document = jwks_of(apple_key)
    monkeypatch.setattr(jwks_module, "http_jwks_fetch", lambda *a, **k: lambda: document)
    app = create_app(
        Settings(app_env="local", apple_client_id=CLIENT_ID, iap_allowed_environments="Production"),
        auth_store=InMemoryAuthStore(),
        shard_store=store,
        transaction_verifier=FakeVerifier(transaction(app_account_token=None)),
    )
    return TestClient(app)


def auth(client: TestClient, apple_key) -> dict:
    nonce = "nonce-iap"
    token = apple_key.token(apple_claims(sub="001234.abcdef0123456789.1234", nonce=sha256_hex(nonce)))
    access = client.post(
        "/auth/apple", json={"identityToken": token, "nonce": nonce}
    ).json()["accessToken"]
    return {"Authorization": f"Bearer {access}"}


def test_http_requires_authentication(client: TestClient):
    assert client.post("/users/me/iap/shards", json={"signedTransaction": "jws"}).status_code == 401


def test_http_body_takes_only_the_signed_transaction(client: TestClient, apple_key):
    """`amount`를 몰래 얹을 수 없다 — schema가 거절한다."""
    headers = auth(client, apple_key)
    response = client.post(
        "/users/me/iap/shards",
        json={"signedTransaction": "jws", "amount": 999},
        headers=headers,
    )
    assert response.status_code == 422


def test_http_rejects_token_mismatch_without_revealing_why(client: TestClient, apple_key):
    headers = auth(client, apple_key)
    response = client.post(
        "/users/me/iap/shards", json={"signedTransaction": "jws"}, headers=headers
    )
    assert response.status_code == 400
    # 어느 검사에서 걸렸는지 알려주지 않는다.
    assert "appAccountToken" not in response.text


def test_http_credits_and_is_idempotent(store, apple_key, jwks_of, monkeypatch):
    from app.auth import jwks as jwks_module

    document = jwks_of(apple_key)
    monkeypatch.setattr(jwks_module, "http_jwks_fetch", lambda *a, **k: lambda: document)

    auth_store = InMemoryAuthStore()
    app = create_app(
        Settings(app_env="local", apple_client_id=CLIENT_ID, iap_allowed_environments="Production"),
        auth_store=auth_store,
        shard_store=store,
        # 로그인한 user id를 알아야 token을 맞출 수 있어 나중에 채운다.
        transaction_verifier=(verifier := FakeVerifier(None)),
    )
    client = TestClient(app)
    headers = auth(client, apple_key)
    user_id = client.get("/users/me", headers=headers).json()["id"]
    verifier.transaction = transaction(app_account_token=user_id)

    first = client.post("/users/me/iap/shards", json={"signedTransaction": "jws"}, headers=headers)
    second = client.post("/users/me/iap/shards", json={"signedTransaction": "jws"}, headers=headers)

    assert first.status_code == second.status_code == 200
    assert first.json() == {"credited": True, "amount": 10, "balance": 10}
    assert second.json() == {"credited": False, "amount": 10, "balance": 10}
    assert len([e for e in store.entries if e.reason is ShardReason.IAP_PURCHASE]) == 1


def test_no_generic_mutation_endpoint(client: TestClient):
    """B-6이 generic 통로를 열지 않았는지 계속 확인한다."""
    for path in ["/shards", "/shards/credit", "/shards/add", "/users/me/shards", "/iap/credit"]:
        assert client.post(path, json={"amount": 100}).status_code in {401, 404, 405}
