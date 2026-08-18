"""Apple 환불 조각 회수 (B-6F-B).

**JWS를 다시 시험하지 않는다** — 서명 검증은 `test_iap_verification.py`와
`test_app_store_notifications.py`가 이미 고정한다. 여기서 보는 것은 그 뒤의 경제다:

1. 되돌릴 양의 authority가 **원본 구매 claim**인가 (catalog도 알림도 아니다)
2. 잔액이 모자라도 **음수가 되지 않는가**, 그리고 못 뺀 몫이 빚이 되지 않는가
3. 같은 환불이 여러 번 와도 **정확히 한 번만** 빠지는가
4. `lifetimeEarned` · `lifetimeSpent`가 **환불로 움직이지 않는가**
"""

from __future__ import annotations

import logging
import threading

import pytest

from app.auth.models import User
from app.iap.models import (
    FAMILY_REVOKE,
    REFUND_FULL,
    REFUND_PRORATED,
    RefundMismatch,
    VerifiedNotification,
    VerifiedTransaction,
    parse_allowed_environments,
    refund_record_id,
    transaction_claim_id,
)
from app.iap.refunds import REFUNDS, IAPRefundService, requested_amount
from app.iap.service import TRANSACTIONS, IAPService
from app.shards.models import ShardReason
from app.shards.service import ShardLedgerService
from app.shards.store import InMemoryShardStore
from tests.test_iap import (
    CLIENT_ID,
    OTHER_USER,
    PRODUCT_10,
    PRODUCT_50,
    USER,
    FakeVerifier,
    transaction,
)

TXN = "2000000123456789"


# MARK: - 환경 만들기


def bought(store: InMemoryShardStore, *, product: str = PRODUCT_10, environment: str = "Sandbox"):
    """실제 지급 경로로 구매를 하나 만든다.

    claim을 손으로 쓰지 않는다 — 환불이 읽는 것은 **B-6A가 만든 바로 그 문서**여야 한다.
    """
    shards = ShardLedgerService(store)
    IAPService(
        verifier=FakeVerifier(
            transaction(transaction_id=TXN, product_id=product, environment=environment)
        ),
        shards=shards,
        bundle_id=CLIENT_ID,
        allowed_environments=parse_allowed_environments(environment),
    ).credit(User(id=USER), "jws")
    return shards


def refund_notification(
    *,
    transaction_id: str = TXN,
    product_id: str = PRODUCT_10,
    environment: str = "Sandbox",
    app_account_token: str | None = USER,
    revocation_type: str | None = REFUND_FULL,
    revocation_percentage_milliunits: int | None = None,
    with_transaction: bool = True,
) -> VerifiedNotification:
    """**검증을 통과한 뒤의** 환불 알림."""
    inner = (
        VerifiedTransaction(
            transaction_id=transaction_id,
            product_id=product_id,
            bundle_id=CLIENT_ID,
            environment=environment,
            app_account_token=app_account_token,
            transaction_type="Consumable",
            revocation_type=revocation_type,
            revocation_percentage_milliunits=revocation_percentage_milliunits,
        )
        if with_transaction
        else None
    )
    return VerifiedNotification(
        notification_type="REFUND",
        subtype=None,
        notification_uuid="11111111-2222-3333-4444-555555555555",
        bundle_id=CLIENT_ID,
        app_apple_id=6800016417,
        environment=environment,
        transaction=inner,
    )


def wallet(store: InMemoryShardStore, user_id: str = USER):
    return store.wallet(user_id)


def refund_record(store: InMemoryShardStore, transaction_id: str = TXN) -> dict | None:
    return store.claims.get((REFUNDS, refund_record_id(transaction_id)))


def refund_entries(store: InMemoryShardStore):
    return [e for e in store.entries if e.reason is ShardReason.IAP_REFUND]


@pytest.fixture
def store() -> InMemoryShardStore:
    return InMemoryShardStore()


# MARK: - 전액 환불 (§12)


