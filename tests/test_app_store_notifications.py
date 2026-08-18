"""App Store Server Notifications V2 (B-6F-A).

**경제 mutation이 0인 것이 이 phase의 핵심**이다. 환불 차감은 B-6F-B다.
그래서 처리 못 하는 알림을 200으로 삼키지 않는지도 함께 고정한다.

Apple production key가 필요 없다 — `test_iap_verification.py`와 같은
Apple 모양의 테스트 체인으로 실제 서명 경로를 지난다.
"""

from __future__ import annotations

import base64
import datetime
import logging
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.iap.models import (
    ACKNOWLEDGED_NOTIFICATIONS,
    DEFERRED_NOTIFICATIONS,
    EnvironmentNotAllowed,
    IAPUnavailable,
    InvalidTransaction,
    NotificationNotHandled,
    NotificationOutcome,
    parse_allowed_environments,
)
from app.iap.notifications import AppStoreNotificationService
from app.main import create_app
from app.shards.store import InMemoryShardStore
from tests.test_iap_verification import BUNDLE_ID, PRODUCT, USER, Chain, APP_APPLE_ID

TRANSACTION_ID = "2000000123456789"


def notification_payload(chain: Chain, **overrides) -> str:
    """Apple V2 알림 모양의 서명 payload."""
    data = {
        "bundleId": BUNDLE_ID,
        "environment": "Sandbox",
        "appAppleId": APP_APPLE_ID,
    }
    if overrides.pop("withTransaction", True):
        data["signedTransactionInfo"] = chain.sign()
    data.update(overrides.pop("data", {}))

    payload = {
        "notificationType": overrides.pop("notificationType", "TEST"),
        "notificationUUID": overrides.pop("notificationUUID", "11111111-2222-3333-4444-555555555555"),
        "version": "2.0",
        "signedDate": int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000),
        "data": data,
    }
    payload.update(overrides)
    from cryptography.hazmat.primitives import serialization

    pem = chain.leaf_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    x5c = [
        base64.b64encode(c.public_bytes(serialization.Encoding.DER)).decode()
        for c in (chain.leaf, chain.intermediate, chain.root)
    ]
    return jwt.encode(payload, pem, algorithm="ES256", headers={"x5c": x5c})


@pytest.fixture
def chain() -> Chain:
    return Chain()


def service(chain: Chain, environments: str = "Sandbox") -> AppStoreNotificationService:
    from app.iap.apple_verifier import build_apple_verifier

    verifier = build_apple_verifier(
        bundle_id=BUNDLE_ID,
        allowed_environments=parse_allowed_environments(environments),
        app_apple_id=APP_APPLE_ID,
        enable_online_checks=False,
        roots=[chain.root_der],
    )
    return AppStoreNotificationService(
        verifier,
        bundle_id=BUNDLE_ID,
        allowed_environments=parse_allowed_environments(environments),
        app_apple_id=APP_APPLE_ID,
    )


# MARK: - 검증


def test_valid_test_notification_is_acknowledged(chain):
    outcome = service(chain).handle(notification_payload(chain, withTransaction=False))
    assert outcome is NotificationOutcome.ACKNOWLEDGED


def test_notification_without_transaction_is_fine(chain):
    """TEST에는 `signedTransactionInfo`가 없다. hash를 만들려고 하지 않는다."""
    assert service(chain).handle(notification_payload(chain, withTransaction=False))


def test_inner_transaction_is_verified_separately(chain):
    """바깥 JWS가 맞다고 안쪽 transaction을 그대로 믿지 않는다."""
    source = Path(__file__).resolve().parent.parent / "app/iap/apple_verifier.py"
    text = source.read_text()
    assert "signedTransactionInfo" in text
    assert "self.verify(data.signedTransactionInfo)" in text


def test_forged_inner_transaction_is_rejected(chain):
    """안쪽만 다른 체인으로 서명하면 거절된다."""
    other = Chain()
    payload = notification_payload(chain, data={"signedTransactionInfo": other.sign()})
    with pytest.raises(InvalidTransaction):
        service(chain).handle(payload)


def test_malformed_payload_is_rejected(chain):
    for bad in ["", "   ", "not-a-jws", "a.b.c"]:
        with pytest.raises((InvalidTransaction, IAPUnavailable)):
            service(chain).handle(bad)


def test_untrusted_signature_is_rejected(chain):
    other = Chain()
    with pytest.raises(InvalidTransaction):
        service(chain).handle(notification_payload(other, withTransaction=False))


