"""계정 삭제.

가장 중요한 두 가지를 먼저 고정한다:

1. **산 사람의 권리는 판 사람이 떠나도 남는다** — snapshot · GCS · 구매자 소유권
2. **같은 Apple 결제로 조각을 두 번 받을 수 없다** — 계정을 지웠다 다시 만들어도

나머지(프로필 · 지갑 · 원장 · 좋아요 · 세션)는 지운다.
"""

from __future__ import annotations

import pytest

from app.auth.deletion import DELETED_OWNER, AccountDeletionService

# 인증 검사 하나만 실제 앱으로 본다. 나머지는 Firestore 없이 계약을 본다.
from tests.test_auth_api import client, store  # noqa: F401


# MARK: - 아주 작은 Firestore 흉내

class FakeDoc:
    def __init__(self, store, collection, doc_id):
        self._store = store
        self._collection = collection
        self.id = doc_id

    @property
    def reference(self):
        return self

    @property
    def exists(self):
        return self.id in self._store.data.get(self._collection, {})

    def to_dict(self):
        return dict(self._store.data.get(self._collection, {}).get(self.id, {}))

    def get(self, transaction=None):
        return self

    def update(self, changes):
        self._store.data[self._collection][self.id].update(changes)

    def delete(self):
        self._store.data[self._collection].pop(self.id, None)


class FakeQuery:
    def __init__(self, store, collection, field=None, value=None, limit=None):
        self._store, self._collection = store, collection
        self._field, self._value, self._limit = field, value, limit

    def where(self, field, _op, value):
        return FakeQuery(self._store, self._collection, field, value, self._limit)

    def limit(self, n):
        return FakeQuery(self._store, self._collection, self._field, self._value, n)

    def stream(self):
        items = self._store.data.get(self._collection, {})
        found = [
            FakeDoc(self._store, self._collection, key)
            for key, value in list(items.items())
            if self._field is None or value.get(self._field) == self._value
        ]
        return found[: self._limit] if self._limit else found


class FakeCollection(FakeQuery):
    def document(self, doc_id):
        return FakeDoc(self._store, self._collection, doc_id)


class FakeBatch:
    def __init__(self):
        self._deletes = []

    def delete(self, ref):
        self._deletes.append(ref)

    def commit(self):
        for ref in self._deletes:
            ref.delete()
        self._deletes = []


class FakeTransaction:
    def delete(self, ref):
        ref.delete()

    def update(self, ref, changes):
        ref.update(changes)


class FakeDB:
    def __init__(self, data):
        self.data = data

    def collection(self, name):
        self.data.setdefault(name, {})
        return FakeCollection(self, name)

    def batch(self):
        return FakeBatch()

    def transaction(self):
        return FakeTransaction()


def _transactional(fn):
    """`@firestore.transactional`을 대신한다 — 그냥 부른다."""
    def wrapper(transaction):
        return fn(transaction)
    return wrapper


@pytest.fixture(autouse=True)
def patch_transactional(monkeypatch):
    from google.cloud import firestore

    monkeypatch.setattr(firestore, "transactional", _transactional)


COLLECTIONS = {
    "users": "users", "identities": "identities", "sessions": "sessions",
    "wallets": "wallets", "ledger": "ledger", "quotas": "quotas",
    "ownership": "ownership", "likes": "likes", "listings": "listings",
    "iap_transactions": "iap", "iap_refunds": "refunds",
    "capacity_operations": "capacity", "acquisitions": "acquisitions",
}

LEAVER = "leaver-1"
BUYER = "buyer-1"


