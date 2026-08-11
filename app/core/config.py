"""환경설정.

pydantic-settings를 쓰지 않는다. 값이 세 개뿐이라 stdlib으로 충분하고,
dependency를 하나 덜 들고 간다. 필요해지면 그때 바꾼다.

secret 기본값을 코드에 두지 않는다. secret이 생기면 기본값 없이 필수로 읽는다.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Mapping

SERVICE_NAME = "ggumirror-be"

_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})


@dataclass(frozen=True)
class Settings:
    app_env: str = "local"
    log_level: str = "INFO"
    port: int = 8080
    # Apple identity token의 expected audience.
    # native iOS Sign in with Apple에서는 **app의 Bundle ID**다 — Services ID가 아니다.
    # 현재 client: com.mark77234.ggumirror (docs 참고)
    apple_client_id: str = ""

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """환경변수를 읽는다. 잘못된 값은 조용히 넘기지 않고 즉시 실패한다."""
    env = os.environ if env is None else env

    app_env = env.get("APP_ENV", "local").strip() or "local"

    log_level = env.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    if log_level not in _LOG_LEVELS:
        raise ValueError(
            f"LOG_LEVEL={log_level!r} is invalid. "
            f"expected one of {sorted(_LOG_LEVELS)}"
        )

    raw_port = env.get("PORT", "8080").strip() or "8080"
    try:
        port = int(raw_port)
    except ValueError as error:
        raise ValueError(f"PORT={raw_port!r} is not an integer") from error
    if not 1 <= port <= 65535:
        raise ValueError(f"PORT={port} is out of range (1-65535)")

    apple_client_id = env.get("APPLE_CLIENT_ID", "").strip()
    if app_env == "production" and not apple_client_id:
        # audience 없이 token을 검증하면 다른 앱의 token도 통과한다.
        # production에서는 조용히 넘어가지 않고 기동을 실패시킨다.
        raise ValueError("APPLE_CLIENT_ID is required when APP_ENV=production")

    return Settings(
        app_env=app_env,
        log_level=log_level,
        port=port,
        apple_client_id=apple_client_id,
    )


def configure_logging(settings: Settings) -> None:
    """표준 logging. 한 줄 한 이벤트.

    credential을 로그에 넣지 않는 것은 formatter가 막아주지 않는다 —
    호출하는 쪽 규칙이다. README의 Security를 따른다.
    """
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
