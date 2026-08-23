"""내장 템플릿 획득 통계.

Marketplace 상품(B-7E)과 **다른 것**이다. 저쪽은 사용자가 올리고 조각으로 사고파는
상품이고, 이쪽은 앱에 들어 있는 공식 템플릿 32종이다. 조각도 소유권도 없다 —
"몇 명이 받아 갔는가" 하나만 센다.

왜 따로 두는가: 내장 템플릿에는 listing도 snapshot도 판매자도 가격도 없다.
Marketplace 구조에 억지로 끼우면 존재하지 않는 필드를 채워야 한다.

**client가 보낸 문자열을 그대로 세지 않는다.** 등록된 id만 받는다 — 아니면 아무
문자열이나 보내 공개 통계를 부풀릴 수 있다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime

from app.shards.models import utcnow

#: 내장 템플릿 **stable id 목록**. client `StoreCatalog`에서 그대로 가져왔다.
#:
#: 이미지를 서버에 복제하지 않는다 — 그림은 앱 번들에 있고 서버는 id만 안다.
#:
#: 제목으로 검증하지 않는다. 제목은 바뀔 수 있고 같은 제목이 여럿일 수 있다.
ARTWORK_TEMPLATE_IDS = frozenset({
    "art-angel-heart", "art-birthday", "art-cafe-note", "art-checklist",
    "art-cherry-love", "art-cream-note", "art-cyber-love", "art-flash-girl",
    "art-gray-check", "art-ink-heart", "art-lavender-star", "art-love-letter",
    "art-lovely-bow", "art-mint-flower", "art-my-diary", "art-pink-ribbon",
    "art-red-point", "art-retro-pop", "art-scrapbook", "art-sky-cloud",
    "art-spring-bloom", "art-summer-trip", "art-winter-letter", "art-y2k-star",
})

#: 단색 기본 거울 8종. `StoreCatalog.basicPrefix + BasicMirror.id`.
BASIC_TEMPLATE_IDS = frozenset({
    f"basic-{name}"
    for name in ("white", "black", "cream", "softPink", "lavender", "sky", "mint", "gray")
})

#: 통계를 셀 수 있는 것 전부. **여기 없는 id는 거절한다.**
TEMPLATE_IDS = ARTWORK_TEMPLATE_IDS | BASIC_TEMPLATE_IDS

#: 한 번에 맞춰 볼 수 있는 개수. 내장 목록이 32종이라 넉넉하다.
#: 상한을 두는 이유는 요청 하나가 임의로 커지지 않게 하기 위해서다.
MAX_BATCH = 64


class UnknownTemplate(Exception):
    """등록되지 않은 template id. **공개 통계를 임의 문자열로 만들 수 없다.**"""


def is_known(template_id: str) -> bool:
    return template_id in TEMPLATE_IDS


def acquisition_id(user_id: str, template_id: str) -> str:
    """`(userId, templateId)` 하나당 문서 하나.

    **길이를 앞에 붙여 이어 붙인다**(원장 idempotency와 같은 규칙) — 그냥 이으면
    서로 다른 조합이 같은 문자열이 될 수 있다.
    """
    raw = f"{len(user_id)}:{user_id}|{len(template_id)}:{template_id}"
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class TemplateAcquisition:
    """어떤 사용자가 어떤 내장 템플릿을 **처음** 받았다는 기록.

    같은 조합으로 두 번 만들어지지 않는다(문서 id가 같고 `create`로 쓴다).
    """

    user_id: str
    template_id: str
    created_at: datetime = field(default_factory=utcnow)

    @property
    def id(self) -> str:
        return acquisition_id(self.user_id, self.template_id)


@dataclass(frozen=True)
class TemplateStat:
    """템플릿 하나의 공개 통계.

    `download_count`는 **서로 다른 사용자의 최초 획득 수**다. 같은 사람이 다시
    받아도 오르지 않고, 구경만 해도 오르지 않는다(Marketplace와 같은 의미).
    """

    template_id: str
    download_count: int = 0


@dataclass(frozen=True)
class AcquisitionResult:
    """획득 요청 하나의 결과.

    `first_acquisition`이 **"이 요청이 처음 기록했는가"**다. 이미 있었으면 `False`이고
    **실패가 아니다** — `download_count`는 정상 현재 값이다.
    """

    template_id: str
    first_acquisition: bool
    download_count: int
