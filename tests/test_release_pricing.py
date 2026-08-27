"""1.1.0 출시 가격 정책. **숫자를 여기서 못 박는다.**

세 값이 서로 다른 축이고, 하나를 바꿀 때 나머지가 조용히 따라 움직이면 안 된다:

    AI 거울 생성      5조각   ─┐ "AI에게 한 장 부탁하는 값"
    AI 스티커 생성    5조각   ─┘  (`AI_GENERATION_PRICE`)
    스티커 상점 등록  10조각  ─┐ "상점에 내놓는 값"
    거울 상점 등록    10조각  ─┘  (`MarketplacePublishPolicy.FEES`)

값만 바꾼다. 차감 순서 · 멱등 · 환불 · 잔액 부족 처리는 기존 계약 그대로이고,
여기서는 **경계값**(정확히 낼 수 있는 잔액, 하나 모자란 잔액)을 확인한다.
"""

from __future__ import annotations

import pytest

from app.ai.models import (
    AI_GENERATION_PRICE,
    DEFAULT_MIRROR_PRICE,
    DEFAULT_STICKER_PRICE,
    AIStickerError,
    AIStickerReason,
    GenerationStatus,
)
from app.ai.mirror import AIMirrorService
from app.ai.service import AIStickerService
from app.ai.storage import InMemoryGenerationStorage
from app.ai.store import InMemoryGenerationStore
from app.marketplace.models import ContentType, ListingStatus, MarketplacePublishPolicy
from app.shards.models import InsufficientShards, ShardReason
from app.shards.service import ShardLedgerService
from app.shards.store import InMemoryShardStore

ALICE = "11111111-2222-4333-8444-555555555555"

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"


class FakeProvider:
    """**돈을 쓰지 않는다.** 몇 번 불렸는지만 기억한다."""

    is_configured = True

    def __init__(self, failure: Exception | None = None) -> None:
        self.calls = 0
        self.failure = failure

    def generate(self, prompt: str) -> bytes:
        self.calls += 1
        if self.failure:
            raise self.failure
        return PNG


@pytest.fixture
def shard_store():
    return InMemoryShardStore()


@pytest.fixture
def shards(shard_store):
    return ShardLedgerService(shard_store)


def fund(shards, amount: int, user: str = ALICE) -> None:
    shards.credit(
        user, amount, ShardReason.ADMIN_ADJUSTMENT, external_event_id=f"seed:{user}:{amount}"
    )


def balance(shards, user: str = ALICE) -> int:
    return shards.wallet(user).balance


# MARK: - 정책 값


def test_ai_generation_costs_five():
    """거울도 스티커도 5조각. 하나의 authority에서 온다."""
    assert AI_GENERATION_PRICE == 5
    assert DEFAULT_MIRROR_PRICE == 5
    assert DEFAULT_STICKER_PRICE == 5


def test_publish_fees_cost_ten():
    """등록비는 둘 다 10조각. 거울 등록비는 이번에 바뀌지 않았다."""
    assert MarketplacePublishPolicy.fee(ContentType.MIRROR) == 10
    assert MarketplacePublishPolicy.fee(ContentType.STICKER) == 10


def test_generation_and_publish_are_different_axes():
    """**한 상수로 묶이지 않았다.** 묶이면 한쪽 정책 변경이 다른 쪽을 끌고 간다."""
    assert AI_GENERATION_PRICE not in MarketplacePublishPolicy.FEES.values()


def test_ledger_reasons_stay_distinct():
    """원장만 보고 무엇에 썼는지 알 수 있어야 한다 — 값이 같아져도 이유는 다르다."""
    reasons = {
        ShardReason.AI_MIRROR.value,
        ShardReason.AI_STICKER.value,
        ShardReason.MIRROR_PUBLISH_FEE.value,
        ShardReason.STICKER_PUBLISH_FEE.value,
    }
    assert len(reasons) == 4


# MARK: - AI 거울


@pytest.fixture
def mirror_provider():
    return FakeProvider()


@pytest.fixture
def mirrors(shards, mirror_provider):
    return AIMirrorService(mirror_provider, "test-model", shards=shards)


def test_mirror_success_costs_exactly_five(mirrors, shards, mirror_provider):
    fund(shards, 100)

    mirrors.generate(ALICE, "핑크 리본", "req-1")

    assert balance(shards) == 95
    assert mirror_provider.calls == 1


def test_mirror_retry_never_charges_twice(mirrors, shards):
    """응답을 잃고 다시 부른 것은 새 구매가 아니다 — 원장 멱등 키가 같다."""
    fund(shards, 100)

    for _ in range(3):
        mirrors.generate(ALICE, "핑크 리본", "req-same")

    assert balance(shards) == 95


