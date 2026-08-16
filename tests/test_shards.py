"""거울 조각 지갑 / 원장.

실제 Firestore에 붙지 않는다 — `InMemoryShardStore`가 transaction의 **의미**를 흉내 낸다
(중복 무시 · 잔액 부족 거부 · 잔액과 원장을 함께 갱신).

여기서 지키는 것:
1. 잔액은 서버가 정한다 — client가 amount / reason / userId를 정하는 통로가 없다
2. 같은 사건은 한 번만 반영된다 (광고 SSV 재전송 · 결제 재시도 · 출석 retry)
3. 잔액은 음수가 될 수 없고, 실패한 거래는 **아무 흔적도 남기지 않는다**
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.auth.store import InMemoryAuthStore
from app.core.config import Settings
from app.main import create_app
from app.shards.models import (
    InsufficientShards,
    InvalidShardAmount,
    ShardReason,
    idempotency_hash,
)
from app.shards.service import ShardLedgerService
from app.shards.store import InMemoryShardStore
from app.auth.models import sha256_hex
from tests.conftest import CLIENT_ID, apple_claims

USER = "internal-user-1"
OTHER = "internal-user-2"


@pytest.fixture
def store() -> InMemoryShardStore:
    return InMemoryShardStore()


@pytest.fixture
def shards(store: InMemoryShardStore) -> ShardLedgerService:
    return ShardLedgerService(store)


# MARK: - 지갑


def test_missing_wallet_is_zero(shards: ShardLedgerService, store: InMemoryShardStore):
    wallet = shards.wallet(USER)
    assert (wallet.balance, wallet.lifetime_earned, wallet.lifetime_spent) == (0, 0, 0)
    # 조회만으로 문서를 만들지 않는다.
    assert store.wallets == {}


def test_first_credit_creates_wallet(shards: ShardLedgerService, store: InMemoryShardStore):
    result = shards.credit(USER, 1, ShardReason.DAILY_ATTENDANCE)
    assert result.wallet.balance == 1
    assert USER in store.wallets


def test_credit_adds(shards: ShardLedgerService):
    assert shards.credit(USER, 1, ShardReason.DAILY_ATTENDANCE).wallet.balance == 1
    assert shards.credit(USER, 4, ShardReason.IAP_PURCHASE).wallet.balance == 5


def test_debit_subtracts(shards: ShardLedgerService):
    shards.credit(USER, 20, ShardReason.IAP_PURCHASE)
    assert shards.debit(USER, 20, ShardReason.MIRROR_PUBLISH_FEE).wallet.balance == 0


def test_insufficient_debit_changes_nothing(shards: ShardLedgerService, store: InMemoryShardStore):
    shards.credit(USER, 19, ShardReason.IAP_PURCHASE)
    entries_before = len(store.entries)

    with pytest.raises(InsufficientShards):
        shards.debit(USER, 20, ShardReason.MIRROR_PUBLISH_FEE)

    # 잔액도 원장도 그대로다.
    assert shards.wallet(USER).balance == 19
    assert len(store.entries) == entries_before


# MARK: - 같은 사건은 한 번만 (광고 SSV · 결제 재시도)


def test_same_event_applies_once(shards: ShardLedgerService, store: InMemoryShardStore):
    first = shards.credit(USER, 1, ShardReason.REWARDED_AD, external_event_id="ssv-txn-1")
    second = shards.credit(USER, 1, ShardReason.REWARDED_AD, external_event_id="ssv-txn-1")

    assert first.wallet.balance == 1
    assert first.applied is True
    assert second.wallet.balance == 1, "같은 SSV transaction이 두 번 반영됐다"
    assert second.applied is False, "재전송된 callback이 지급했다고 답했다"
    assert len(store.entries) == 1


def test_different_events_each_apply(shards: ShardLedgerService, store: InMemoryShardStore):
    for index in range(5):
        shards.credit(USER, 1, ShardReason.REWARDED_AD, external_event_id=f"ssv-txn-{index}")

    assert shards.wallet(USER).balance == 5
    assert len(store.entries) == 5


def test_same_id_different_reason_is_a_different_event(shards: ShardLedgerService):
    shards.credit(USER, 1, ShardReason.REWARDED_AD, external_event_id="abc")
    shards.credit(USER, 1, ShardReason.DAILY_ATTENDANCE, external_event_id="abc")
    # 출처가 다르면 다른 사건이다 — 우연히 같은 문자열이라고 막히면 안 된다.
    assert shards.wallet(USER).balance == 2


def test_repeated_debit_event_applies_once(shards: ShardLedgerService):
    shards.credit(USER, 40, ShardReason.IAP_PURCHASE)
    shards.debit(USER, 20, ShardReason.MIRROR_PURCHASE, external_event_id="purchase-1")
    shards.debit(USER, 20, ShardReason.MIRROR_PURCHASE, external_event_id="purchase-1")
    assert shards.wallet(USER).balance == 20


def test_idempotency_hash_hides_the_original(shards: ShardLedgerService, store: InMemoryShardStore):
    shards.credit(USER, 1, ShardReason.REWARDED_AD, external_event_id="ssv-secret-transaction")

    entry = store.entries[0]
    assert entry.idempotency_key_hash == idempotency_hash(
        USER, ShardReason.REWARDED_AD, "ssv-secret-transaction"
    )
    # 원본 외부 식별자도, raw user id도 원장 문서 ID에 남지 않는다.
    assert "ssv-secret-transaction" not in entry.idempotency_key_hash
    assert USER not in entry.idempotency_key_hash
    assert len(entry.idempotency_key_hash) == 64


# MARK: - idempotency는 사용자 단위다 (cross-user collision 금지)


def test_same_event_is_scoped_per_user(shards: ShardLedgerService, store: InMemoryShardStore):
    """출석처럼 event id가 날짜뿐이어도 사용자끼리 겨루지 않는다.

    user scope가 없으면 `daily_attendance` + `2026-08-12`가 전 사용자 공용 문서가 되고
    **하루에 한 사람만** 조각을 받는다.
    """
    date = "2026-08-12"

    # A. userA 첫 출석 → 지급
    assert shards.credit(USER, 1, ShardReason.DAILY_ATTENDANCE, external_event_id=date).wallet.balance == 1

    # B. userA 같은 날 다시 → 중복, 두 번 지급되지 않는다
    assert shards.credit(USER, 1, ShardReason.DAILY_ATTENDANCE, external_event_id=date).wallet.balance == 1

    # C. userB 같은 날 → 남의 사건이 아니다. 정상 지급
    assert shards.credit(OTHER, 1, ShardReason.DAILY_ATTENDANCE, external_event_id=date).wallet.balance == 1

    assert shards.wallet(USER).balance == 1
    assert shards.wallet(OTHER).balance == 1
    assert len(store.entries) == 2, "사용자별로 한 줄씩 남아야 한다"


def test_rewarded_ad_event_is_scoped_per_user(shards: ShardLedgerService, store: InMemoryShardStore):
    """D · E — AdMob SSV transaction_id가 global unique여도 원장 invariant는 user-scoped다."""
    transaction_id = "transaction-123"

    # D. 같은 사용자에게 두 번 도착 → 1회만
    shards.credit(USER, 1, ShardReason.REWARDED_AD, external_event_id=transaction_id)
    shards.credit(USER, 1, ShardReason.REWARDED_AD, external_event_id=transaction_id)
    assert shards.wallet(USER).balance == 1

    # E. 다른 사용자에게 같은 id가 와도 충돌하지 않는다
    shards.credit(OTHER, 1, ShardReason.REWARDED_AD, external_event_id=transaction_id)
    assert shards.wallet(OTHER).balance == 1

    assert shards.wallet(USER).balance == 1, "다른 사용자의 지급이 남의 잔액을 건드렸다"
    assert len(store.entries) == 2


def test_scoped_key_differs_by_user(shards: ShardLedgerService):
    a = idempotency_hash(USER, ShardReason.DAILY_ATTENDANCE, "2026-08-12")
    b = idempotency_hash(OTHER, ShardReason.DAILY_ATTENDANCE, "2026-08-12")
    assert a != b


def test_canonical_encoding_has_no_ambiguity():
    """구분자가 값에 섞여 들어와도 서로 다른 조합이 같은 열쇠가 되지 않는다.

    단순히 `":".join(...)`이면 ("u:extra", reason, "1")과 ("u", reason, "extra:1")이
    같은 문자열이 되어 서로 다른 사건이 하나로 합쳐진다.
    """
    keys = {
        idempotency_hash("u:x", ShardReason.REWARDED_AD, "1"),
        idempotency_hash("u", ShardReason.REWARDED_AD, "x:1"),
        idempotency_hash("u|x", ShardReason.REWARDED_AD, "1"),
        idempotency_hash("u", ShardReason.REWARDED_AD, "x|1"),
    }
    assert len(keys) == 4


# MARK: - 원장 내용


def test_ledger_records_running_balance(shards: ShardLedgerService, store: InMemoryShardStore):
    shards.credit(USER, 3, ShardReason.IAP_PURCHASE)
    shards.debit(USER, 1, ShardReason.MIRROR_PURCHASE)
    shards.credit(USER, 2, ShardReason.MIRROR_SALE)

    assert [(e.delta, e.balance_after) for e in store.entries] == [(3, 3), (-1, 2), (2, 4)]


def test_lifetime_counters(shards: ShardLedgerService):
    shards.credit(USER, 10, ShardReason.IAP_PURCHASE)
    shards.debit(USER, 4, ShardReason.MIRROR_PURCHASE)
    shards.credit(USER, 1, ShardReason.DAILY_ATTENDANCE)

    wallet = shards.wallet(USER)
    assert (wallet.balance, wallet.lifetime_earned, wallet.lifetime_spent) == (7, 11, 4)


def test_ledger_is_append_only(shards: ShardLedgerService, store: InMemoryShardStore):
    shards.credit(USER, 1, ShardReason.DAILY_ATTENDANCE)
    snapshot = list(store.entries)

    shards.credit(USER, 1, ShardReason.REWARDED_AD, external_event_id="ssv-1")

    # 기존 줄은 그대로 있고 뒤에 붙기만 한다.
    assert store.entries[: len(snapshot)] == snapshot
    assert len(store.entries) == len(snapshot) + 1
    # 저장소에 수정/삭제 연산 자체가 없다.
    assert not hasattr(store, "update_entry")
    assert not hasattr(store, "delete_entry")


def test_wallets_are_separate(shards: ShardLedgerService):
    shards.credit("user-a", 5, ShardReason.IAP_PURCHASE)
    shards.credit("user-b", 2, ShardReason.IAP_PURCHASE)
    assert shards.wallet("user-a").balance == 5
    assert shards.wallet("user-b").balance == 2


# MARK: - 잘못된 금액


@pytest.mark.parametrize("amount", [0, -1, -100])
def test_rejects_non_positive_amount(shards: ShardLedgerService, amount: int):
    for move in (shards.credit, shards.debit):
        with pytest.raises(InvalidShardAmount):
            move(USER, amount, ShardReason.ADMIN_ADJUSTMENT)


def test_rejects_absurd_amount(shards: ShardLedgerService):
    with pytest.raises(InvalidShardAmount):
        shards.credit(USER, 10_000_000, ShardReason.IAP_PURCHASE)


def test_rejects_non_integer_amount(shards: ShardLedgerService):
    for amount in [1.5, "1", True]:
        with pytest.raises(InvalidShardAmount):
            shards.credit(USER, amount, ShardReason.IAP_PURCHASE)  # type: ignore[arg-type]


def test_balance_stays_integer(shards: ShardLedgerService):
    shards.credit(USER, 3, ShardReason.IAP_PURCHASE)
    wallet = shards.wallet(USER)
    assert isinstance(wallet.balance, int)
    assert not isinstance(wallet.balance, bool)


# MARK: - 동시성
#
# `InMemoryShardStore.apply`가 lock 안에서 통째로 일어난다 —
# Firestore transaction이 주는 원자성과 같은 의미다. test가 직렬화 장치를 따로 끼우지 않는다.


def test_concurrent_credits_do_not_lose_updates(store: InMemoryShardStore):
    """+1이 여러 번 동시에 들어와도 하나도 사라지지 않는다."""
    shards = ShardLedgerService(store)
    threads = [
        threading.Thread(
            target=lambda index=index: shards.credit(
                USER, 1, ShardReason.REWARDED_AD, external_event_id=f"ssv-{index}"
            )
        )
        for index in range(20)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert shards.wallet(USER).balance == 20
    assert len(store.entries) == 20


def test_concurrent_debits_cannot_go_negative(store: InMemoryShardStore):
    """잔액 20에 -20이 두 번 동시에 오면 하나만 성공한다."""
    shards = ShardLedgerService(store)
    shards.credit(USER, 20, ShardReason.IAP_PURCHASE)

    failures: list[Exception] = []

    def spend(index: int) -> None:
        try:
            shards.debit(USER, 20, ShardReason.MIRROR_PURCHASE, external_event_id=f"buy-{index}")
        except InsufficientShards as error:
            failures.append(error)

    threads = [threading.Thread(target=spend, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert shards.wallet(USER).balance == 0
    assert len(failures) == 1


def test_same_event_concurrently_applies_once(store: InMemoryShardStore):
    """같은 SSV transaction이 동시에 두 번 도착해도 한 번만 반영된다."""
    shards = ShardLedgerService(store)
    threads = [
        threading.Thread(
            target=lambda: shards.credit(USER, 1, ShardReason.REWARDED_AD, external_event_id="ssv-dup")
        )
        for _ in range(5)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert shards.wallet(USER).balance == 1
    assert len(store.entries) == 1


def test_concurrent_same_event_has_exactly_one_applied(store: InMemoryShardStore):
    """같은 사건이 동시에 여러 번 와도 **"내가 적었다"고 답하는 것은 하나뿐이다.**

    출석(B-4)만의 규칙이 아니다 — 광고 SSV 재전송 · 결제 재시도도 이 답을 그대로 쓴다.
    """
    shards = ShardLedgerService(store)
    results: list = [None] * 5
    barrier = threading.Barrier(5)

    def run(index: int) -> None:
        barrier.wait()
        results[index] = shards.credit(
            USER, 1, ShardReason.REWARDED_AD, external_event_id="ssv-same"
        )

    threads = [threading.Thread(target=run, args=(index,)) for index in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(1 for r in results if r.applied) == 1
    assert all(r.wallet.balance == 1 for r in results)
    assert len(store.entries) == 1


# MARK: - B-5 AdMob SSV 준비


def test_rewarded_ad_uses_ssv_transaction_id(shards: ShardLedgerService, store: InMemoryShardStore):
    """B-5가 그대로 연결될 수 있어야 한다: SSV transaction_id → 중복 없는 +1."""
    transaction_id = "4f8c1d2e-ssv"

    # 광고 하나가 SSV callback으로 두 번 도착하는 상황.
    shards.credit(USER, 1, ShardReason.REWARDED_AD, external_event_id=transaction_id)
    shards.credit(USER, 1, ShardReason.REWARDED_AD, external_event_id=transaction_id)

    assert shards.wallet(USER).balance == 1
    assert store.entries[0].reason == ShardReason.REWARDED_AD


def test_daily_attendance_uses_date_as_event(shards: ShardLedgerService):
    """B-4도 같은 구조로 붙는다: 하루치 날짜가 곧 사건 id다."""
    for _ in range(3):
        shards.credit(USER, 1, ShardReason.DAILY_ATTENDANCE, external_event_id="2026-08-12")
    shards.credit(USER, 1, ShardReason.DAILY_ATTENDANCE, external_event_id="2026-08-13")

    assert shards.wallet(USER).balance == 2


# MARK: - HTTP


@pytest.fixture
def client(store: InMemoryShardStore, apple_key, jwks_of, monkeypatch) -> TestClient:
    from app.auth import jwks as jwks_module

    document = jwks_of(apple_key)
    monkeypatch.setattr(jwks_module, "http_jwks_fetch", lambda *a, **k: lambda: document)

    app = create_app(
        Settings(app_env="local", apple_client_id=CLIENT_ID),
        auth_store=InMemoryAuthStore(),
        shard_store=store,
    )
    return TestClient(app)


def sign_in(client: TestClient, apple_key) -> str:
    nonce = "nonce-abc"
    token = apple_key.token(apple_claims(nonce=sha256_hex(nonce)))
    response = client.post("/auth/apple", json={"identityToken": token, "nonce": nonce})
    return response.json()["accessToken"]


def test_wallet_requires_authentication(client: TestClient):
    assert client.get("/users/me/shards").status_code == 401
    assert client.get("/users/me/shards", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_new_user_sees_zero(client: TestClient, apple_key):
    token = sign_in(client, apple_key)
    response = client.get("/users/me/shards", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"balance": 0, "lifetimeEarned": 0, "lifetimeSpent": 0}


def test_wallet_reflects_ledger(client: TestClient, apple_key, store: InMemoryShardStore):
    token = sign_in(client, apple_key)
    user_id = client.get("/users/me", headers={"Authorization": f"Bearer {token}"}).json()["id"]

    ShardLedgerService(store).credit(user_id, 12, ShardReason.IAP_PURCHASE)
    body = client.get("/users/me/shards", headers={"Authorization": f"Bearer {token}"}).json()

    assert body["balance"] == 12
    # 누구인지는 응답에 넣지 않는다 — 부르는 쪽이 이미 자기 자신이다.
    assert "userId" not in body


def test_wallet_belongs_to_the_caller(client: TestClient, apple_key, store: InMemoryShardStore):
    """다른 사람의 지갑을 요청할 방법이 없다 — userId를 받는 자리가 없다."""
    token = sign_in(client, apple_key)
    ShardLedgerService(store).credit("someone-else", 999, ShardReason.IAP_PURCHASE)

    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/users/me/shards", headers=headers).json()["balance"] == 0
    # query / body로 우겨넣어도 무시된다.
    assert client.get("/users/me/shards?userId=someone-else", headers=headers).json()["balance"] == 0


def test_no_mutation_endpoint_exists(client: TestClient):
    """client가 잔액을 바꾸는 통로가 **없어야 한다.**"""
    paths = {route.path for route in client.app.routes if hasattr(route, "path")}
    assert "/users/me/shards" in paths
    for forbidden in ["/shards/credit", "/shards/debit", "/shards/add", "/wallet/add", "/wallet/set"]:
        assert forbidden not in paths

    headers = {"Authorization": "Bearer whatever"}
    for method in ["post", "put", "patch"]:
        response = getattr(client, method)("/users/me/shards", headers=headers, json={"amount": 9999})
        assert response.status_code in (401, 404, 405)
    assert client.delete("/users/me/shards", headers=headers).status_code in (401, 404, 405)


def test_logs_have_no_sensitive_values(client: TestClient, apple_key, store, caplog):
    import logging

    token = sign_in(client, apple_key)
    with caplog.at_level(logging.DEBUG):
        client.get("/users/me/shards", headers={"Authorization": f"Bearer {token}"})
        ShardLedgerService(store).credit(USER, 1, ShardReason.REWARDED_AD, external_event_id="ssv-secret")

    assert "shard_wallet_read" in caplog.text
    assert "shard_ledger_credit" in caplog.text
    for secret in (token, "ssv-secret", USER):
        assert secret not in caplog.text
