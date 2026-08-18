"""Apple 공식 library로 StoreKit transaction JWS를 검증한다.

**x5c 인증서 체인 검증기를 직접 만들지 않는다.** 체인 · 만료 · 폐기 판단을 우리가 지면
틀렸을 때 조각이 공짜가 된다. Apple `app-store-server-library`의 `SignedDataVerifier`가
그 일을 한다(설치본 3.1.2 기준 API로 구현했다).

이 wrapper가 하는 일은 셋뿐이다:
1. 허용된 environment마다 verifier를 하나씩 준비 (없는 environment는 **아예 만들지 않는다**)
2. **서명 검증 전에 payload를 들여다보지 않고** 허용된 verifier들로 차례로 검증
3. 검증된 payload를 내부 모델로 옮기기

App Store Server **API**는 부르지 않는다 — 그래서 `.p8` · issuerId · keyId가 필요 없다.
JWS 검증에 필요한 것은 **공개된 Apple root certificate**뿐이다.
"""

from __future__ import annotations

import logging
from pathlib import Path

from appstoreserverlibrary.models.Environment import Environment
from appstoreserverlibrary.signed_data_verifier import (
    SignedDataVerifier,
    VerificationException,
    VerificationStatus,
)

from app.iap.models import (
    IAPEnvironment,
    IAPUnavailable,
    InvalidTransaction,
    VerifiedNotification,
    VerifiedTransaction,
)
from app.iap.verifier import TransactionVerifier, UnconfiguredVerifier

logger = logging.getLogger(__name__)

# Apple PKI의 공개 root certificate. **secret이 아니다** — 누구나 내려받는 값이다.
# 출처와 지문은 README에 적어 두었다. runtime에 외부 URL을 부르지 않는다 —
# 결제 검증이 남의 사이트 가용성에 묶이면 안 되고, 값이 조용히 바뀌어도 안 된다.
ROOTS_DIRECTORY = Path(__file__).parent / "certs"

# `IAP_ALLOWED_ENVIRONMENTS`에서 받아들이는 값 → library enum.
# **`Xcode` · `LocalTesting`은 여기 없다.** 로컬에서 만든 서명이라 Apple 신뢰 사슬이 없고,
# verifier를 만들 수 있게 두면 누구나 조각을 만들 수 있다.
_ENVIRONMENTS: dict[str, Environment] = {
    IAPEnvironment.PRODUCTION: Environment.PRODUCTION,
    IAPEnvironment.SANDBOX: Environment.SANDBOX,
}

# 검증 순서. 결과가 실행마다 달라지지 않게 고정한다.
_ORDER = (IAPEnvironment.PRODUCTION, IAPEnvironment.SANDBOX)

# OCSP 조회 실패처럼 **다시 해보면 될 수도 있는** 실패.
_RETRYABLE = frozenset({VerificationStatus.RETRYABLE_VERIFICATION_FAILURE})


def load_root_certificates(directory: Path = ROOTS_DIRECTORY) -> list[bytes]:
    """repo에 넣어 둔 DER root를 읽는다. 순서를 고정해 빌드가 결정적이게 한다."""
    return [path.read_bytes() for path in sorted(directory.glob("*.cer"))]


class AppleTransactionVerifier:
    """허용된 environment의 verifier들로만 검증한다."""

    def __init__(self, verifiers: dict[str, SignedDataVerifier]) -> None:
        self._verifiers = verifiers

    @property
    def is_configured(self) -> bool:
        return bool(self._verifiers)

    def verify(self, signed_transaction: str) -> VerifiedTransaction:
        """서명 검증을 통과한 payload만 돌려준다.

        **unverified payload를 먼저 decode해서 environment를 읽지 않는다.**
        그 값으로 verifier를 고르면 공격자가 `environment`를 바꿔 원하는 verifier로
        보낼 수 있고, 그건 검증을 client에게 맡기는 것과 같다.

        대신 **서버가 허용한 verifier들로 차례로 시도**한다. library가 payload의
        environment와 자기 environment를 대조하므로(`INVALID_ENVIRONMENT`),
        Production JWS는 Production verifier만 통과한다.
        """
        return _mapped(self._attempt("transaction", lambda v: v.verify_and_decode_signed_transaction(signed_transaction)))

    def verify_notification(self, signed_payload: str) -> VerifiedNotification:
        """App Store Server Notification V2. **transaction과 같은 규칙으로 검증한다.**

        서명 전에 `notificationType` · `environment` · `bundleId`를 읽어 verifier를
        고르지 않는다. 허용된 verifier로 차례로 시도하고 정확히 하나만 성공해야 한다.

        안쪽 `signedTransactionInfo`는 **따로 검증한다** — 바깥 JWS가 맞다고
        안쪽을 그대로 믿지 않는다.
        """
        payload = self._attempt(
            "notification", lambda v: v.verify_and_decode_notification(signed_payload)
        )
        data = payload.data
        transaction: VerifiedTransaction | None = None
        if data is not None and data.signedTransactionInfo:
            transaction = self.verify(data.signedTransactionInfo)

        notification_type = (
            payload.notificationType.value if payload.notificationType else payload.rawNotificationType
        )
        subtype = payload.subtype.value if payload.subtype else payload.rawSubtype
        environment = None
        if data is not None:
            environment = data.environment.value if data.environment else data.rawEnvironment

        return VerifiedNotification(
            notification_type=str(notification_type or ""),
            subtype=str(subtype or "") or None,
            notification_uuid=payload.notificationUUID or "",
            bundle_id=(data.bundleId if data else None) or "",
            app_apple_id=data.appAppleId if data else None,
            environment=str(environment or ""),
            transaction=transaction,
        )

    def _attempt(self, kind: str, decode):
        """허용된 verifier들로 차례로 시도한다. **정확히 하나만** 성공해야 한다."""
        if not self._verifiers:
            raise IAPUnavailable("no transaction verifier is configured")

        verified: list[tuple[str, object]] = []
        retryable = False

        for name in _ORDER:
            verifier = self._verifiers.get(name)
            if verifier is None:
                continue
            try:
                verified.append((name, decode(verifier)))
            except VerificationException as error:
                # 어떤 환경에서 왜 실패했는지는 **로그에만** 남긴다.
                # client에 알려주면 어느 값을 고치면 되는지 가르쳐 주는 셈이다.
                status = getattr(error, "status", None)
                if status in _RETRYABLE:
                    retryable = True
                logger.info(
                    "iap_verify_failed kind=%s environment=%s status=%s",
                    kind, name, getattr(status, "name", status),
                )
            except Exception as error:  # library가 올리는 형식 오류 등
                logger.info(
                    "iap_verify_failed kind=%s environment=%s error=%s",
                    kind, name, type(error).__name__,
                )

        if not verified:
            if retryable:
                # 인증서 폐기 조회가 안 됐다. **검증을 건너뛰지 않는다.**
                logger.warning("iap_verify_retryable_failure kind=%s", kind)
                raise IAPUnavailable("certificate verification could not be completed")
            raise InvalidTransaction("signature verification failed")

        if len(verified) > 1:
            # 일어나면 안 된다 — library가 environment를 대조하기 때문이다.
            logger.error("iap_verify_ambiguous kind=%s environments=%d", kind, len(verified))
            raise InvalidTransaction("verified in more than one environment")

        return verified[0][1]


