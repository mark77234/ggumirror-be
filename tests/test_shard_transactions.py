"""호출자가 소유하는 transaction 안에서의 조각 이동 (B-7B).

Marketplace가 이것을 필요로 하는 이유는 하나다 — **구매는 지갑 두 개와 원장 두 줄과
소유권을 한 transaction에서 커밋해야 한다.** `credit`/`debit`은 각자 transaction을 열어
버려서 "구매자만 차감된" 중간 상태를 만든다.

여기서 시험하는 것:
1. 두 지갑이 **한 transaction**에서 움직이는가
2. 중간에 실패하면 **아무것도 남지 않는가**
3. 잔액은 여전히 음수가 되지 않는가
4. 같은 지갑을 두 번 바꾸려는 시도를 막는가 (자기 자신에게 파는 경우)
"""

from __future__ import annotations

import threading

import pytest

from app.shards.models import (
    InsufficientShards,
    InvalidShardAmount,
    ShardReason,
    WalletAlreadyChanged,
    idempotency_hash,
)
from app.shards.service import ShardLedgerService
from app.shards.store import InMemoryShardStore

BUYER = "11111111-2222-4333-8444-555555555555"
SELLER = "99999999-8888-4777-8666-555555555555"
LISTING = "listing-abc"


@pytest.fixture
def store() -> InMemoryShardStore:
    return InMemoryShardStore()


@pytest.fixture
def shards(store) -> ShardLedgerService:
    return ShardLedgerService(store)


def seed(shards: ShardLedgerService, user: str, amount: int) -> None:
    shards.credit(user, amount, ShardReason.ADMIN_ADJUSTMENT, external_event_id=f"seed:{user}")


def purchase(shards: ShardLedgerService, tx, price: int, listing: str = LISTING) -> None:
    """marketplace 구매가 하게 될 그대로 — 한 transaction에 두 번 얹는다."""
    shards.apply_in_transaction(tx, BUYER, -price, ShardReason.MIRROR_PURCHASE, listing)
    shards.apply_in_transaction(tx, SELLER, price, ShardReason.MIRROR_SALE, listing)


# MARK: - 두 지갑, 한 transaction (§7)


def test_two_wallets_move_in_one_transaction(store, shards):
    seed(shards, BUYER, 100)
    seed(shards, SELLER, 10)

    with store.transaction() as tx:
        purchase(shards, tx, 30)
        # **commit 전에는 아무것도 반영되지 않는다.**
        assert store.wallet(BUYER).balance == 100
        assert store.wallet(SELLER).balance == 10
        committed_before = tx.commits

    buyer, seller = store.wallet(BUYER), store.wallet(SELLER)
    assert (buyer.balance, seller.balance) == (70, 40)
    assert buyer.lifetime_spent == 30
    assert seller.lifetime_earned == 10 + 30
    # 판매는 "쓴 것"이 아니다.
    assert seller.lifetime_spent == 0
    assert buyer.lifetime_earned == 100

    moves = [e for e in store.entries if e.reason in
             (ShardReason.MIRROR_PURCHASE, ShardReason.MIRROR_SALE)]
    assert len(moves) == 2
    assert sorted(e.delta for e in moves) == [-30, 30]
    assert committed_before == 0
    assert tx.commits == 1, "commit이 정확히 한 번이어야 한다"


def test_publish_fee_moves_one_wallet_in_a_caller_transaction(store, shards):
    """B-7C가 쓸 모양 — listing 문서와 같은 transaction에 수수료를 얹는다."""
    seed(shards, SELLER, 25)

    with store.transaction() as tx:
        shards.apply_in_transaction(
            tx, SELLER, -20, ShardReason.MIRROR_PUBLISH_FEE, LISTING
        )

    wallet = store.wallet(SELLER)
    assert wallet.balance == 5
    assert wallet.lifetime_spent == 20
    assert [e.delta for e in store.entries if e.reason is ShardReason.MIRROR_PUBLISH_FEE] == [-20]


def test_ledger_replay_matches_balance(store, shards):
    seed(shards, BUYER, 100)
    seed(shards, SELLER, 10)
    with store.transaction() as tx:
        purchase(shards, tx, 30)

    for user in (BUYER, SELLER):
        replayed = sum(e.delta for e in store.entries if e.user_id == user)
        assert store.wallet(user).balance == replayed


# MARK: - 실패 원자성 (§8)


def test_insufficient_buyer_leaves_both_wallets_untouched(store, shards):
    """A. 구매자 잔액 부족 — 판매자도 움직이지 않는다."""
    seed(shards, BUYER, 10)
    seed(shards, SELLER, 10)

    with pytest.raises(InsufficientShards):
        with store.transaction() as tx:
            purchase(shards, tx, 30)

    assert store.wallet(BUYER).balance == 10
    assert store.wallet(SELLER).balance == 10
    assert [e for e in store.entries if e.reason is ShardReason.MIRROR_PURCHASE] == []
    assert [e for e in store.entries if e.reason is ShardReason.MIRROR_SALE] == []


