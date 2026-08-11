"""Apple identity token 검증 test.

**Apple을 실제로 호출하는 test는 없다.** 인터넷 없이 전부 통과해야 한다.
JWKS는 fixture로 주입하고, key는 test 실행 중에 생성한다.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
from typing import Any

import jwt
import pytest

from app.auth.apple import AppleTokenVerifier, VerifiedAppleIdentity
from app.auth.errors import AppleTokenError, AppleTokenReason
from app.auth.jwks import AppleJWKSProvider
from tests.conftest import CLIENT_ID, FakeAppleKey, apple_claims


# HS256 바꿔치기 test용. 길이 경고만 피하려고 32byte를 쓴다.
_HMAC_SECRET = "x" * 32


class Clock:
    """test용 시계. 실제 시간을 기다리지 않는다."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class Fetcher:
    """JWKS fetch를 대신한다. 호출 횟수를 센다."""

    def __init__(self, document: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.document = document
        self.error = error
        self.count = 0

    def __call__(self) -> dict[str, Any]:
        self.count += 1
        if self.error is not None:
            raise self.error
        assert self.document is not None
        return self.document


def verifier_for(fetcher: Fetcher, clock: Clock | None = None, client_id: str = CLIENT_ID) -> AppleTokenVerifier:
    clock = clock or Clock()
    return AppleTokenVerifier(
        client_id=client_id,
        jwks=AppleJWKSProvider(fetch=fetcher, ttl_seconds=600.0, min_refresh_interval=60.0, now=clock),
    )


def rejects(verifier: AppleTokenVerifier, token: str, reason: AppleTokenReason, **kwargs: Any) -> None:
    with pytest.raises(AppleTokenError) as error:
        verifier.verify(token, **kwargs)
    assert error.value.reason is reason


# MARK: - 통과하는 경우


def test_valid_token_verifies(apple_key, jwks_of):
    verifier = verifier_for(Fetcher(jwks_of(apple_key)))
    identity = verifier.verify(apple_key.token(apple_claims()))
    assert isinstance(identity, VerifiedAppleIdentity)


def test_returns_subject(apple_key, jwks_of):
    verifier = verifier_for(Fetcher(jwks_of(apple_key)))
    identity = verifier.verify(apple_key.token(apple_claims(sub="001999.deadbeef.4321")))
    assert identity.subject == "001999.deadbeef.4321"


def test_maps_email_claims(apple_key, jwks_of):
    """email은 식별자가 아니라 부가 정보다. Apple의 문자열 bool도 받는다."""
    verifier = verifier_for(Fetcher(jwks_of(apple_key)))
    identity = verifier.verify(apple_key.token(apple_claims()))
    assert identity.email == "someone@privaterelay.appleid.com"
    assert identity.is_email_verified is True
    assert identity.is_private_email is True


def test_email_claims_optional(apple_key, jwks_of):
    """두 번째 로그인부터 Apple은 email을 주지 않는다. 그래도 검증은 통과해야 한다."""
    verifier = verifier_for(Fetcher(jwks_of(apple_key)))
    claims = apple_claims(email=None, email_verified=None, is_private_email=None)
    identity = verifier.verify(apple_key.token(claims))
    assert (identity.email, identity.is_email_verified, identity.is_private_email) == (None, None, None)


def test_apple_issuer_accepted(apple_key, jwks_of):
    verifier = verifier_for(Fetcher(jwks_of(apple_key)))
    token = apple_key.token(apple_claims(iss="https://appleid.apple.com"))
    assert verifier.verify(token).subject


def test_configured_audience_accepted(apple_key, jwks_of):
    verifier = verifier_for(Fetcher(jwks_of(apple_key)), client_id="com.example.other")
    assert verifier.verify(apple_key.token(apple_claims(aud="com.example.other"))).subject


def test_selects_key_matching_kid(apple_key, jwks_of):
    """여러 key가 게시돼 있어도 kid가 맞는 key로만 검증한다. 첫 key를 쓰지 않는다."""
    other = FakeAppleKey("apple-key-0")
    third = FakeAppleKey("apple-key-2")
    verifier = verifier_for(Fetcher(jwks_of(other, apple_key, third)))
    assert verifier.verify(apple_key.token(apple_claims())).subject


# MARK: - 거부하는 경우


def test_wrong_issuer_rejected(apple_key, jwks_of):
    verifier = verifier_for(Fetcher(jwks_of(apple_key)))
    token = apple_key.token(apple_claims(iss="https://evil.example.com"))
    rejects(verifier, token, AppleTokenReason.INVALID_ISSUER)


def test_wrong_audience_rejected(apple_key, jwks_of):
    """다른 앱의 Apple token으로 우리 서버에 들어올 수 없어야 한다."""
    verifier = verifier_for(Fetcher(jwks_of(apple_key)))
    token = apple_key.token(apple_claims(aud="com.someone.else"))
    rejects(verifier, token, AppleTokenReason.INVALID_AUDIENCE)


def test_expired_token_rejected(apple_key, jwks_of):
    verifier = verifier_for(Fetcher(jwks_of(apple_key)))
    past = int(time.time()) - 3600
    token = apple_key.token(apple_claims(iat=past, exp=past + 600))
    rejects(verifier, token, AppleTokenReason.EXPIRED_TOKEN)


def test_invalid_signature_rejected(apple_key, jwks_of):
    """같은 kid를 주장하지만 다른 key로 서명한 token."""
    impostor = FakeAppleKey(apple_key.kid)
    verifier = verifier_for(Fetcher(jwks_of(apple_key)))
    rejects(verifier, impostor.token(apple_claims()), AppleTokenReason.INVALID_SIGNATURE)


def test_malformed_token_rejected(apple_key, jwks_of):
    verifier = verifier_for(Fetcher(jwks_of(apple_key)))
    for token in ("", "not-a-jwt", "aaa.bbb.ccc", "eyJhbGciOiJSUzI1NiJ9.only-two-parts"):
        rejects(verifier, token, AppleTokenReason.MALFORMED_TOKEN)


def test_unsigned_token_rejected(apple_key, jwks_of):
    """`alg: none`. 절대 통과하면 안 된다."""

    def segment(payload: dict[str, Any]) -> str:
        raw = json.dumps(payload).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    unsigned = f"{segment({'alg': 'none', 'kid': apple_key.kid})}.{segment(apple_claims())}."
    verifier = verifier_for(Fetcher(jwks_of(apple_key)))
    rejects(verifier, unsigned, AppleTokenReason.UNSUPPORTED_ALGORITHM)


def test_unsupported_algorithm_rejected(apple_key, jwks_of):
    """HS256으로 바꿔치기한 token (key confusion)."""
    token = jwt.encode(apple_claims(), _HMAC_SECRET, algorithm="HS256", headers={"kid": apple_key.kid})
    verifier = verifier_for(Fetcher(jwks_of(apple_key)))
    rejects(verifier, token, AppleTokenReason.UNSUPPORTED_ALGORITHM)


def test_missing_kid_rejected(apple_key, jwks_of):
    token = jwt.encode(apple_claims(), _HMAC_SECRET, algorithm="HS256")
    verifier = verifier_for(Fetcher(jwks_of(apple_key)))
    rejects(verifier, token, AppleTokenReason.UNSUPPORTED_ALGORITHM)


def test_missing_subject_rejected(apple_key, jwks_of):
    verifier = verifier_for(Fetcher(jwks_of(apple_key)))
    rejects(verifier, apple_key.token(apple_claims(sub=None)), AppleTokenReason.MISSING_CLAIM)


def test_missing_expiration_rejected(apple_key, jwks_of):
    verifier = verifier_for(Fetcher(jwks_of(apple_key)))
    rejects(verifier, apple_key.token(apple_claims(exp=None)), AppleTokenReason.MISSING_CLAIM)


def test_missing_issued_at_rejected(apple_key, jwks_of):
    verifier = verifier_for(Fetcher(jwks_of(apple_key)))
    rejects(verifier, apple_key.token(apple_claims(iat=None)), AppleTokenReason.MISSING_CLAIM)


def test_empty_client_id_refused():
    """audience 없이 검증하면 아무 token이나 통과한다. 만들 때 막는다."""
    with pytest.raises(ValueError, match="client_id"):
        AppleTokenVerifier(client_id="   ")


# MARK: - JWKS / cache


def test_unknown_kid_triggers_one_refresh(apple_key, jwks_of):
    """rotation 직후일 수 있으니 한 번은 다시 받아 본다."""
    rotated = FakeAppleKey("apple-key-rotated")
    fetcher = Fetcher(jwks_of(apple_key))
    verifier = verifier_for(fetcher)

    # 첫 검증으로 cache를 채운다.
    verifier.verify(apple_key.token(apple_claims()))
    assert fetcher.count == 1

    # Apple이 새 key를 게시했다.
    fetcher.document = jwks_of(apple_key, rotated)
    assert verifier.verify(rotated.token(apple_claims())).subject
    assert fetcher.count == 2


def test_still_unknown_kid_rejected(apple_key, jwks_of):
    stranger = FakeAppleKey("who-is-this")
    fetcher = Fetcher(jwks_of(apple_key))
    verifier = verifier_for(fetcher)
    rejects(verifier, stranger.token(apple_claims()), AppleTokenReason.UNKNOWN_KID)


def test_unknown_kid_burst_does_not_hammer_apple(apple_key, jwks_of):
    """임의 kid를 넣은 요청이 그대로 Apple 호출로 증폭되지 않는다."""
    fetcher = Fetcher(jwks_of(apple_key))
    verifier = verifier_for(fetcher)
    for index in range(5):
        stranger = FakeAppleKey(f"stranger-{index}")
        rejects(verifier, stranger.token(apple_claims()), AppleTokenReason.UNKNOWN_KID)
    # 최초 load 1회 + rotation 확인 1회.
    assert fetcher.count == 2


def test_cache_prevents_repeat_fetch(apple_key, jwks_of):
    fetcher = Fetcher(jwks_of(apple_key))
    verifier = verifier_for(fetcher)
    for _ in range(3):
        verifier.verify(apple_key.token(apple_claims()))
    assert fetcher.count == 1


def test_cache_expiry_refreshes(apple_key, jwks_of):
    clock = Clock()
    fetcher = Fetcher(jwks_of(apple_key))
    verifier = verifier_for(fetcher, clock=clock)

    verifier.verify(apple_key.token(apple_claims()))
    assert fetcher.count == 1

    clock.now += 601  # TTL 만료
    verifier.verify(apple_key.token(apple_claims()))
    assert fetcher.count == 2


def test_jwks_network_failure_handled(apple_key):
    """network가 없어도 crash하지 않는다."""
    fetcher = Fetcher(error=urllib.error.URLError("no route to host"))
    verifier = verifier_for(fetcher)
    rejects(verifier, apple_key.token(apple_claims()), AppleTokenReason.JWKS_UNAVAILABLE)


def test_jwks_timeout_handled(apple_key):
    fetcher = Fetcher(error=TimeoutError("timed out"))
    verifier = verifier_for(fetcher)
    rejects(verifier, apple_key.token(apple_claims()), AppleTokenReason.JWKS_UNAVAILABLE)


def test_jwks_failure_falls_back_to_cache(apple_key, jwks_of):
    """Apple이 잠깐 죽어도 이미 받아 둔 key로 계속 검증한다."""
    clock = Clock()
    fetcher = Fetcher(jwks_of(apple_key))
    verifier = verifier_for(fetcher, clock=clock)
    verifier.verify(apple_key.token(apple_claims()))

    fetcher.error = urllib.error.URLError("apple is down")
    clock.now += 601
    assert verifier.verify(apple_key.token(apple_claims())).subject


def test_malformed_jwks_handled(apple_key):
    fetcher = Fetcher({"keys": [{"kty": "nonsense"}]})
    verifier = verifier_for(fetcher)
    rejects(verifier, apple_key.token(apple_claims()), AppleTokenReason.JWKS_UNAVAILABLE)


# MARK: - nonce
#
# 현재 iOS client는 nonce를 보내지 않는다(README 참고).
# 검증하지 않은 것을 했다고 하지 않기 위해, expected_nonce를 준 경우에만 검증한다.


def test_nonce_match_accepted(apple_key, jwks_of):
    verifier = verifier_for(Fetcher(jwks_of(apple_key)))
    token = apple_key.token(apple_claims(nonce="abc123"))
    assert verifier.verify(token, expected_nonce="abc123").subject


def test_nonce_mismatch_rejected(apple_key, jwks_of):
    verifier = verifier_for(Fetcher(jwks_of(apple_key)))
    token = apple_key.token(apple_claims(nonce="abc123"))
    rejects(verifier, token, AppleTokenReason.NONCE_MISMATCH, expected_nonce="something-else")


def test_nonce_required_but_missing_rejected(apple_key, jwks_of):
    verifier = verifier_for(Fetcher(jwks_of(apple_key)))
    rejects(verifier, apple_key.token(apple_claims()), AppleTokenReason.NONCE_MISMATCH, expected_nonce="abc123")


def test_nonce_not_checked_when_not_expected(apple_key, jwks_of):
    """client가 nonce를 쓰지 않는 지금, token에 nonce가 있어도 없어도 통과한다."""
    verifier = verifier_for(Fetcher(jwks_of(apple_key)))
    assert verifier.verify(apple_key.token(apple_claims(nonce="stale"))).subject


# MARK: - 로그


def test_logs_never_contain_credentials(apple_key, jwks_of, caplog):
    subject = "001777.secretsubject.9999"
    email = "leaky@privaterelay.appleid.com"
    token = apple_key.token(apple_claims(sub=subject, email=email))
    verifier = verifier_for(Fetcher(jwks_of(apple_key)))

    with caplog.at_level(logging.DEBUG):
        verifier.verify(token)
        rejects(verifier, apple_key.token(apple_claims(aud="nope")), AppleTokenReason.INVALID_AUDIENCE)

    logged = caplog.text
    assert "apple_token_verified" in logged
    assert "invalid_audience" in logged
    for secret in (token, subject, email, token.split(".")[2]):
        assert secret not in logged
