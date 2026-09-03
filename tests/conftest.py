"""Apple을 흉내 내는 test key / token 만들기.

실제 Apple private key는 존재하지 않는다. **실제 token / credential을 repo에 넣지 않는다.**
여기서 만드는 RSA key는 매 test 실행마다 새로 생성되고 test scope에만 존재한다.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth.apple import APPLE_ISSUER
from tests.adc_guard import blocked

CLIENT_ID = "com.mark77234.ggumirror"


@pytest.fixture(autouse=True)
def no_google_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """**Google ADC lookup을 금지한다.** store를 주입하지 않은 test를 여기서 잡는다.

    `create_app`은 store를 안 주면 Firestore로 fallback한다(production 기본값이 그래야
    한다). 개발 기기에는 `gcloud` ADC가 있으니 그 fallback이 **조용히 성공**했고,
    주입을 빠뜨린 test가 실제 Firestore를 붙잡은 채로 초록이었다. GitHub Actions에는
    ADC가 없어서 같은 코드가 `DefaultCredentialsError` → 503 연쇄로 무너졌다.

    이제 두 환경이 같은 것을 본다. 이 fixture가 터지면 고칠 곳은 production 코드가
    아니라 **그 test의 `create_app(...)` 인자**다 — 빠진 store를 in-memory fake로 준다.

    개발 기기의 ADC가 가리키는 project는 꾸미러가 아닐 수도 있다. test가 그쪽
    Firestore로 나가는 것을 막는 것 자체가 이 guard의 두 번째 이유다.
    """
    monkeypatch.setattr("google.auth.default", blocked)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """test에서 Apple(또는 어떤 host든)로 나가는 HTTP를 막는다.

    unit test는 인터넷 없이 통과해야 한다. 실수로 실제 endpoint를 부르는 test가
    들어오면 통과하는 게 아니라 여기서 실패한다.
    """

    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("unit test tried to open a network connection")

    monkeypatch.setattr("urllib.request.urlopen", blocked)


class FakeAppleKey:
    """Apple key 하나. JWKS 항목과 그 key로 서명한 token을 만든다."""

    def __init__(self, kid: str) -> None:
        self.kid = kid
        # 2048bit. test에서 4096은 그냥 느리다.
        self._private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    @property
    def jwk(self) -> dict[str, Any]:
        document = jwt.algorithms.RSAAlgorithm.to_jwk(self._private.public_key(), as_dict=True)
        return {**document, "kid": self.kid, "alg": "RS256", "use": "sig"}

    def token(self, claims: dict[str, Any], headers: dict[str, Any] | None = None) -> str:
        return jwt.encode(
            claims,
            self._private,
            algorithm="RS256",
            headers={"kid": self.kid, **(headers or {})},
        )


def apple_claims(**overrides: Any) -> dict[str, Any]:
    """Apple identity token이 실제로 담는 claim 모양."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": APPLE_ISSUER,
        "aud": CLIENT_ID,
        "sub": "001234.abcdef0123456789.1234",
        "iat": now,
        "exp": now + 600,
        "email": "someone@privaterelay.appleid.com",
        "email_verified": "true",
        "is_private_email": "true",
    }
    claims.update(overrides)
    return {key: value for key, value in claims.items() if value is not None}


@pytest.fixture
def apple_key() -> FakeAppleKey:
    return FakeAppleKey("apple-key-1")


@pytest.fixture
def jwks_of() -> Callable[..., dict[str, Any]]:
    def build(*keys: FakeAppleKey) -> dict[str, Any]:
        return {"keys": [key.jwk for key in keys]}

    return build
