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
    """marketplace 구매가 하게 될 그대로 — 한 transaction에 두 번 얹는다.

    `context`는 **attempt마다** 만든다 — 재시도가 이전 시도의 기록을 물려받지 않는다.
    """
    scoped = shards.context(tx)
    shards.apply_in_transaction(scoped, BUYER, -price, ShardReason.MIRROR_PURCHASE, listing)
    shards.apply_in_transaction(scoped, SELLER, price, ShardReason.MIRROR_SALE, listing)


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
                shards.context(tx), SELLER, -20, ShardReason.MIRROR_PUBLISH_FEE, LISTING
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
            shards.apply_in_transaction(
                shards.context(tx), BUYER, -30, ShardReason.MIRROR_PURCHASE, LISTING)
            # ownership 생성 실패 같은 상황
            raise RuntimeError("ownership write failed")

    assert store.wallet(BUYER).balance == 100, "구매자만 빠진 상태가 남았다"
    assert store.entries and all(e.reason is ShardReason.ADMIN_ADJUSTMENT for e in store.entries)


def test_failure_on_seller_leg_rolls_back_the_buyer(store, shards):
    """C. 판매자 쪽에서 터져도 구매자만 빠지는 상태가 없다."""
    seed(shards, BUYER, 100)

    with pytest.raises(WalletAlreadyChanged):
        with store.transaction() as tx:
            # **같은 attempt**이므로 context 하나를 공유한다 — 실제 호출부와 같은 모양이다.
            scoped = shards.context(tx)
            shards.apply_in_transaction(scoped, BUYER, -30, ShardReason.MIRROR_PURCHASE, LISTING)
            # 자기 자신에게 파는 상황 — 판매자 leg이 거절된다
            shards.apply_in_transaction(scoped, BUYER, 30, ShardReason.MIRROR_SALE, LISTING)

    assert store.wallet(BUYER).balance == 100
    assert [e for e in store.entries if e.reason is ShardReason.MIRROR_PURCHASE] == []


# MARK: - 잔액 안전 (§5)


@pytest.mark.parametrize("price", [11, 30, 1000])
def test_balance_never_goes_negative(store, shards, price):
    seed(shards, BUYER, 10)

    with pytest.raises(InsufficientShards):
        with store.transaction() as tx:
            shards.apply_in_transaction(
                shards.context(tx), BUYER, -price, ShardReason.MIRROR_PURCHASE, LISTING)

    assert store.wallet(BUYER).balance == 10


def test_exact_balance_is_allowed(store, shards):
    seed(shards, BUYER, 30)
    with store.transaction() as tx:
        shards.apply_in_transaction(
                shards.context(tx), BUYER, -30, ShardReason.MIRROR_PURCHASE, LISTING)
    assert store.wallet(BUYER).balance == 0


@pytest.mark.parametrize("delta", [0, True, 100_001, -100_001])
def test_invalid_delta_is_rejected(store, shards, delta):
    seed(shards, BUYER, 100)
    with pytest.raises(InvalidShardAmount):
        with store.transaction() as tx:
            shards.apply_in_transaction(
                shards.context(tx), BUYER, delta, ShardReason.MIRROR_PURCHASE, LISTING)
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
            scoped = shards.context(tx)
            shards.apply_in_transaction(scoped, BUYER, -30, ShardReason.MIRROR_PURCHASE, LISTING)
            shards.apply_in_transaction(scoped, BUYER, 30, ShardReason.MIRROR_SALE, LISTING)

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
        first = shards.apply_in_transaction(
                shards.context(tx), BUYER, -30, ShardReason.MIRROR_PURCHASE, LISTING)
    with store.transaction() as tx:
        second = shards.apply_in_transaction(
                shards.context(tx), BUYER, -30, ShardReason.MIRROR_PURCHASE, LISTING)

    assert (first.applied, second.applied) == (True, False)
    assert second.wallet.balance == 70, "중복도 정상 잔액을 돌려준다"


