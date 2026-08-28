"""AI 스티커 — 생성 작업은 **서버가 소유하는 durable resource**다.

    GET  /ai/stickers/config              쓸 수 있는지 + 가격 (+ 묶인 조각 정리)
    POST /ai/stickers                     {requestId, prompt} → 작업 생성 / 재시도
    GET  /ai/stickers/{generationId}      상태 조회
    GET  /ai/stickers/{generationId}/image 결과 PNG (소유자만)

`POST`는 **멱등이다.** 같은 `(user, requestId)`는 몇 번을 보내도 provider를 한 번만 부르고
조각도 한 번만 나간다. 응답을 잃었으면 같은 requestId로 다시 보내거나
`GET`으로 조회하면 된다 — "한 번 응답하면 사라지는 이미지"가 아니다.

이미지는 **우리 endpoint로만** 나간다. signed URL을 쓰지 않는다 —
그건 그 자체가 credential이라 로그에 한 번 찍히면 누구나 받을 수 있다.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.ai.models import (
    MAX_PROMPT_LENGTH,
    AIStickerError,
    AIStickerReason,
    Generation,
    GenerationStatus,
)
from app.ai.mirror import AIMirrorService
from app.ai.service import AIStickerService
from app.api.deps import CurrentUser, ai_mirror_service, ai_sticker_service

router = APIRouter(prefix="/ai", tags=["ai"])

# 사용자에게 보여줄 말. provider 내부 사정을 옮기지 않는다.
MESSAGES = {
    AIStickerReason.NOT_CONFIGURED: "지금은 AI 스티커를 만들 수 없어요.",
    AIStickerReason.EMPTY_PROMPT: "만들고 싶은 스티커를 적어 주세요.",
    AIStickerReason.PROMPT_TOO_LONG: f"설명은 {MAX_PROMPT_LENGTH}자까지 쓸 수 있어요.",
    AIStickerReason.INVALID_REQUEST_ID: "요청을 만들지 못했어요. 다시 시도해 주세요.",
    # provider가 **입력 단계에서** 거절했다(OpenAI `moderation_blocked`).
    # provider는 이유를 나누지 않는다 — 저작권인지 안전인지 알려주지 않으므로
    # **우리도 단정하지 않는다.** 대신 무엇을 바꾸면 되는지만 말한다.
    AIStickerReason.PROVIDER_REJECTED: (
        "이 요청은 이미지 생성 정책에 따라 처리하기 어려워요. "
        "특정 캐릭터나 브랜드 이름 대신 원하는 색상, 분위기, 패턴을 적어 주세요."
    ),
    AIStickerReason.PROVIDER_UNAVAILABLE: "지금은 만들지 못했어요. 잠시 뒤 다시 시도해 주세요.",
    AIStickerReason.INSUFFICIENT_SHARDS: "거울조각이 모자라요.",
    AIStickerReason.STORAGE_FAILED: "만든 그림을 저장하지 못했어요. 조각은 돌려드렸어요.",
    AIStickerReason.INTERRUPTED: "만드는 도중에 끊겼어요. 조각은 돌려드렸어요.",
    AIStickerReason.NOT_FOUND: "요청을 찾을 수 없어요.",
    AIStickerReason.RESULT_EXPIRED: "보관 기간이 지나 그림을 다시 받을 수 없어요.",
    AIStickerReason.STILL_PENDING: "아직 만드는 중이에요.",
}

STATUS = {
    AIStickerReason.NOT_CONFIGURED: status.HTTP_503_SERVICE_UNAVAILABLE,
    AIStickerReason.EMPTY_PROMPT: status.HTTP_400_BAD_REQUEST,
    AIStickerReason.PROMPT_TOO_LONG: status.HTTP_400_BAD_REQUEST,
    AIStickerReason.INVALID_REQUEST_ID: status.HTTP_400_BAD_REQUEST,
    AIStickerReason.PROVIDER_REJECTED: status.HTTP_422_UNPROCESSABLE_CONTENT,
    AIStickerReason.PROVIDER_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
    AIStickerReason.INSUFFICIENT_SHARDS: status.HTTP_409_CONFLICT,
    AIStickerReason.STORAGE_FAILED: status.HTTP_503_SERVICE_UNAVAILABLE,
    AIStickerReason.INTERRUPTED: status.HTTP_409_CONFLICT,
    # **남의 작업도 404다.** 403으로 나누면 "그 id는 존재한다"가 새어 나간다.
    AIStickerReason.NOT_FOUND: status.HTTP_404_NOT_FOUND,
    AIStickerReason.RESULT_EXPIRED: status.HTTP_410_GONE,
    AIStickerReason.STILL_PENDING: status.HTTP_409_CONFLICT,
}


class AIStickerConfigPayload(BaseModel):
    """CTA를 켤지와 가격. **가격의 출처는 서버 하나다.**"""

    available: bool
    price: int
    # 결과를 며칠 동안 다시 받을 수 있는지. client가 복구 안내 문구를 정할 때 쓴다.
    result_retention_days: int = Field(serialization_alias="resultRetentionDays")

    model_config = {"populate_by_name": True}


class AIStickerRequest(BaseModel):
    """client가 정할 수 있는 것은 이 둘뿐이다.

    `requestId`는 **client가 만드는 멱등 키**다(UUID). 같은 값으로 다시 보내면
    새 생성이 아니라 기존 작업을 돌려받는다 — 응답을 잃었을 때의 유일한 재시도 수단이다.

    `prompt`는 **만들 때만 필요하다.** 이미 있는 작업을 이어받을 때는 비워 보낸다 —
    응답을 잃은 client는 무엇을 적었는지 다시 보낼 수 없고, 서버도 저장하지 않기 때문이다.
    (없는 작업을 빈 프롬프트로 부르면 400이고, 그때는 아무것도 차감되지 않았다는 뜻이다.)
    """

    request_id: str = Field(validation_alias="requestId")
    prompt: str = ""

    model_config = {"populate_by_name": True}


class AIStickerPayload(BaseModel):
    """작업 상태. **이미지가 여기 들어 있지 않다** — 성공했으면 별도 endpoint로 받는다.

    A-1A는 응답에 이미지를 실어 보냈고, 그래서 응답을 잃으면 끝이었다.
    """

    generation_id: str = Field(serialization_alias="generationId")
    status: GenerationStatus
    created_at: str = Field(serialization_alias="createdAt")
    balance: int
    reason: str | None = None
    message: str | None = None

    model_config = {"populate_by_name": True}


def _payload(generation: Generation, balance: int) -> AIStickerPayload:
    reason = generation.failure_reason
    return AIStickerPayload(
        generation_id=generation.id,
        status=generation.status,
        created_at=generation.created_at.isoformat(),
        balance=balance,
        reason=reason.value if reason else None,
        message=MESSAGES.get(reason) if reason else None,
    )


def _failure(error: AIStickerError) -> HTTPException:
    return HTTPException(
        STATUS[error.reason],
        {"reason": error.reason.value, "message": MESSAGES[error.reason]},
    )


@router.get("/stickers/config", response_model=AIStickerConfigPayload)
def sticker_config(
    user: CurrentUser,
    service: Annotated[AIStickerService, Depends(ai_sticker_service)],
) -> AIStickerConfigPayload:
    """조각을 **새로** 움직이지 않는 읽기.

    다만 이 사용자의 묶인 작업이 있으면 여기서 정리한다 — 앱을 켜면 부르는 자리라,
    별도 worker 없이 복구가 도는 가장 자연스러운 지점이다. 실패해도 응답을 막지 않는다.
    """
    service.sweep(user.id)
    return AIStickerConfigPayload(
        available=service.is_available,
        price=service.price,
        result_retention_days=service.retention_days,
    )


class AIMirrorRequest(BaseModel):
    """**프롬프트와 멱등 키뿐이다.** 모델 · provider · 좌표 · 가격을 받는 자리가 없다.

    `requestId`는 client가 만드는 UUID다(스티커와 같은 규칙). 응답을 잃고 같은 값으로
    다시 보내면 **조각이 다시 빠지지 않는다** — 원장 키가 같기 때문이다.
    """

    prompt: str = Field(min_length=1, max_length=300)
    request_id: str = Field(alias="requestId", min_length=8, max_length=64)

    model_config = {"populate_by_name": True}


class AIMirrorConfigPayload(BaseModel):
    available: bool
    #: 한 장에 몇 조각인가. **화면은 이 값을 그대로 보여 준다** — 앱에 숫자를 적지 않는다.
    price: int

    model_config = {"populate_by_name": True}


@router.get("/mirrors/config", response_model=AIMirrorConfigPayload, response_model_by_alias=True)
def ai_mirror_config(
    user: CurrentUser,
    service: Annotated[AIMirrorService, Depends(ai_mirror_service)],
) -> AIMirrorConfigPayload:
    """CTA를 켤지와 값. **하루 횟수 제한이 없다** — 값이 곧 제한이다."""
    return AIMirrorConfigPayload(
        available=service.is_available,
        price=service.price,
    )


@router.post("/mirrors/generate")
def generate_mirror(
    body: AIMirrorRequest,
    user: CurrentUser,
    service: Annotated[AIMirrorService, Depends(ai_mirror_service)],
) -> Response:
    """프롬프트 → 거울 그림 PNG.

    **그림을 그대로 돌려준다** — 서버에 보관하지 않는다. 보관하면 비용과
    삭제 의무만 늘고, 이 그림은 사용자가 저장할지 말지 바로 정한다.

    카메라 자리 표시는 여기서 하지 않는다. 모델은 확률적이라 정확한 좌표와
    색을 맡길 수 없어서, **client가 결정적으로 찍은 뒤** Phase C 규격을 지난다.
    """
    try:
        result = service.generate(user.id, body.prompt, body.request_id)
    except AIStickerError as error:
        raise _failure(error) from error
    return Response(content=result.png, media_type="image/png")


@router.post("/stickers", response_model=AIStickerPayload)
def create_sticker(
    body: AIStickerRequest,
    user: CurrentUser,
    service: Annotated[AIStickerService, Depends(ai_sticker_service)],
) -> AIStickerPayload:
    try:
        generation = service.generate(user.id, body.request_id, body.prompt)
    except AIStickerError as error:
        raise _failure(error) from error
    return _payload(generation, service.balance(user.id))


@router.get("/stickers/{generation_id}", response_model=AIStickerPayload)
def sticker_status(
    generation_id: str,
    user: CurrentUser,
    service: Annotated[AIStickerService, Depends(ai_sticker_service)],
) -> AIStickerPayload:
    try:
        generation = service.status(user.id, generation_id)
    except AIStickerError as error:
        raise _failure(error) from error
    return _payload(generation, service.balance(user.id))


@router.get("/stickers/{generation_id}/image")
def sticker_image(
    generation_id: str,
    user: CurrentUser,
    service: Annotated[AIStickerService, Depends(ai_sticker_service)],
) -> Response:
    """결과 PNG. **Bearer + 소유자 검증을 통과해야만** 나간다.

    signed URL을 만들지 않는다 — URL 자체가 credential이 되면 로그 한 줄로 새어 나간다.
    """
    try:
        png = service.result(user.id, generation_id)
    except AIStickerError as error:
        raise _failure(error) from error
    return Response(
        content=png,
        media_type="image/png",
        # 우리 endpoint를 지나 캐시에 남지 않게 한다. 사용자 콘텐츠다.
        headers={"Cache-Control": "no-store"},
    )