def test_mirror_failure_leaves_no_net_loss(shards, mirror_provider):
    """만들어 주지 못했으면 조각을 가져가지 않는다."""
    fund(shards, 100)
    mirror_provider.failure = RuntimeError("provider exploded")
    service = AIMirrorService(mirror_provider, "m", shards=shards)

    with pytest.raises(Exception):
        service.generate(ALICE, "핑크 리본", "req-boom")

    assert balance(shards) == 100


def test_mirror_four_shards_never_reaches_the_provider(shards, mirror_provider):
    """하나 모자라면 **provider를 부르기 전에** 거절한다. 요금이 발생하면 안 된다."""
    fund(shards, 4)
    service = AIMirrorService(mirror_provider, "m", shards=shards)

    with pytest.raises(AIStickerError) as error:
        service.generate(ALICE, "핑크 리본", "req-poor")

    assert error.value.reason is AIStickerReason.INSUFFICIENT_SHARDS
    assert mirror_provider.calls == 0
    assert balance(shards) == 4


def test_mirror_exactly_five_shards_works(mirrors, shards, mirror_provider):
    """딱 맞는 잔액으로 만들 수 있고, 만들고 나면 0이 된다."""
    fund(shards, 5)

    result = mirrors.generate(ALICE, "핑크 리본", "req-exact")

    assert result.png == PNG
    assert mirror_provider.calls == 1
    assert balance(shards) == 0


def test_mirror_has_no_daily_limit(mirrors, shards, mirror_provider):
    """**조각만 있으면 하루에 몇 번이든 만든다.** 값이 곧 제한이다."""
    fund(shards, 100)

    for index in range(6):
        mirrors.generate(ALICE, "핑크 리본", f"req-{index}")

    assert mirror_provider.calls == 6
    assert balance(shards) == 100 - 6 * 5
    # 세는 것이 없으니 service에 남은 횟수를 말하는 자리도 없다.
    for gone in ("daily_limit", "remaining", "quota"):
        assert not hasattr(mirrors, gone)


def test_mirror_request_carries_no_price():
    """요청이 값을 정하지 못한다 — body에 가격을 실을 자리가 없다."""
    from app.api.ai import AIMirrorRequest

    fields = set(AIMirrorRequest.model_fields)
    assert fields == {"prompt", "request_id"}
    for forbidden in ("price", "shardAmount", "cost", "amount"):
        assert forbidden not in fields


# MARK: - AI 스티커


@pytest.fixture
def sticker_provider():
    return FakeProvider()


@pytest.fixture
def stickers(shards, sticker_provider):
    return AIStickerService(
        shards=shards,
        provider=sticker_provider,
        store=InMemoryGenerationStore(),
        storage=InMemoryGenerationStorage(),
    )


def test_sticker_success_costs_exactly_five(stickers, shards, sticker_provider):
    fund(shards, 100)

    generation = stickers.generate(ALICE, "req-1", "고양이")

    assert generation.status is GenerationStatus.SUCCEEDED
    assert balance(shards) == 95
    assert sticker_provider.calls == 1


def test_sticker_retry_never_charges_twice(stickers, shards, sticker_provider):
    fund(shards, 100)

    for _ in range(3):
        stickers.generate(ALICE, "req-same", "고양이")

    assert balance(shards) == 95
    # provider도 한 번만 부른다(스티커는 결과를 보관하므로 다시 만들지 않는다).
    assert sticker_provider.calls == 1


def test_sticker_failure_refunds(shards, shard_store, sticker_provider):
    """provider가 거절하면 조각을 돌려준다 — **기존 환불 계약 그대로다.**

    스티커는 작업이 문서로 남으므로 예외가 아니라 `refunded` 상태로 돌아온다.
    이 구조를 바꾸지 않는다 — 값만 5조각이 됐다.
    """
    fund(shards, 100)
    sticker_provider.failure = AIStickerError(AIStickerReason.PROVIDER_UNAVAILABLE)
    service = AIStickerService(
        shards=shards,
        provider=sticker_provider,
        store=InMemoryGenerationStore(),
        storage=InMemoryGenerationStorage(),
    )

    generation = service.generate(ALICE, "req-boom", "고양이")

    assert generation.status is GenerationStatus.REFUNDED
    assert balance(shards) == 100
    refunds = [e for e in shard_store.entries if e.reason is ShardReason.REFUND]
    assert len(refunds) == 1
    assert refunds[0].delta == 5


