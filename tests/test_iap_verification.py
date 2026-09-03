"""Apple JWS 검증 wrapper (B-6B).

**Apple production key가 필요 없다.** Apple이 쓰는 것과 같은 *모양*의 체인
(root → intermediate → leaf, 각 단계에 Apple이 요구하는 OID)을 테스트에서 직접 만들고,
그 root를 신뢰 목록으로 넣어 검증기를 돌린다. 그래서 **실제 서명 검증 경로**가
성공/실패 계약을 지키는지 확인할 수 있다.

`app-store-server-library` 3.1.2가 요구하는 것(소스에서 확인):
- 체인 길이 정확히 3
- leaf에 OID `1.2.840.113635.100.6.11.1`
- intermediate에 OID `1.2.840.113635.100.6.2.1`
- `X509_STRICT` — SKI/AKI + CA keyUsage 필요
- alg는 `ES256`만
"""

from __future__ import annotations

import base64
import datetime
import logging
from pathlib import Path

import jwt
import pytest
from appstoreserverlibrary.signed_data_verifier import VerificationException, VerificationStatus
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.iap.apple_verifier import (
    AppleTransactionVerifier,
    build_apple_verifier,
    load_root_certificates,
)
from app.iap.models import IAPEnvironment, IAPUnavailable, InvalidTransaction

BUNDLE_ID = "com.mark77234.ggumirror"
USER = "063cd7cb-fd94-4055-b6d8-2e4866879ed9"
PRODUCT = "com.mark77234.ggumirror.shards.10"
APP_APPLE_ID = 1234567890
TRANSACTION_ID = "2000000123456789"

LEAF_OID = "1.2.840.113635.100.6.11.1"
INTERMEDIATE_OID = "1.2.840.113635.100.6.2.1"


# MARK: - Apple 모양의 테스트 체인


def _key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _cert(subject, issuer_name, issuer_key, public_key, *, ca: bool, oid: str | None = None):
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
        .issuer_name(issuer_name)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if ca:
        # X509_STRICT는 CA에 keyUsage를 요구한다.
        builder = builder.add_extension(
            x509.KeyUsage(
                digital_signature=False, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
    builder = builder.add_extension(
        x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False
    ).add_extension(
        x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), critical=False
    )
    if oid:
        builder = builder.add_extension(
            x509.UnrecognizedExtension(x509.ObjectIdentifier(oid), b""), critical=False
        )
    return builder.sign(issuer_key, hashes.SHA256())


class Chain:
    """테스트용 서명 체인. Apple 것을 흉내 낸 *모양*이고 Apple 키가 아니다."""

    def __init__(self) -> None:
        root_key, intermediate_key, self.leaf_key = _key(), _key(), _key()
        root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Root")])
        self.root = _cert("Test Root", root_name, root_key, root_key.public_key(), ca=True)
        self.intermediate = _cert(
            "Test Intermediate", self.root.subject, root_key,
            intermediate_key.public_key(), ca=True, oid=INTERMEDIATE_OID,
        )
        self.leaf = _cert(
            "Test Leaf", self.intermediate.subject, intermediate_key,
            self.leaf_key.public_key(), ca=False, oid=LEAF_OID,
        )

    @property
    def root_der(self) -> bytes:
        return self.root.public_bytes(serialization.Encoding.DER)

    def _x5c(self) -> list[str]:
        return [
            base64.b64encode(c.public_bytes(serialization.Encoding.DER)).decode()
            for c in (self.leaf, self.intermediate, self.root)
        ]

    def sign(self, **overrides) -> str:
        payload = {
            "transactionId": TRANSACTION_ID,
            "originalTransactionId": TRANSACTION_ID,
            "bundleId": BUNDLE_ID,
            "productId": PRODUCT,
            "type": "Consumable",
            "environment": IAPEnvironment.SANDBOX.value,
            "appAccountToken": USER,
            "purchaseDate": 1700000000000,
            "quantity": 1,
            "signedDate": int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000),
        }
        payload.update(overrides)
        pem = self.leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        return jwt.encode(payload, pem, algorithm="ES256", headers={"x5c": self._x5c()})


@pytest.fixture
def chain() -> Chain:
    return Chain()


def verifier(chain: Chain, environments: str = "Sandbox", app_apple_id: int | None = APP_APPLE_ID):
    from app.iap.models import parse_allowed_environments

    return build_apple_verifier(
        bundle_id=BUNDLE_ID,
        allowed_environments=parse_allowed_environments(environments),
        app_apple_id=app_apple_id,
        # 합성 인증서에는 OCSP responder가 없다. 온라인 검사 자체는 아래에서 따로 고정한다.
        enable_online_checks=False,
        roots=[chain.root_der],
    )


# MARK: - 성공 경로


