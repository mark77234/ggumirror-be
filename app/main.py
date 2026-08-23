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
from app.ai.provider import ImageProvider, build_provider
from app.ai.service import AIStickerService
from app.ai.storage import GenerationStorage, build_storage
from app.ai.store import GenerationStore
from app.iap.models import parse_allowed_environments
from app.iap.notifications import AppStoreNotificationService
from app.iap.refunds import IAPRefundService
from app.marketplace.service import MarketplaceService
from app.iap.service import IAPService
from app.iap.apple_verifier import build_apple_verifier
from app.iap.verifier import TransactionVerifier
from app.api import capacity as capacity_api
from app.api import catalog as catalog_api
from app.api import ads, ai, app_store, auth, health, iap, marketplace, users
from app.auth.apple import AppleTokenVerifier
from app.auth.store import AuthStore
from app.shards.service import ShardLedgerService
from app.shards.store import ShardStore
from app.core.config import SERVICE_NAME, Settings, configure_logging, load_settings

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    auth_store: AuthStore | None = None,
    transaction_verifier: TransactionVerifier | None = None,
    shard_store: ShardStore | None = None,
    reward_context_store: RewardContextStore | None = None,
    admob_keys: AdMobKeyProvider | None = None,
    image_provider: ImageProvider | None = None,
    generation_store: GenerationStore | None = None,
    generation_storage: GenerationStorage | None = None,
    marketplace_store=None,
    marketplace_assets=None,
    catalog_store=None,
    capacity_store=None,
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

    @lru_cache(maxsize=1)
    def ai_stickers() -> AIStickerService:
        # provider나 bucket이 없어도 service는 만들어진다 — `is_available`이 False가 되고,
        # `/ai/stickers/config`가 client에게 CTA를 감추라고 답한다(fail closed).
        if generation_store is not None:
            store: GenerationStore = generation_store
        else:
            from app.ai.firestore_store import FirestoreGenerationStore

            store = FirestoreGenerationStore(_firestore())

        return AIStickerService(
            shards=shards(),
            provider=image_provider
            or build_provider(
                api_key=settings.ai_image_api_key,
                model=settings.ai_image_model,
                quality=settings.ai_image_quality,
            ),
            store=store,
            storage=generation_storage or build_storage(settings.ai_result_bucket),
        )

    @lru_cache(maxsize=1)
    def shard_iap() -> IAPService:
        # 검증기가 없어도 service는 만들어진다 — `is_available`이 False가 될 뿐이다.
        # **가짜 검증기를 설정으로 켤 수 있는 경로를 만들지 않는다**(test 주입 전용).
        return IAPService(
            verifier=transaction_verifier
            or build_apple_verifier(
                bundle_id=settings.apple_client_id,
                allowed_environments=parse_allowed_environments(settings.iap_allowed_environments),
                app_apple_id=settings.iap_app_apple_id,
            ),
            shards=shards(),
            bundle_id=settings.apple_client_id,
            allowed_environments=parse_allowed_environments(settings.iap_allowed_environments),
        )

    @lru_cache(maxsize=1)
    def notifications() -> AppStoreNotificationService:
        # 같은 verifier를 쓴다 — 알림도 결제와 **같은 신뢰 사슬**을 지난다.
        return AppStoreNotificationService(
            transaction_verifier
            or build_apple_verifier(
                bundle_id=settings.apple_client_id,
                allowed_environments=parse_allowed_environments(settings.iap_allowed_environments),
                app_apple_id=settings.iap_app_apple_id,
            ),
            bundle_id=settings.apple_client_id,
            allowed_environments=parse_allowed_environments(settings.iap_allowed_environments),
            app_apple_id=settings.iap_app_apple_id,
            # 환불만 조각을 움직인다. 알림 service는 여기로 넘기기만 한다.
            refunds=IAPRefundService(shards()),
        )

    @lru_cache(maxsize=1)
    def marketplace_listings() -> MarketplaceService:
        from app.marketplace.firestore_store import FirestoreMarketplaceStore

        # 조각 원장과 **같은 Firestore client**를 쓴다 — 등록비와 listing이
        # 한 transaction에서 commit되려면 같은 database여야 한다.
        from app.marketplace.assets import GCSMarketplaceAssetStorage

        # **AI 결과 bucket을 재사용하지 않는다** — 그쪽은 7일 lifecycle이고 이쪽은 영구다.
        # bucket 이름이 없으면 storage가 `None`이고 업로드/전달만 503이 된다(fail closed).
        return MarketplaceService(
            store=marketplace_store or FirestoreMarketplaceStore(_firestore()),
            shards=shards(),
            assets=marketplace_assets
            or (
                GCSMarketplaceAssetStorage(settings.marketplace_asset_bucket)
                if settings.marketplace_asset_bucket
                else None
            ),
        )

    @lru_cache(maxsize=1)
    def catalog():
        from app.catalog.service import CatalogService
        from app.catalog.store import FirestoreCatalogStore

        # 조각 원장과 **같은 Firestore client**를 쓴다.
        return CatalogService(catalog_store or FirestoreCatalogStore(_firestore()))

    @lru_cache(maxsize=1)
    def mirror_capacity():
        from app.capacity.service import MirrorCapacityService
        from app.capacity.store import FirestoreCapacityStore

        # 조각 원장과 **같은 service 객체**를 쓴다 — 새 지갑 시스템을 만들지 않는다.
        return MirrorCapacityService(
            capacity_store or FirestoreCapacityStore(_firestore()), shards()
        )

    app.state.mirror_capacity_service = mirror_capacity
    app.state.catalog_service = catalog
    app.state.apple_verifier = verifier
    app.state.auth_store = store
    app.state.shard_service = shards
    app.state.rewarded_ad_service = rewarded_ads
    app.state.ai_sticker_service = ai_stickers
    app.state.iap_service = shard_iap
    app.state.app_store_notifications = notifications
    app.state.marketplace_service = marketplace_listings

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(ads.router)
    app.include_router(ai.router)
    app.include_router(iap.router)
    app.include_router(app_store.router)
    app.include_router(marketplace.router)
    app.include_router(catalog_api.router)
    app.include_router(capacity_api.router)
    app.include_router(marketplace.purchases_router)

    logger.info("app created env=%s log_level=%s", settings.app_env, settings.log_level)
    return app


app = create_app()
