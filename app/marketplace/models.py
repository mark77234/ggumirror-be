"""Marketplace listing 도메인 (B-7C).

**서버가 정책의 authority다.** client가 등록 비용 · 판매자 · 상태 · counter를
정하는 자리가 없다 — 조각 원장(B-3)에서 배운 것과 같은 규칙이다.

이번 단계는 **등록과 최초 게시까지**다. 구매 · 소유권 · 판매자 지급 · 다운로드 증가는
아직 없다(B-7E). 실제 asset 업로드도 없다(B-7F).
"""

from __future__ import annotations

import hashlib
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
    """등록의 상태. 심사/보류 같은 것을 MVP에 만들지 않는다."""

    DRAFT = "draft"
    PUBLISHED = "published"
    #: 목록에서 빠지고 살 수 없다. **이미 산 사람은 계속 쓴다.**
    #:
    #: 판매자가 잠시 내린 것이고 다시 올릴 수 있다. 새 client UI는 이 상태를
    #: 만들지 않지만(삭제를 쓴다) 기존 문서와 `unpublish` endpoint 때문에 남긴다.
    UNLISTED = "unlisted"
    #: **끝 상태.** 판매자가 삭제했다. 다시 올릴 수 없다.
    #:
    #: `unlisted`와 다른 것이다 — 사용자가 "삭제"를 골랐으면 되살아나는 상품처럼
    #: 행동해서는 안 된다. 그렇다고 실제로 지우지도 않는다: snapshot · GCS object ·
    #: 소유권 · 원장은 **그대로 남는다.** 이미 산 사람이 계속 받아야 하기 때문이다.
    #: 등록비도 돌려주지 않는다.
    DELETED = "deleted"

    @property
    def is_terminal(self) -> bool:
        """되돌릴 수 없는 상태인가. 여기서 나가는 전이는 없다."""
        return self is ListingStatus.DELETED


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

    #: 구매자 차감 / 판매자 지급의 이유. 등록비와 **같은 규칙**으로 종류별로 나눈다.
    PURCHASE_REASONS: dict[ContentType, ShardReason] = {
        ContentType.MIRROR: ShardReason.MIRROR_PURCHASE,
        ContentType.STICKER: ShardReason.STICKER_PURCHASE,
    }
    SALE_REASONS: dict[ContentType, ShardReason] = {
        ContentType.MIRROR: ShardReason.MIRROR_SALE,
        ContentType.STICKER: ShardReason.STICKER_SALE,
    }

    @classmethod
    def fee(cls, content_type: ContentType) -> int:
        return cls.FEES[content_type]

    @classmethod
    def reason(cls, content_type: ContentType) -> ShardReason:
        return cls.REASONS[content_type]

    @classmethod
    def purchase_reason(cls, content_type: ContentType) -> ShardReason:
        return cls.PURCHASE_REASONS[content_type]

    @classmethod
    def sale_reason(cls, content_type: ContentType) -> ShardReason:
        return cls.SALE_REASONS[content_type]


