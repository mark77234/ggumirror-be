"""AI 스티커 — **프롬프트 정리와 provider 경계**.

여기서 고정하는 것:
1. 프롬프트는 정리만 하고 **저장하지 않는다**
2. PNG를 준다고 **확인된 model만** 통과한다
3. **`background`를 보내지 않는다** — production model이 거절하고, 우리는 필요 없다
   (투명은 기기의 기존 배경제거가 만든다)
4. 프롬프트 원문 · API key가 **로그에 절대 남지 않는다**

작업 내구성(멱등 · 차감/환불 · 복구 · 소유자 · HTTP)은 `test_ai_durability.py`가 본다.
실제 OpenAI에 붙지 않는다.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error

import pytest

from app.ai.models import MAX_PROMPT_LENGTH, AIStickerError, AIStickerReason
from app.ai.prompt import normalize_prompt
from app.ai.provider import (
    SUPPORTED_MODELS,
    OpenAIImageProvider,
    UnconfiguredProvider,
    build_provider,
)
from app.core.config import load_settings

PNG = b"\x89PNG\r\n\x1a\n" + b"transparent-pixels"


# MARK: - 프롬프트


def test_collapses_whitespace():
    assert normalize_prompt("  귀여운   고양이\n스티커 ") == "귀여운 고양이 스티커"


def test_empty_prompt_is_rejected():
    for raw in ("", "   ", "\n\t", "​​"):
        with pytest.raises(AIStickerError) as error:
            normalize_prompt(raw)
        assert error.value.reason is AIStickerReason.EMPTY_PROMPT


def test_control_characters_are_stripped():
    assert normalize_prompt("cat\x00\x07dog") == "catdog"


def test_zero_width_characters_cannot_smuggle_length():
    """길이 제한을 zero-width로 우회하지 못한다 — 제거 **후** 길이를 잰다."""
    padded = "a" + "​" * 1000
    assert normalize_prompt(padded) == "a"


def test_too_long_prompt_is_rejected_not_truncated():
    """조용히 자르지 않는다. 자르면 사용자가 쓴 것과 다른 그림에 조각이 나간다."""
    with pytest.raises(AIStickerError) as error:
        normalize_prompt("가" * (MAX_PROMPT_LENGTH + 1))
    assert error.value.reason is AIStickerReason.PROMPT_TOO_LONG


def test_prompt_at_the_limit_passes():
    assert len(normalize_prompt("가" * MAX_PROMPT_LENGTH)) == MAX_PROMPT_LENGTH


# MARK: - provider 선택 (fail closed)


def test_missing_key_or_model_is_unconfigured():
    assert not build_provider("", "gpt-image-2").is_configured
    assert not build_provider("sk-test", "").is_configured
    assert not build_provider("", "").is_configured


def test_production_model_is_accepted():
    """`gpt-image-2`가 production 기본값이다 — PNG를 주므로 통과한다."""
    assert "gpt-image-2" in SUPPORTED_MODELS
    assert build_provider("sk-test", "gpt-image-2").is_configured


def test_verified_models_are_accepted():
    for model in SUPPORTED_MODELS:
        assert build_provider("sk-test", model).is_configured


def test_unverified_model_is_refused():
    """확인하지 않은 이름은 통과시키지 않는다. 추측해서 늘리지 않는다."""
    for model in ("dall-e-3", "gpt-image-9", "imagen-3"):
        assert not build_provider("sk-test", model).is_configured


def test_unknown_model_is_logged_with_its_name(caplog):
    """B-5의 `observed_ad_unit`과 같은 진단. model 이름은 secret이 아니다."""
    with caplog.at_level(logging.ERROR):
        build_provider("sk-test", "some-new-model")
    assert "observed_model=some-new-model" in caplog.text


def test_unconfigured_provider_raises_not_configured():
    with pytest.raises(AIStickerError) as error:
        UnconfiguredProvider().generate("cat")
    assert error.value.reason is AIStickerReason.NOT_CONFIGURED


# MARK: - provider 요청 모양


def test_request_shape(monkeypatch):
    """**`background`를 보내지 않는다.** production model(gpt-image-2)이 거절한다:
    `400 / param=background / "Transparent background is not supported for this model."`
    투명은 기기가 만든다."""
    sent: dict = {}

    class Response:
        def read(self):
            return json.dumps({"data": [{"b64_json": base64.b64encode(PNG).decode()}]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=None):
        sent["body"] = json.loads(request.data.decode())
        sent["headers"] = request.headers
        return Response()

    monkeypatch.setattr("app.ai.provider.urllib.request.urlopen", fake_urlopen)

    provider = OpenAIImageProvider(api_key="sk-test", model="gpt-image-2")
    assert provider.generate("귀여운 고양이") == PNG

    assert "background" not in sent["body"], "production model이 거절하는 값을 보냈다"
    assert sent["body"]["output_format"] == "png"
    assert sent["body"]["size"] == "1024x1024"
    assert sent["body"]["quality"] == "low"
    assert sent["body"]["model"] == "gpt-image-2"
    # 사용자 프롬프트를 버리지 않고 감싼다.
    assert "귀여운 고양이" in sent["body"]["prompt"]


def test_default_quality_is_low():
    from app.ai.provider import build_provider as build

    provider = build("sk-test", "gpt-image-2")
    assert provider._quality == "low"  # noqa: SLF001 — 요청에 실리는 값을 고정한다


def test_non_png_response_is_refused(monkeypatch):
    """PNG가 아니면 알파가 없다. client가 투명한 줄 알고 그리게 두지 않는다."""
    class Response:
        def read(self):
            return json.dumps({"data": [{"b64_json": base64.b64encode(b"\xff\xd8JPEG").decode()}]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("app.ai.provider.urllib.request.urlopen", lambda *a, **k: Response())
    with pytest.raises(AIStickerError) as error:
        OpenAIImageProvider(api_key="sk-test", model="gpt-image-2").generate("cat")
    assert error.value.reason is AIStickerReason.PROVIDER_UNAVAILABLE


@pytest.mark.parametrize(
    "status_code,expected",
    [
        (400, AIStickerReason.PROVIDER_REJECTED),
        (422, AIStickerReason.PROVIDER_REJECTED),
        (401, AIStickerReason.NOT_CONFIGURED),
        (403, AIStickerReason.NOT_CONFIGURED),
        (429, AIStickerReason.PROVIDER_UNAVAILABLE),
        (500, AIStickerReason.PROVIDER_UNAVAILABLE),
    ],
)
def test_http_errors_map_to_reasons(monkeypatch, status_code, expected):
    def raise_http(*args, **kwargs):
        raise urllib.error.HTTPError("u", status_code, "err", {}, None)

    monkeypatch.setattr("app.ai.provider.urllib.request.urlopen", raise_http)
    with pytest.raises(AIStickerError) as error:
        OpenAIImageProvider(api_key="sk-test", model="gpt-image-2").generate("cat")
    assert error.value.reason is expected


def test_provider_error_log_has_no_prompt_or_key(monkeypatch, caplog):
    def raise_http(*args, **kwargs):
        raise urllib.error.HTTPError("u", 400, "err", {}, None)

    monkeypatch.setattr("app.ai.provider.urllib.request.urlopen", raise_http)
    with caplog.at_level(logging.DEBUG), pytest.raises(AIStickerError):
        OpenAIImageProvider(api_key="sk-secret-key", model="gpt-image-2").generate("비밀 프롬프트")

    assert "sk-secret-key" not in caplog.text
    assert "비밀 프롬프트" not in caplog.text