def test_full_refund_recovers_everything(store):
    shards = bought(store)
    assert wallet(store).balance == 10

    IAPRefundService(shards).handle(refund_notification())

    w = wallet(store)
    assert w.balance == 0
    assert w.lifetime_refunded == 10
    # **받았다는 사실도, 쓴 적 없다는 사실도 바뀌지 않는다.**
    assert w.lifetime_earned == 10
    assert w.lifetime_spent == 0

    entries = refund_entries(store)
    assert len(entries) == 1
    assert entries[0].delta == -10
    assert entries[0].balance_after == 0

    record = refund_record(store)
    assert record["requestedAmount"] == 10
    assert record["recoveredAmount"] == 10
    assert record["unrecoveredAmount"] == 0
    assert record["originalAmount"] == 10
    assert record["ledgerEntryId"] == entries[0].id


def test_refund_does_not_touch_lifetime_spent(store):
    """generic debit이었다면 `lifetimeSpent`가 올랐을 것이다 — 그게 이 경로가 따로 있는 이유다."""
    shards = bought(store, product=PRODUCT_50)
    shards.debit(USER, 6, ShardReason.AI_STICKER, external_event_id="ai:1")
    assert (wallet(store).lifetime_spent, wallet(store).balance) == (6, 44)

    IAPRefundService(shards).handle(refund_notification(product_id=PRODUCT_50))

    w = wallet(store)
    assert w.lifetime_spent == 6, "환불이 '사용'으로 집계됐다"
    assert w.lifetime_earned == 50
    assert w.lifetime_refunded == 44
    assert w.balance == 0


# MARK: - 부분 회수 (§11)


def test_partial_recovery_stops_at_zero(store):
    shards = bought(store, product=PRODUCT_50)
    shards.debit(USER, 40, ShardReason.AI_STICKER, external_event_id="ai:spend")
    assert wallet(store).balance == 10

    IAPRefundService(shards).handle(refund_notification(product_id=PRODUCT_50))

    w = wallet(store)
    assert w.balance == 0, "잔액이 음수가 됐다"
    assert w.lifetime_refunded == 10, "요청량이 아니라 회수량만 쌓여야 한다"

    entries = refund_entries(store)
    assert len(entries) == 1
    assert entries[0].delta == -10
    assert entries[0].balance_after == 0

    record = refund_record(store)
    assert (record["requestedAmount"], record["recoveredAmount"], record["unrecoveredAmount"]) == (
        50, 10, 40,
    )


def test_unrecovered_is_not_a_debt(store):
    """못 뺀 40이 **나중에 번 조각을 깎지 않는다.** 부채 상계 시스템은 없다."""
    shards = bought(store, product=PRODUCT_50)
    shards.debit(USER, 50, ShardReason.AI_STICKER, external_event_id="ai:all")
    IAPRefundService(shards).handle(refund_notification(product_id=PRODUCT_50))

    shards.credit(USER, 1, ShardReason.DAILY_ATTENDANCE, external_event_id="2026-08-19")

    assert wallet(store).balance == 1
    assert wallet(store).lifetime_refunded == 0


# MARK: - 회수 0 (§10)


def test_zero_balance_still_records_the_refund(store):
    shards = bought(store, product=PRODUCT_50)
    shards.debit(USER, 50, ShardReason.AI_STICKER, external_event_id="ai:all")
    assert wallet(store).balance == 0

    IAPRefundService(shards).handle(refund_notification(product_id=PRODUCT_50))

    w = wallet(store)
    assert w.balance == 0
    assert w.lifetime_refunded == 0
    assert w.lifetime_spent == 50

    # **delta 0짜리 원장 줄을 만들지 않는다** — 조각이 움직이지 않았다.
    assert refund_entries(store) == []

    record = refund_record(store)
    assert record is not None, "처리 완료된 환불인데 기록이 없다"
    assert (record["requestedAmount"], record["recoveredAmount"], record["unrecoveredAmount"]) == (
        50, 0, 50,
    )
    assert record["ledgerEntryId"] is None


# MARK: - 멱등 (§8)


@pytest.mark.parametrize("times", [2, 5])
def test_duplicate_refund_deducts_once(store, times):
    shards = bought(store)
    refunds = IAPRefundService(shards)

    for _ in range(times):
        refunds.handle(refund_notification())

    w = wallet(store)
    assert w.balance == 0
    assert w.lifetime_refunded == 10, "중복 환불이 누적을 또 올렸다"
    assert len(refund_entries(store)) == 1


