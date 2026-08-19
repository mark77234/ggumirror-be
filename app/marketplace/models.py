"""Marketplace listing 도메인 (B-7C).

**서버가 정책의 authority다.** client가 등록 비용 · 판매자 · 상태 · counter를
정하는 자리가 없다 — 조각 원장(B-3)에서 배운 것과 같은 규칙이다.

이번 단계는 **등록과 최초 게시까지**다. 구매 · 소유권 · 판매자 지급 · 다운로드 증가는
아직 없다(B-7E). 실제 asset 업로드도 없다(B-7F).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from app.shards.models import ShardReason, utcnow

SCHEMA_VERSION = 1

MAX_TITLE = 24
MAX_DESCRIPTION = 200
PRICE_RANGE = range(0, 1000)


class ContentType(StrEnum):
    """무엇을 파는가. **원장 reason과 비용이 여기서 갈린다.**"""

    MIRROR = "mirror"
    STICKER = "sticker"


class ListingStatus(StrEnum):
    """등록의 상태. **셋뿐이다** — 심사/보류 같은 것을 MVP에 만들지 않는다."""

    DRAFT = "draft"
    PUBLISHED = "published"
    #: 목록에서 빠지고 살 수 없다. **이미 산 사람은 계속 쓴다.**
    UNLISTED = "unlisted"


class MarketplacePublishPolicy:
    """등록 비용. **client 값을 믿지 않는다.**

    client에도 같은 숫자가 있지만(`MirrorPublishPolicy.feeInShards` 등) 그건 화면에
    보여주기 위한 것이고, 실제로 얼마를 받을지는 **여기서만** 정한다.
    요청 body에 비용을 실을 자리가 없다.

    무료 상품(`priceShards == 0`)도 등록 비용은 같다 — "만드는 값"이지
    "파는 값"이 아니기 때문이다.
    """

    FEES: dict[ContentType, int] = {
        ContentType.MIRROR: 10,
        ContentType.STICKER: 5,
    }

    #: 비용 원장의 이유. 콘텐츠 종류마다 다르다 — 원장만 보고 무엇이었는지 알 수 있어야 한다.
    REASONS: dict[ContentType, ShardReason] = {
        ContentType.MIRROR: ShardReason.MIRROR_PUBLISH_FEE,
        ContentType.STICKER: ShardReason.STICKER_PUBLISH_FEE,
    }

    @classmethod
    def fee(cls, content_type: ContentType) -> int:
        return cls.FEES[content_type]

    @classmethod
    def reason(cls, content_type: ContentType) -> ShardReason:
        return cls.REASONS[content_type]


@dataclass(frozen=True)
class Snapshot:
    """게시되는 **불변 내용물**. 판매자가 나중에 바꿔도 구매자 권리가 깨지지 않는다.

    이번 단계에서는 존재와 주인만 확인한다 — 실제 asset 업로드는 B-7F다.
    """

    id: str
    seller_user_id: str
    content_type: ContentType
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True)
class Listing:
    """상점에 올라가는 것 하나."""

    id: str
    seller_user_id: str
    content_type: ContentType
    title: str
    description: str
    price_shards: int
    snapshot_id: str
    status: ListingStatus = ListingStatus.DRAFT
    #: **한 번 True가 되면 다시 False가 되지 않는다.** republish가 공짜인 근거다.
    publish_fee_paid: bool = False
    #: 서버가 센다. 앱이 올리지 않는다. 실제 증가는 B-7E(소유권 획득 성공).
    download_count: int = 0
    like_count: int = 0
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    #: **최초** 게시 시각. republish가 덮어쓰지 않는다(UI의 "업로드 날짜").
    published_at: datetime | None = None
    schema_version: int = SCHEMA_VERSION

    @staticmethod
    def new_id() -> str:
        return str(uuid.uuid4())

    @property
    def is_visible(self) -> bool:
        return self.status is ListingStatus.PUBLISHED


class MarketplaceSort(StrEnum):
    """공개 목록 정렬. **client UI-P3와 같은 계약이다.**"""

    LATEST = "latest"
    POPULAR = "popular"
    LIKES = "likes"

    @classmethod
    def default(cls) -> "MarketplaceSort":
        return cls.LATEST

    def sorted(self, listings: list["Listing"]) -> list["Listing"]:
        """**"인기"의 authority는 다운로드 수 하나다.**

        좋아요와 섞은 가중 점수를 만들지 않는다 — 섞으면 왜 이 순서인지 아무도
        설명할 수 없다. 이름만 "인기 순"이다.

        마지막 열쇠는 언제나 `id`다. 값이 모두 같아도 순서가 실행마다 흔들리면
        목록이 이유 없이 재배열돼 보인다.
        """
        return sorted(listings, key=self._key)

    def _key(self, listing: "Listing"):
        # 큰 것이 먼저여야 하는 값은 음수로 뒤집는다. id만 오름차순이다.
        published = -(listing.published_at.timestamp() if listing.published_at else 0.0)
        match self:
            case MarketplaceSort.LATEST:
                return (published, listing.id)
            case MarketplaceSort.POPULAR:
                return (-listing.download_count, published, listing.id)
            case MarketplaceSort.LIKES:
                return (-listing.like_count, -listing.download_count, published, listing.id)


@dataclass(frozen=True)
class PublishResult:
    """게시 결과.

    `published`는 **이번 요청이 상태를 바꿨는가**다 — 이미 게시돼 있으면 `False`이고
    그때도 실패가 아니다(B-4 `claimed` · B-6 `credited`와 같은 뜻).
    `fee_charged`도 마찬가지로 **이번 요청이 받았는가**다.
    """

    listing: Listing
    published: bool
    fee_charged: bool
    fee_shards: int
    balance: int


class MarketplaceError(Exception):
    """등록 처리 실패. endpoint가 client에 맞는 응답으로 바꾼다."""


class ListingNotFound(MarketplaceError):
    """없거나 **내 것이 아니다.** 둘을 구분해 알려주지 않는다 —
    남의 listing이 존재한다는 사실 자체가 정보다(AI 스티커와 같은 규칙)."""


class SnapshotNotFound(MarketplaceError):
    """올릴 내용물이 없다. 서버가 모르는 내용을 게시하지 않는다."""


class InvalidListing(MarketplaceError):
    """제목 · 설명 · 가격 · 종류가 규칙에 맞지 않는다."""


class InvalidTransition(MarketplaceError):
    """그 상태에서 할 수 없는 일이다(예: draft를 내리기)."""


def normalized_title(raw: str) -> str:
    title = raw.strip()
    if not title or len(title) > MAX_TITLE:
        raise InvalidListing("title")
    return title


def normalized_description(raw: str) -> str:
    description = raw.strip()
    if len(description) > MAX_DESCRIPTION:
        raise InvalidListing("description")
    return description


def checked_price(price: int) -> int:
    """0(무료)도 정상이다. `bool`이 `int`로 새어 들어오는 것도 막는다."""
    if not isinstance(price, int) or isinstance(price, bool) or price not in PRICE_RANGE:
        raise InvalidListing("price")
    return price