@dataclass(frozen=True)
class Snapshot:
    """게시되는 **불변 내용물**. 판매자가 나중에 바꿔도 구매자 권리가 깨지지 않는다.

    ⚠️ **이 문서가 있다는 것은 asset이 전부 올라갔다는 뜻이다.** 업로드가 반쪽으로
    끝나면 문서를 만들지 않는다 — 그래서 listing이 반쪽 snapshot을 참조할 수 없다.

    한 번 만들어진 `id`가 가리키는 내용은 **영원히 같다.** 수정 기능이 생기면
    새 `id`를 만든다(같은 자리를 덮어쓰지 않는다).
    """

    id: str
    seller_user_id: str
    content_type: ContentType
    #: 서버가 계산한 manifest SHA-256. client가 보낸 값을 authority로 쓰지 않는다.
    manifest_checksum: str = ""
    #: 이 snapshot이 어느 **local 콘텐츠**에서 나왔는지. `MyMirror.id` /
    #: `StickerProject.id`다 — manifest top-level `id`에서 그대로 뽑는다.
    #:
    #: 판매자가 "내 거울 → 판매 중"에서 자기 상품을 찾으려면 이 연결이 필요하다.
    #: 제목으로 맞추면 같은 제목이 여러 개일 때 틀린다.
    #:
    #: **공개 응답에 넣지 않는다.** 판매자 자신에게만 돌려준다.
    #: 옛 문서에는 이 값이 없다 — 그때는 저장된 manifest에서 읽는다(rewrite 없음).
    source_content_id: str = ""
    asset_count: int = 0
    total_bytes: int = 0
    created_at: datetime = field(default_factory=utcnow)
    schema_version: int = SCHEMA_VERSION

    @staticmethod
    def new_id() -> str:
        return str(uuid.uuid4())

    @property
    def is_complete(self) -> bool:
        """B-7C 시절 fixture처럼 asset 없이 만들어진 문서를 구분한다.

        불완전한 snapshot으로는 **미리보기도 템플릿도 내보내지 않는다** —
        거짓 그림을 만들지 않는다.
        """
        return bool(self.manifest_checksum)


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
class Ownership:
    """**"이 사람이 이것을 갖고 있다"** — 구매와 소유를 한 문서로 합쳤다(MVP 결정).

    구매 당시의 `snapshotId` · `sellerUserId` · `pricePaid`를 **고정 저장한다.**
    나중에 판매자가 내리거나 정책이 바뀌어도 산 사람의 권리가 흔들리지 않는다.

    만든 뒤 **고치지 않는다.** 같은 사람이 같은 상품을 다시 사도 이 문서를
    조용히 교체하지 않는다(`create`만 쓴다).
    """

    id: str
    user_id: str
    listing_id: str
    seller_user_id: str
    snapshot_id: str
    price_paid: int
    #: 무료면 둘 다 `None` — 조각이 움직이지 않았으므로 원장 줄이 없다.
    buyer_ledger_entry_id: str | None = None
    seller_ledger_entry_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class Like:
    """"이 사람이 이것을 좋아한다" — **관계 자체가 authority**다.

    `listing.likeCount`는 조회 성능용 projection이고, 진실은 이 문서들의 개수다.
    최소 필드만 담는다 — `sellerUserId` · `title` · count를 복사하지 않는다.
    """

    id: str
    user_id: str
    listing_id: str
    created_at: datetime = field(default_factory=utcnow)
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class LikeResult:
    """좋아요 결과.

    `changed`는 **이번 요청이 관계를 바꿨는가**다. 같은 요청을 반복하면 `False`이고
    그때도 실패가 아니다 — 연타·재시도는 정상 동작이다.
    """

    listing_id: str
    liked: bool
    changed: bool
    like_count: int


@dataclass(frozen=True)
class PurchaseResult:
    """획득 결과.

    `purchased`는 **이번 요청이 소유권을 만들었는가**다. 이미 갖고 있으면 `False`이고
    그때도 실패가 아니다 — 재시도·연타는 정상 동작이다(B-4 `claimed`와 같은 뜻).
    """

    ownership: Ownership
    purchased: bool
    already_owned: bool
    price_paid: int
    balance: int
    download_count: int


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


class SelfLike(MarketplaceError):
    """자기 상품에 좋아요를 누를 수 없다.

    판매자가 자기 상품의 인기도를 직접 올리지 못하게 한다.
    **취소는 허용한다** — 잘못 생긴 관계를 지우는 동작이기 때문이다.
    """


class LikeCountInconsistent(MarketplaceError):
    """`likeCount` projection이 관계와 어긋났다.

    좋아요 관계가 있는데 count가 0이거나 count가 음수인 상태다. **조용히 보정하지
    않는다** — 거짓 값으로 덮으면 언제부터 틀렸는지 알 수 없게 된다.
    """


class SelfPurchase(MarketplaceError):
    """자기 상품은 살 수 없다.

    경제적으로 no-op인데 원장에 -P/+P 두 줄이 쌓여 판매 통계가 오염되고,
    같은 지갑을 한 transaction에서 두 번 만지는 특수 경로가 생긴다.
    판매자는 **사지 않고도** 자기 상품을 쓸 권리가 있다.
    """


class InvalidTransition(MarketplaceError):
    """그 상태에서 할 수 없는 일이다(예: draft를 내리기)."""


def ownership_id(user_id: str, listing_id: str) -> str:
    """소유권 문서 ID. **`(구매자, 상품)` 조합이 곧 business 멱등 열쇠다.**

    이 자리에 `create()`로 쓰므로 중복 구매가 **구조적으로** 막힌다 —
    "조회해서 없으면 만든다"로 바꾸지 않는다(그 사이에 틈이 생긴다).

    raw user id · listing id를 문서 ID에 노출하지 않는다. 길이 접두사 canonical
    encoding은 원장(`idempotency_hash`) · IAP claim과 **같은 규칙**이다.
    """
    canonical = "|".join(
        f"{len(part.encode())}:{part}"
        for part in ("marketplace_ownership", user_id, listing_id)
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def like_id(user_id: str, listing_id: str) -> str:
    """좋아요 문서 ID. `(사용자, 상품)` 조합이 곧 유일성 열쇠다.

    소유권 · 원장과 **같은 규칙**이다 — raw id를 문서 ID에 노출하지 않는다.
    """
    canonical = "|".join(
        f"{len(part.encode())}:{part}"
        for part in ("marketplace_like", user_id, listing_id)
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


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