def world():
    """떠나는 사람(LEAVER)이 판 상품을 BUYER가 이미 샀다."""
    return {
        "users": {LEAVER: {"displayName": "떠나는사람"}, BUYER: {"displayName": "산사람"}},
        "identities": {"hash-leaver": {"userId": LEAVER}, "hash-buyer": {"userId": BUYER}},
        "sessions": {"s1": {"userId": LEAVER}, "s2": {"userId": BUYER}},
        "wallets": {LEAVER: {"balance": 42}, BUYER: {"balance": 7}},
        "ledger": {
            "l1": {"userId": LEAVER, "reason": "mirror_sale", "delta": 3},
            "l2": {"userId": BUYER, "reason": "mirror_purchase", "delta": -3},
        },
        "quotas": {"q1": {"userId": LEAVER}},
        "ownership": {
            # 산 사람의 소유권 — **지우면 안 된다.**
            "own-buyer": {"userId": BUYER, "listingId": "sold"},
            # 떠나는 사람이 남에게서 산 것 — 개인 데이터라 지운다.
            "own-leaver": {"userId": LEAVER, "listingId": "other"},
        },
        "likes": {"like-1": {"userId": LEAVER, "listingId": "other"}},
        "listings": {
            "sold": {"sellerUserId": LEAVER, "status": "published", "likeCount": 0,
                     "snapshotId": "snap-1"},
            "draft": {"sellerUserId": LEAVER, "status": "draft", "likeCount": 0},
            "other": {"sellerUserId": BUYER, "status": "published", "likeCount": 1,
                      "snapshotId": "snap-2"},
        },
        "iap": {"claim-1": {"userId": LEAVER, "productId": "shards.10", "amount": 10}},
        "refunds": {"r1": {"userId": LEAVER}},
        "capacity": {"op-1": {"userId": LEAVER, "purchasedSlotsAfter": 5}},
        "acquisitions": {"a1": {"userId": LEAVER}},
    }


def run_deletion(data, user_id=LEAVER):
    service = AccountDeletionService(FakeDB(data), COLLECTIONS)
    return service.delete(user_id)


# MARK: - 구매자 보존 (가장 중요)


def test_buyer_keeps_ownership_and_assets():
    data = world()
    run_deletion(data)
    # 판 사람이 떠나도 **산 사람의 권리는 그대로다.**
    assert "own-buyer" in data["ownership"]
    # snapshot 참조가 살아 있어야 템플릿을 계속 받을 수 있다.
    assert data["listings"]["sold"]["snapshotId"] == "snap-1"


def test_sold_listing_leaves_the_store_but_is_not_erased():
    data = world()
    run_deletion(data)
    listing = data["listings"]["sold"]
    # 상점에서는 내려간다.
    assert listing["status"] == "deleted"
    # 그러나 문서 자체는 남는다 — 구매자가 계속 받아야 한다.
    assert "sold" in data["listings"]
    assert listing["deletionReason"] == "account_deleted"


def test_seller_identity_is_removed_from_the_listing():
    data = world()
    run_deletion(data)
    # 판매자 표시가 사라진다. 내부 id가 남아 있으면 안 된다.
    assert data["listings"]["sold"]["sellerUserId"] == DELETED_OWNER
    assert LEAVER not in str(data["listings"]["sold"])


def test_draft_is_retired_too():
    data = world()
    run_deletion(data)
    assert data["listings"]["draft"]["status"] == "deleted"


def test_other_sellers_listing_is_untouched():
    data = world()
    before = dict(data["listings"]["other"])
    run_deletion(data)
    assert data["listings"]["other"]["sellerUserId"] == before["sellerUserId"]
    assert data["listings"]["other"]["status"] == "published"


# MARK: - 결제 재사용 방지 (두 번째로 중요)


def test_iap_claim_survives_so_the_payment_cannot_be_reused():
    data = world()
    run_deletion(data)
    # **지우면 같은 결제를 다시 내서 조각을 또 받을 수 있다.** 그래서 남긴다.
    assert "claim-1" in data["iap"]


def test_iap_claim_owner_is_anonymized():
    data = world()
    run_deletion(data)
    claim = data["iap"]["claim-1"]
    # 개인과의 연결은 끊는다.
    assert claim["userId"] == DELETED_OWNER
    # 재사용을 막는 데 필요한 값은 남는다.
    assert claim["productId"] == "shards.10"


