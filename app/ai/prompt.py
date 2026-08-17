"""프롬프트 정리.

**저장하지 않는 값이라 검증만 하고 흘려보낸다.** 여기서 하는 일은 세 가지뿐이다:
공백 정리 · 길이 제한 · 제어문자 제거.

내용 필터를 여기서 만들지 않는다 — provider가 이미 자기 정책으로 거절하고,
우리가 흉내 내면 provider 정책이 바뀔 때마다 두 곳이 어긋난다.
"""

from __future__ import annotations

import unicodedata

from app.ai.models import MAX_PROMPT_LENGTH, AIStickerError, AIStickerReason


def normalize_prompt(raw: str) -> str:
    """사용자 입력 → provider에 보낼 한 줄.

    줄바꿈과 연속 공백을 하나로 접는다. 제어문자는 지운다 —
    provider 요청 본문에 그대로 실려 나갈 이유가 없다.
    """
    if not isinstance(raw, str):
        raise AIStickerError(AIStickerReason.EMPTY_PROMPT)

    # `Cc`(제어) · `Cf`(형식) 문자를 지운다. zero-width 문자로 길이 제한을 우회하는 것도 막는다.
    #
    # 줄바꿈 · tab은 **지우지 않고 남긴다.** `Cc`라고 통째로 버리면 "고양이\n스티커"가
    # "고양이스티커"로 붙어 다른 말이 된다 — 아래 `split()`이 공백으로 접는다.
    cleaned = "".join(
        character
        for character in raw
        if character.isspace() or unicodedata.category(character) not in ("Cc", "Cf")
    )
    collapsed = " ".join(cleaned.split())

    if not collapsed:
        raise AIStickerError(AIStickerReason.EMPTY_PROMPT)
    # 자르지 않고 거절한다. 조용히 자르면 사용자가 쓴 것과 다른 그림이 나오고,
    # 그 값으로 이미 조각을 차감한 뒤다.
    if len(collapsed) > MAX_PROMPT_LENGTH:
        raise AIStickerError(AIStickerReason.PROMPT_TOO_LONG)
    return collapsed