def test_valid_transaction_is_verified_and_mapped(chain):
    result = verifier(chain).verify(chain.sign())

    assert result.transaction_id == TRANSACTION_ID
    assert result.original_transaction_id == TRANSACTION_ID
    assert result.product_id == PRODUCT
    assert result.bundle_id == BUNDLE_ID
    # 내부 모델은 문자열을 쓴다 — B-6A 검사들이 그 값으로 비교한다.
    assert result.environment == "Sandbox"
    assert result.transaction_type == "Consumable"
    assert result.app_account_token == USER
    assert result.shard_amount == 10


def test_verifier_is_configured_when_built(chain):
    assert verifier(chain).is_configured is True


# MARK: - 서명 / 체인 거절


def test_tampered_payload_is_rejected(chain):
    token = chain.sign()
    header, payload, signature = token.split(".")
    forged = base64.urlsafe_b64encode(b'{"productId":"free"}').decode().rstrip("=")

    with pytest.raises(InvalidTransaction):
        verifier(chain).verify(f"{header}.{forged}.{signature}")


def test_signature_from_untrusted_chain_is_rejected(chain):
    """다른 root로 만든 체인은 통과하지 못한다."""
    other = Chain()
    with pytest.raises(InvalidTransaction):
        verifier(chain).verify(other.sign())


def test_malformed_jws_is_rejected(chain):
    for bad in ["", "not-a-jws", "a.b.c", "eyJhbGciOiJFUzI1NiJ9.e30.", "..."]:
        with pytest.raises((InvalidTransaction, IAPUnavailable)):
            verifier(chain).verify(bad)


def test_unsigned_token_is_rejected(chain):
    """`alg=none`으로 서명을 지운 token."""
    unsigned = jwt.encode({"transactionId": TRANSACTION_ID}, key="", algorithm="none")
    with pytest.raises(InvalidTransaction):
        verifier(chain).verify(unsigned)


def test_wrong_bundle_id_is_rejected(chain):
    with pytest.raises(InvalidTransaction):
        verifier(chain).verify(chain.sign(bundleId="com.someone.else"))


def test_payload_missing_required_fields_is_rejected(chain):
    with pytest.raises(InvalidTransaction):
        verifier(chain).verify(chain.sign(transactionId=None))
    with pytest.raises(InvalidTransaction):
        verifier(chain).verify(chain.sign(productId=None))


# MARK: - environment routing (security 요구사항)


def test_environment_claim_does_not_choose_the_verifier(chain):
    """★ unverified payload의 `environment`로 verifier를 고르지 않는다.

    Sandbox만 허용된 서버에 `environment=Production`이라고 적힌 JWS를 보내도
    통과하지 못한다 — library가 자기 environment와 payload를 대조한다.
    """
    with pytest.raises(InvalidTransaction):
        verifier(chain, environments="Sandbox").verify(
            chain.sign(environment=IAPEnvironment.PRODUCTION.value)
        )


def test_sandbox_transaction_rejected_when_only_production_allowed(chain):
    with pytest.raises(InvalidTransaction):
        verifier(chain, environments="Production").verify(chain.sign())


def test_exactly_one_verifier_succeeds_when_both_allowed(chain):
    """둘 다 허용이어도 Sandbox JWS는 Sandbox verifier 하나만 통과한다."""
    result = verifier(chain, environments="Production,Sandbox").verify(chain.sign())
    assert result.environment == "Sandbox"


def test_xcode_environment_never_gets_a_verifier(chain):
    built = verifier(chain, environments="Production,Sandbox")
    assert IAPEnvironment.XCODE not in getattr(built, "_verifiers", {})


def test_xcode_signed_transaction_is_rejected(chain):
    """library는 Xcode/LocalTesting에서 **서명 검증을 건너뛴다**(source 확인).

    그래서 Xcode verifier를 만들면 위조 payload가 그대로 통과한다.
    우리는 그 verifier를 아예 만들지 않는다.
    """
    with pytest.raises(InvalidTransaction):
        verifier(chain, environments="Production,Sandbox").verify(
            chain.sign(environment="Xcode")
        )


# MARK: - fail closed


def test_production_without_app_apple_id_is_unavailable(chain):
    built = verifier(chain, environments="Production", app_apple_id=None)
    assert built.is_configured is False
    with pytest.raises(IAPUnavailable):
        built.verify(chain.sign())


def test_production_disabled_but_sandbox_still_works(chain):
    """appAppleId가 없으면 **Production만** 꺼진다. 느슨해지지 않는다."""
    built = verifier(chain, environments="Production,Sandbox", app_apple_id=None)
    assert sorted(getattr(built, "_verifiers", {})) == [IAPEnvironment.SANDBOX]
    with pytest.raises(InvalidTransaction):
        built.verify(chain.sign(environment=IAPEnvironment.PRODUCTION.value))


def test_missing_bundle_id_is_unavailable(chain):
    from app.iap.models import parse_allowed_environments

    built = build_apple_verifier(
        bundle_id="",
        allowed_environments=parse_allowed_environments("Sandbox"),
        app_apple_id=APP_APPLE_ID,
        roots=[chain.root_der],
    )
    assert built.is_configured is False


