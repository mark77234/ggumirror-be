"""앱 안 알림 기록 (Phase F).

**전달과 기록을 나눈다.** APNs가 실패해도 이 기록은 남는다 — 판매자가 앱을 열면
무엇이 팔렸는지 볼 수 있어야 하고, 그것이 push 성공 여부에 매달리면 안 된다.

조각 원장과 다른 것이다. 여기에는 잔액도 금액 authority도 없다 —
`shard_amount`는 **원장이 이미 확정한 값의 사본**이고 화면에 보여 주기 위한 것이다.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from app.shards.models import utcnow

SCHEMA_VERSION = 1

#: 한 번에 읽는 알림 수. 판매자 한 명이 훑는 목록이라 크게 잡을 이유가 없다.
PAGE_SIZE = 25
MAX_PAGE = 50


class NotificationType(StrEnum):
    """알림의 종류.

    **모르는 값 하나 때문에 목록 전체가 깨지면 안 된다.** 나중에 서버가 새 종류를
    보내도 옛 앱은 그것만 일반 알림으로 보여 주고 나머지를 계속 읽어야 한다.
    그래서 `UNKNOWN`이 있고, 읽기는 `of()`를 지난다.
    """

    MARKETPLACE_SALE = "marketplace_sale"
    #: 새로 올라온 거울 모아 보기. 특정 상품 하나에 매이지 않는다.
    MIRROR_DIGEST = "mirror_digest"
    #: 다시 둘러보라는 소식. **알림센터에 쌓지 않는다**(push 전용).
    RECOMMENDATION = "recommendation"
    #: 이 앱이 모르는 종류. 저장하는 값이 아니라 **읽을 때만** 나온다.
    UNKNOWN = "unknown"

    @classmethod
    def of(cls, raw) -> "NotificationType":
        """모르는 값을 만나도 던지지 않는다.

        예전에는 `NotificationType(...)`을 그대로 불러서, 새 종류가 하나라도
        섞이면 그 페이지 전체가 `ValueError`로 죽었다 — 알림센터가 통째로 비었다.
        """
        if not raw:
            return cls.MARKETPLACE_SALE
        try:
            return cls(raw)
        except ValueError:
            return cls.UNKNOWN

    @property
    def is_stored_in_center(self) -> bool:
        """알림센터에 남길 종류인가.

        추천 소식은 남기지 않는다 — 홍보성 알림이 쌓이면 판매 소식이 묻힌다.
        """
        return self is not NotificationType.RECOMMENDATION


@dataclass(frozen=True)
class NotificationEvent:
    """"내 상품이 팔렸다" 하나.

    `title_snapshot`은 **팔린 그때의 제목**이다. 나중에 판매자가 제목을 바꿔도
    기록은 그때 팔린 것을 가리켜야 한다. listing을 다시 읽어 오지 않는 이유이기도
    하다 — 목록 하나에 상품마다 조회가 붙는다.

    **구매자를 담지 않는다.** id도 이름도 없다 — 누가 샀는지는 판매자가 알 일이 아니고,
    잠금화면에 뜰 수 있는 값이다.
    """

    id: str
    user_id: str
    type: NotificationType
    #: **판매 알림에만 있다.** 모아 보기는 상품 하나에 매이지 않는다.
    listing_id: str = ""
    content_type: str = ""
    title_snapshot: str = ""
    #: 이번 판매로 판매자가 받은 조각. **원장이 확정한 값의 사본**이다.
    shard_amount: int = 0
    #: 종류와 무관하게 화면이 그대로 보여 줄 수 있는 문구.
    #:
    #: 판매 알림에는 없다(옛 문서에도 없다) — 그때는 화면이 상품 이름과 조각으로
    #: 문장을 만든다. 모아 보기처럼 상품이 없는 알림은 이 값을 쓴다.
    headline: str = ""
    body: str = ""
    created_at: datetime = field(default_factory=utcnow)
    read_at: datetime | None = None
    schema_version: int = SCHEMA_VERSION

    @property
    def is_read(self) -> bool:
        return self.read_at is not None

    @staticmethod
    def new_id() -> str:
        return str(uuid.uuid4())


def sale_event_id(ownership_id_value: str) -> str:
    """판매 알림 문서 ID. **소유권 문서 하나에 알림 하나다.**

    소유권 생성이 이미 `(구매자, 상품)` 조합으로 멱등하므로, 그 열쇠에서 알림 id를
    끌어오면 **재시도·연타가 알림을 두 번 만들 수 없다.** "조회해서 없으면 만든다"로
    바꾸지 않는다 — 그 사이에 틈이 생긴다.

    raw id를 문서 ID에 노출하지 않는다. 원장 · 소유권 · IAP claim과 같은 규칙이다.
    """
    canonical = "|".join(
        f"{len(part.encode())}:{part}"
        for part in ("notification_sale", ownership_id_value)
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class SaleStat:
    """상품 하나가 몇 번 팔렸는가.

    **새 counter를 만들지 않았다.** `listing.downloadCount`가 이미 정확히 이 값이다 —
    소유권이 새로 생길 때만 오르고, 같은 사람이 다시 받아도 오르지 않으며,
    구매와 같은 transaction 안에서 오른다. 여기에 두 번째 counter를 두면 둘이
    어긋날 수 있는 자리가 새로 생기고, 어긋났을 때 어느 쪽이 맞는지 알 수 없다.

    그래서 이 값은 저장되지 않는다. 판매자의 listing에서 **읽어서 만든다.**
    알림 목록 pagination과 무관하므로 "총 판매 횟수"가 언제나 정확하다.
    """

    listing_id: str
    content_type: str
    title: str
    sale_count: int
    price_shards: int


class NotificationError(Exception):
    """알림 처리 실패."""


class NotificationNotFound(NotificationError):
    """없거나 **내 것이 아니다.** 둘을 구분해 알려주지 않는다."""