def test_duplicate_refund_with_a_new_notification_uuid_still_deducts_once(store):
    """business 멱등은 **원본 구매 transaction 기준**이다. notificationUUID가 아니다."""
    shards = bought(store)
    refunds = IAPRefundService(shards)

    refunds.handle(refund_notification())
    second = refund_notification()
    object.__setattr__(second, "notification_uuid", "99999999-8888-7777-6666-555555555555")
    refunds.handle(second)

    assert wallet(store).balance == 0
    assert wallet(store).lifetime_refunded == 10
    assert len(refund_entries(store)) == 1


def test_refund_after_zero_recovery_is_not_retried_into_a_deduction(store):
    """회수 0으로 끝난 환불이 나중에 잔액이 생겼다고 다시 빼면 안 된다."""
    shards = bought(store)
    shards.debit(USER, 10, ShardReason.AI_STICKER, external_event_id="ai:all")
    refunds = IAPRefundService(shards)
    refunds.handle(refund_notification())
    assert refund_record(store)["recoveredAmount"] == 0

    shards.credit(USER, 30, ShardReason.REWARDED_AD, external_event_id="ad:1")
    refunds.handle(refund_notification())

    assert wallet(store).balance == 30
    assert wallet(store).lifetime_refunded == 0
    assert refund_entries(store) == []


# MARK: - 동시성 (§21)


def test_concurrent_refunds_deduct_exactly_once(store):
    shards = bought(store, product=PRODUCT_50)
    refunds = IAPRefundService(shards)
    start = threading.Barrier(8)

    def run():
        start.wait()
        refunds.handle(refund_notification(product_id=PRODUCT_50))

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert wallet(store).balance == 0
    assert wallet(store).lifetime_refunded == 50
    assert len(refund_entries(store)) == 1
    assert len([k for k in store.claims if k[0] == REFUNDS]) == 1


# MARK: - 원본 구매가 없다 (§6)


def test_unknown_purchase_is_not_an_error(store):
    """우리가 조각을 준 적 없는 결제다. 되돌릴 것이 없고 재시도해도 답이 같다."""
    shards = ShardLedgerService(store)
    shards.credit(USER, 20, ShardReason.DAILY_ATTENDANCE, external_event_id="2026-08-18")

    IAPRefundService(shards).handle(refund_notification())

    assert wallet(store).balance == 20
    assert wallet(store).lifetime_refunded == 0
    assert refund_entries(store) == []
    assert refund_record(store) is None


# MARK: - 대조 실패 (§20)


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"product_id": PRODUCT_50}, "다른 상품"),
        ({"environment": "Production"}, "다른 환경"),
        ({"app_account_token": OTHER_USER}, "다른 사람"),
    ],
)
def test_mismatch_never_touches_the_economy(store, kwargs, why):
    shards = bought(store)

    with pytest.raises(RefundMismatch):
        IAPRefundService(shards).handle(refund_notification(**kwargs))

    assert wallet(store).balance == 10, why
    assert wallet(store).lifetime_refunded == 0
    assert refund_entries(store) == []
    assert refund_record(store) is None


def test_other_user_wallet_is_never_touched(store):
    """`appAccountToken`이 다른 사람이면 **그 사람 지갑도** 건드리지 않는다."""
    shards = bought(store)
    shards.credit(OTHER_USER, 100, ShardReason.DAILY_ATTENDANCE, external_event_id="2026-08-18")

    with pytest.raises(RefundMismatch):
        IAPRefundService(shards).handle(refund_notification(app_account_token=OTHER_USER))

    assert wallet(store, OTHER_USER).balance == 100
    assert wallet(store).balance == 10


@pytest.mark.parametrize("token", [None, "", "not-a-uuid"])
def test_missing_account_token_recovers_nothing(store, token):
    shards = bought(store)

    IAPRefundService(shards).handle(refund_notification(app_account_token=token))

    assert wallet(store).balance == 10
    assert refund_record(store) is None


# MARK: - prorated (§13, §19)