def test_recreated_account_cannot_claim_the_old_payment():
    data = world()
    run_deletion(data)
    # 다시 로그인하면 **새 계정**이 된다(identity 연결을 지웠으므로).
    recreated = "leaver-2"
    owner = data["iap"]["claim-1"]["userId"]
    # 그 새 계정은 이 claim의 주인이 아니다 → 재제출이 거절된다.
    assert owner != recreated
    assert owner == DELETED_OWNER


def test_capacity_operations_are_anonymized_not_deleted():
    data = world()
    run_deletion(data)
    # 보관 칸 구매도 멱등 기록이다 — 지우면 같은 요청이 다시 통할 수 있다.
    assert data["capacity"]["op-1"]["userId"] == DELETED_OWNER


def test_recreated_account_starts_without_capacity():
    data = world()
    run_deletion(data)
    # 산 칸은 user 문서에 있었고 그 문서를 지웠다 — 새 계정은 기본값에서 시작한다.
    assert LEAVER not in data["users"]


# MARK: - 개인 데이터 삭제


@pytest.mark.parametrize(
    "collection,key",
    [("users", LEAVER), ("wallets", LEAVER), ("sessions", "s1"), ("ledger", "l1"),
     ("quotas", "q1"), ("ownership", "own-leaver"), ("likes", "like-1"),
     ("acquisitions", "a1"), ("identities", "hash-leaver")],
)
def test_personal_documents_are_deleted(collection, key):
    data = world()
    run_deletion(data)
    assert key not in data[collection]


def test_remaining_balance_is_gone_and_not_transferred():
    data = world()
    run_deletion(data)
    assert LEAVER not in data["wallets"]
    # 남의 지갑으로 옮겨 가지 않는다.
    assert data["wallets"][BUYER]["balance"] == 7


def test_other_account_is_untouched():
    data = world()
    run_deletion(data)
    assert BUYER in data["users"]
    assert BUYER in data["wallets"]
    assert "s2" in data["sessions"]
    assert "l2" in data["ledger"]
    assert "hash-buyer" in data["identities"]


def test_other_users_ledger_history_survives():
    data = world()
    run_deletion(data)
    # 남의 판매/구매 기록을 깨뜨리지 않는다.
    assert data["ledger"]["l2"]["reason"] == "mirror_purchase"


# MARK: - 좋아요 정합성


def test_like_count_is_decremented_exactly():
    data = world()
    assert data["listings"]["other"]["likeCount"] == 1
    run_deletion(data)
    # 문서만 지우면 숫자가 영영 부풀어 있는다.
    assert data["listings"]["other"]["likeCount"] == 0


def test_like_count_never_goes_negative():
    data = world()
    data["listings"]["other"]["likeCount"] = 0  # 이미 어긋나 있었다면
    run_deletion(data)
    assert data["listings"]["other"]["likeCount"] == 0


# MARK: - 멱등


def test_running_twice_is_safe():
    data = world()
    first = run_deletion(data)
    second = run_deletion(data)
    # 두 번째는 할 일이 없다.
    assert second.documents_deleted == 0
    assert second.listings_hidden == 0
    assert second.likes_removed == 0
    # 첫 번째 결과는 그대로 유지된다.
    assert data["listings"]["sold"]["status"] == "deleted"
    assert data["iap"]["claim-1"]["userId"] == DELETED_OWNER
    assert first.documents_deleted > 0


def test_partial_failure_can_be_retried():
    data = world()
    # 상점 처리까지만 끝난 상태를 흉내 낸다.
    data["listings"]["sold"].update({"status": "deleted", "sellerUserId": DELETED_OWNER})
    run_deletion(data)
    # 나머지가 마저 정리된다.
    assert LEAVER not in data["users"]
    assert "like-1" not in data["likes"]


# MARK: - API


def test_delete_requires_authentication(client):
    assert client.delete("/users/me/account").status_code == 401
