"""외부 image provider.

**client는 provider를 직접 부르지 않는다.** API key가 앱 bundle에 들어가면
누구나 꺼내서 우리 요금으로 이미지를 만들 수 있다. key는 서버에만 있다.

httpx를 runtime dependency로 올리지 않는다 — `app/auth/jwks.py`와
`app/ads/verifier.py`가 이미 stdlib urllib으로 외부를 부르고 있고, 여기도 요청 하나다.

## 투명 PNG

"투명 배경을 지원한다"를 추측하지 않았다. 공식 문서에서 확인한 것만 쓴다:

- OpenAI images API는 `background="transparent"`를 받는다. 값을 쓰려면
  `output_format`이 투명을 담을 수 있어야 한다(`png` 또는 `webp`)
- 이 parameter는 **GPT image 계열 일부만** 지원한다.
  `gpt-image-1` · `gpt-image-1.5` · `gpt-image-1-mini`는 되고,
  **`gpt-image-2`는 안 된다** — `background="transparent"`를 보내면 오류다
- Google Gemini / Imagen 계열은 alpha channel 자체를 내보내지 못한다

그래서 model 이름을 자유롭게 받지 않고 **확인된 것만 통과시킨다**(`TRANSPARENT_MODELS`).
모르는 model이 설정되면 지급도 생성도 하지 않고 `not_configured`로 멈춘다 —
잘못 설정한 채로 불투명한 사각형 스티커를 만들어 조각만 태우는 것보다 낫다.
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

# `background="transparent"`를 공식 문서에서 확인한 model만. 추측해서 늘리지 않는다.
TRANSPARENT_MODELS = frozenset({"gpt-image-1", "gpt-image-1.5", "gpt-image-1-mini"})

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
STICKER_DIRECTION = (
    "A single die-cut sticker of: {prompt}. "
    "Centered, one subject only, thick clean outline, flat vivid colors, "
    "cute illustration style, no text, no watermark, no drop shadow, "
    "completely transparent background."
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
    """OpenAI images API. 투명 PNG를 **native로** 받는다 — 배경 제거 후처리가 없다."""

    is_configured = True

    def __init__(
        self,
        api_key: str,
        model: str,
        quality: str = "medium",
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
            # 이 둘은 짝이다. `transparent`를 쓰려면 format이 투명을 담을 수 있어야 한다.
            "background": "transparent",
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
        """상태 코드만 남긴다. **응답 본문을 로그에 넣지 않는다** — 프롬프트가 그대로 되돌아온다.

        400 / 422는 프롬프트 문제(정책 거절 · 형식)이고 재시도해도 같다.
        429와 5xx는 잠시 뒤 되는 것이라 사용자에게 다르게 말해야 한다.
        """
        logger.warning("ai_provider_http_error status=%d", error.code)
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

        # PNG signature. 다른 형식이 오면 client가 알파를 기대하고 깨진 것을 그린다.
        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
            logger.warning("ai_provider_not_png")
            raise AIStickerError(AIStickerReason.PROVIDER_UNAVAILABLE)
        return image


def build_provider(api_key: str, model: str, quality: str = "medium") -> ImageProvider:
    """설정 → provider. **하나라도 비었거나 모르는 model이면 fail closed다.**"""
    if not api_key or not model:
        logger.info("ai_provider_not_configured has_key=%s has_model=%s", bool(api_key), bool(model))
        return UnconfiguredProvider()
    if model not in TRANSPARENT_MODELS:
        # B-5의 `observed_ad_unit`과 같은 진단이다 — 값을 추측해 넣지 않고,
        # 무엇이 설정됐는지만 안전하게 남긴다(model 이름은 secret이 아니다).
        logger.error("ai_provider_model_lacks_transparency observed_model=%s", model)
        return UnconfiguredProvider()
    return OpenAIImageProvider(api_key=api_key, model=model, quality=quality)
