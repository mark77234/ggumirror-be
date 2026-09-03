"""거울 보관 공간 확장 — 정책 · 원자성 · 멱등.

가장 위험한 것은 **반복 구매와 재시도를 구분하는 일**이다.
`user + packId`를 열쇠로 쓰면 두 번째 확장을 영원히 못 산다. 반대로 재시도마다
새 열쇠를 만들면 응답을 잃었을 때 조각이 두 번 빠진다.
그래서 **의도 하나 = operationId 하나**가 authority다.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.auth.models import sha256_hex
from app.auth.store import InMemoryAuthStore
from app.capacity.models import (
    BASE_MIRROR_SLOTS,
    MIRROR_SLOT_PACK,
    PACKS,
    UnknownPack,
    operation_key,
    pack,
)
from app.capacity.service import MirrorCapacityService
from app.capacity.store import InMemoryCapacityStore
from app.core.config import Settings
from app.main import create_app
from app.shards.models import (
    InsufficientShards,
    ShardReason,
)
from app.shards.service import ShardLedgerService
from app.shards.store import InMemoryShardStore
from tests.conftest import apple_claims

ALICE = "user-alice"
BOB = "user-bob"
CLIENT_ID = "com.mark77234.ggumirror"


@pytest.fixture
def shards() -> ShardLedgerService:
    return ShardLedgerService(InMemoryShardStore())


@pytest.fixture
def store() -> InMemoryCapacityStore:
    return InMemoryCapacityStore()


@pytest.fixture
def service(store, shards) -> MirrorCapacityService:
    return MirrorCapacityService(store, shards)


def fund(shards: ShardLedgerService, user_id: str, amount: int) -> None:
    """test용 잔액. 실제 경로(원장)를 그대로 쓴다 — 지갑을 직접 쓰지 않는다."""
    shards.credit(user_id, amount, ShardReason.ADMIN_ADJUSTMENT, f"seed-{user_id}-{amount}")


def op() -> str:
    return str(uuid4())


# MARK: - 정책 (§30)


def test_base_capacity_is_five():
    assert BASE_MIRROR_SLOTS == 5


def test_pack_costs_ten_for_five_slots():
    assert MIRROR_SLOT_PACK.id == "mirror_slots_5"
    assert MIRROR_SLOT_PACK.cost_shards == 10
    assert MIRROR_SLOT_PACK.slot_delta == 5


def test_unknown_pack_is_rejected():
    with pytest.raises(UnknownPack):
        pack("mirror_slots_500")
    with pytest.raises(UnknownPack):
        pack("")


def test_only_registered_packs_exist():
    assert set(PACKS) == {"mirror_slots_5"}


def test_request_has_no_place_for_cost_or_slots():
    """**client가 가격과 칸 수를 보낼 수 없다.**"""
    from app.api.capacity import PurchaseRequest

    fields = set(PurchaseRequest.model_fields)
    assert fields == {"pack_id", "operation_id"}
    for forbidden in ("cost", "cost_shards", "slot_delta", "slots", "amount", "balance"):
        assert forbidden not in fields


def test_legacy_user_has_zero_purchased_slots(service):
    capacity = service.capacity("never-seen")
    assert capacity.purchased_slots == 0
    assert capacity.base_slots == 5
    assert capacity.effective_slots == 5


# MARK: - transaction (§31)


def test_purchase_charges_and_grants(service, shards):
    fund(shards, ALICE, 20)

    result = service.purchase(ALICE, MIRROR_SLOT_PACK.id, op())

    assert result.applied is True
    assert result.charged_shards == 10
    assert result.slot_delta == 5
    assert result.balance == 10
    assert result.capacity.purchased_slots == 5
    assert result.capacity.effective_slots == 10
    assert shards.wallet(ALICE).balance == 10


def test_second_intentional_purchase_stacks(service, shards):
    fund(shards, ALICE, 20)

    first = service.purchase(ALICE, MIRROR_SLOT_PACK.id, op())
    second = service.purchase(ALICE, MIRROR_SLOT_PACK.id, op())

    assert (first.balance, first.capacity.effective_slots) == (10, 10)
    assert (second.balance, second.capacity.effective_slots) == (0, 15)
    assert second.applied is True


def test_three_purchases_reach_twenty_slots(service, shards):
    fund(shards, ALICE, 30)
    for _ in range(3):
        service.purchase(ALICE, MIRROR_SLOT_PACK.id, op())
    assert service.capacity(ALICE).effective_slots == 20
    assert shards.wallet(ALICE).balance == 0


def test_insufficient_shards_changes_nothing(service, shards):
    fund(shards, ALICE, 9)

    with pytest.raises(InsufficientShards):
        service.purchase(ALICE, MIRROR_SLOT_PACK.id, op())

    assert shards.wallet(ALICE).balance == 9
    assert service.capacity(ALICE).purchased_slots == 0
    assert shards.wallet(ALICE).lifetime_spent == 0


def test_zero_balance_changes_nothing(service, shards):
    with pytest.raises(InsufficientShards):
        service.purchase(ALICE, MIRROR_SLOT_PACK.id, op())
    assert service.capacity(ALICE).purchased_slots == 0


def test_unknown_pack_moves_no_economy(service, shards):
    fund(shards, ALICE, 100)
    with pytest.raises(UnknownPack):
        service.purchase(ALICE, "mirror_slots_999", op())
    assert shards.wallet(ALICE).balance == 100
    assert service.capacity(ALICE).purchased_slots == 0


def test_ledger_records_the_purchase(service, shards):
    fund(shards, ALICE, 20)
    service.purchase(ALICE, MIRROR_SLOT_PACK.id, op())

    wallet = shards.wallet(ALICE)
    assert wallet.lifetime_spent == 10
    assert wallet.balance == 10


def test_capacity_is_per_user(service, shards):
    fund(shards, ALICE, 20)
    fund(shards, BOB, 20)

    service.purchase(ALICE, MIRROR_SLOT_PACK.id, op())

    assert service.capacity(ALICE).effective_slots == 10
    assert service.capacity(BOB).effective_slots == 5
    assert shards.wallet(BOB).balance == 20


# MARK: - 멱등 (§31)


def test_same_operation_id_retry_moves_nothing_twice(service, shards):
    fund(shards, ALICE, 20)
    intent = op()

    first = service.purchase(ALICE, MIRROR_SLOT_PACK.id, intent)
    retry = service.purchase(ALICE, MIRROR_SLOT_PACK.id, intent)

    assert first.applied is True
    assert retry.applied is False
    # 같은 결과를 그대로 돌려준다 — client가 추측하지 않아도 된다.
    assert retry.charged_shards == 10
    assert retry.slot_delta == 5
    assert retry.capacity.effective_slots == 10
    assert retry.balance == 10
    # **경제는 한 번만 움직였다.**
    assert shards.wallet(ALICE).balance == 10
    assert shards.wallet(ALICE).lifetime_spent == 10
    assert service.capacity(ALICE).purchased_slots == 5


def test_many_retries_stay_at_one_purchase(service, shards):
    fund(shards, ALICE, 50)
    intent = op()
    for _ in range(6):
        service.purchase(ALICE, MIRROR_SLOT_PACK.id, intent)
    assert shards.wallet(ALICE).balance == 40
    assert service.capacity(ALICE).purchased_slots == 5


def test_operation_key_is_scoped_to_the_user():
    """남이 만든 id로 남의 기록을 건드릴 수 없다."""
    shared = "1a2b3c4d-0000-0000-0000-000000000000"
    assert operation_key(ALICE, shared) != operation_key(BOB, shared)


def test_pack_id_is_not_the_idempotency_key(service, shards):
    """**반복 구매가 가능해야 한다** — packId를 열쇠로 쓰면 두 번째를 못 산다."""
    fund(shards, ALICE, 20)
    service.purchase(ALICE, MIRROR_SLOT_PACK.id, op())
    second = service.purchase(ALICE, MIRROR_SLOT_PACK.id, op())
    assert second.applied is True
    assert second.capacity.purchased_slots == 10


def test_retry_of_a_rejected_purchase_can_succeed_later(service, shards):
    """거절은 기록을 남기지 않는다 — 조각이 생기면 같은 의도로 다시 살 수 있다."""
    intent = op()
    with pytest.raises(InsufficientShards):
        service.purchase(ALICE, MIRROR_SLOT_PACK.id, intent)

    fund(shards, ALICE, 10)
    result = service.purchase(ALICE, MIRROR_SLOT_PACK.id, intent)

    assert result.applied is True
    assert result.capacity.effective_slots == 10
    assert shards.wallet(ALICE).balance == 0


# MARK: - 원자성


def test_no_partial_state_when_shards_run_out(store, shards):
    """조각이 모자라 실패한 뒤에도 **칸도 기록도 남지 않는다.**"""
    service = MirrorCapacityService(store, shards)
    fund(shards, ALICE, 5)

    with pytest.raises(InsufficientShards):
        service.purchase(ALICE, MIRROR_SLOT_PACK.id, op())

    assert store._purchased == {}
    assert store._operations == {}


def test_reason_is_its_own_event():
    """원장만 보고 무엇에 썼는지 알 수 있어야 한다."""
    assert ShardReason.MIRROR_CAPACITY_PURCHASE.value == "mirror_capacity_purchase"
    # 기존 값을 재사용하지 않는다.
    assert ShardReason.MIRROR_CAPACITY_PURCHASE not in {
        ShardReason.MIRROR_PURCHASE, ShardReason.MIRROR_PUBLISH_FEE
    }


# MARK: - HTTP


@pytest.fixture
def client(store, shards, apple_key, jwks_of, monkeypatch) -> TestClient:
    from app.auth import jwks as jwks_module

    document = jwks_of(apple_key)
    monkeypatch.setattr(jwks_module, "http_jwks_fetch", lambda *a, **k: lambda: document)

    app = create_app(
        Settings(app_env="local", apple_client_id=CLIENT_ID),
        auth_store=InMemoryAuthStore(),
        shard_store=shards._store,
        capacity_store=store,
    )
    return TestClient(app, raise_server_exceptions=False)


def sign_in(client: TestClient, apple_key, subject: str = "001234.abcdef0123456789.1234") -> str:
    nonce = f"nonce-{subject}"
    token = apple_key.token(apple_claims(sub=subject, nonce=sha256_hex(nonce)))
    response = client.post("/auth/apple", json={"identityToken": token, "nonce": nonce})
    return response.json()["accessToken"]


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_capacity_needs_authentication(client):
    assert client.get("/users/me/mirror-capacity").status_code == 401
    assert client.post(
        "/users/me/mirror-capacity/purchases",
        json={"packId": MIRROR_SLOT_PACK.id, "operationId": op()},
    ).status_code == 401


def test_capacity_response_carries_the_pack(client, apple_key):
    token = sign_in(client, apple_key)
    body = client.get("/users/me/mirror-capacity", headers=auth(token)).json()

    assert body["baseSlots"] == 5
    assert body["purchasedSlots"] == 0
    assert body["effectiveSlots"] == 5
    # **client가 10과 5를 적어 두지 않아도 된다.**
    assert body["pack"] == {"id": "mirror_slots_5", "costShards": 10, "slotDelta": 5}


def test_purchase_endpoint_returns_authoritative_state(client, apple_key, shards):
    token = sign_in(client, apple_key)
    user_id = client.get("/users/me", headers=auth(token)).json()["id"]
    fund(shards, user_id, 20)

    response = client.post(
        "/users/me/mirror-capacity/purchases",
        json={"packId": MIRROR_SLOT_PACK.id, "operationId": op()},
        headers=auth(token),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    assert body["chargedShards"] == 10
    assert body["slotDelta"] == 5
    assert body["balance"] == 10
    assert body["effectiveSlots"] == 10
    assert body["purchasedSlots"] == 5


def test_purchase_endpoint_rejects_insufficient_shards(client, apple_key, shards):
    token = sign_in(client, apple_key)
    user_id = client.get("/users/me", headers=auth(token)).json()["id"]
    fund(shards, user_id, 9)

    response = client.post(
        "/users/me/mirror-capacity/purchases",
        json={"packId": MIRROR_SLOT_PACK.id, "operationId": op()},
        headers=auth(token),
    )

    assert response.status_code == 409
    assert shards.wallet(user_id).balance == 9
    assert client.get(
        "/users/me/mirror-capacity", headers=auth(token)
    ).json()["effectiveSlots"] == 5


def test_purchase_endpoint_rejects_unknown_pack(client, apple_key, shards):
    token = sign_in(client, apple_key)
    user_id = client.get("/users/me", headers=auth(token)).json()["id"]
    fund(shards, user_id, 100)

    response = client.post(
        "/users/me/mirror-capacity/purchases",
        json={"packId": "mirror_slots_500", "operationId": op()},
        headers=auth(token),
    )

    assert response.status_code == 404
    assert shards.wallet(user_id).balance == 100


def test_purchase_endpoint_requires_a_uuid_operation(client, apple_key, shards):
    token = sign_in(client, apple_key)
    user_id = client.get("/users/me", headers=auth(token)).json()["id"]
    fund(shards, user_id, 100)

    response = client.post(
        "/users/me/mirror-capacity/purchases",
        json={"packId": MIRROR_SLOT_PACK.id, "operationId": "not-a-uuid"},
        headers=auth(token),
    )

    assert response.status_code == 422
    assert shards.wallet(user_id).balance == 100


def test_purchase_endpoint_ignores_extra_economy_fields(client, apple_key, shards):
    """client가 숫자를 실어 보내도 서버 정책이 이긴다."""
    token = sign_in(client, apple_key)
    user_id = client.get("/users/me", headers=auth(token)).json()["id"]
    fund(shards, user_id, 20)

    body = client.post(
        "/users/me/mirror-capacity/purchases",
        json={
            "packId": MIRROR_SLOT_PACK.id,
            "operationId": op(),
            "costShards": 0,
            "slotDelta": 500,
            "balance": 9999,
        },
        headers=auth(token),
    ).json()

    assert body["chargedShards"] == 10
    assert body["slotDelta"] == 5
    assert body["effectiveSlots"] == 10
    assert shards.wallet(user_id).balance == 10


def test_endpoint_retry_with_same_operation_id(client, apple_key, shards):
    token = sign_in(client, apple_key)
    user_id = client.get("/users/me", headers=auth(token)).json()["id"]
    fund(shards, user_id, 20)
    intent = op()
    payload = {"packId": MIRROR_SLOT_PACK.id, "operationId": intent}

    first = client.post(
        "/users/me/mirror-capacity/purchases", json=payload, headers=auth(token)
    ).json()
    retry = client.post(
        "/users/me/mirror-capacity/purchases", json=payload, headers=auth(token)
    ).json()

    assert first["applied"] is True
    assert retry["applied"] is False
    assert retry["effectiveSlots"] == first["effectiveSlots"] == 10
    assert shards.wallet(user_id).balance == 10


def test_each_user_buys_their_own(client, apple_key, shards):
    alice = sign_in(client, apple_key, subject="001234.aaaa.1")
    bob = sign_in(client, apple_key, subject="001234.bbbb.2")
    alice_id = client.get("/users/me", headers=auth(alice)).json()["id"]
    bob_id = client.get("/users/me", headers=auth(bob)).json()["id"]
    fund(shards, alice_id, 20)
    fund(shards, bob_id, 20)

    client.post(
        "/users/me/mirror-capacity/purchases",
        json={"packId": MIRROR_SLOT_PACK.id, "operationId": op()},
        headers=auth(alice),
    )

    assert client.get(
        "/users/me/mirror-capacity", headers=auth(bob)
    ).json()["effectiveSlots"] == 5
    assert shards.wallet(bob_id).balance == 20


def test_no_generic_capacity_mutation_endpoint(client):
    """칸을 직접 쓰는 경로를 만들지 않았다."""
    for path in [
        "/users/me/mirror-capacity/slots",
        "/users/me/mirror-capacity/grant",
        "/mirror-capacity",
        "/users/me/mirror-capacity/set",
    ]:
        assert client.post(path, json={"slots": 100}).status_code in (401, 404, 405)
    # 읽기 endpoint에 쓰기가 없다.
    assert client.put("/users/me/mirror-capacity", json={"purchasedSlots": 99}).status_code in (401, 404, 405)


def test_logs_have_no_sensitive_values(client, apple_key, shards, caplog):
    import logging

    token = sign_in(client, apple_key)
    user_id = client.get("/users/me", headers=auth(token)).json()["id"]
    fund(shards, user_id, 20)

    with caplog.at_level(logging.INFO):
        client.post(
            "/users/me/mirror-capacity/purchases",
            json={"packId": MIRROR_SLOT_PACK.id, "operationId": op()},
            headers=auth(token),
        )

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert token not in text
    assert "Bearer" not in text
    assert "Authorization" not in text


# MARK: - Firestore ABORTED 재시도 (§32)
#
# Firestore는 commit이 `ABORTED`되면 **같은 Python Transaction 객체로** callable을
# 다시 부른다. 아래는 **실제 SDK wrapper**와 **실제 `FirestoreCapacityStore.purchase`**를
# 그대로 돌린다 — production Firestore를 부르지 않는다.


class FakeSnapshot:
    def __init__(self, data: dict | None) -> None:
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict:
        return dict(self._data or {})


class FakeRef:
    def __init__(self, db: "FakeDB", collection: str, document: str) -> None:
        self._db = db
        self.key = (collection, document)

    def get(self, transaction=None) -> FakeSnapshot:
        # transaction 읽기는 **commit된 상태**만 본다 — Firestore와 같은 규칙이다.
        return FakeSnapshot(self._db.data.get(self.key))


class FakeCollection:
    def __init__(self, db: "FakeDB", name: str) -> None:
        self._db = db
        self._name = name

    def document(self, document_id: str) -> FakeRef:
        return FakeRef(self._db, self._name, document_id)


class FakeDB:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], dict] = {}

    def collection(self, name: str) -> FakeCollection:
        return FakeCollection(self, name)


class AbortingCapacityTransaction:
    """`@firestore.transactional`이 부르는 것 + 우리 store가 부르는 것만 구현한다."""

    _max_attempts = 5
    _read_only = False

    def __init__(self, db: FakeDB, aborts: int) -> None:
        self._db = db
        self._aborts = aborts
        self.commits = 0
        self.staged: list = []
        self.attempts = 0

    def _clean_up(self) -> None:
        # SDK가 지우는 것은 이 둘뿐이다. 이전 시도의 staged write는 버린다.
        self._write_pbs = []
        self._id = None
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

    # --- 조각 저장소가 부르는 부분 ---
    def add(self, write) -> None:
        self.staged.append(write)

    # --- capacity 저장소가 부르는 부분 ---
    def set(self, ref: FakeRef, data: dict, merge: bool = False) -> None:
        def write() -> None:
            if merge and ref.key in self._db.data:
                self._db.data[ref.key].update(data)
            else:
                self._db.data[ref.key] = dict(data)

        self.staged.append(write)

    def create(self, ref: FakeRef, data: dict) -> None:
        def write() -> None:
            if ref.key in self._db.data:
                raise AssertionError("create on an existing document")
            self._db.data[ref.key] = dict(data)

        self.staged.append(write)


class FixedTransactionShards(ShardLedgerService):
    """`transaction()`이 우리가 준 가짜를 돌려준다. 나머지는 전부 실제 동작이다."""

    def __init__(self, store, transaction) -> None:
        super().__init__(store)
        self._fixed = transaction

    def transaction(self):
        return self._fixed


def firestore_purchase(aborts: int):
    from app.capacity.store import FirestoreCapacityStore

    db = FakeDB()
    shard_store = InMemoryShardStore()
    transaction = AbortingCapacityTransaction(db, aborts=aborts)
    shards = FixedTransactionShards(shard_store, transaction)
    fund(shards, ALICE, 20)
    return FirestoreCapacityStore(db), shards, db, transaction


@pytest.mark.parametrize("aborts", [1, 2, 4])
def test_firestore_retry_charges_and_grants_exactly_once(aborts):
    store, shards, db, transaction = firestore_purchase(aborts)

    result = store.purchase(shards, ALICE, MIRROR_SLOT_PACK, op())

    assert transaction.commits == 1
    assert result.applied is True
    # **-10과 +5가 시도마다 쌓이지 않는다.**
    assert shards.wallet(ALICE).balance == 10
    assert shards.wallet(ALICE).lifetime_spent == 10
    assert db.data[("ggumirror_users", ALICE)]["purchasedMirrorSlots"] == 5
    entries = [
        e for e in shards._store.entries
        if e.reason is ShardReason.MIRROR_CAPACITY_PURCHASE
    ]
    assert len(entries) == 1


def test_firestore_retry_does_not_refuse_the_second_attempt():
    """회귀: 표시를 transaction 객체에 붙이면 재시도가 `WalletAlreadyChanged`로 죽는다."""
    store, shards, _, _ = firestore_purchase(aborts=3)
    result = store.purchase(shards, ALICE, MIRROR_SLOT_PACK, op())
    assert result.capacity.effective_slots == 10


def test_firestore_purchase_keeps_other_user_fields():
    """user 문서의 다른 field를 덮지 않는다 — `merge`다."""
    store, shards, db, _ = firestore_purchase(aborts=0)
    db.data[("ggumirror_users", ALICE)] = {"createdAt": "yesterday"}

    store.purchase(shards, ALICE, MIRROR_SLOT_PACK, op())

    document = db.data[("ggumirror_users", ALICE)]
    assert document["createdAt"] == "yesterday"
    assert document["purchasedMirrorSlots"] == 5


def test_firestore_purchase_reads_before_writing():
    """Firestore transaction은 쓰기 뒤 읽기를 허용하지 않는다."""
    source = _capacity_source()
    body = source[source.index("def run(transaction)"):]
    first_write = min(
        body.index("transaction.set("), body.index("transaction.create(")
    )
    for read in ["operation_ref.get(transaction=transaction)", "user_ref.get(transaction=transaction)"]:
        assert body.index(read) < first_write, read


def test_firestore_purchase_makes_a_new_context_each_attempt():
    assert "scoped = shards.context(transaction)" in _capacity_source()


def _capacity_source() -> str:
    from pathlib import Path

    return Path("app/capacity/store.py").read_text(encoding="utf-8")