def _mapped(payload) -> VerifiedTransaction:
    """검증된 payload → 내부 모델. **필요한 최소 값만** 옮긴다.

    enum이 파싱되지 않으면 `raw*` 문자열을 쓴다. 모르는 값을 `None`으로 만들어
    비교를 통과시키지 않고, 그대로 들고 가서 뒤 검사에서 거절되게 한다.
    """
    environment = payload.environment.value if payload.environment else payload.rawEnvironment
    transaction_type = payload.type.value if payload.type else payload.rawType

    if not payload.transactionId or not payload.productId or not payload.bundleId:
        # 서명은 맞는데 필수 field가 없다. 추측해서 채우지 않는다.
        raise InvalidTransaction("verified payload is missing required fields")

    return VerifiedTransaction(
        transaction_id=payload.transactionId,
        product_id=payload.productId,
        bundle_id=payload.bundleId,
        environment=str(environment or ""),
        app_account_token=payload.appAccountToken,
        transaction_type=str(transaction_type or ""),
        original_transaction_id=payload.originalTransactionId,
    )


def build_apple_verifier(
    *,
    bundle_id: str,
    allowed_environments: frozenset[str],
    app_apple_id: int | None,
    enable_online_checks: bool = True,
    roots: list[bytes] | None = None,
) -> TransactionVerifier:
    """허용된 environment의 verifier만 만든다. 하나도 못 만들면 **fail closed**다.

    `enable_online_checks`는 기본 **True**다 — 인증서 **폐기(OCSP)** 확인을 켠다.
    돈이 오가는 경로에서 폐기된 서명 인증서를 받아 주는 것이 더 위험하고,
    조회가 실패하면 지급하지 않고 멈춘다(우회 경로를 만들지 않는다).
    client가 아직 `finish()`하지 않았으므로 그 결제는 유실되지 않고 다시 온다.
    """
    if not bundle_id:
        logger.error("iap_verifier_unconfigured reason=missing_bundle_id")
        return UnconfiguredVerifier()

    certificates = roots if roots is not None else load_root_certificates()
    if not certificates:
        logger.error("iap_verifier_unconfigured reason=missing_root_certificates")
        return UnconfiguredVerifier()

    verifiers: dict[str, SignedDataVerifier] = {}
    for name in _ORDER:
        if name not in allowed_environments:
            continue

        # **Production은 appAppleId 없이 만들 수 없다**(library가 생성 시점에 거절한다).
        # 없다고 느슨하게 만들지 않는다 — Production만 조용히 꺼진다.
        if name == IAPEnvironment.PRODUCTION and app_apple_id is None:
            logger.error("iap_verifier_unconfigured reason=missing_app_apple_id environment=Production")
            continue

        verifiers[name] = SignedDataVerifier(
            certificates,
            enable_online_checks,
            _ENVIRONMENTS[name],
            bundle_id,
            app_apple_id if name == IAPEnvironment.PRODUCTION else None,
        )

    if not verifiers:
        logger.error("iap_verifier_unconfigured reason=no_allowed_environment")
        return UnconfiguredVerifier()

    logger.info(
        "iap_verifier_ready environments=%s online_checks=%s roots=%d",
        ",".join(sorted(verifiers)), enable_online_checks, len(certificates),
    )
    return AppleTransactionVerifier(verifiers)
