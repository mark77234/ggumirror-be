"""FastAPI app.

CORS는 넣지 않았다 — client가 iOS native다. web client가 생기면 그때 추가한다.
global exception handler도 넣지 않았다 — FastAPI 기본 동작이 이미
500 응답에 stack trace를 담지 않는다(debug=False). 필요해지기 전에 만들지 않는다.

verifier / store는 **처음 쓰일 때** 만든다. /health가 Apple 설정이나 Firestore
credential에 의존하면, 멀쩡한 container가 죽었다고 판정된다.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from fastapi import FastAPI

from app.ads.service import RewardedAdConfig, RewardedAdService
from app.ads.store import RewardContextStore
from app.ads.verifier import AdMobKeyProvider
from app.api import ads, auth, health, users
from app.auth.apple import AppleTokenVerifier
from app.auth.store import AuthStore
from app.shards.service import ShardLedgerService
from app.shards.store import ShardStore
from app.core.config import SERVICE_NAME, Settings, configure_logging, load_settings

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    auth_store: AuthStore | None = None,
    shard_store: ShardStore | None = None,
    reward_context_store: RewardContextStore | None = None,
    admob_keys: AdMobKeyProvider | None = None,
) -> FastAPI:
    """store를 주면 그것을 쓴다 — test는 in-memory fake와 test key를 넣는다."""
    settings = settings or load_settings()
    configure_logging(settings)

    app = FastAPI(
        title=SERVICE_NAME,
        # production에서 traceback이 response로 나가지 않게 한다.
        debug=False,
        # 아직 공개할 API contract가 없다.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
    )
    app.state.settings = settings

    @lru_cache(maxsize=1)
    def verifier() -> AppleTokenVerifier:
        # client_id가 비어 있으면 여기서 ValueError → dependency가 503으로 바꾼다.
        return AppleTokenVerifier(client_id=settings.apple_client_id)

    @lru_cache(maxsize=1)
    def _firestore():
        # Firestore import를 여기서 한다 — dependency가 없는 환경에서도 app은 뜬다.
        from google.cloud import firestore

        client = firestore.Client(
            project=settings.gcp_project_id or None,
            database=settings.firestore_database,
        )
        logger.info("firestore_client_created database=%s", settings.firestore_database)
        return client

    @lru_cache(maxsize=1)
    def store() -> AuthStore:
        if auth_store is not None:
            return auth_store
        from app.auth.firestore_store import FirestoreAuthStore

        return FirestoreAuthStore(_firestore())

    @lru_cache(maxsize=1)
    def shards() -> ShardLedgerService:
        if shard_store is not None:
            return ShardLedgerService(shard_store)
        from app.shards.firestore_store import FirestoreShardStore

        return ShardLedgerService(FirestoreShardStore(_firestore()))

    @lru_cache(maxsize=1)
    def rewarded_ads() -> RewardedAdService:
        if reward_context_store is not None:
            contexts: RewardContextStore = reward_context_store
        else:
            from app.ads.firestore_store import FirestoreRewardContextStore

            contexts = FirestoreRewardContextStore(_firestore())

        return RewardedAdService(
            shards=shards(),
            contexts=contexts,
            keys=admob_keys or AdMobKeyProvider(),
            # ad unit이 비어 있으면 서명이 맞아도 지급하지 않는다(fail closed).
            config=RewardedAdConfig(
                ad_unit=settings.admob_ssv_expected_ad_unit,
                reward_item=settings.admob_reward_item,
            ),
        )

    app.state.apple_verifier = verifier
    app.state.auth_store = store
    app.state.shard_service = shards
    app.state.rewarded_ad_service = rewarded_ads

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(ads.router)

    logger.info("app created env=%s log_level=%s", settings.app_env, settings.log_level)
    return app


app = create_app()
