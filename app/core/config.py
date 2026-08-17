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
    # Firestore. Cloud Run에서는 Application Default Credentials를 쓴다 —
    # service account JSON key를 repo에 넣지 않는다.
    gcp_project_id: str = ""
    firestore_database: str = "(default)"
    # SSV callback의 `ad_unit` 필드와 **글자 그대로 비교할 값**이다.
    #
    # client가 광고를 load할 때 쓰는 `ca-app-pub-…/…` 형식과 **같다고 가정하지 않는다.**
    # 확인된 것은 "Google이 서명한 callback에 `ad_unit`이 들어온다"는 사실뿐이고,
    # 그 값의 정확한 표현은 실제 callback을 한 번 받아봐야 안다.
    # 그래서 이름을 client 쪽(`ADMOB_REWARDED_AD_UNIT_ID`)과 일부러 다르게 둔다.
    #
    # 채우는 방법: AdMob console에서 ad unit 생성 → SSV callback URL 등록 →
    # SSV Test Tool 1회 실행 → 검증을 통과한 callback의 `ad_unit` 값을 그대로 넣는다.
    #
    # 비어 있으면 서명 검증을 통과해도 **지급하지 않는다**(fail closed).
    # 추측한 값을 넣지 않는다.
    admob_ssv_expected_ad_unit: str = ""
    admob_reward_item: str = ""
    # AI 스티커 provider. **key는 서버에만 있다** — client bundle에 넣지 않는다.
    #
    # 비어 있으면 fail closed다: 서비스는 뜨고 다른 기능은 그대로이며,
    # `GET /ai/stickers/config`가 `available=false`를 돌려줘 client가 CTA를 감춘다.
    # 앱을 다시 빌드하지 않고 이 값만 채우면 기능이 열린다.
    #
    # model은 아무거나 넣을 수 없다 — 우리 요청 모양으로 PNG를 주는 것이 확인된 것만
    # 통과한다(`app/ai/provider.py`의 `SUPPORTED_MODELS`). production 기본값은 `gpt-image-2`다.
    # **투명 배경은 요구하지 않는다** — 배경제거는 기기가 한다(A-1B.2).
    # production에서는 **Secret Manager reference로 주입한다** — plain env value로 넣지 않는다.
    # (`gcloud run deploy --set-secrets=AI_IMAGE_API_KEY=ggumirror-openai-api-key:latest`)
    # 값은 로그에 남기지 않는다. 이 필드를 print / repr에 싣는 코드를 만들지 않는다.
    ai_image_api_key: str = ""
    ai_image_model: str = ""
    ai_image_quality: str = "low"
    # 생성 결과를 잠시 두는 꾸미러 전용 private bucket. **DailyOPIc bucket을 쓰지 않는다.**
    #
    # 비어 있으면 fail closed다 — 결과를 durable하게 두지 못하면 응답이 유실됐을 때
    # 복구할 방법이 없고, 그건 A-1A의 구멍 그대로다.
    ai_result_bucket: str = ""
    # 조각 IAP가 받아들일 Apple 환경. 쉼표로 나눈다 — 예: `Production,Sandbox`.
    #
    # **비어 있으면 아무것도 허용하지 않는다(fail closed).** 지금이 그 상태다.
    # Sandbox는 TestFlight · App Review · sandbox E2E에 필요하지만, Debug 빌드도
    # production API를 쓰기 때문에 켜 두면 sandbox 결제가 production 경제에 들어온다.
    # 그래서 **명시적으로 켤 때만** 허용한다.
    #
    # `Xcode`는 값에 적어도 무시된다(`parse_allowed_environments`) —
    # 로컬 서명이라 신뢰 사슬이 없고, 받아 주면 누구나 조각을 만들 수 있다.
    iap_allowed_environments: str = ""
    # App Store의 **numeric Apple ID**(adamId). Production verifier에 필수다 —
    # Apple 공식 library가 `Environment.PRODUCTION`에서 이 값 없이는 verifier를
    # 만들지도 못한다. 없으면 Production IAP만 조용히 꺼진다(fail closed).
    # bundle id와 다른 값이고, App Store Connect > App Information에서 확인한다.
    # secret이 아니다(공개 앱 식별자).
    iap_app_apple_id: int | None = None

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

    gcp_project_id = env.get("GCP_PROJECT_ID", "").strip()
    if app_env == "production" and not gcp_project_id:
        raise ValueError("GCP_PROJECT_ID is required when APP_ENV=production")

    firestore_database = env.get("FIRESTORE_DATABASE", "").strip() or "(default)"

    # production에서도 **필수가 아니다.** 아직 AdMob ad unit이 없어도 서비스는 떠야 하고,
    # 없으면 광고 보상만 조용히 지급되지 않는다(다른 기능은 그대로).
    admob_ssv_expected_ad_unit = env.get("ADMOB_SSV_EXPECTED_AD_UNIT", "").strip()
    admob_reward_item = env.get("ADMOB_REWARD_ITEM", "").strip()

    # production에서도 필수가 아니다. 없으면 AI 스티커만 조용히 꺼진다.
    ai_image_api_key = env.get("AI_IMAGE_API_KEY", "").strip()
    ai_image_model = env.get("AI_IMAGE_MODEL", "").strip()
    ai_image_quality = env.get("AI_IMAGE_QUALITY", "").strip() or "low"
    ai_result_bucket = env.get("AI_RESULT_BUCKET", "").strip()
    iap_allowed_environments = env.get("IAP_ALLOWED_ENVIRONMENTS", "").strip()

    raw_app_apple_id = env.get("IAP_APP_APPLE_ID", "").strip()
    if raw_app_apple_id:
        try:
            iap_app_apple_id: int | None = int(raw_app_apple_id)
        except ValueError as error:
            # 조용히 무시하면 Production IAP가 이유 없이 꺼진 것처럼 보인다.
            raise ValueError(f"IAP_APP_APPLE_ID={raw_app_apple_id!r} is not an integer") from error
    else:
        iap_app_apple_id = None

    return Settings(
        app_env=app_env,
        log_level=log_level,
        port=port,
        apple_client_id=apple_client_id,
        gcp_project_id=gcp_project_id,
        firestore_database=firestore_database,
        admob_ssv_expected_ad_unit=admob_ssv_expected_ad_unit,
        admob_reward_item=admob_reward_item,
        ai_image_api_key=ai_image_api_key,
        ai_image_model=ai_image_model,
        ai_image_quality=ai_image_quality,
        ai_result_bucket=ai_result_bucket,
        iap_allowed_environments=iap_allowed_environments,
        iap_app_apple_id=iap_app_apple_id,
    )


