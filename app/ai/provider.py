"""외부 image provider.

**client는 provider를 직접 부르지 않는다.** API key가 앱 bundle에 들어가면
누구나 꺼내서 우리 요금으로 이미지를 만들 수 있다. key는 서버에만 있다.

httpx를 runtime dependency로 올리지 않는다 — `app/auth/jwks.py`와
`app/ads/verifier.py`가 이미 stdlib urllib으로 외부를 부르고 있고, 여기도 요청 하나다.

## output contract = **valid PNG** (A-1B.2)

예전에는 provider가 **투명 PNG**를 주는 것이 계약이었고, 그래서
`background="transparent"`를 보내고 그것을 지원하는 model만 통과시켰다.

바뀌었다. 실제 capability probe에서 확인된 것:

- `gpt-image-1-mini`는 transparent를 **지원한다**. 하지만 deprecated라 production
  기본 model로 채택하지 않는다
- **`gpt-image-2`는 transparent를 지원하지 않는다** —
  `HTTP 400 / param=background / "Transparent background is not supported for this model."`
  이 model이 현재 production model이다

그래서 allowlist를 억지로 넓히지 않고 **계약을 바꿨다**: provider는 `valid PNG`만 주면 된다.
**서버는 alpha를 요구하지 않는다.** 투명 배경은 client가 만든다 —
꾸미러에는 이미 사진 배경제거(`PhotoStickerMaker`, Vision on-device)가 있고,
AI 결과도 같은 길을 지난다. 배경제거 API를 따로 붙이지 않는다.

model 이름은 여전히 자유롭게 받지 않는다(`SUPPORTED_MODELS`) — 다만 기준이
"투명을 지원하는가"에서 "우리 요청 모양으로 PNG를 주는 것이 확인됐는가"로 바뀌었다.
모르는 model이면 fail closed다.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import urllib.error
import urllib.request
from typing import Protocol

from app.ai.models import IMAGE_SIZE, AIStickerError, AIStickerReason

logger = logging.getLogger(__name__)

OPENAI_IMAGE_URL = "https://api.openai.com/v1/images/generations"

# 우리 요청 모양(`size` · `quality` · `output_format=png`)으로 **PNG를 주는 것이 확인된** model.
# 추측해서 늘리지 않는다.
#
# `gpt-image-2`가 production 기본값이다. 나머지는 설정으로 바꿀 수 있는 선택지일 뿐이고
# 자동으로 전환되지 않는다 — `gpt-image-1-mini`는 transparent까지 되지만 deprecated다.
SUPPORTED_MODELS = frozenset({
    "gpt-image-2", "gpt-image-1", "gpt-image-1.5", "gpt-image-1-mini",
})

# provider HTTP timeout. **Cloud Run request timeout(180초)보다 충분히 짧아야 한다.**
#
#     provider(90s) < Cloud Run(180s) < client(200s)
#
# 이 순서가 중요한 이유: Cloud Run timeout은 container를 죽이지 않는다.
# provider 쪽이 먼저 끊겨야 application이 실패를 **직접 보고** 환불까지 마칠 수 있다.
# 반대로 provider timeout이 더 길면, Cloud Run이 client 연결만 끊은 뒤 worker는
# 계속 돌고 정리는 아무도 하지 않는 구간이 길어진다.
#
# urllib의 timeout은 socket 수준이라 호출은 반드시 돌아온다 — 무한정 매달리지 않는다.
DEFAULT_TIMEOUT = 90.0

# 스티커답게 나오도록 붙이는 고정 지시. 사용자 프롬프트를 대체하지 않고 감싼다.
#
# **투명 배경을 요구하지 않는다.** 배경은 기기에서 지운다(`PhotoStickerMaker`).
# 대신 그 배경제거가 잘 되도록 요구한다 — 피사체 하나 · 또렷한 외곽선 ·
# 균일한 단색 배경. Vision의 foreground mask는 이런 그림에서 가장 정확하다.
STICKER_DIRECTION = (
    "A single die-cut sticker of: {prompt}. "
    "Centered, one subject only, thick clean outline, flat vivid colors, "
    "cute illustration style, no text, no watermark, no drop shadow. "
    "Place the subject on a plain uniform solid white background "
    "with clear separation between the subject and the background."
)


class ImageProvider(Protocol):
    """프롬프트 하나 → PNG bytes 하나. 실패는 `AIStickerError`로만 나온다."""

    def generate(self, prompt: str) -> bytes: ...

    @property
    def is_configured(self) -> bool: ...


class UnconfiguredProvider:
    """provider가 설정되지 않았을 때. **fail closed** — 조용히 기본값으로 떨어지지 않는다.

    `ADMOB_SSV_EXPECTED_AD_UNIT`이 비어 있을 때와 같은 태도다: 서비스는 뜨고,
    다른 기능은 그대로 동작하고, 이 기능만 안 된다.
    """

    is_configured = False

    def generate(self, prompt: str) -> bytes:
        raise AIStickerError(AIStickerReason.NOT_CONFIGURED)


class OpenAIImageProvider:
    """OpenAI images API. **불투명 PNG를 받는다** — 배경은 기기에서 지운다."""

    is_configured = True

    def __init__(
        self,
        api_key: str,
        model: str,
        quality: str = "low",
        url: str = OPENAI_IMAGE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._quality = quality
        self._url = url
        self._timeout = timeout

    def generate(self, prompt: str) -> bytes:
        body = json.dumps({
            "model": self._model,
            "prompt": STICKER_DIRECTION.format(prompt=prompt),
            "size": f"{IMAGE_SIZE}x{IMAGE_SIZE}",
            "quality": self._quality,
            # **`background`를 보내지 않는다.** production model(`gpt-image-2`)이
            # `transparent`를 거절하고(400 / param=background), 우리는 그것이 필요 없다 —
            # 투명은 client가 기존 배경제거로 만든다.
            "output_format": "png",
            "n": 1,
        }).encode()

        request = urllib.request.Request(
            self._url,
            data=body,
            headers={
                # key는 header에만 있고 어디에도 기록되지 않는다.
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            raise self._http_failure(error) from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            logger.warning("ai_provider_unavailable error=%s", type(error).__name__)
            raise AIStickerError(AIStickerReason.PROVIDER_UNAVAILABLE) from error

        return self._decode(payload)

    def _http_failure(self, error: urllib.error.HTTPError) -> AIStickerError:
        """상태 코드와 **provider가 붙인 분류**를 남긴다.

        본문 전체는 넣지 않는다 — 프롬프트가 그대로 되돌아오고 요청 id도 들어 있다.
        남기는 것은 `type`/`code` 두 값뿐이고, 그것은 provider가 정한 고정 어휘라
        사용자 데이터가 아니다.

        **이 두 값이 없으면 400을 구분할 수 없다.** `moderation_blocked`(사용자가
        고칠 수 있는 것)와 잘못된 요청 모양(우리가 고쳐야 하는 것)이 똑같이
        "그 설명으로는 만들 수 없어요"로 보인다 — 실기기 QA에서 실제로 그랬다.

        400 / 422는 프롬프트 문제(정책 거절 · 형식)이고 재시도해도 같다.
        429와 5xx는 잠시 뒤 되는 것이라 사용자에게 다르게 말해야 한다.
        """
        logger.warning(
            "ai_provider_http_error status=%d %s", error.code, _error_labels(error)
        )
        if error.code in (400, 422):
            return AIStickerError(AIStickerReason.PROVIDER_REJECTED)
        # 401 / 403은 우리 key 문제다. 사용자에게 "다시 해보라"고 하면 안 된다.
        if error.code in (401, 403):
            logger.error("ai_provider_credential_rejected status=%d", error.code)
            return AIStickerError(AIStickerReason.NOT_CONFIGURED)
        return AIStickerError(AIStickerReason.PROVIDER_UNAVAILABLE)

    @staticmethod
    def _decode(payload: object) -> bytes:
        """GPT image 계열은 언제나 base64로 준다(URL을 주지 않는다)."""
        try:
            encoded = payload["data"][0]["b64_json"]  # type: ignore[index]
            image = base64.b64decode(encoded, validate=True)
        except (KeyError, IndexError, TypeError, binascii.Error) as error:
            logger.warning("ai_provider_unreadable_response error=%s", type(error).__name__)
            raise AIStickerError(AIStickerReason.PROVIDER_UNAVAILABLE) from error

        # PNG signature. **이것이 계약의 전부다** — alpha는 요구하지 않는다.
        # 형식이 다르면 기기의 배경제거가 읽지 못한다.
        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
            logger.warning("ai_provider_not_png")
            raise AIStickerError(AIStickerReason.PROVIDER_UNAVAILABLE)
        return image


def build_provider(api_key: str, model: str, quality: str = "low") -> ImageProvider:
    """설정 → provider. **하나라도 비었거나 모르는 model이면 fail closed다.**"""
    if not api_key or not model:
        logger.info("ai_provider_not_configured has_key=%s has_model=%s", bool(api_key), bool(model))
        return UnconfiguredProvider()
    if model not in SUPPORTED_MODELS:
        # B-5의 `observed_ad_unit`과 같은 진단이다 — 값을 추측해 넣지 않고,
        # 무엇이 설정됐는지만 안전하게 남긴다(model 이름은 secret이 아니다).
        logger.error("ai_provider_model_unsupported observed_model=%s", model)
        return UnconfiguredProvider()
    return OpenAIImageProvider(api_key=api_key, model=model, quality=quality)


def _error_labels(error: urllib.error.HTTPError) -> str:
    """provider 오류의 **분류만** 꺼낸다. `message`도 요청 id도 꺼내지 않는다.

    OpenAI는 `{"error": {"type": ..., "code": ..., "message": ...}}`를 준다.
    `message`에는 사람이 읽는 문장과 요청 id가 들어 있어 로그에 넣지 않는다.
    본문을 못 읽어도 **원래 오류를 덮지 않는다** — 진단이 실패의 이유가 되면 안 된다.
    """
    try:
        payload = json.loads(error.read().decode())
        found = payload.get("error") or {}
        return f"provider_type={found.get('type')} provider_code={found.get('code')}"
    except Exception:  # noqa: BLE001 — 진단은 절대 실패를 바꾸지 않는다
        return "provider_type=? provider_code=?"
