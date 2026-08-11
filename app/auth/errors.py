"""Apple token 검증 실패 분류.

내부적으로는 구분한다 — 무엇이 잘못됐는지 모르면 운영에서 아무것도 못 한다.
외부 응답에는 이 reason을 그대로 내보내지 않는다: endpoint(B-2B)는
`jwks_unavailable`만 503으로, 나머지는 전부 401 + 일반 메시지로 바꾼다.
공격자에게 "audience는 맞았고 signature만 틀렸다"고 알려줄 이유가 없다.
"""

from __future__ import annotations

from enum import StrEnum


class AppleTokenReason(StrEnum):
    MALFORMED_TOKEN = "malformed_token"
    UNSUPPORTED_ALGORITHM = "unsupported_algorithm"
    UNKNOWN_KID = "unknown_kid"
    JWKS_UNAVAILABLE = "jwks_unavailable"
    INVALID_SIGNATURE = "invalid_signature"
    INVALID_ISSUER = "invalid_issuer"
    INVALID_AUDIENCE = "invalid_audience"
    EXPIRED_TOKEN = "expired_token"
    INVALID_ISSUED_AT = "invalid_issued_at"
    NONCE_MISMATCH = "nonce_mismatch"
    MISSING_CLAIM = "missing_claim"


class AppleTokenError(Exception):
    """검증 실패. message에 token / claim 값을 담지 않는다 — 로그로 새어 나간다."""

    def __init__(self, reason: AppleTokenReason) -> None:
        super().__init__(reason.value)
        self.reason = reason

    @property
    def is_upstream_failure(self) -> bool:
        """우리 잘못도 client 잘못도 아닌 경우. B-2B에서 503으로 매핑한다."""
        return self.reason is AppleTokenReason.JWKS_UNAVAILABLE