def test_failure_after_buyer_debit_rolls_back(store, shards):
    """B. 구매자 차감을 얹은 **뒤** 실패해도 구매자가 원상복귀한다."""
    seed(shards, BUYER, 100)
    seed(shards, SELLER, 10)

    with pytest.raises(RuntimeError):
        with store.transaction() as tx:
            shards.apply_in_transaction(tx, BUYER, -30, ShardReason.MIRROR_PURCHASE, LISTING)
            # ownership 생성 실패 같은 상황
            raise RuntimeError("ownership write failed")

    assert store.wallet(BUYER).balance == 100, "구매자만 빠진 상태가 남았다"
    assert store.entries and all(e.reason is ShardReason.ADMIN_ADJUSTMENT for e in store.entries)


def test_failure_on_seller_leg_rolls_back_the_buyer(store, shards):
    """C. 판매자 쪽에서 터져도 구매자만 빠지는 상태가 없다."""
    seed(shards, BUYER, 100)

    with pytest.raises(WalletAlreadyChanged):
        with store.transaction() as tx:
            shards.apply_in_transaction(tx, BUYER, -30, ShardReason.MIRROR_PURCHASE, LISTING)
            # 자기 자신에게 파는 상황 — 판매자 leg이 거절된다
            shards.apply_in_transaction(tx, BUYER, 30, ShardReason.MIRROR_SALE, LISTING)

    assert store.wallet(BUYER).balance == 100
    assert [e for e in store.entries if e.reason is ShardReason.MIRROR_PURCHASE] == []


# MARK: - 잔액 안전 (§5)


@pytest.mark.parametrize("price", [11, 30, 1000])
def test_balance_never_goes_negative(store, shards, price):
    seed(shards, BUYER, 10)

    with pytest.raises(InsufficientShards):
        with store.transaction() as tx:
            shards.apply_in_transaction(tx, BUYER, -price, ShardReason.MIRROR_PURCHASE, LISTING)

    assert store.wallet(BUYER).balance == 10


def test_exact_balance_is_allowed(store, shards):
    seed(shards, BUYER, 30)
    with store.transaction() as tx:
        shards.apply_in_transaction(tx, BUYER, -30, ShardReason.MIRROR_PURCHASE, LISTING)
    assert store.wallet(BUYER).balance == 0


@pytest.mark.parametrize("delta", [0, True, 100_001, -100_001])
def test_invalid_delta_is_rejected(store, shards, delta):
    seed(shards, BUYER, 100)
    with pytest.raises(InvalidShardAmount):
        with store.transaction() as tx:
            shards.apply_in_transaction(tx, BUYER, delta, ShardReason.MIRROR_PURCHASE, LISTING)
    assert store.wallet(BUYER).balance == 100


# MARK: - 같은 지갑 두 번 (§10)


def test_same_wallet_twice_in_one_transaction_is_refused(store, shards):
    """자기 자신에게 파는 경우가 정확히 이 모양이다.

    Firestore transaction의 읽기는 시작 시점 snapshot이라, 두 번째 호출은 첫 번째가
    계산한 잔액을 보지 못하고 **덮어쓴다** — 조각이 조용히 사라진다.
    """
    seed(shards, BUYER, 100)

    with pytest.raises(WalletAlreadyChanged):
        with store.transaction() as tx:
            shards.apply_in_transaction(tx, BUYER, -30, ShardReason.MIRROR_PURCHASE, LISTING)
            shards.apply_in_transaction(tx, BUYER, 30, ShardReason.MIRROR_SALE, LISTING)

    assert store.wallet(BUYER).balance == 100


def test_different_wallets_are_fine(store, shards):
    seed(shards, BUYER, 100)
    with store.transaction() as tx:
        purchase(shards, tx, 30)
    assert (store.wallet(BUYER).balance, store.wallet(SELLER).balance) == (70, 30)


# MARK: - 멱등 (§6)


def test_ledger_ids_are_deterministic_and_distinct():
    buyer_key = idempotency_hash(BUYER, ShardReason.MIRROR_PURCHASE, LISTING)
    seller_key = idempotency_hash(SELLER, ShardReason.MIRROR_SALE, LISTING)
    fee_key = idempotency_hash(SELLER, ShardReason.MIRROR_PUBLISH_FEE, LISTING)

    assert len({buyer_key, seller_key, fee_key}) == 3, "세 사건이 같은 문서를 겨룬다"
    # 같은 입력이면 같은 열쇠다 — 재시도가 두 번 적지 않는다.
    assert buyer_key == idempotency_hash(BUYER, ShardReason.MIRROR_PURCHASE, LISTING)
    # raw 값이 문서 ID에 노출되지 않는다.
    for key in (buyer_key, seller_key, fee_key):
        assert BUYER not in key and SELLER not in key and LISTING not in key