def test_different_listings_are_different_events(store, shards):
    seed(shards, BUYER, 100)
    for listing in ("listing-a", "listing-b"):
        with store.transaction() as tx:
            shards.apply_in_transaction(
                shards.context(tx), BUYER, -30, ShardReason.MIRROR_PURCHASE, listing
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
                shards.context(tx), BUYER, -30, ShardReason.MIRROR_PURCHASE, f"listing-{index % 2}"
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


# MARK: - Firestore 자동 재시도 (B-7B.1)
#
# Firestore는 commit이 `ABORTED`되면 **같은 Python Transaction 객체로** callable을
# 다시 부른다(설치본 2.22.0 `_Transactional.__call__`). `_clean_up()`이 지우는 것은
# `_write_pbs`와 `_id`뿐이라, transaction 객체에 붙인 표시는 다음 시도까지 살아남는다.
#
# 그래서 표시를 transaction이 아니라 **attempt마다 새로 만드는 context**에 담는다.
# 아래 test들은 **실제 SDK wrapper**로 그 동작을 고정한다 — production Firestore를 부르지 않는다.


class AbortingTransaction:
    """실제 `@firestore.transactional`이 부르는 것만 구현한 가짜 transaction.

    `_commit()`이 정해진 횟수만큼 `Aborted`를 던져 **진짜 재시도 loop**를 돌린다.
    """

    _max_attempts = 5
    _read_only = False

    def __init__(self, store: InMemoryShardStore, aborts: int) -> None:
        self._store = store
        self._aborts = aborts
        self.commits = 0
        self.rollbacks = 0
        self.staged: list = []

    # --- SDK가 부르는 부분 ---

    def _clean_up(self) -> None:
        # SDK가 지우는 것은 이 둘뿐이다. **우리 표시는 건드리지 않는다.**
        self._write_pbs = []
        self._id = None
        # 이전 시도의 staged write는 버린다(rollback과 같은 뜻).
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
        self.rollbacks += 1
        self.staged = []

    # --- 조각 저장소가 부르는 부분 ---

    def add(self, write) -> None:
        self.staged.append(write)


def transactional_purchase(shards: ShardLedgerService, price: int = 30):
    """실제 SDK wrapper로 감싼 구매 한 건. marketplace가 쓰게 될 모양 그대로다."""
    from google.cloud import firestore

    attempts: list[int] = []

    @firestore.transactional
    def run(transaction):
        attempts.append(len(attempts) + 1)
        # **attempt마다 새 context** — 이 한 줄이 재시도 안전성의 전부다.
        scoped = shards.context(transaction)
        shards.apply_in_transaction(scoped, BUYER, -price, ShardReason.MIRROR_PURCHASE, LISTING)
        shards.apply_in_transaction(scoped, SELLER, price, ShardReason.MIRROR_SALE, LISTING)
        return "done"

    return run, attempts


def test_retry_after_abort_reapplies_cleanly(store, shards):
    """§8 — ABORTED 후 재시도가 **정확히 한 번** 반영된다."""
    seed(shards, BUYER, 100)
    seed(shards, SELLER, 10)
    run, attempts = transactional_purchase(shards)

    result = run(AbortingTransaction(store, aborts=1))

    assert result == "done"
    assert attempts == [1, 2], "재시도가 일어나지 않았다"

    buyer, seller = store.wallet(BUYER), store.wallet(SELLER)
    assert (buyer.balance, seller.balance) == (70, 40), "두 번 반영됐다"
    assert buyer.lifetime_spent == 30
    assert seller.lifetime_earned == 40
    assert len([e for e in store.entries if e.reason is ShardReason.MIRROR_PURCHASE]) == 1
    assert len([e for e in store.entries if e.reason is ShardReason.MIRROR_SALE]) == 1


@pytest.mark.parametrize("aborts", [1, 2, 4])
def test_retry_never_raises_wallet_already_changed(store, shards, aborts):
    """회귀: 표시를 transaction 객체에 붙였을 때 **재시도가 잘못 거절됐다.**"""
    seed(shards, BUYER, 100)
    seed(shards, SELLER, 10)
    run, attempts = transactional_purchase(shards)

    run(AbortingTransaction(store, aborts=aborts))   # WalletAlreadyChanged가 나오면 실패

    assert len(attempts) == aborts + 1
    assert store.wallet(BUYER).balance == 70


def test_same_wallet_guard_survives_retry(store, shards):
    """§9 — 재시도가 있다고 같은 attempt 안의 guard를 없애지 않는다."""
    from google.cloud import firestore

    seed(shards, BUYER, 100)
    attempts: list[int] = []

    @firestore.transactional
    def run(transaction):
        attempts.append(len(attempts) + 1)
        scoped = shards.context(transaction)
        shards.apply_in_transaction(scoped, BUYER, -30, ShardReason.MIRROR_PURCHASE, LISTING)
        # 같은 attempt · 같은 지갑 — 여전히 거절된다.
        shards.apply_in_transaction(scoped, BUYER, 30, ShardReason.MIRROR_SALE, LISTING)

    with pytest.raises(WalletAlreadyChanged):
        run(AbortingTransaction(store, aborts=1))

    assert store.wallet(BUYER).balance == 100
    assert attempts == [1], "도메인 거절은 재시도 대상이 아니다"


def test_retry_reads_the_current_balance(store, shards):
    """§10 — 재시도는 **다시 읽는다.** 이전 시도가 본 잔액을 쓰지 않는다."""
    from google.api_core import exceptions
    from google.cloud import firestore

    seed(shards, BUYER, 30)
    attempts: list[int] = []

    class DrainingTransaction(AbortingTransaction):
        def _commit(self):
            if self._aborts > 0:
                self._aborts -= 1
                # 첫 시도가 깨지는 동안 **다른 요청이 잔액을 다 써버렸다.**
                self._store.wallets[BUYER] = self._store.wallet(BUYER).__class__(
                    user_id=BUYER,
                    balance=0,
                    lifetime_earned=30,
                    lifetime_spent=30,
                )
                raise exceptions.Aborted("simulated contention")
            return super()._commit()

    @firestore.transactional
    def run(transaction):
        attempts.append(len(attempts) + 1)
        scoped = shards.context(transaction)
        shards.apply_in_transaction(scoped, BUYER, -30, ShardReason.MIRROR_PURCHASE, LISTING)

    with pytest.raises(InsufficientShards):
        run(DrainingTransaction(store, aborts=1))

    # stale한 30을 그대로 써서 -30을 만들지 않았다.
    assert store.wallet(BUYER).balance == 0
    assert attempts == [1, 2], "재시도가 없었다"


def test_retry_keeps_idempotency(store, shards):
    """§11 — 재시도가 원장을 두 줄 만들지 않는다. 같은 요청 재호출도 여전히 한 번이다."""
    seed(shards, BUYER, 100)
    seed(shards, SELLER, 10)

    run, _ = transactional_purchase(shards)
    run(AbortingTransaction(store, aborts=2))

    # 같은 business 요청을 다시 보낸다(네트워크 재시도).
    run2, _ = transactional_purchase(shards)
    run2(AbortingTransaction(store, aborts=1))

    assert (store.wallet(BUYER).balance, store.wallet(SELLER).balance) == (70, 40)
    assert len([e for e in store.entries if e.reason is ShardReason.MIRROR_PURCHASE]) == 1
    assert len([e for e in store.entries if e.reason is ShardReason.MIRROR_SALE]) == 1


def test_context_state_never_lives_on_the_transaction(store, shards):
    """표시를 transaction 객체에 붙이면 재시도 사이에 살아남는다 — 구조로 막는다."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for path in ["app/shards/firestore_store.py", "app/shards/store.py"]:
        code = _code_only((root / path).read_text())
        for banned in ["transaction._ggumirror", "setattr(transaction", "getattr(transaction"]:
            assert banned not in code, f"{path}: transaction 객체에 상태를 붙인다 ({banned})"

    # context는 attempt마다 새로 만들어져야 한다 — 두 번 부르면 서로 다른 객체다.
    tx = object()
    assert shards.context(tx) is not shards.context(tx)
    assert shards.context(tx).changed_wallets == set()


# MARK: - 쓰기 뒤 읽기 (production 500의 원인)
#
# Firestore transaction은 **쓰기가 하나라도 나가면 그 뒤 읽기를 거절한다**
# (`ReadAfterWriteError`). 지갑 두 개가 움직이는 구매에서 구매자 차감이 곧바로 쓰면
# 판매자 지급의 읽기가 죽는다 — production에서 실제로 500이 났고, 그때까지 테스트는
# 전부 통과했다. in-memory fake가 쓰기를 처음부터 미뤄 둬서 이 규칙을 흉내 내지
# 않았기 때문이다. 아래 fake는 **실제 규칙을 강제한다.**


class ReadAfterWrite(Exception):
    """Firestore가 이 상황에서 던지는 것과 같은 뜻."""


class StrictSnapshot:
    def __init__(self, data): self._data = data
    @property
    def exists(self): return self._data is not None
    def to_dict(self): return dict(self._data or {})


class StrictRef:
    def __init__(self, db, key):
        self._db, self.key = db, key
        self.id = key[1]

    def get(self, transaction=None):
        if transaction is not None and transaction.has_written:
            # 실제 SDK가 `get_transaction_id`에서 죽는 지점이다.
            raise ReadAfterWrite(self.key)
        return StrictSnapshot(self._db.data.get(self.key))


class StrictDB:
    def __init__(self): self.data = {}
    def collection(self, name):
        db = self
        class _C:
            def document(self, doc_id): return StrictRef(db, (name, doc_id))
        return _C()


class StrictTransaction:
    """읽기 뒤 쓰기 규칙을 지키는지 확인하는 최소 transaction."""

    def __init__(self, db):
        self._db = db
        self.has_written = False
        self.writes = 0

    def create(self, ref, data):
        if ref.key in self._db.data:
            raise AssertionError("create on existing")
        self.has_written = True
        self.writes += 1
        self._db.data[ref.key] = dict(data)

    def set(self, ref, data, merge=False):
        self.has_written = True
        self.writes += 1
        if merge and ref.key in self._db.data:
            self._db.data[ref.key].update(data)
        else:
            self._db.data[ref.key] = dict(data)


def test_two_wallets_in_one_transaction_do_not_read_after_write():
    """§15 회귀 — 유료 구매가 production에서 500이던 바로 그 순서다."""
    from app.shards.firestore_store import FirestoreShardStore

    db = StrictDB()
    tx = StrictTransaction(db)
    store = FirestoreShardStore(db)
    scoped = store.context(tx)

    # 구매자에게 잔액을 만들어 둔다(원장 경로가 아니라 fixture다).
    db.data[("ggumirror_shard_wallets", BUYER)] = {
        "balance": 3, "lifetimeEarned": 3, "lifetimeSpent": 0,
    }

    # 구매자 차감 → 판매자 지급. **두 번째의 읽기가 죽으면 안 된다.**
    store.apply_in_transaction(
        scoped, BUYER, -1, ShardReason.MIRROR_PURCHASE,
        idempotency_hash(BUYER, ShardReason.MIRROR_PURCHASE, LISTING),
    )
    store.apply_in_transaction(
        scoped, SELLER, 1, ShardReason.MIRROR_SALE,
        idempotency_hash(SELLER, ShardReason.MIRROR_SALE, LISTING),
    )

    # 읽기가 끝나기 전에는 아무것도 쓰지 않았다.
    assert tx.writes == 0, "읽기가 남았는데 벌써 썼다"

    scoped.flush()
    # 지갑 둘 + 원장 둘.
    assert tx.writes == 4

    assert db.data[("ggumirror_shard_wallets", BUYER)]["balance"] == 2
    assert db.data[("ggumirror_shard_wallets", SELLER)]["balance"] == 1


def test_insufficient_balance_writes_nothing_even_staged():
    from app.shards.firestore_store import FirestoreShardStore

    db = StrictDB()
    tx = StrictTransaction(db)
    store = FirestoreShardStore(db)
    scoped = store.context(tx)
    db.data[("ggumirror_shard_wallets", BUYER)] = {
        "balance": 0, "lifetimeEarned": 0, "lifetimeSpent": 0,
    }

    with pytest.raises(InsufficientShards):
        store.apply_in_transaction(
            scoped, BUYER, -1, ShardReason.MIRROR_PURCHASE,
            idempotency_hash(BUYER, ShardReason.MIRROR_PURCHASE, LISTING),
        )

    scoped.flush()
    assert tx.writes == 0


def test_three_shards_buying_one_is_not_insufficient():
    """§22 — 잔액 3에 1짜리를 사는 것은 부족이 아니다."""
    from app.shards.firestore_store import FirestoreShardStore

    db = StrictDB()
    tx = StrictTransaction(db)
    store = FirestoreShardStore(db)
    scoped = store.context(tx)
    db.data[("ggumirror_shard_wallets", BUYER)] = {
        "balance": 3, "lifetimeEarned": 3, "lifetimeSpent": 0,
    }

    wallet, _, applied = store.apply_in_transaction(
        scoped, BUYER, -1, ShardReason.MIRROR_PURCHASE,
        idempotency_hash(BUYER, ShardReason.MIRROR_PURCHASE, LISTING),
    )
    scoped.flush()
    assert applied is True
    assert wallet.balance == 2


def test_every_caller_flushes_before_its_own_writes():
    """읽기를 마친 뒤 flush하지 않으면 조각이 조용히 사라진다."""
    import pathlib

    for path, calls in [
        ("app/marketplace/firestore_store.py", 2),   # 구매 · 등록비
        ("app/capacity/store.py", 1),                # 보관 공간 구매
    ]:
        source = _code_only(pathlib.Path(path).read_text(encoding="utf-8"))
        applies = source.count("apply_in_transaction(")
        flushes = source.count("scoped.flush()")
        assert flushes >= calls, f"{path}: flush {flushes} < 지점 {calls}"
        assert applies > 0, path