@pytest.mark.parametrize(
    ("original", "milliunits", "expected"),
    [
        # ⚠️ Apple `revocationPercentage`는 **milliunits**다: 100% = 100000.
        (50, 50_000, 25),    # 50%
        (50, 33_000, 16),    # 33%   floor(16.5)
        (10, 33_000, 3),     # 33%   floor(3.3)
        (10, 1_000, 1),      # 1%    floor(0.1) → 최소 1
        (10, 100_000, 10),   # 100%
        (50, 100_000, 50),   # 100%
        # Apple이 실제로 보내는 소수 정밀도
        (50, 67_932, 33),    # 67.932%  floor(33.966)
        (10, 15, 1),         # 0.015%   floor(0.0015) → 최소 1
    ],
)
def test_prorated_amounts(original, milliunits, expected):
    assert requested_amount(original, REFUND_PRORATED, milliunits) == expected


@pytest.mark.parametrize(
    ("milliunits", "expected"),
    [(1, 1), (99_999, 9), (100_000, 10)],
)
def test_prorated_boundaries(milliunits, expected):
    """0에 붙은 값도 최소 1, 100000은 정확히 전액, 그 사이는 절대 원본을 넘지 않는다."""
    assert requested_amount(10, REFUND_PRORATED, milliunits) == expected


def test_percentage_is_not_read_as_a_plain_percent():
    """**회귀 방지.** 100으로 나누면 50%가 25가 아니라 0(→최소 1)이 된다."""
    assert requested_amount(50, REFUND_PRORATED, 50_000) == 25
    # 0..100 스케일로 읽었다면 50이 "50%"라 25가 나왔을 값이다. milliunits에서는 0.05%다.
    assert requested_amount(50, REFUND_PRORATED, 50) == 1


def test_prorated_never_exceeds_the_original_amount():
    """§5 property: `0 < p <= 100000`이면 언제나 `1 <= requested <= original`."""
    for original in (1, 6, 10, 50, 100):
        for milliunits in (1, 15, 999, 1_000, 33_000, 50_000, 67_932, 99_999, 100_000):
            requested = requested_amount(original, REFUND_PRORATED, milliunits)
            assert 1 <= requested <= original, (original, milliunits, requested)


def test_prorated_is_monotonic():
    """§5 property: percentage가 오르면 회수량은 절대 줄지 않는다."""
    for original in (1, 10, 50, 100):
        previous = 0
        for milliunits in range(1, 100_001, 7):
            requested = requested_amount(original, REFUND_PRORATED, milliunits)
            assert requested >= previous, (original, milliunits)
            previous = requested
        assert requested_amount(original, REFUND_PRORATED, 100_000) == original


def test_prorated_refund_deducts_the_floor(store):
    shards = bought(store, product=PRODUCT_50)

    IAPRefundService(shards).handle(
        refund_notification(
            product_id=PRODUCT_50,
            revocation_type=REFUND_PRORATED,
            revocation_percentage_milliunits=33_000,
        )
    )

    assert wallet(store).balance == 34
    assert wallet(store).lifetime_refunded == 16
    record = refund_record(store)
    assert (record["requestedAmount"], record["recoveredAmount"]) == (16, 16)
    # **raw milliunit을 그대로 저장한다.** 33으로 정규화하지 않는다.
    assert record["revocationPercentage"] == 33_000


def test_prorated_without_percentage_changes_nothing(store):
    """얼마인지 Apple이 말해주지 않았다. **추측하지 않는다.**"""
    shards = bought(store)

    IAPRefundService(shards).handle(
        refund_notification(revocation_type=REFUND_PRORATED, revocation_percentage_milliunits=None)
    )

    assert wallet(store).balance == 10
    assert wallet(store).lifetime_refunded == 0
    assert refund_record(store) is None


@pytest.mark.parametrize("percentage", [0, -1, 100_001, 1_000_000])
def test_invalid_percentage_never_deducts(store, percentage):
    shards = bought(store)

    with pytest.raises(Exception):
        IAPRefundService(shards).handle(
            refund_notification(
                revocation_type=REFUND_PRORATED, revocation_percentage_milliunits=percentage
            )
        )

    assert wallet(store).balance == 10
    assert refund_record(store) is None


# MARK: - 매핑하지 않는 것 (§1)


