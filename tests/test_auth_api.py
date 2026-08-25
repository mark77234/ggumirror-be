"""POST /auth/apple · GET /users/me · POST /auth/logout

Apple token은 test에서 만든 RSA key로 서명한 synthetic JWT다.
저장소는 in-memory fake — **실제 Firestore에 붙지 않는다.**
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth.errors import AppleTokenError, AppleTokenReason
from app.auth.models import SESSION_LIFETIME, Session, sha256_hex, utcnow
from app.auth.store import InMemoryAuthStore, StoreUnavailable
from app.core.config import Settings
from app.main import create_app
from tests.conftest import CLIENT_ID, apple_claims

RAW_NONCE = "nonce-from-client-abc123"


@pytest.fixture
def store() -> InMemoryAuthStore:
    return InMemoryAuthStore()


@pytest.fixture
def client(store: InMemoryAuthStore, apple_key, jwks_of, monkeypatch) -> TestClient:
    """JWKS는 고정 fixture를 주입한다. network는 conftest가 막고 있다."""
    from app.auth import jwks as jwks_module

    document = jwks_of(apple_key)
    monkeypatch.setattr(jwks_module, "http_jwks_fetch", lambda *a, **k: lambda: document)

    app = create_app(
        Settings(app_env="local", apple_client_id=CLIENT_ID),
        auth_store=store,
    )
    return TestClient(app)


def token_for(apple_key, **overrides: Any) -> str:
    """client가 raw nonce를 보내고, Apple token에는 그 SHA-256이 들어 있다."""
    claims = apple_claims(nonce=sha256_hex(RAW_NONCE), **overrides)
    return apple_key.token(claims)


def sign_in(client: TestClient, apple_key, nonce: str = RAW_NONCE, **overrides: Any):
    return client.post(
        "/auth/apple",
        json={"identityToken": token_for(apple_key, **overrides), "nonce": nonce},
    )


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# MARK: - User


def test_valid_token_creates_user(client, apple_key, store):
    response = sign_in(client, apple_key)
    assert response.status_code == 200
    assert len(store.users) == 1


def test_same_subject_returns_same_user(client, apple_key, store):
    first = sign_in(client, apple_key).json()["user"]["id"]
    second = sign_in(client, apple_key).json()["user"]["id"]
    assert first == second
    assert len(store.users) == 1


def test_repeated_login_does_not_duplicate_user(client, apple_key, store):
    """같은 subject로 여러 번 들어와도 User는 하나다 (identity key가 deterministic)."""
    ids = {sign_in(client, apple_key).json()["user"]["id"] for _ in range(5)}
    assert len(ids) == 1
    assert len(store.users) == 1
    assert len(store.identities) == 1


def test_concurrent_first_login_does_not_duplicate_user(store):
    """동시에 처음 로그인해도 identity 문서가 하나이므로 User도 하나다."""
    results = [store.user_for_identity("apple", "001.same.subject") for _ in range(3)]
    assert len({user.id for user, _ in results}) == 1
    assert [created for _, created in results] == [True, False, False]


def test_different_subject_creates_different_user(client, apple_key, store):
    first = sign_in(client, apple_key, sub="001.first.1").json()["user"]["id"]
    second = sign_in(client, apple_key, sub="001.second.2").json()["user"]["id"]
    assert first != second
    assert len(store.users) == 2


def test_email_does_not_determine_identity(client, apple_key, store):
    """email이 바뀌어도 같은 subject면 같은 User다. email이 달라도 새 User가 아니다."""
    first = sign_in(client, apple_key, email="a@privaterelay.appleid.com").json()["user"]["id"]
    second = sign_in(client, apple_key, email="b@example.com").json()["user"]["id"]
    assert first == second

    # 반대로 email이 같고 subject가 다르면 다른 User다.
    other = sign_in(client, apple_key, sub="001.other.9", email="a@privaterelay.appleid.com")
    assert other.json()["user"]["id"] != first
    assert len(store.users) == 2


def test_apple_subject_not_in_response(client, apple_key):
    subject = "001.verysecret.777"
    response = sign_in(client, apple_key, sub=subject)
    assert subject not in response.text
    assert response.json()["user"]["id"] != subject


def test_user_id_is_uuid(client, apple_key):
    from uuid import UUID

    UUID(sign_in(client, apple_key).json()["user"]["id"])  # 형식이 틀리면 예외


# MARK: - Session / token


def test_returns_opaque_access_token(client, apple_key):
    body = sign_in(client, apple_key).json()
    assert body["tokenType"] == "Bearer"
    assert body["expiresAt"]
    # JWT가 아니다 — 점으로 나뉜 3부분이 아니어야 한다.
    assert body["accessToken"].count(".") == 0


def test_token_entropy_policy(client, apple_key):
    """32 byte urlsafe → 43자 이상."""
    token = sign_in(client, apple_key).json()["accessToken"]
    assert len(token) >= 43
    assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def test_raw_token_not_persisted(client, apple_key, store):
    token = sign_in(client, apple_key).json()["accessToken"]
    stored = list(store.sessions.values())
    assert len(stored) == 1
    assert token not in store.sessions  # key는 hash다
    assert all(token not in str(session) for session in stored)


def test_stored_hash_matches_token(client, apple_key, store):
    token = sign_in(client, apple_key).json()["accessToken"]
    assert sha256_hex(token) in store.sessions
    assert store.sessions[sha256_hex(token)].token_hash == sha256_hex(token)


def test_sessions_get_different_tokens(client, apple_key, store):
    first = sign_in(client, apple_key).json()["accessToken"]
    second = sign_in(client, apple_key).json()["accessToken"]
    assert first != second
    assert len(store.sessions) == 2


def test_session_expiry_uses_single_policy(client, apple_key, store):
    sign_in(client, apple_key)
    session = next(iter(store.sessions.values()))
    assert abs((session.expires_at - session.created_at) - SESSION_LIFETIME) < timedelta(seconds=2)


# MARK: - Bearer


def test_valid_bearer_resolves_user(client, apple_key):
    body = sign_in(client, apple_key).json()
    response = client.get("/users/me", headers=bearer(body["accessToken"]))
    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == body["user"]["id"]
    # 프로필 필드가 **더해졌다**(1.1.0). 1.0.7 client는 `id`만 읽고 나머지를 버린다.
    assert payload["displayName"] is None
    assert payload["canChangeDisplayName"] is True
    assert payload["nextDisplayNameChangeAt"] is None
    # 여전히 새어 나가면 안 되는 것들. 이 test의 원래 목적이다.
    assert set(payload) == {"id", "displayName", "canChangeDisplayName", "nextDisplayNameChangeAt"}
    assert "email" not in payload and "sub" not in payload


def test_missing_bearer_rejected(client):
    assert client.get("/users/me").status_code == 401


@pytest.mark.parametrize("header", [{}, {"Authorization": "Bearer"}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer   "}])
def test_malformed_authorization_rejected(client, header):
    assert client.get("/users/me", headers=header).status_code == 401


def test_invalid_token_rejected(client, apple_key):
    sign_in(client, apple_key)
    assert client.get("/users/me", headers=bearer("not-a-real-token")).status_code == 401


def test_expired_session_rejected(client, apple_key, store):
    token = sign_in(client, apple_key).json()["accessToken"]
    hashed = sha256_hex(token)
    old = store.sessions[hashed]
    store.sessions[hashed] = Session(
        token_hash=hashed,
        user_id=old.user_id,
        created_at=old.created_at,
        expires_at=utcnow() - timedelta(seconds=1),
    )
    assert client.get("/users/me", headers=bearer(token)).status_code == 401


def test_revoked_session_rejected(client, apple_key, store):
    token = sign_in(client, apple_key).json()["accessToken"]
    store.revoke_session(sha256_hex(token))
    assert client.get("/users/me", headers=bearer(token)).status_code == 401


# MARK: - Logout


def test_logout_revokes_session(client, apple_key, store):
    token = sign_in(client, apple_key).json()["accessToken"]
    assert client.post("/auth/logout", headers=bearer(token)).status_code == 204
    assert store.sessions[sha256_hex(token)].revoked_at is not None
    assert client.get("/users/me", headers=bearer(token)).status_code == 401


def test_logout_does_not_delete_user(client, apple_key, store):
    body = sign_in(client, apple_key).json()
    client.post("/auth/logout", headers=bearer(body["accessToken"]))
    assert body["user"]["id"] in store.users
    # 다시 로그인하면 같은 User로 돌아온다.
    assert sign_in(client, apple_key).json()["user"]["id"] == body["user"]["id"]


def test_logout_requires_bearer(client):
    assert client.post("/auth/logout").status_code == 401


def test_logout_of_unknown_token_succeeds(client, apple_key):
    """이미 사라진 session이어도 client는 로그아웃할 수 있어야 한다."""
    sign_in(client, apple_key)
    assert client.post("/auth/logout", headers=bearer("gone" * 12)).status_code == 204


# MARK: - 오류 매핑


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "com.someone.else"},
        {"iss": "https://evil.example.com"},
        {"sub": None},
    ],
)
def test_invalid_apple_token_rejected(client, apple_key, overrides):
    assert sign_in(client, apple_key, **overrides).status_code == 401


def test_expired_apple_token_rejected(client, apple_key):
    past = int(utcnow().timestamp()) - 3600
    assert sign_in(client, apple_key, iat=past, exp=past + 600).status_code == 401


def test_malformed_apple_token_rejected(client):
    response = client.post("/auth/apple", json={"identityToken": "garbage", "nonce": RAW_NONCE})
    assert response.status_code == 401


def test_nonce_mismatch_rejected(client, apple_key):
    """client가 다른 nonce를 보내면 통과하지 못한다 — 서버가 실제로 검사한다."""
    assert sign_in(client, apple_key, nonce="someone-elses-nonce").status_code == 401


def test_nonce_required(client, apple_key):
    response = client.post("/auth/apple", json={"identityToken": token_for(apple_key)})
    assert response.status_code == 422


def test_token_without_nonce_claim_rejected(client, apple_key):
    """nonce 없이 발급된 token으로는 로그인할 수 없다."""
    from tests.conftest import apple_claims as claims

    token = apple_key.token(claims())
    response = client.post("/auth/apple", json={"identityToken": token, "nonce": RAW_NONCE})
    assert response.status_code == 401


def test_jwks_unavailable_returns_503(store, apple_key, monkeypatch):
    from app.auth import jwks as jwks_module

    def boom() -> dict:
        raise OSError("apple unreachable")

    monkeypatch.setattr(jwks_module, "http_jwks_fetch", lambda *a, **k: boom)
    app = create_app(Settings(app_env="local", apple_client_id=CLIENT_ID), auth_store=store)
    response = TestClient(app).post(
        "/auth/apple", json={"identityToken": token_for(apple_key), "nonce": RAW_NONCE}
    )
    assert response.status_code == 503


def test_missing_apple_client_id_returns_503(store, apple_key):
    """설정이 빠진 것은 client 잘못이 아니다."""
    app = create_app(Settings(app_env="local", apple_client_id=""), auth_store=store)
    response = TestClient(app).post(
        "/auth/apple", json={"identityToken": token_for(apple_key), "nonce": RAW_NONCE}
    )
    assert response.status_code == 503


def test_persistence_failure_does_not_leak_internals(client, apple_key, store, monkeypatch):
    def boom(*args: object, **kwargs: object) -> None:
        raise StoreUnavailable("ggumirror_users/abc: permission denied on project secret-project")

    monkeypatch.setattr(store, "user_for_identity", boom)
    response = sign_in(client, apple_key)
    assert response.status_code == 500
    assert "permission denied" not in response.text
    assert "ggumirror_users" not in response.text
    assert "Traceback" not in response.text


def test_store_failure_on_session_lookup_returns_503(client, apple_key, store, monkeypatch):
    token = sign_in(client, apple_key).json()["accessToken"]

    def boom(*args: object, **kwargs: object) -> None:
        raise StoreUnavailable("firestore down")

    monkeypatch.setattr(store, "session_by_token_hash", boom)
    assert client.get("/users/me", headers=bearer(token)).status_code == 503


# MARK: - 로그


def test_logs_have_no_credentials(client, apple_key, store, caplog):
    subject = "001.logsecret.4242"
    email = "leak@privaterelay.appleid.com"
    identity_token = token_for(apple_key, sub=subject, email=email)

    with caplog.at_level(logging.DEBUG):
        response = client.post(
            "/auth/apple", json={"identityToken": identity_token, "nonce": RAW_NONCE}
        )
        access_token = response.json()["accessToken"]
        client.get("/users/me", headers=bearer(access_token))
        client.post("/auth/logout", headers=bearer(access_token))

    logged = caplog.text
    assert "apple_sign_in_ok" in logged
    for secret in (identity_token, access_token, subject, email, RAW_NONCE):
        assert secret not in logged


# MARK: - B-1 regression


def test_health_unchanged(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_production_docs_closed(store):
    app = create_app(
        Settings(app_env="production", apple_client_id=CLIENT_ID, gcp_project_id="p"),
        auth_store=store,
    )
    assert TestClient(app).get("/docs").status_code == 404


def test_no_unexpected_routes(client):
    paths = {route.path for route in client.app.routes if hasattr(route, "path")}
    assert {"/health", "/", "/auth/apple", "/auth/logout", "/users/me"} <= paths
    # shard / store / listing / purchase는 아직 없다.
    assert not {p for p in paths if p.startswith(("/shards", "/store", "/listings", "/purchases"))}
