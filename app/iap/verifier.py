"""Apple signed transaction 검증 seam.

**B-6A는 자리만 만든다.** 실제 검증은 B-6B에서 Apple 공식
`app-store-server-library`의 `SignedDataVerifier`로 채운다 —
x5c 인증서 체인 검증기를 직접 만들지 않는다(체인 · 만료 · 폐기 판단을
우리가 지게 되고, 틀리면 조각이 공짜가 된다).

**production runtime에서 가짜 검증기를 켤 수 있는 설정을 만들지 않는다.**
`build_verifier`가 돌려주는 것은 실제 검증기 아니면 `UnconfiguredVerifier`뿐이다.
test는 생성자 주입으로만 가짜를 넣는다.
"""

from __future__ import annotations

import logging
from typing import Protocol

from app.iap.models import IAPUnavailable, VerifiedTransaction

logger = logging.getLogger(__name__)


class TransactionVerifier(Protocol):
    """서명된 JWS → 검증된 transaction.

    구현은 **서명을 통과한 값만** 돌려준다. 실패는 예외이고, 부분 결과를 만들지 않는다.
    """

    @property
    def is_configured(self) -> bool: ...

    def verify(self, signed_transaction: str) -> VerifiedTransaction:
        """서명 · 인증서 체인을 검증하고 payload를 돌려준다.

        실패하면 `InvalidTransaction`. **절대 검증 실패를 성공으로 바꾸지 않는다.**
        """


class UnconfiguredVerifier:
    """아직 설정되지 않았다. **fail closed** — 검증 없이 지급하지 않는다.

    A-1A provider · B-5 ad unit과 같은 규칙이다: 서비스는 뜨고 IAP만 조용히 꺼진다.
    """

    is_configured = False

    def verify(self, signed_transaction: str) -> VerifiedTransaction:
        raise IAPUnavailable("transaction verifier is not configured")


def build_verifier() -> TransactionVerifier:
    """production 검증기. **B-6B에서 채운다.**

    지금은 항상 fail closed다 — 추측한 검증 로직으로 조각을 지급하지 않는다.
    """
    logger.info("iap_verifier_unconfigured")
    return UnconfiguredVerifier()
