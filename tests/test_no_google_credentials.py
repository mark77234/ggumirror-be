"""**test가 Google credential을 찾으면 안 된다.**

이 파일이 있는 이유는 실제 사고다. `POST /auth/apple`이 guest 지갑 인계 때문에
조각 원장을 보게 되면서, `shard_store`를 주입하지 않은 test들의 app이
`FirestoreShardStore(_firestore())`로 fallback했다. 개발 기기에는 `gcloud` ADC가
있어서 그 fallback이 **조용히 성공**했고 test는 초록이었다. GitHub Actions에는 ADC가
없어서 같은 코드가 `DefaultCredentialsError`를 냈고, dependency가 그것을 503으로
바꿔 auth · marketplace · catalog · notifications · profile까지 69개가 무너졌다.

고칠 곳은 production fallback이 아니다 — production은 store를 안 주면 Firestore를
써야 한다. 고칠 곳은 **test의 주입**이다. `tests/conftest.py`의
`no_google_credentials`(guard 본체는 `tests/adc_guard.py`)가 그것을 전 suite에서 강제하고, 여기서는 그 규칙 자체가
살아 있는지와 대표 경로가 credential 없이 도는지를 고정한다.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth.store import InMemoryAuthStore
from app.core.config import Settings
from app.main import create_app
from app.marketplace.store import InMemoryMarketplaceStore
from app.shards.store import InMemoryShardStore
from tests.adc_guard import GoogleCredentialLookup
from tests.conftest import CLIENT_ID


def test_the_guard_itself_is_installed() -> None:
    """guard가 꺼지면 이 파일의 나머지가 전부 무의미해진다."""
    import google.auth

    with pytest.raises(GoogleCredentialLookup):
        google.auth.default()


def test_a_fully_injected_app_never_looks_up_credentials() -> None:
    """store를 다 주면 credential lookup이 **한 번도** 일어나지 않는다.

    guard가 `BaseException`이라, 여기서 Firestore로 새면 503이 아니라 이 test가
    그 자리에서 깨진다.
    """
    shard_store = InMemoryShardStore()
    app = create_app(
        Settings(app_env="local", apple_client_id=CLIENT_ID),
        auth_store=InMemoryAuthStore(),
        shard_store=shard_store,
        marketplace_store=InMemoryMarketplaceStore(shard_store),
    )
    client = TestClient(app)

    # 로그인 없이 도는 대표 경로들. guest 조각 구매가 지나는 길이기도 하다.
    assert client.get("/health").status_code == 200
    assert client.get("/marketplace/listings").status_code == 200
    # 익명 신원 발급 — 여기서 지갑 주인이 정해진다.
    assert client.post("/auth/guest").status_code == 200


def test_a_missing_store_is_caught_here_not_in_ci() -> None:
    """**주입을 빠뜨리면 곧바로 실패한다.** CI에서 503으로 알게 되지 않는다.

    `shard_store`가 없으면 `POST /auth/guest`조차 조각 원장을 만들다 걸린다 —
    guest 지갑 인계가 그 경로에 붙어 있기 때문이다.
    """
    app = create_app(
        Settings(app_env="local", apple_client_id=CLIENT_ID),
        auth_store=InMemoryAuthStore(),
        # shard_store를 일부러 주지 않는다 — 이것이 CI를 무너뜨렸던 모양이다.
    )
    client = TestClient(app)

    with pytest.raises(GoogleCredentialLookup):
        client.post("/auth/apple", json={"identityToken": "x", "nonce": "y"})


def test_production_still_falls_back_to_firestore() -> None:
    """**production 동작을 테스트 때문에 바꾸지 않았다.**

    store를 주지 않으면 여전히 Firestore를 쓰려고 한다 — `APP_ENV=test`면 in-memory로
    바꾸는 숨은 분기 같은 것은 없다. guard가 잡아 준다는 사실 자체가 그 증거다.
    """
    app = create_app(Settings(app_env="local"))

    with pytest.raises(GoogleCredentialLookup):
        # dependency를 거치지 않고 factory를 직접 부른다 — 503으로 감싸지 않은 날것.
        app.state.shard_service()