def test_family_revoke_changes_nothing(store):
    shards = bought(store)

    IAPRefundService(shards).handle(refund_notification(revocation_type=FAMILY_REVOKE))

    assert wallet(store).balance == 10
    assert wallet(store).lifetime_refunded == 0
    assert refund_record(store) is None, "가족 회수를 일반 환불로 매핑했다"


@pytest.mark.parametrize("revocation", [None, "", "SOMETHING_NEW"])
def test_unknown_revocation_type_changes_nothing(store, revocation):
    shards = bought(store)

    IAPRefundService(shards).handle(refund_notification(revocation_type=revocation))

    assert wallet(store).balance == 10
    assert refund_record(store) is None


def test_notification_without_transaction_changes_nothing(store):
    shards = bought(store)

    IAPRefundService(shards).handle(refund_notification(with_transaction=False))

    assert wallet(store).balance == 10
    assert refund_record(store) is None


# MARK: - 금액의 authority (§5)


def test_amount_comes_from_the_purchase_claim_not_the_catalog(store):
    """catalog가 나중에 바뀌어도 **예전 구매는 그때 준 만큼만** 되돌린다."""
    shards = bought(store, product=PRODUCT_10)
    claim = store.claims[(TRANSACTIONS, transaction_claim_id(TXN))]
    assert claim["amount"] == 10

    # 상점이 10 → 30으로 바뀐 세상. 원장은 여전히 10만 되돌려야 한다.
    from app.iap import models as iap_models

    original = dict(iap_models.SHARD_PRODUCTS)
    iap_models.SHARD_PRODUCTS[PRODUCT_10] = 30
    try:
        shards.credit(USER, 30, ShardReason.REWARDED_AD, external_event_id="ad:top-up")
        IAPRefundService(shards).handle(refund_notification())
    finally:
        iap_models.SHARD_PRODUCTS.clear()
        iap_models.SHARD_PRODUCTS.update(original)

    assert wallet(store).lifetime_refunded == 10, "catalog 값으로 되돌렸다"
    assert wallet(store).balance == 30


def test_zero_amount_claim_is_rejected(store):
    shards = bought(store)
    store.claims[(TRANSACTIONS, transaction_claim_id(TXN))]["amount"] = 0

    with pytest.raises(RefundMismatch):
        IAPRefundService(shards).handle(refund_notification())

    assert refund_record(store) is None


# MARK: - 로그 (§15)


def test_logs_never_contain_raw_values(store, caplog):
    shards = bought(store)

    with caplog.at_level(logging.DEBUG):
        IAPRefundService(shards).handle(refund_notification())

    assert TXN not in caplog.text
    assert USER not in caplog.text
    assert "eyJ" not in caplog.text
    # 남아도 되는 것: 결과 값과 12자리 hash
    assert "recovered=10" in caplog.text


def test_mismatch_logs_never_contain_identity(store, caplog):
    shards = bought(store)

    with caplog.at_level(logging.DEBUG), pytest.raises(RefundMismatch):
        IAPRefundService(shards).handle(refund_notification(app_account_token=OTHER_USER))

    assert USER not in caplog.text
    assert OTHER_USER not in caplog.text


# MARK: - 예전 지갑 (§3)


def test_wallet_without_lifetime_refunded_reads_as_zero(store):
    """B-6F-B 이전에 만들어진 지갑에는 이 field가 없다. migration 없이 0으로 읽는다."""
    from app.shards.firestore_store import _wallet

    old = _wallet(USER, {"balance": 5, "lifetimeEarned": 5, "lifetimeSpent": 0})
    assert old.lifetime_refunded == 0


def test_ordinary_credit_preserves_lifetime_refunded(store):
    """환불 뒤의 일반 거래가 누적을 지우면 안 된다."""
    shards = bought(store)
    IAPRefundService(shards).handle(refund_notification())
    assert wallet(store).lifetime_refunded == 10

    shards.credit(USER, 1, ShardReason.DAILY_ATTENDANCE, external_event_id="2026-08-19")
    shards.debit(USER, 1, ShardReason.AI_STICKER, external_event_id="ai:2")

    assert wallet(store).lifetime_refunded == 10


# MARK: - HTTP status 매핑 (§14)