def test_wrong_bundle_is_rejected(chain):
    payload = notification_payload(
        chain, withTransaction=False, data={"bundleId": "com.someone.else"}
    )
    with pytest.raises(InvalidTransaction):
        service(chain).handle(payload)


def test_wrong_environment_is_rejected(chain):
    payload = notification_payload(chain, withTransaction=False, data={"environment": "Production"})
    with pytest.raises((InvalidTransaction, EnvironmentNotAllowed)):
        service(chain, environments="Sandbox").handle(payload)


def test_xcode_and_local_testing_never_verify(chain):
    for environment in ["Xcode", "LocalTesting"]:
        payload = notification_payload(chain, withTransaction=False, data={"environment": environment})
        with pytest.raises((InvalidTransaction, EnvironmentNotAllowed)):
            service(chain, environments="Production,Sandbox").handle(payload)


def test_unconfigured_verifier_is_unavailable(chain):
    built = service(chain, environments="")
    with pytest.raises(IAPUnavailable):
        built.handle(notification_payload(chain, withTransaction=False))


# MARK: - 타입 분류


@pytest.mark.parametrize("kind", sorted(ACKNOWLEDGED_NOTIFICATIONS))
@pytest.mark.parametrize("with_transaction", [False, True])
def test_no_op_notifications_are_acknowledged(chain, kind, with_transaction):
    """transaction이 있든 없든 no-op 알림은 소비한다."""
    payload = notification_payload(chain, withTransaction=with_transaction, notificationType=kind)
    assert service(chain).handle(payload) is NotificationOutcome.ACKNOWLEDGED


@pytest.mark.parametrize("kind", sorted(DEFERRED_NOTIFICATIONS))
def test_refund_notifications_are_deferred(chain, kind):
    """**200으로 삼키지 않는다** — 아직 구현하지 않았으므로 재시도를 받아야 한다."""
    payload = notification_payload(chain, notificationType=kind)
    with pytest.raises(NotificationNotHandled):
        service(chain).handle(payload)


def test_unknown_type_is_deferred_not_swallowed(chain):
    """모르는/새 타입을 무조건 200으로 삼키면 조각에 영향 있는 알림이 사라진다."""
    payload = notification_payload(chain, withTransaction=False, notificationType="SUBSCRIBED")
    with pytest.raises(NotificationNotHandled):
        service(chain).handle(payload)


def test_consumption_request_is_not_a_refund(chain):
    """환불 승인이 아니다. 조각을 건드리지 않고 Apple에 응답도 하지 않는다."""
    payload = notification_payload(chain, notificationType="CONSUMPTION_REQUEST")
    assert service(chain).handle(payload) is NotificationOutcome.ACKNOWLEDGED


def test_refund_declined_changes_nothing(chain):
    payload = notification_payload(chain, notificationType="REFUND_DECLINED")
    assert service(chain).handle(payload) is NotificationOutcome.ACKNOWLEDGED


# MARK: - ONE_TIME_CHARGE (consumable 구매의 정상 알림)


def test_one_time_charge_is_acknowledged(chain):
    """consumable 구매마다 온다. 정상 알림에 503을 주면 Apple이 영원히 재시도한다."""
    payload = notification_payload(chain, notificationType="ONE_TIME_CHARGE")
    assert service(chain).handle(payload) is NotificationOutcome.ACKNOWLEDGED


def test_one_time_charge_verifies_inner_transaction(chain):
    """안쪽 transaction도 반드시 검증한다 — 바깥만 맞으면 통과시키지 않는다."""
    other = Chain()
    payload = notification_payload(
        chain, notificationType="ONE_TIME_CHARGE",
        data={"signedTransactionInfo": other.sign()},
    )
    with pytest.raises(InvalidTransaction):
        service(chain).handle(payload)


def test_one_time_charge_without_claim_is_still_acknowledged(chain):
    """알림이 client fulfillment보다 **먼저** 올 수 있다.

    claim이 없다고 4xx/5xx를 주면 정상 순서를 오류로 취급하는 셈이다 — 200으로 받는다.
    (애초에 claim을 조회하지 않으므로 race가 성립하지 않는다.)
    """
    payload = notification_payload(chain, notificationType="ONE_TIME_CHARGE")
    assert service(chain).handle(payload) is NotificationOutcome.ACKNOWLEDGED


