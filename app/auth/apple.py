"""Apple identity token 검증.

암호 알고리즘을 직접 만들지 않는다. PyJWT + cryptography가 signature / claim 검증을 한다.

이 module이 하는 일은 검증 정책을 명시하는 것뿐이다:
어떤 alg만 허용하는지, 어떤 claim을 반드시 요구하는지, 실패를 어떻게 분류하는지.

이 Phase에는 endpoint가 없다. `/auth/apple`도, verify-test용 debug endpoint도 만들지 않는다 —
debug endpoint는 결국 production에 남아 검증 없는 우회로가 된다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from hmac import compare_digest

import jwt

from app.auth.errors import AppleTokenError, AppleTokenReason
from app.auth.jwks import AppleJWKSProvider

logger = logging.getLogger(__name__)

APPLE_ISSUER = "https://appleid.apple.com"

# Apple identity token은 RS256이다. token이 주장하는 alg를 그대로 믿지 않는다.
# 목록으로 제한하면 `none`(unsigned)과 HS256 key confusion이 함께 막힌다.
ALLOWED_ALGORITHMS = ("RS256",)

# client와 server 시계 차이. 이 이상 늘리면 만료된 token이 살아난다.
CLOCK_LEEWAY_SECONDS = 30

REQUIRED_CLAIMS = ("iss", "aud", "exp", "iat", "sub")


@dataclass(frozen=True)
class VerifiedAppleIdentity:
    """검증을 통과한 Apple identity. **raw JWT는 이 경계를 넘지 않는다.**

    `subject`는 Apple identity provider의 opaque subject일 뿐 꾸미러 user ID가 아니다.
    꾸미러 user ID(internal UUID)는 B-2B에서 만들고, 이 subject는 mapping의 lookup key로만 쓴다.
    Apple subject를 public API user identifier로 노출하지 않는다.

    email은 **식별자가 아니다.** Apple이 첫 로그인에만 주고, private relay면 바뀔 수 있고,
    사용자가 계정 email을 바꿀 수도 있다. 식별은 언제나 `subject`로 한다.
    """

    subject: str
    email: str | None = None
    is_email_verified: bool | None = None
    is_private_email: bool | None = None


class AppleTokenVerifier:
    def __init__(self, client_id: str, jwks: AppleJWKSProvider | None = None) -> None:
        # audience가 비어 있으면 사실상 검증이 없는 것과 같다. 여기서 막는다.
        if not client_id.strip():
            raise ValueError("client_id is required to verify Apple identity tokens")
        self._client_id = client_id.strip()
        self._jwks = jwks or AppleJWKSProvider()

    def verify(self, identity_token: str, expected_nonce: str | None = None) -> VerifiedAppleIdentity:
        """검증 성공 → identity, 실패 → `AppleTokenError`.

        `expected_nonce`를 주면 token의 nonce claim과 일치해야 한다.
        주지 않으면 nonce를 검증하지 않는다 — 검증하지 않은 것을 했다고 하지 않는다.
        (현재 iOS client는 nonce를 보내지 않는다. README의 Client nonce 참고.)
        """
        key = self._jwks.key_for(self._kid(identity_token))

        try:
            claims = jwt.decode(
                identity_token,
                key=key,
                algorithms=list(ALLOWED_ALGORITHMS),
                audience=self._client_id,
                issuer=APPLE_ISSUER,
                leeway=CLOCK_LEEWAY_SECONDS,
                options={"require": list(REQUIRED_CLAIMS)},
            )
        except jwt.ExpiredSignatureError as error:
            raise self._fail(AppleTokenReason.EXPIRED_TOKEN) from error
        except jwt.InvalidAudienceError as error:
            raise self._fail(AppleTokenReason.INVALID_AUDIENCE) from error
        except jwt.InvalidIssuerError as error:
            raise self._fail(AppleTokenReason.INVALID_ISSUER) from error
        except jwt.InvalidSignatureError as error:
            raise self._fail(AppleTokenReason.INVALID_SIGNATURE) from error
        except jwt.MissingRequiredClaimError as error:
            raise self._fail(AppleTokenReason.MISSING_CLAIM) from error
        except jwt.InvalidAlgorithmError as error:
            raise self._fail(AppleTokenReason.UNSUPPORTED_ALGORITHM) from error
        except jwt.ImmatureSignatureError as error:
            raise self._fail(AppleTokenReason.INVALID_ISSUED_AT) from error
        except jwt.InvalidTokenError as error:
            # DecodeError를 포함한 나머지. 상세를 message로 흘리지 않는다.
            raise self._fail(AppleTokenReason.MALFORMED_TOKEN) from error

        identity = self._identity(claims, expected_nonce)
        logger.info("apple_token_verified")
        return identity

    # MARK: - 내부

    def _kid(self, identity_token: str) -> str:
        try:
            header = jwt.get_unverified_header(identity_token)
        except jwt.InvalidTokenError as error:
            raise self._fail(AppleTokenReason.MALFORMED_TOKEN) from error

        # header를 믿어서 alg를 고르는 게 아니라, 허용 목록에 없으면 그 자리에서 끝낸다.
        # `alg: none`이 여기서 걸러진다.
        if header.get("alg") not in ALLOWED_ALGORITHMS:
            raise self._fail(AppleTokenReason.UNSUPPORTED_ALGORITHM)

        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise self._fail(AppleTokenReason.MALFORMED_TOKEN)
        return kid

    def _identity(self, claims: dict, expected_nonce: str | None) -> VerifiedAppleIdentity:
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise self._fail(AppleTokenReason.MISSING_CLAIM)

        # 미래에서 발급된 token은 시계 문제이거나 조작이다. exp만으로는 안 걸린다.
        issued_at = claims.get("iat")
        if not isinstance(issued_at, (int, float)):
            raise self._fail(AppleTokenReason.INVALID_ISSUED_AT)

        if expected_nonce is not None:
            # 없거나 다르면 둘 다 실패다. 비교는 상수 시간으로 한다.
            nonce = claims.get("nonce")
            if not isinstance(nonce, str) or not _constant_time_equals(nonce, expected_nonce):
                raise self._fail(AppleTokenReason.NONCE_MISMATCH)

        return VerifiedAppleIdentity(
            subject=subject,
            email=_optional_str(claims.get("email")),
            is_email_verified=_optional_bool(claims.get("email_verified")),
            is_private_email=_optional_bool(claims.get("is_private_email")),
        )

    def _fail(self, reason: AppleTokenReason) -> AppleTokenError:
        # 분류만 남긴다. token / subject / email / claim 값은 로그에 넣지 않는다.
        logger.warning("apple_token_rejected reason=%s", reason.value)
        return AppleTokenError(reason)


def _constant_time_equals(left: str, right: str) -> bool:
    return compare_digest(left.encode(), right.encode())


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_bool(value: object) -> bool | None:
    """Apple은 email_verified를 bool 또는 "true"/"false" 문자열로 보낸다."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value in ("true", "false"):
        return value == "true"
    return None