def test_sticker_four_shards_never_reaches_the_provider(shards, sticker_provider):
    fund(shards, 4)
    service = AIStickerService(
        shards=shards,
        provider=sticker_provider,
        store=InMemoryGenerationStore(),
        storage=InMemoryGenerationStorage(),
    )

    with pytest.raises(AIStickerError) as error:
        service.generate(ALICE, "req-poor", "고양이")

    assert error.value.reason is AIStickerReason.INSUFFICIENT_SHARDS
    assert sticker_provider.calls == 0
    assert balance(shards) == 4


def test_sticker_exactly_five_shards_works(stickers, shards):
    fund(shards, 5)

    generation = stickers.generate(ALICE, "req-exact", "고양이")

    assert generation.status is GenerationStatus.SUCCEEDED
    assert balance(shards) == 0


def test_sticker_request_carries_no_price():
    from app.api.ai import AIStickerRequest

    fields = set(AIStickerRequest.model_fields)
    assert fields == {"prompt", "request_id"}
    for forbidden in ("price", "shardAmount", "cost", "amount"):
        assert forbidden not in fields


# MARK: - 스티커 상점 등록 (10조각)
#
# 등록 economy는 `tests/test_marketplace.py`의 harness가 이미 전부 고정한다.
# 여기서는 **새 값의 경계**만 본다 — 9는 안 되고 10은 된다.


@pytest.fixture
def publishing(shard_store, shards):
    """등록 harness. `tests/test_marketplace.py`가 쓰는 in-memory 구성 그대로다 —
    새 economy pipeline을 만들지 않는다."""
    from app.marketplace.service import MarketplaceService
    from app.marketplace.store import InMemoryMarketplaceStore
    from tests.test_marketplace import draft as make_draft
    from tests.test_marketplace import seed as seed_shards
    from tests.test_marketplace import user

    store = InMemoryMarketplaceStore(shard_store)
    service = MarketplaceService(store, shards)

    class Harness:
        seller = user()

        def seed(self, amount: int) -> None:
            seed_shards(shards, amount, self.seller.id)

        def draft(self, kind: ContentType):
            return make_draft(service, store, kind, owner=self.seller.id)

    harness = Harness()
    return service, store, shard_store, harness


def test_sticker_publish_costs_exactly_ten(publishing):
    service, store, shard_store, h = publishing
    h.seed(100)
    listing = h.draft(ContentType.STICKER)
    seller = h.seller

    result = service.publish(seller, listing.id)

    assert result.fee_shards == 10
    assert result.balance == 90
    assert shard_store.wallet(seller.id).balance == 90


def test_nine_shards_cannot_publish_a_sticker(publishing):
    """**하나 모자라면 listing도 그대로다.** 서버 상태가 하나도 바뀌지 않는다."""
    service, store, shard_store, h = publishing
    h.seed(9)
    listing = h.draft(ContentType.STICKER)
    seller = h.seller

    with pytest.raises(InsufficientShards):
        service.publish(seller, listing.id)

    assert store.listings[listing.id].status is ListingStatus.DRAFT
    assert store.listings[listing.id].publish_fee_paid is False
    assert store.listings[listing.id].published_at is None
    assert shard_store.wallet(seller.id).balance == 9


def test_ten_shards_can_publish_a_sticker(publishing):
    service, store, shard_store, h = publishing
    h.seed(10)
    listing = h.draft(ContentType.STICKER)
    seller = h.seller

    result = service.publish(seller, listing.id)

    assert result.published is True
    assert shard_store.wallet(seller.id).balance == 0


def test_sticker_publish_retry_charges_once(publishing):
    """재시도가 두 번 받지 않는다. 중복 listing도 생기지 않는다."""
    service, store, shard_store, h = publishing
    h.seed(100)
    listing = h.draft(ContentType.STICKER)
    seller = h.seller

    first = service.publish(seller, listing.id)
    second = service.publish(seller, listing.id)

    assert (first.fee_charged, second.fee_charged) == (True, False)
    assert shard_store.wallet(seller.id).balance == 90
    assert len(store.listings) == 1
    fees = [e for e in shard_store.entries if e.reason is ShardReason.STICKER_PUBLISH_FEE]
    assert len(fees) == 1
    assert fees[0].delta == -10


def test_mirror_publish_fee_did_not_move(publishing):
    """스티커 등록비를 바꾸면서 **거울 등록비가 따라 움직이면 안 된다.**"""
    service, store, shard_store, h = publishing
    h.seed(100)
    listing = h.draft(ContentType.MIRROR)
    seller = h.seller

    result = service.publish(seller, listing.id)

    assert result.fee_shards == 10
    assert shard_store.wallet(seller.id).balance == 90
