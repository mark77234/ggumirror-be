"""AI 거울 생성.

**규격은 AI가 지키는 것이 아니라 앱이 찍는 것이다.**

모델은 확률적이라 "1080×2340으로 그려라", "가운데를 #00FF00으로 칠해라"를
매번 정확히 지키지 않는다. 그래서 프롬프트에는 *어디를 비워 두라*고만 말하고,
카메라 자리 표시는 client가 결정적으로 찍은 뒤 Phase C 규격을 지난다.
여기서 하는 일은 **그림 한 장을 받아 오는 것**까지다.

한 장에 조각을 받는다(I-7). **하루 횟수 제한은 없다** — 값이 곧 제한이다.
조각이 있으면 하루에 몇 번이든 만들 수 있고, 조각이 없으면 한 번도 못 만든다.
횟수 제한은 값 위에 덧붙은 두 번째 관문이었고, 값을 낸 사용자를 막는 쪽으로만 일했다.

**경제는 AI 스티커가 쓰는 것을 그대로 쓴다.** 새 예약/정산 체계를 만들지 않았다:
같은 원장, 같은 멱등 키 규칙(`external_event_id`), 같은 환불 방식이다.
차이는 하나뿐이다 — 거울은 응답을 그 자리에서 돌려주는 동기 요청이라
스티커의 lease/재개 기계장치가 필요 없다.

AI 스티커가 이미 쓰는 provider · 저장소 · 프롬프트 정규화를 그대로 쓴다.
두 번째 provider 체계를 만들지 않는다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.ai.models import (
    DEFAULT_MIRROR_PRICE,
    AIStickerError,
    AIStickerReason,
    generation_id,
)
from app.ai.prompt import normalize_prompt
from app.shards.models import InsufficientShards, ShardReason

logger = logging.getLogger(__name__)

#: 사용자 요청을 감싸는 서버 소유 지시문.
#:
#: 좌표를 그대로 적어 주지만 **AI가 정확히 지킬 것이라고 믿지 않는다.**
#: 중요한 것은 "가운데를 비워 둬라"이고, 실제 자리는 나중에 앱이 찍는다.
MIRROR_PROMPT_TEMPLATE = """\
Create a decorative vertical mirror frame artwork, portrait orientation.

The design surrounds a large central camera opening that must stay visually empty.
The opening covers roughly the middle 80% of the width and 83% of the height.

Rules:
- Put decoration around the outer frame and edges.
- Do not place important objects, faces, or text in the central area.
- The central area should be plain and uncluttered.
- No watermarks, no signatures, no UI elements.

Style request from the user:
{user_prompt}
"""


@dataclass(frozen=True)
class GeneratedMirror:
    """만들어진 그림 한 장. **오래 보관하지 않는다** — 응답으로 나가고 끝이다."""

    operation_id: str
    png: bytes
    model: str


class AIMirrorService:
    """프롬프트 하나 → 거울 그림 한 장.

    **저장하지 않는다.** 스티커와 다르게 결과를 GCS에 두지 않고 그대로 돌려준다 —
    생성 원본을 오래 보관할 제품 요구가 없고, 보관하면 비용과 삭제 의무만 는다.
    응답을 잃으면 사용자가 **같은 `requestId`로** 다시 부른다. 그때 조각은 다시
    빠지지 않는다(원장 멱등 키가 같다). 그림은 다시 만들어야 하므로 provider는
    한 번 더 부른다.
    """

    def __init__(
        self,
        provider,
        model: str,
        shards=None,
        price: int = DEFAULT_MIRROR_PRICE,
    ) -> None:
        self._provider = provider
        self._model = model
        # 조각 원장. **AI 스티커가 쓰는 것과 같은 service다.**
        self._shards = shards
        self._price = price

    @property
    def price(self) -> int:
        """한 장에 몇 조각인가. **서버가 정한다** — 요청에 값을 실을 자리가 없다."""
        return self._price

    @property
    def is_available(self) -> bool:
        return getattr(self._provider, "is_configured", False)

    def generate(self, user_id: str, raw_prompt: str, request_id: str) -> GeneratedMirror:
        """- 고칠 수 있는 실패는 **돈을 쓰기 전에** 전부 거른다.
        - 잔액이 모자라면 **provider를 부르기 전에** 거절한다.
        - provider는 **한 번만** 부른다. 자동 재시도는 비용을 두세 배로 만든다 —
          정말 만들어졌는데 응답만 잃었을 수도 있기 때문이다.
        - 차감 뒤에 무엇이 실패하든 **되돌린다.** 사용자가 조각만 잃는 상태를
          만들지 않는다.
        """
        if not self.is_available:
            raise AIStickerError(AIStickerReason.NOT_CONFIGURED)

        # 프롬프트가 잘못된 것은 비용이 아니다. 먼저 거른다.
        prompt = normalize_prompt(raw_prompt)

        # **멱등 키.** 같은 requestId면 원장에 같은 줄을 가리키므로 두 번 빠지지 않는다.
        # 스티커가 쓰는 것과 같은 함수다 — raw id가 키에 남지 않는다.
        operation_id = generation_id(user_id, request_id)

        # 여기서부터 돈이다. 잔액이 모자라면 `InsufficientShards`가 나가고
        # **아무것도 기록되지 않는다**(원장이 보장한다).
        self._debit(user_id, operation_id)

        try:
            composed = MIRROR_PROMPT_TEMPLATE.format(user_prompt=prompt)
            png = self._provider.generate(composed)
        except BaseException:
            # 차감한 뒤 실패했다. 되돌린다 — provider 실패든 그 밖의 무엇이든
            # 사용자가 조각을 잃을 이유가 되지 않는다.
            self._refund(user_id, operation_id)
            raise

        # **프롬프트 원문도 그림도 남기지 않는다.** 남길 이유가 없다.
        logger.info(
            "ai_mirror_generated model=%s prompt_length=%d bytes=%d price=%d",
            self._model, len(prompt), len(png), self._price,
        )
        return GeneratedMirror(operation_id=operation_id, png=png, model=self._model)

    def _debit(self, user_id: str, operation_id: str) -> None:
        """조각을 뺀다. **같은 작업은 몇 번을 불러도 한 번만 빠진다.**"""
        if self._shards is None or self._price <= 0:
            return
        try:
            self._shards.debit(
                user_id, self._price, ShardReason.AI_MIRROR,
                external_event_id=operation_id,
            )
        except InsufficientShards as error:
            logger.info("ai_mirror_insufficient_shards price=%d", self._price)
            raise AIStickerError(AIStickerReason.INSUFFICIENT_SHARDS) from error

    def _refund(self, user_id: str, operation_id: str) -> None:
        """되돌린다. 키가 결정적이라 몇 번 불러도 원장에는 한 줄뿐이다.

        환불 실패가 **원래 오류를 덮지 않는다** — 사용자에게는 생성이 왜 실패했는지가
        먼저이고, 조각은 같은 requestId로 다시 부를 때 정리된다.
        """
        if self._shards is None or self._price <= 0:
            return
        try:
            result = self._shards.credit(
                user_id, self._price, ShardReason.REFUND,
                external_event_id=f"ai_mirror_refund:{operation_id}",
            )
            logger.info("ai_mirror_refunded amount=%d applied=%s", self._price, result.applied)
        except Exception as error:  # noqa: BLE001
            logger.error("ai_mirror_refund_failed error=%s", type(error).__name__)