def test_one_time_charge_is_not_a_fulfillment_authority():
    """알림 경로에 지급 코드가 없다 — 한 결제에 두 번 지급될 수 없다."""
    root = Path(__file__).resolve().parent.parent
    code = _code_only((root / "app/iap/notifications.py").read_text())
    for banned in ["shard_amount", "SHARD_PRODUCTS", "IAPService", "credit"]:
        assert banned not in code, f"알림이 지급 authority가 됐다 ({banned})"


def test_one_time_charge_is_no_longer_unknown():
    from app.iap.models import ACKNOWLEDGED_NOTIFICATIONS, DEFERRED_NOTIFICATIONS

    assert "ONE_TIME_CHARGE" in ACKNOWLEDGED_NOTIFICATIONS
    assert "ONE_TIME_CHARGE" not in DEFERRED_NOTIFICATIONS
    # 환불 계열은 여전히 deferred다.
    assert {"REFUND", "REFUND_REVERSED"} <= DEFERRED_NOTIFICATIONS


def test_one_time_charge_logs_no_raw_values(chain, caplog):
    payload = notification_payload(chain, notificationType="ONE_TIME_CHARGE")
    with caplog.at_level(logging.DEBUG):
        service(chain).handle(payload)
    assert payload not in caplog.text
    assert TRANSACTION_ID not in caplog.text
    assert USER not in caplog.text
    assert "eyJ" not in caplog.text


# MARK: - 경제 mutation 0 (이 phase의 핵심)


def _code_only(source: str) -> str:
    import io
    import tokenize

    return "".join(
        token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type not in (tokenize.COMMENT, tokenize.STRING)
    )


def test_notification_path_never_touches_the_economy():
    root = Path(__file__).resolve().parent.parent
    for path in ["app/iap/notifications.py", "app/api/app_store.py"]:
        code = _code_only((root / path).read_text())
        for banned in [".credit(", ".debit(", ".apply(", "ShardLedgerService", "ExclusiveClaim",
                       "ggumirror_shard_wallets", "ggumirror_iap_transactions", "ggumirror_iap_refunds"]:
            assert banned not in code, f"{path}: 경제를 건드린다 ({banned})"


def test_no_app_store_server_api_client():
    """`.p8` · issuerId · keyId는 이번 phase에서 도입하지 않는다."""
    root = Path(__file__).resolve().parent.parent
    for path in ["app/iap/notifications.py", "app/api/app_store.py", "app/core/config.py"]:
        code = _code_only((root / path).read_text())
        for banned in ["AppStoreServerAPIClient", "issuer_id", "key_id", "send_consumption"]:
            assert banned not in code, f"{path}: {banned}"


def test_official_library_is_used_for_notifications():
    source = (Path(__file__).resolve().parent.parent / "app/iap/apple_verifier.py").read_text()
    assert "verify_and_decode_notification" in source


# MARK: - 로그


def test_logs_never_contain_raw_values(chain, caplog):
    payload = notification_payload(chain)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(NotificationNotHandled):
            service(chain).handle(notification_payload(chain, notificationType="REFUND"))
        service(chain).handle(notification_payload(chain, withTransaction=False))

    assert payload not in caplog.text
    assert TRANSACTION_ID not in caplog.text
    assert USER not in caplog.text
    assert "eyJ" not in caplog.text


# MARK: - HTTP


@pytest.fixture
def client(chain) -> TestClient:
    app = create_app(
        Settings(
            app_env="local",
            apple_client_id=BUNDLE_ID,
            iap_allowed_environments="Sandbox",
            iap_app_apple_id=APP_APPLE_ID,
        ),
        shard_store=InMemoryShardStore(),
    )
    return TestClient(app, raise_server_exceptions=False)


def test_http_requires_no_bearer(client, chain):
    """Apple server-to-server다. 세션을 요구하면 알림을 받을 수 없다."""
    response = client.post(
        "/app-store/notifications/v2", json={"signedPayload": "not-a-jws"}
    )
    assert response.status_code != 401


def test_http_rejects_extra_fields(client):
    response = client.post(
        "/app-store/notifications/v2",
        json={"signedPayload": "x", "notificationType": "REFUND"},
    )
    assert response.status_code == 422


def test_http_rejects_malformed_payload(client):
    response = client.post("/app-store/notifications/v2", json={"signedPayload": "not-a-jws"})
    # 검증기가 production root를 쓰므로 400(영구) 또는 503(설정) 중 하나다.
    assert response.status_code in {400, 503}


def test_no_generic_mutation_endpoint(client):
    for path in ["/shards", "/shards/credit", "/app-store/credit"]:
        assert client.post(path, json={"amount": 100}).status_code in {401, 404, 405}
