"""정기 발송 endpoint는 누가 부를 수 있는가 (Phase J).

**이 경로가 열려 있으면 누구나 우리 사용자 전체에게 push를 쏜다.**

Cloud Run IAM은 여기서 문이 되지 못한다 — 이 service는 `allUsers`에게 열려 있다
(로그인 없이 상점을 봐야 한다). 그래서 앱이 직접 확인하고, 이 파일이 그것을 고정한다.

실제 Google을 부르지 않는다. 검증기 이음매에 fake를 끼운다.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.scheduler_identity import SchedulerIdentityError, verify_scheduler
from app.core.config import Settings

SCHEDULER = "ggumirror-scheduler@ggumirror-prod.iam.gserviceaccount.com"
AUDIENCE = "https://ggumirror-api.example.run.app"

JOBS = [
    "/jobs/mirror-digest/daily",
    "/jobs/mirror-digest/weekly",
    "/jobs/recommendation/weekly",
]


class FakeVerifier:
    """Google이 서명했다고 **가정**하고 claim을 돌려준다.

    signature 자체는 라이브러리가 보는 것이라 여기서 흉내 내지 않는다.
    여기서 보는 것은 **그다음** — 우리가 정한 계정인지다.
    """

    def __init__(self, claims: dict | None = None, error: Exception | None = None) -> None:
        self.claims = claims or {}
        self.error = error
        self.audiences: list[str] = []

    def verify(self, token: str, audience: str) -> dict:
        self.audiences.append(audience)
        if self.error:
            raise self.error
        # 실제 라이브러리는 audience가 다르면 던진다. 그 동작을 흉내 낸다.
        if self.claims.get("aud") not in (None, audience):
            raise ValueError("audience mismatch")
        return self.claims


def good_claims(**overrides) -> dict:
    claims = {
        "iss": "https://accounts.google.com",
        "email": SCHEDULER,
        "email_verified": True,
        "aud": AUDIENCE,
    }
    claims.update(overrides)
    return claims


def check(verifier, *, service_account=SCHEDULER, audience=AUDIENCE, token="tok"):
    return verify_scheduler(
        token,
        expected_service_account=service_account,
        expected_audience=audience,
        verifier=verifier,
    )


# MARK: - 검증 규칙


def test_valid_scheduler_token_is_accepted():
    claims = check(FakeVerifier(good_claims()))
    assert claims["email"] == SCHEDULER


def test_missing_token_is_denied():
    with pytest.raises(SchedulerIdentityError):
        check(FakeVerifier(good_claims()), token=None)


def test_wrong_service_account_is_denied():
    """**서명만 맞으면 누구나 통과하던 자리다.**

    Google 계정을 가진 사람이라면 자기 token을 만들 수 있다. 그것이 우리
    scheduler가 아니라는 것을 여기서 본다.
    """
    with pytest.raises(SchedulerIdentityError):
        check(FakeVerifier(good_claims(email="attacker@gmail.com")))


def test_unverified_email_is_denied():
    with pytest.raises(SchedulerIdentityError):
        check(FakeVerifier(good_claims(email_verified=False)))


def test_wrong_issuer_is_denied():
    with pytest.raises(SchedulerIdentityError):
        check(FakeVerifier(good_claims(iss="https://evil.example.com")))


def test_wrong_audience_is_denied():
    """audience가 다르면 **다른 서비스로 가야 할 token**이다."""
    with pytest.raises(SchedulerIdentityError):
        check(FakeVerifier(good_claims(aud="https://someone-else.run.app")))


def test_expired_or_bad_signature_is_denied():
    """라이브러리가 던지는 것은 전부 거절이다(만료 · 서명 불일치 · 형식 오류)."""
    for error in (ValueError("Token expired"), ValueError("Wrong signature")):
        with pytest.raises(SchedulerIdentityError):
            check(FakeVerifier(error=error))


def test_unconfigured_denies_before_calling_google():
    """**설정이 없으면 막는다 — 그리고 검증기를 부르지도 않는다.**

    예전에는 설정이 없으면 통과였다. 공개 service에서 그건 문을 열어 두는 것이다.

    신원 확인이 뒤에서 한 번 더 잡아 주긴 하지만(그래서 이 검사를 지워도 여전히
    거절된다), 여기서 먼저 끊어야 **설정을 빼먹은 배포가 매 요청마다 Google에
    나가지 않는다.** 그래서 "거절했는가"가 아니라 "부르지 않았는가"를 본다.
    """
    for kwargs in ({"service_account": ""}, {"audience": ""}):
        verifier = FakeVerifier(good_claims())
        with pytest.raises(SchedulerIdentityError):
            check(verifier, **kwargs)
        assert verifier.audiences == [], "설정도 없는데 Google을 불렀다"


def test_expected_audience_is_passed_to_the_verifier():
    verifier = FakeVerifier(good_claims())
    check(verifier)
    assert verifier.audiences == [AUDIENCE]


def test_failures_do_not_log_the_token(caplog):
    import logging

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(SchedulerIdentityError):
            check(FakeVerifier(good_claims(email="attacker@gmail.com")), token="super-secret")
    assert "super-secret" not in caplog.text


# MARK: - endpoint


def app_with(verifier, **settings_overrides):
    from app.main import create_app

    settings = Settings(
        app_env="local",
        scheduler_service_account=SCHEDULER,
        scheduler_audience=AUDIENCE,
        **settings_overrides,
    )
    return TestClient(create_app(settings, id_token_verifier=verifier),
                      raise_server_exceptions=False)


@pytest.mark.parametrize("path", JOBS)
def test_unauthenticated_request_is_denied(path):
    client = app_with(FakeVerifier(good_claims()))
    assert client.post(path).status_code == 403


@pytest.mark.parametrize("path", JOBS)
def test_ordinary_app_bearer_is_denied(path):
    """**앱 사용자의 token으로는 못 부른다.**

    사용자 token은 Google이 서명한 것이 아니라 검증기에서 떨어진다.
    """
    client = app_with(FakeVerifier(error=ValueError("not a google token")))
    response = client.post(path, headers={"Authorization": "Bearer apple-user-session-token"})
    assert response.status_code == 403


@pytest.mark.parametrize("path", JOBS)
def test_wrong_identity_is_denied_at_the_endpoint(path):
    client = app_with(FakeVerifier(good_claims(email="someone-else@gmail.com")))
    response = client.post(path, headers={"Authorization": "Bearer tok"})
    assert response.status_code == 403


@pytest.mark.parametrize("path", JOBS)
def test_unconfigured_endpoint_denies(path):
    """설정을 빼먹은 배포에서 열려 있으면 안 된다."""
    from app.main import create_app

    client = TestClient(
        create_app(Settings(app_env="local"), id_token_verifier=FakeVerifier(good_claims())),
        raise_server_exceptions=False,
    )
    assert client.post(path, headers={"Authorization": "Bearer tok"}).status_code == 403


def test_denial_does_not_reveal_the_reason():
    client = app_with(FakeVerifier(good_claims(email="attacker@gmail.com")))
    body = client.post(JOBS[0], headers={"Authorization": "Bearer tok"}).text
    for leak in ("audience", "issuer", "identity", "service account", SCHEDULER):
        assert leak not in body


def test_job_token_is_a_second_lock_not_the_only_one():
    """OIDC를 통과해도 `JOBS_TOKEN`이 설정돼 있으면 그것도 맞아야 한다."""
    client = app_with(FakeVerifier(good_claims()), jobs_token="second-lock")
    headers = {"Authorization": "Bearer tok"}
    assert client.post(JOBS[0], headers=headers).status_code == 403
    ok = client.post(JOBS[0], headers={**headers, "X-Ggumirror-Job-Token": "second-lock"})
    assert ok.status_code != 403