@pytest.mark.parametrize("times", [2, 5])
def test_repeating_the_same_purchase_moves_shards_once(store, shards, times):
    seed(shards, BUYER, 100)
    seed(shards, SELLER, 10)

    for _ in range(times):
        with store.transaction() as tx:
            purchase(shards, tx, 30)

    assert (store.wallet(BUYER).balance, store.wallet(SELLER).balance) == (70, 40)
    assert len([e for e in store.entries if e.reason is ShardReason.MIRROR_PURCHASE]) == 1
    assert len([e for e in store.entries if e.reason is ShardReason.MIRROR_SALE]) == 1


def test_duplicate_reports_applied_false_without_writing(store, shards):
    seed(shards, BUYER, 100)
    with store.transaction() as tx:
        first = shards.apply_in_transaction(tx, BUYER, -30, ShardReason.MIRROR_PURCHASE, LISTING)
    with store.transaction() as tx:
        second = shards.apply_in_transaction(tx, BUYER, -30, ShardReason.MIRROR_PURCHASE, LISTING)

    assert (first.applied, second.applied) == (True, False)
    assert second.wallet.balance == 70, "중복도 정상 잔액을 돌려준다"


def test_different_listings_are_different_events(store, shards):
    seed(shards, BUYER, 100)
    for listing in ("listing-a", "listing-b"):
        with store.transaction() as tx:
            shards.apply_in_transaction(
                tx, BUYER, -30, ShardReason.MIRROR_PURCHASE, listing
            )
    assert store.wallet(BUYER).balance == 40


# MARK: - 동시성 (§9)


def test_concurrent_debits_cannot_overdraw(store, shards):
    """잔액 30에 서로 다른 두 사건이 각각 30을 빼려 한다. **정확히 하나만** 성공한다."""
    seed(shards, BUYER, 30)
    start = threading.Barrier(8)
    outcome: list = []

    def run(index: int):
        start.wait()
        try:
            with store.transaction() as tx:
                result = shards.apply_in_transaction(
                    tx, BUYER, -30, ShardReason.MIRROR_PURCHASE, f"listing-{index % 2}"
                )
            # **`applied`가 authority다.** 예외가 없다고 뺀 것이 아니다 —
            # 같은 열쇠의 재전송은 조용히 `False`로 끝난다(정상).
            outcome.append(result.applied)
        except (InsufficientShards, WalletAlreadyChanged):
            outcome.append(False)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert store.wallet(BUYER).balance == 0, "잔액이 음수가 됐다"
    assert sum(outcome) == 1, f"실제 차감이 {sum(outcome)}건 — 정확히 1이어야 한다"
    assert len([e for e in store.entries if e.reason is ShardReason.MIRROR_PURCHASE]) == 1


def test_concurrent_purchases_of_the_same_listing_apply_once(store, shards):
    seed(shards, BUYER, 100)
    start = threading.Barrier(8)

    def run():
        start.wait()
        with store.transaction() as tx:
            purchase(shards, tx, 30)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert (store.wallet(BUYER).balance, store.wallet(SELLER).balance) == (70, 30)
    assert len([e for e in store.entries if e.reason is ShardReason.MIRROR_SALE]) == 1


# MARK: - 소스 불변식 (§2)


def _code_only(source: str) -> str:
    import io
    import tokenize

    return "".join(
        t.string
        for t in tokenize.generate_tokens(io.StringIO(source).readline)
        if t.type not in (tokenize.COMMENT, tokenize.STRING)
    )


def test_no_generic_transfer_primitive():
    """범용 이체는 제품에 없다. 보내는 사람·받는 사람을 한 번에 받는 함수를 만들지 않는다."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for path in ["app/shards/service.py", "app/shards/store.py", "app/shards/firestore_store.py"]:
        code = _code_only((root / path).read_text())
        for banned in ["def transfer", "from_user", "to_user", "sender", "recipient"]:
            assert banned not in code, f"{path}: 범용 이체가 생겼다 ({banned})"


def test_no_generic_transfer_endpoint(client_app=None):
    from fastapi.testclient import TestClient

    from app.core.config import Settings
    from app.main import create_app

    client = TestClient(
        create_app(Settings(app_env="local"), shard_store=InMemoryShardStore()),
        raise_server_exceptions=False,
    )
    for path in ["/shards/transfer", "/users/me/shards/transfer", "/shards", "/marketplace/purchase"]:
        assert client.post(path, json={"amount": 10}).status_code in {401, 404, 405}
