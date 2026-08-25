"""APNs provider (Phase F §56).

**실제 Apple로 나가지 않는다.** transport 이음매에 fake를 끼우고 JWT · topic ·
host · payload · 응답 해석을 확인한다.

private key는 이 파일에서 **매번 새로 만드는 test key**다. 실제 `.p8`은 repo에
없고, 있어서도 안 된다.
"""

from __future__ import annotations

import json
import logging

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.push.models import PushDevice, PushEnvironment, PushMessage, push_device_id
from app.push.provider import (
    APNsPushProvider,
    NullPushProvider,
    build_push_provider,
)

TOKEN = "a1" * 32
BUNDLE = "com.mark77234.ggumirror"
KEY_ID = "TESTKEYID1"
TEAM_ID = "TESTTEAM12"


@pytest.fixture(autouse=True)
def no_real_apns(monkeypatch: pytest.MonkeyPatch) -> None:
    """이 파일에서 **실제 APNs로 나가는 길을 막는다.**

    `conftest`의 그물은 `urllib`용이고 APNs는 httpx로 나간다. 이음매에 fake를
    끼우는 것을 한 번이라도 잊으면 test가 진짜 Apple을 부르게 되는데,
    그때 통과해 버리는 것이 가장 나쁘다.
    """
    import httpx

    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("test tried to open a real APNs connection")

    monkeypatch.setattr(httpx, "Client", blocked)


@pytest.fixture
def private_key() -> str:
    """**test scope에만 존재하는 P-256 key.** 실행마다 새로 만든다."""
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


class FakeTransport:
    """HTTP/2 한 번을 흉내 낸다. **network에 나가지 않는다.**"""

    def __init__(self, status: int = 200, reason: str = "") -> None:
        self.status = status
        self.reason = reason
        self.calls: list[tuple[str, str, dict, bytes]] = []
        self.raises: Exception | None = None

    def post(self, host, path, headers, body):
        self.calls.append((host, path, dict(headers), body))
        if self.raises:
            raise self.raises
        payload = json.dumps({"reason": self.reason}).encode() if self.reason else b""
        return self.status, payload


def device(environment: PushEnvironment = PushEnvironment.PRODUCTION) -> PushDevice:
    return PushDevice(
        id=push_device_id(TOKEN), user_id="u1", token=TOKEN, environment=environment
    )


def provider(private_key: str, transport: FakeTransport) -> APNsPushProvider:
    return APNsPushProvider(
        key_id=KEY_ID, team_id=TEAM_ID, private_key=private_key,
        bundle_id=BUNDLE, transport=transport,
    )


MESSAGE = PushMessage(title="내 거울 '먹방거울'이 판매됐어요!", body="3조각을 받았어요.")


# MARK: - 자격 증명


def test_missing_credentials_send_nothing():
    """**하나라도 없으면 보내지 않는다.** 반쯤 설정된 채로 실패하는 것보다 낫다."""
    for missing in ("key_id", "team_id", "private_key", "bundle_id"):
        kwargs = {
            "key_id": KEY_ID, "team_id": TEAM_ID,
            "private_key": "k", "bundle_id": BUNDLE,
        }
        kwargs[missing] = ""
        assert not APNsPushProvider(**kwargs).is_available


def test_builder_falls_back_instead_of_raising():
    """자격 증명이 없다고 **앱이 죽지 않는다** — 알림만 안 간다."""
    built = build_push_provider(key_id="", team_id="", private_key="", bundle_id="")
    assert isinstance(built, NullPushProvider)
    assert not built.is_available


def test_builder_uses_apns_when_configured(private_key):
    built = build_push_provider(
        key_id=KEY_ID, team_id=TEAM_ID, private_key=private_key, bundle_id=BUNDLE
    )
    assert isinstance(built, APNsPushProvider)


# MARK: - provider token


def test_provider_token_is_es256_with_the_key_id(private_key):
    transport = FakeTransport()
    provider(private_key, transport).send(device(), MESSAGE)

    authorization = transport.calls[0][2]["authorization"]
    assert authorization.startswith("bearer ")
    token = authorization.removeprefix("bearer ")

    header = jwt.get_unverified_header(token)
    assert header["alg"] == "ES256"      # Apple이 요구하는 알고리즘
    assert header["kid"] == KEY_ID
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["iss"] == TEAM_ID
    assert "iat" in claims


def test_provider_token_is_reused(private_key):
    """**요청마다 새로 만들지 않는다** — Apple이 잦은 재발급을 거절한다."""
    transport = FakeTransport()
    push = provider(private_key, transport)
    push.send(device(), MESSAGE)
    push.send(device(), MESSAGE)

    assert transport.calls[0][2]["authorization"] == transport.calls[1][2]["authorization"]


# MARK: - 요청 모양


def test_topic_is_the_bundle_id(private_key):
    transport = FakeTransport()
    provider(private_key, transport).send(device(), MESSAGE)
    assert transport.calls[0][2]["apns-topic"] == BUNDLE