class FakeNotificationVerifier:
    """서명 검증을 흉내 내지 않는다 — **이미 검증된 알림**을 그대로 돌려준다.

    실제 서명 경로는 `test_app_store_notifications.py`가 지난다. 여기서 보는 것은
    "처리 결과가 어떤 status가 되는가"뿐이다.
    """

    is_configured = True

    def __init__(self, notification: VerifiedNotification) -> None:
        self._notification = notification

    def verify_notification(self, signed_payload: str) -> VerifiedNotification:
        return self._notification

    def verify(self, signed_transaction: str) -> VerifiedTransaction:  # pragma: no cover
        raise AssertionError("환불 경로는 안쪽 transaction을 다시 검증하지 않는다")


def notification_client(store: InMemoryShardStore, notification: VerifiedNotification):
    from fastapi.testclient import TestClient

    from app.core.config import Settings
    from app.main import create_app

    app = create_app(
        Settings(
            app_env="local",
            apple_client_id=CLIENT_ID,
            iap_allowed_environments="Sandbox",
            iap_app_apple_id=6800016417,
        ),
        shard_store=store,
        transaction_verifier=FakeNotificationVerifier(notification),
    )
    return TestClient(app, raise_server_exceptions=False)


def post(client) -> int:
    return client.post("/app-store/notifications/v2", json={"signedPayload": "signed"}).status_code


def test_http_handled_refund_is_200(store):
    bought(store)
    client = notification_client(store, refund_notification())

    assert post(client) == 200
    assert wallet(store).balance == 0


def test_http_duplicate_refund_is_200(store):
    bought(store)
    client = notification_client(store, refund_notification())

    assert [post(client), post(client), post(client)] == [200, 200, 200]
    assert wallet(store).lifetime_refunded == 10
    assert len(refund_entries(store)) == 1


@pytest.mark.parametrize(
    "notification",
    [
        refund_notification(),  # 원본 구매가 없다
        refund_notification(revocation_type=FAMILY_REVOKE),
        refund_notification(revocation_type=REFUND_PRORATED, revocation_percentage_milliunits=None),
    ],
    ids=["unknown_purchase", "family_revoke", "missing_percentage"],
)
def test_http_no_op_refunds_are_200(store, notification):
    """"되돌릴 것이 없다"는 실패가 아니다 — Apple에게 계속 503을 주지 않는다."""
    client = notification_client(store, notification)

    assert post(client) == 200
    assert refund_entries(store) == []


def test_http_mismatch_is_400(store):
    """재시도해도 답이 같은 **영구 불일치**다."""
    bought(store)
    client = notification_client(store, refund_notification(product_id=PRODUCT_50))

    assert post(client) == 400
    assert wallet(store).balance == 10


def test_http_store_failure_is_5xx(store):
    """일시적 Firestore 장애는 **삼키지 않는다** — Apple이 다시 보내야 한다."""
    from app.auth.store import StoreUnavailable

    bought(store)

    class Failing:
        def __getattr__(self, name):
            raise StoreUnavailable("refund")

    client = notification_client(Failing(), refund_notification())

    assert post(client) >= 500


def test_http_response_never_leaks_business_details(store):
    """응답은 Apple용이다. 얼마를 회수했는지 알려줄 이유가 없다."""
    bought(store)
    client = notification_client(store, refund_notification())

    body = client.post("/app-store/notifications/v2", json={"signedPayload": "signed"}).text

    assert "10" not in body
    assert "recovered" not in body
    assert USER not in body


# MARK: - 소스 불변식 (§2, §4)


def _code_only(source: str) -> str:
    import io
    import tokenize

    return "".join(
        token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type not in (tokenize.COMMENT, tokenize.STRING)
    )


def test_refund_never_uses_generic_debit():
    """generic debit은 음수 delta를 `lifetimeSpent`로 집계한다 — 환불에 쓰면 거짓말이 된다."""
    from pathlib import Path

    code = _code_only((Path(__file__).resolve().parent.parent / "app/iap/refunds.py").read_text())
    for banned in [".debit(", ".credit(", "lifetime_spent", "lifetimeSpent"]:
        assert banned not in code, f"환불이 generic 경로를 쓴다 ({banned})"
    assert "refund_iap" in code, "전용 원자적 경로를 쓰지 않는다"


