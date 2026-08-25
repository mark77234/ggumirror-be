"""AI 거울 생성.

**규격은 AI가 지키는 것이 아니라 앱이 찍는 것이다.**

모델은 확률적이라 "1080×2340으로 그려라", "가운데를 #00FF00으로 칠해라"를
매번 정확히 지키지 않는다. 그래서 프롬프트에는 *어디를 비워 두라*고만 말하고,
카메라 자리 표시는 client가 결정적으로 찍은 뒤 Phase C 규격을 지난다.
여기서 하는 일은 **그림 한 장을 받아 오는 것**까지다.

조각을 받지 않는다(§16). 대신 하루 횟수로 비용을 막는다 — 값을 정하지 않은
기능에 과금 경로를 먼저 만들면 나중에 되돌리기 어렵다.

AI 스티커가 이미 쓰는 provider · 저장소 · 프롬프트 정규화를 그대로 쓴다.
두 번째 provider 체계를 만들지 않는다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from app.ai.models import AIStickerError, AIStickerReason
from app.ai.prompt import normalize_prompt
from app.shards.attendance import attendance_date

logger = logging.getLogger(__name__)

#: 하루에 provider까지 가는 시도 횟수. **서버가 정한다** — client가 못 늘린다.
DEFAULT_DAILY_LIMIT = 3

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


class DailyLimitReached(Exception):
    """오늘 몫을 다 썼다. **실패가 아니라 정상적인 거절이다.**"""


@dataclass(frozen=True)
class GeneratedMirror:
    """만들어진 그림 한 장. **오래 보관하지 않는다** — 응답으로 나가고 끝이다."""

    operation_id: str
    png: bytes
    model: str


class AIMirrorQuota:
    """`(사용자, KST 날짜)`당 시도 횟수.

    조각 원장에 기록하지 않는다 — 돈이 오간 일이 아니라 **비용을 막는 계수기**다.
    확인과 증가가 한 transaction 안에서 일어나야 동시에 들어온 두 요청이
    상한을 넘기지 못한다.
    """

    def __init__(self, db, collection: str, limit: int = DEFAULT_DAILY_LIMIT) -> None:
        self._db = db
        self._collection = collection
        self._limit = limit

    @property
    def limit(self) -> int:
        return self._limit

    @staticmethod
    def key(user_id: str, day: str) -> str:
        return f"{user_id}:{day}"

    def used(self, user_id: str, now: datetime) -> int:
        snapshot = self._db.collection(self._collection).document(
            self.key(user_id, attendance_date(now))
        ).get()
        return int((snapshot.to_dict() or {}).get("count") or 0) if snapshot.exists else 0

    def claim(self, user_id: str, now: datetime) -> int:
        """한 번 쓴다. 상한을 넘으면 `DailyLimitReached`이고 **아무것도 늘지 않는다.**"""
        from google.cloud import firestore

        day = attendance_date(now)
        reference = self._db.collection(self._collection).document(self.key(user_id, day))

        @firestore.transactional
        def run(transaction) -> int:
            snapshot = reference.get(transaction=transaction)
            used = int((snapshot.to_dict() or {}).get("count") or 0) if snapshot.exists else 0
            if used >= self._limit:
                raise DailyLimitReached()
            transaction.set(
                reference, {"userId": user_id, "day": day, "count": used + 1}, merge=True
            )
            return used + 1

        return run(self._db.transaction())


class AIMirrorService:
    """프롬프트 하나 → 거울 그림 한 장.

    **저장하지 않는다.** 스티커와 다르게 결과를 GCS에 두지 않고 그대로 돌려준다 —
    생성 원본을 오래 보관할 제품 요구가 없고, 보관하면 비용과 삭제 의무만 는다.
    응답을 잃으면 사용자가 다시 만든다(그때는 하루 몫을 한 번 더 쓴다).
    """

    def __init__(self, provider, quota: AIMirrorQuota | None, model: str) -> None:
        self._provider = provider
        self._quota = quota
        self._model = model

    @property
    def is_available(self) -> bool:
        return getattr(self._provider, "is_configured", False) and self._quota is not None

    @property
    def daily_limit(self) -> int:
        return self._quota.limit if self._quota else 0

    def remaining(self, user_id: str, now: datetime) -> int:
        if self._quota is None:
            return 0
        return max(0, self._quota.limit - self._quota.used(user_id, now))

    def generate(self, user_id: str, raw_prompt: str, now: datetime) -> GeneratedMirror:
        """- 고칠 수 있는 실패는 **몫을 쓰기 전에** 전부 거른다.
        - provider는 **한 번만** 부른다. 자동 재시도는 비용을 두세 배로 만든다 —
          정말 만들어졌는데 응답만 잃었을 수도 있기 때문이다.
        """
        if not self.is_available:
            raise AIStickerError(AIStickerReason.NOT_CONFIGURED)

        # 프롬프트가 잘못된 것은 비용이 아니다. 먼저 거른다.
        prompt = normalize_prompt(raw_prompt)

        # 여기서부터 비용이다. **provider에 가는 시도만** 센다.
        assert self._quota is not None
        used = self._quota.claim(user_id, now)

        composed = MIRROR_PROMPT_TEMPLATE.format(user_prompt=prompt)
        png = self._provider.generate(composed)

        # **프롬프트 원문도 그림도 남기지 않는다.** 남길 이유가 없다.
        logger.info(
            "ai_mirror_generated model=%s prompt_length=%d used=%d/%d bytes=%d",
            self._model, len(prompt), used, self._quota.limit, len(png),
        )
        return GeneratedMirror(operation_id="", png=png, model=self._model)
