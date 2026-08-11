import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings, load_settings
from app.main import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Settings()))


def test_app_creates():
    app = create_app(Settings())
    assert isinstance(app, FastAPI)
    # production에서 traceback이 response로 새지 않게 하는 기본값.
    assert app.debug is False


def test_health_returns_200(client: TestClient):
    assert client.get("/health").status_code == 200


def test_health_body(client: TestClient):
    assert client.get("/health").json() == {"status": "ok"}


def test_root_body(client: TestClient):
    body = client.get("/").json()
    assert body == {"service": "ggumirror-be", "status": "ok"}


def test_config_defaults():
    settings = load_settings({})
    assert settings == Settings(app_env="local", log_level="INFO", port=8080)
    assert settings.is_production is False


def test_config_reads_env():
    settings = load_settings({"APP_ENV": "production", "LOG_LEVEL": "warning", "PORT": "9090"})
    assert (settings.app_env, settings.log_level, settings.port) == ("production", "WARNING", 9090)
    assert settings.is_production is True


@pytest.mark.parametrize(
    "env, expected",
    [
        ({"PORT": "not-a-port"}, "PORT='not-a-port' is not an integer"),
        ({"PORT": "70000"}, "out of range"),
        ({"LOG_LEVEL": "chatty"}, "LOG_LEVEL='CHATTY' is invalid"),
    ],
)
def test_invalid_config_fails_clearly(env: dict[str, str], expected: str):
    with pytest.raises(ValueError, match=expected):
        load_settings(env)


def test_no_feature_routes_yet():
    """B-1의 API는 /health와 /뿐이다. 다음 Phase 전에 contract를 확정하지 않는다."""
    paths = {route.path for route in create_app(Settings()).routes if hasattr(route, "path")}
    assert {"/health", "/"} <= paths
    assert not {p for p in paths if p.startswith(("/auth", "/users", "/shards", "/store", "/listings", "/purchases"))}