def test_no_roots_is_unavailable():
    from app.iap.models import parse_allowed_environments

    built = build_apple_verifier(
        bundle_id=BUNDLE_ID,
        allowed_environments=parse_allowed_environments("Sandbox"),
        app_apple_id=APP_APPLE_ID,
        roots=[],
    )
    assert built.is_configured is False


def test_retryable_failure_becomes_unavailable_not_rejection():
    """OCSP 조회 실패는 **검증을 건너뛰지 않고** 재시도 가능한 실패로 올린다.

    client가 아직 `finish()`하지 않았으므로 그 결제는 유실되지 않고 다시 온다.
    """

    class Retryable:
        def verify_and_decode_signed_transaction(self, signed):
            raise VerificationException(VerificationStatus.RETRYABLE_VERIFICATION_FAILURE)

    built = AppleTransactionVerifier({IAPEnvironment.SANDBOX: Retryable()})
    with pytest.raises(IAPUnavailable):
        built.verify("jws")


def test_online_checks_are_enabled_by_default():
    """폐기 확인을 기본으로 켠다. 돈이 오가는 경로에서 폐기된 인증서를 받지 않는다."""
    import inspect

    from app.iap import apple_verifier

    signature = inspect.signature(apple_verifier.build_apple_verifier)
    assert signature.parameters["enable_online_checks"].default is True


# MARK: - root certificates


def test_repo_ships_apple_root_certificates():
    roots = load_root_certificates()
    assert len(roots) >= 1
    for der in roots:
        # DER로 읽히는지 실제로 확인한다 — 깨진 파일이 조용히 신뢰 목록에 들어가면 안 된다.
        certificate = x509.load_der_x509_certificate(der)
        assert "Apple" in certificate.subject.rfc4514_string()


def test_root_certificates_are_not_fetched_at_runtime():
    source = (Path(__file__).resolve().parent.parent / "app/iap/apple_verifier.py").read_text()
    for banned in ["urlopen", "requests.get", "http://", "https://www.apple.com"]:
        assert banned not in source, f"runtime에 인증서를 내려받는다: {banned}"


# MARK: - 우회 금지 (구조 고정)


def test_production_uses_the_official_apple_library():
    source = (Path(__file__).resolve().parent.parent / "app/iap/apple_verifier.py").read_text()
    assert "from appstoreserverlibrary.signed_data_verifier import" in source
    assert "SignedDataVerifier" in source
    assert "verify_and_decode_signed_transaction" in source


def test_no_hand_rolled_chain_verifier():
    """x5c 체인 검증기를 직접 만들지 않았다."""
    source = (Path(__file__).resolve().parent.parent / "app/iap/apple_verifier.py").read_text()
    for banned in ["def verify_chain", "x5c_header", "load_der_x509_certificate(", "OCSPRequestBuilder"]:
        assert banned not in source, f"체인 검증을 직접 구현했다: {banned}"


def _code_only(source: str) -> str:
    """주석과 문자열을 걷어낸 코드만. 설명문에 나온 단어를 위반으로 세지 않는다."""
    import io
    import tokenize

    return "".join(
        token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type not in (tokenize.COMMENT, tokenize.STRING)
    )


def test_app_store_server_api_credentials_are_not_required():
    """JWS 검증에는 `.p8` / issuerId / keyId가 필요 없다.

    설명문에는 그 단어들이 나오므로 **코드만** 검사한다.
    """
    root = Path(__file__).resolve().parent.parent
    for path in ["app/iap/apple_verifier.py", "app/core/config.py", "app/main.py"]:
        code = _code_only((root / path).read_text())
        for banned in ["issuer_id", "issuerId", "private_key_p8", "AppStoreServerAPIClient"]:
            assert banned not in code, f"{path}: 불필요한 Apple secret/API client를 들였다 ({banned})"

    # `key_id`는 **검증기 안에서만** 금지한다. Phase F가 APNs key id를 들여왔는데
    # 그것은 전혀 다른 Apple 자격 증명이다 — 이름이 닮았다고 같이 막으면, 이 test는
    # 원래 잡으려던 것(App Store Server API 도입) 대신 무관한 기능을 막게 된다.
    # JWS 검증 경로에 key id가 나타나는 것은 여전히 드리프트다.
    assert "key_id" not in _code_only((root / "app/iap/apple_verifier.py").read_text())
    # config에서도 App Store 쪽 이름은 계속 막는다.
    config = _code_only((root / "app/core/config.py").read_text())
    for banned in ["app_store_key_id", "iap_key_id", "app_store_private_key"]:
        assert banned not in config, f"config: {banned}"


# MARK: - 로그


def test_logs_never_contain_jws_or_raw_identifiers(chain, caplog):
    token = chain.sign()
    with caplog.at_level(logging.DEBUG):
        verifier(chain).verify(token)
        with pytest.raises(InvalidTransaction):
            verifier(chain).verify(Chain().sign())

    assert token not in caplog.text
    assert TRANSACTION_ID not in caplog.text
    assert USER not in caplog.text
