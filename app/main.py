"""FastAPI app.

CORS는 넣지 않았다 — client가 iOS native다. web client가 생기면 그때 추가한다.
global exception handler도 넣지 않았다 — FastAPI 기본 동작이 이미
500 응답에 stack trace를 담지 않는다(debug=False). 필요해지기 전에 만들지 않는다.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from app.api import health
from app.core.config import SERVICE_NAME, Settings, configure_logging, load_settings

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
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
    app.include_router(health.router)

    logger.info("app created env=%s log_level=%s", settings.app_env, settings.log_level)
    return app


app = create_app()