def test_push_type_is_alert(private_key):
    transport = FakeTransport()
    provider(private_key, transport).send(device(), MESSAGE)
    assert transport.calls[0][2]["apns-push-type"] == "alert"


@pytest.mark.parametrize(
    "environment,host",
    [
        (PushEnvironment.SANDBOX, "api.sandbox.push.apple.com"),
        (PushEnvironment.PRODUCTION, "api.push.apple.com"),
    ],
)
def test_host_follows_the_device_environment(private_key, environment, host):
    """**개발 빌드의 token을 production APNs로 보내지 않는다.**"""
    transport = FakeTransport()
    provider(private_key, transport).send(device(environment), MESSAGE)
    assert transport.calls[0][0] == host


def test_path_carries_the_token(private_key):
    transport = FakeTransport()
    provider(private_key, transport).send(device(), MESSAGE)
    assert transport.calls[0][1] == f"/3/device/{TOKEN}"


def test_payload_shape(private_key):
    transport = FakeTransport()
    provider(private_key, transport).send(device(), MESSAGE)

    payload = json.loads(transport.calls[0][3])
    assert payload["aps"]["alert"]["title"] == MESSAGE.title
    assert payload["aps"]["alert"]["body"] == MESSAGE.body
    assert payload["kind"] == "marketplace_sale"
    # 한글이 이스케이프되지 않는다 — 그대로 보낸다.
    assert "먹방거울" in transport.calls[0][3].decode()


def test_payload_has_no_buyer_or_wallet(private_key):
    transport = FakeTransport()
    provider(private_key, transport).send(device(), MESSAGE)

    body = transport.calls[0][3].decode()
    for banned in ("buyer", "userId", "balance", "transactionId", "email"):
        assert banned not in body


# MARK: - 응답 해석


def test_success(private_key):
    outcome = provider(private_key, FakeTransport(200)).send(device(), MESSAGE)
    assert outcome.delivered and not outcome.terminal


@pytest.mark.parametrize(
    "status,reason",
    [(410, "Unregistered"), (400, "BadDeviceToken"), (400, "DeviceTokenNotForTopic")],
)
def test_terminal_token_failures(private_key, status, reason):
    outcome = provider(private_key, FakeTransport(status, reason)).send(device(), MESSAGE)
    assert not outcome.delivered and outcome.terminal


@pytest.mark.parametrize(
    "status,reason",
    [(500, "InternalServerError"), (503, "ServiceUnavailable"),
     (429, "TooManyRequests"), (403, "ExpiredProviderToken"),
     (400, "PayloadTooLarge")],
)
def test_temporary_failures_are_not_terminal(private_key, status, reason):
    """**여기서 terminal이라고 하면 사용자의 기기 등록이 지워진다.**"""
    outcome = provider(private_key, FakeTransport(status, reason)).send(device(), MESSAGE)
    assert not outcome.delivered and not outcome.terminal


def test_network_failure_is_not_terminal(private_key):
    transport = FakeTransport()
    transport.raises = OSError("connection reset")
    outcome = provider(private_key, transport).send(device(), MESSAGE)
    assert not outcome.delivered and not outcome.terminal


def test_malformed_response_body_is_not_terminal(private_key):
    """Apple이 이상한 것을 보내도 등록을 지우지 않는다."""

    class Garbage(FakeTransport):
        def post(self, host, path, headers, body):
            return 410, b"<html>not json</html>"

    outcome = provider(private_key, Garbage()).send(device(), MESSAGE)
    assert not outcome.terminal


# MARK: - 로그


def test_logs_have_no_token_or_payload(private_key, caplog):
    transport = FakeTransport(410, "Unregistered")
    with caplog.at_level(logging.DEBUG):
        provider(private_key, transport).send(device(), MESSAGE)

    text = caplog.text
    assert TOKEN not in text
    assert private_key not in text
    assert "먹방거울" not in text      # payload를 찍지 않는다
    assert "bearer" not in text.lower()


def test_the_real_transport_is_blocked_here(private_key):
    """이음매를 잊으면 **조용히 통과하지 않고 여기서 터진다.**

    `conftest`가 막는 것은 `urllib`이고 APNs는 httpx를 쓴다. 그래서 이 파일이
    자기 몫의 그물을 하나 더 친다(아래 autouse fixture).
    """
    from app.push.provider import HTTPXAPNsTransport

    # transport를 주지 않으면 진짜 client를 만들려 하고, fixture가 그것을 막는다.
    default = APNsPushProvider(
        key_id=KEY_ID, team_id=TEAM_ID, private_key=private_key, bundle_id=BUNDLE
    )
    outcome = default.send(device(), MESSAGE)
    # 막혔으므로 보내지 못했고, **끝난 token으로 오해하지도 않았다.**
    assert not outcome.delivered and not outcome.terminal
    assert isinstance(default._transport, HTTPXAPNsTransport)