# query string에 credential이 실려 오는 경로. **access log가 URL을 통째로 남긴다.**
SENSITIVE_QUERY_PATHS = ("/admob/rewarded/ssv",)


class RedactSensitiveQuery(logging.Filter):
    """access log에서 민감한 query string을 지운다.

    우리 코드가 조심하는 것만으로는 부족하다 — uvicorn의 access logger는
    요청 줄을 **query까지 통째로** 남긴다. AdMob SSV callback의 query에는
    Google signature와 우리가 발급한 reward context(사용자를 가리키는 값)가 들어 있어
    그대로 Cloud Run 로그에 적히면 안 된다.

    경로는 남긴다 — callback이 왔다는 사실 자체는 운영에 필요하다.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(self._redact(value) for value in record.args)
        return True

    @staticmethod
    def _redact(value: object) -> object:
        """경로 **끝**으로 판단한다.

        access logger는 `/admob/rewarded/ssv?…`를 주지만, client logger나 proxy는
        `http://host/admob/rewarded/ssv?…`처럼 앞이 붙은 형태를 준다.
        정확히 일치하는지만 보면 그런 줄이 그대로 새어 나간다.
        """
        # str이 아닐 수도 있다 — client logger는 URL 객체를 그대로 넘긴다.
        # logging이 어차피 문자열로 만들 값이므로 여기서 미리 본다.
        text = value if isinstance(value, str) else str(value)
        if "?" not in text:
            return value
        path, _, _ = text.partition("?")
        if any(path.endswith(sensitive) for sensitive in SENSITIVE_QUERY_PATHS):
            return f"{path}?<redacted>"
        return value


def configure_logging(settings: Settings) -> None:
    """표준 logging. 한 줄 한 이벤트.

    credential을 로그에 넣지 않는 것은 formatter가 막아주지 않는다 —
    호출하는 쪽 규칙이다. README의 Security를 따른다.

    예외가 하나 있다: **access log는 우리가 부르는 것이 아니라서** filter로 막는다.
    """
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )

    redaction = RedactSensitiveQuery()
    for name in ("uvicorn.access", "httpx"):
        logger = logging.getLogger(name)
        # 같은 filter가 두 번 붙지 않게 한다(create_app이 test에서 여러 번 불린다).
        if not any(isinstance(existing, RedactSensitiveQuery) for existing in logger.filters):
            logger.addFilter(redaction)