def test_iap_refund_reason_is_not_the_ai_refund_reason():
    """`refund`는 AI 생성 실패 복구용이다. 섞으면 원장에서 둘을 구분할 수 없다."""
    assert ShardReason.IAP_REFUND.value == "iap_refund"
    assert ShardReason.REFUND.value == "refund"
    assert ShardReason.IAP_REFUND is not ShardReason.REFUND


def test_reversed_reason_is_not_added_before_it_is_used():
    """B-6F-C 전에 쓰지 않는 enum을 만들어 두지 않는다 (dead logic 금지)."""
    assert not hasattr(ShardReason, "IAP_REFUND_REVERSED")


def test_refund_record_stores_no_raw_credentials(store):
    shards = bought(store)
    IAPRefundService(shards).handle(refund_notification())

    record = refund_record(store)
    flat = str(record)
    assert TXN not in flat, "raw transactionId가 저장됐다"
    assert "signedPayload" not in flat
    assert "eyJ" not in flat
    # 남는 것은 business state뿐이다.
    assert set(record) == {
        "userId", "productId", "environment", "purchaseTransactionClaimId",
        "originalAmount", "requestedAmount", "recoveredAmount", "unrecoveredAmount",
        "revocationType", "revocationPercentage", "ledgerEntryId", "createdAt", "schemaVersion",
    }


def test_refund_record_id_differs_from_the_purchase_claim_id():
    """같은 transaction이지만 **다른 문서**다. 겹치면 구매 claim을 덮어쓴다."""
    assert refund_record_id(TXN) != transaction_claim_id(TXN)
    assert TXN not in refund_record_id(TXN)


def test_no_generic_refund_endpoint(store):
    """환불을 부를 수 있는 통로는 Apple 서명 알림 하나뿐이다."""
    client = notification_client(store, refund_notification())
    for path in ["/refunds", "/shards/refund", "/app-store/refund", "/users/me/iap/refund"]:
        assert client.post(path, json={"amount": 10}).status_code in {401, 404, 405}


# MARK: - milliunit 단위 (B-6F-B.1)


def test_full_refund_ignores_the_percentage(store):
    """`REFUND_FULL`은 percentage를 **금액 authority로 쓰지 않는다.**"""
    shards = bought(store, product=PRODUCT_50)

    IAPRefundService(shards).handle(
        refund_notification(
            product_id=PRODUCT_50,
            revocation_type=REFUND_FULL,
            revocation_percentage_milliunits=1,  # 있어도 무시한다
        )
    )

    assert wallet(store).lifetime_refunded == 50
    assert refund_record(store)["requestedAmount"] == 50


def test_apple_precision_refund_deducts_the_floor(store):
    """67.932%는 `67932`로 온다. floor(50 × 0.67932) = 33."""
    shards = bought(store, product=PRODUCT_50)

    IAPRefundService(shards).handle(
        refund_notification(
            product_id=PRODUCT_50,
            revocation_type=REFUND_PRORATED,
            revocation_percentage_milliunits=67_932,
        )
    )

    assert wallet(store).balance == 17
    assert wallet(store).lifetime_refunded == 33
    record = refund_record(store)
    assert record["revocationPercentage"] == 67_932
    assert record["requestedAmount"] == 33


def test_tiny_percentage_still_recovers_one(store):
    """0.015%(=15)도 최소 1은 회수한다 — Apple이 돌려줬는데 우리가 0이면 안 된다."""
    shards = bought(store)

    IAPRefundService(shards).handle(
        refund_notification(
            revocation_type=REFUND_PRORATED, revocation_percentage_milliunits=15
        )
    )

    assert wallet(store).balance == 9
    assert wallet(store).lifetime_refunded == 1


def test_milliunit_unit_is_named_in_the_source():
    """0..100으로 다시 오해하지 않도록 단위가 이름과 상수에 박혀 있어야 한다."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    refunds = (root / "app/iap/refunds.py").read_text()
    assert "MILLIUNITS_PER_UNIT = 100_000" in refunds
    assert "// 100" not in _code_only(refunds), "다시 100으로 나눈다"

    models = (root / "app/iap/models.py").read_text()
    assert "revocation_percentage_milliunits" in models
    assert "milliunits" in models.lower()
