"""AI 거울 생성.

**실제 provider를 부르지 않는다.** test가 돈을 쓰면 안 된다 —
모든 test는 fake provider를 쓴다.

지키는 것 셋:
1. 인증 없이는 못 부른다
2. **하루 횟수 제한이 없다** — 조각만 있으면 몇 번이든 만든다
3. 고칠 수 있는 실패는 **조각을 쓰기 전에** 걸러서 비용이 새지 않는다
"""

from __future__ import annotations

import pytest

from app.ai.mirror import (
    MIRROR_PROMPT_TEMPLATE,
    AIMirrorService,
)
from app.ai.models import AIStickerError, AIStickerReason
from tests.test_auth_api import client, store  # noqa: F401

ALICE = "11111111-2222-4333-8444-555555555555"
BOB = "99999999-8888-4777-8666-555555555555"

#: 연속 생성을 확인할 때 도는 횟수. 예전 하루 몫(3)보다 크다 —
#: 상한이 사라졌다는 것을 보려면 그 수를 넘겨야 한다.
DEFAULT_ROUNDS = 5


class FakeProvider:
    """**돈을 쓰지 않는다.** 무엇을 요청받았는지만 기억한다."""

    is_configured = True

    def __init__(self, failure: Exception | None = None) -> None:
        self.prompts: list[str] = []
        self.failure = failure

    def generate(self, prompt: str) -> bytes:
        self.prompts.append(prompt)
        if self.failure:
            raise self.failure
        return b"PNG-BYTES"


class UnconfiguredProvider:
    is_configured = False

    def generate(self, prompt: str) -> bytes:
        raise AIStickerError(AIStickerReason.NOT_CONFIGURED)


# MARK: - 아주 작은 Firestore 흉내


class FakeDoc:
    def __init__(self, store, key):
        self._store, self.id = store, key

    @property
    def exists(self):
        return self.id in self._store.data

    def to_dict(self):
        return dict(self._store.data.get(self.id, {}))

    def get(self, transaction=None):
        return self

    def set(self, value, merge=False):
        self._store.data.setdefault(self.id, {}).update(value)


class FakeCollection:
    def __init__(self, store):
        self._store = store

    def document(self, key):
        return FakeDoc(self._store, key)


class FakeTransaction:
    def set(self, ref, value, merge=False):
        ref.set(value, merge=merge)


class FakeDB:
    def __init__(self):
        self.data: dict[str, dict] = {}

    def collection(self, _name):
        return FakeCollection(self)

    def transaction(self):
        return FakeTransaction()


def _transactional(fn):
    def wrapper(transaction):
        return fn(transaction)
    return wrapper


@pytest.fixture(autouse=True)
def patch_transactional(monkeypatch):
    from google.cloud import firestore

    monkeypatch.setattr(firestore, "transactional", _transactional)


@pytest.fixture
def db():
    return FakeDB()


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def shard_store():
    from app.shards.store import InMemoryShardStore

    return InMemoryShardStore()


@pytest.fixture
def shards(shard_store):
    from app.shards.service import ShardLedgerService

    return ShardLedgerService(shard_store)


def fund(shards, user_id: str, amount: int) -> None:
    from app.shards.models import ShardReason

    shards.credit(user_id, amount, ShardReason.ADMIN_ADJUSTMENT,
                  external_event_id=f"seed:{user_id}:{amount}")


@pytest.fixture
def service(db, provider, shards):
    # **조각을 넉넉히 채워 둔다** — 값을 보는 test가 아닌 곳에서 잔액 때문에 막히지 않게.
    fund(shards, ALICE, 1000)
    fund(shards, BOB, 1000)
    return AIMirrorService(provider, "test-model", shards=shards)


# MARK: - 프롬프트


def test_server_owns_the_geometry_instructions(service, provider):
    service.generate(ALICE, "핑크 리본", "req-1")
    sent = provider.prompts[0]
    # 사용자 문장만 그대로 보내지 않는다 — 서버 지시문과 함께 간다.
    assert "핑크 리본" in sent
    assert "central camera opening" in sent
    assert "Do not place important objects" in sent


def test_prompt_template_does_not_promise_exact_geometry():
    """**AI에게 정확한 좌표와 색을 맡기지 않는다.**

    표시는 client가 결정적으로 찍는다. 프롬프트가 `#00FF00`을 요구하면
    지켜졌는지 검사해야 하고, 모델은 확률적이라 그 검사는 언젠가 실패한다.
    """
    assert "#00FF00" not in MIRROR_PROMPT_TEMPLATE
    assert "1080" not in MIRROR_PROMPT_TEMPLATE


def test_empty_prompt_never_reaches_the_provider(service, provider, db):
    with pytest.raises(AIStickerError):
        service.generate(ALICE, "   ", "req-1")
    # 고칠 수 있는 실패다 — 비용도 몫도 쓰지 않는다.
    assert provider.prompts == []
    assert db.data == {}


def test_oversized_prompt_never_reaches_the_provider(service, provider):
    with pytest.raises(AIStickerError):
        service.generate(ALICE, "가" * 5000, "req-1")
    assert provider.prompts == []


def test_unconfigured_provider_is_refused(db):
    service = AIMirrorService(UnconfiguredProvider(), "m")
    assert service.is_available is False
    with pytest.raises(AIStickerError):
        service.generate(ALICE, "핑크 리본", "req-1")
    assert db.data == {}


# MARK: - 하루 횟수 제한 없음


def test_repeated_generation_is_never_blocked_by_a_daily_quota(service, provider, shards):
    """**핵심 계약.** 조각이 있으면 하루에 몇 번이든 만든다.

    예전에는 하루 3번이 상한이었다. 값을 낸 사용자를 막는 쪽으로만 일했으므로 없앴다 —
    이제 상한은 잔액 하나다.
    """
    before = balance(shards, ALICE)

    for index in range(5):
        result = service.generate(ALICE, "핑크 리본", f"req-{index}")
        assert result.png == b"PNG-BYTES"

    # 다섯 번 전부 provider까지 갔고, 각각 정확히 5조각이다.
    assert len(provider.prompts) == 5
    assert balance(shards, ALICE) == before - 25


def test_no_quota_state_is_written_anywhere(service, db):
    """세는 것이 없으니 **적는 것도 없다.** 남은 write는 원장뿐이다."""
    for index in range(5):
        service.generate(ALICE, "핑크 리본", f"req-{index}")
    assert db.data == {}


def test_the_service_has_no_daily_limit_surface(service):
    """API에도 service에도 하루 몫을 말하는 자리가 없다."""
    for gone in ("daily_limit", "remaining", "quota"):
        assert not hasattr(service, gone)

    from app.api.ai import AIMirrorConfigPayload

    assert set(AIMirrorConfigPayload.model_fields) == {"available", "price"}


def test_generate_takes_no_clock(service):
    """하루 경계가 사라졌으므로 **시각을 받을 이유도 없다.**"""
    import inspect

    parameters = set(inspect.signature(service.generate).parameters)
    assert parameters == {"user_id", "raw_prompt", "request_id"}


def test_the_module_no_longer_defines_a_quota():
    """죽은 개념을 남겨 두지 않는다."""
    import app.ai.mirror as mirror

    for gone in ("AIMirrorQuota", "DailyLimitReached", "DEFAULT_DAILY_LIMIT"):
        assert not hasattr(mirror, gone)


def test_availability_no_longer_depends_on_a_quota(provider):
    """provider가 설정돼 있으면 쓸 수 있다. 계수기가 조건이 아니다."""
    assert AIMirrorService(provider, "m").is_available is True


def test_users_are_independent(service, provider):
    for index in range(5):
        service.generate(ALICE, "핑크 리본", f"req-{index}")
    service.generate(BOB, "Y2K", "req-1")
    assert len(provider.prompts) == 6


# MARK: - 비용


def test_provider_is_called_exactly_once_per_generation(service, provider):
    service.generate(ALICE, "핑크 리본", "req-1")
    # **자동 재시도가 없다.** 정말 만들어졌는데 응답만 잃었을 수도 있어서,
    # 다시 부르면 비용이 두세 배가 된다.
    assert len(provider.prompts) == 1


def test_provider_failure_does_not_block_the_next_attempt(shards, provider):
    """실패는 아무것도 소진하지 않는다 — 조각은 돌아오고 다음 시도도 그대로 간다."""
    fund(shards, ALICE, 1000)
    provider.failure = AIStickerError(AIStickerReason.PROVIDER_UNAVAILABLE)
    service = AIMirrorService(provider, "m", shards=shards)

    for index in range(3):
        with pytest.raises(AIStickerError):
            service.generate(ALICE, "핑크 리본", f"req-{index}")

    assert len(provider.prompts) == 3


def test_nothing_is_stored_about_the_prompt(service, db):
    service.generate(ALICE, "아주 개인적인 문장", "req-1")
    # 프롬프트도 그림도 남기지 않는다 — 남길 이유가 없다.
    assert "아주 개인적인 문장" not in str(db.data)
    assert "PNG-BYTES" not in str(db.data)


def test_price_is_five_shards(service):
    """**한 장에 5조각이다.** 값의 authority는 서버다 — 요청에 실을 자리가 없다."""
    from app.ai.models import DEFAULT_MIRROR_PRICE

    assert DEFAULT_MIRROR_PRICE == 5
    assert service.price == 5


def test_generation_prices_are_unified():
    """**AI에게 한 장 부탁하는 값은 하나다** — 거울도 스티커도 5조각."""
    from app.ai.models import (
        AI_GENERATION_PRICE,
        DEFAULT_MIRROR_PRICE,
        DEFAULT_STICKER_PRICE,
    )

    assert AI_GENERATION_PRICE == 5
    assert DEFAULT_MIRROR_PRICE == 5
    assert DEFAULT_STICKER_PRICE == 5


def test_publish_fees_did_not_follow():
    """생성값을 바꾸면서 **등록비가 따라 움직이면 안 된다** — 다른 축이다."""
    from app.marketplace.models import ContentType, MarketplacePublishPolicy

    assert MarketplacePublishPolicy.fee(ContentType.MIRROR) == 10
    assert MarketplacePublishPolicy.fee(ContentType.STICKER) == 10


def test_mirror_and_sticker_spend_are_distinguishable():
    """원장만 보고 어디에 썼는지 알 수 있어야 한다."""
    from app.shards.models import ShardReason

    assert ShardReason.AI_MIRROR.value == "ai_mirror"
    assert ShardReason.AI_STICKER.value == "ai_sticker"
    assert ShardReason.AI_MIRROR is not ShardReason.AI_STICKER


# MARK: - API


def test_generate_requires_authentication(client):
    assert client.post("/ai/mirrors/generate", json={"prompt": "핑크"}).status_code == 401


def test_config_requires_authentication(client):
    assert client.get("/ai/mirrors/config").status_code == 401


def test_client_cannot_choose_model_or_geometry():
    from app.api.ai import AIMirrorRequest

    fields = set(AIMirrorRequest.model_fields)
    # 프롬프트와 멱등 키뿐이다. `requestId`는 값을 정하지 않는다 —
    # 같은 요청인지만 알려주고, 얼마인지는 서버 표가 정한다.
    assert fields == {"prompt", "request_id"}
    for forbidden in ("model", "provider", "apiKey", "cameraRect", "userId", "price"):
        assert forbidden not in fields


# MARK: - 조각 (I-7) — release-critical


def balance(shards, user_id: str) -> int:
    return shards.wallet(user_id).balance


def test_success_costs_exactly_five(service, shards, provider):
    """성공하면 정확히 5조각. 한 번만."""
    before = balance(shards, ALICE)

    service.generate(ALICE, "핑크 리본", "req-success")

    assert balance(shards, ALICE) == before - 5
    assert len(provider.prompts) == 1


def test_retry_with_the_same_request_id_does_not_charge_twice(service, shards, provider):
    """**BLOCKER guard.** 응답을 잃고 다시 부른 것은 새 구매가 아니다.

    그림은 다시 만들어야 하므로 provider는 다시 부르지만, **조각은 다시 빠지지 않는다** —
    원장 멱등 키가 같기 때문이다.
    """
    before = balance(shards, ALICE)

    service.generate(ALICE, "핑크 리본", "req-same")
    service.generate(ALICE, "핑크 리본", "req-same")
    service.generate(ALICE, "핑크 리본", "req-same")

    assert balance(shards, ALICE) == before - 5


def test_different_requests_each_cost_five(service, shards):
    """다른 요청은 각각 값을 낸다 — 멱등이 공짜를 뜻하지 않는다."""
    before = balance(shards, ALICE)

    service.generate(ALICE, "핑크 리본", "req-a")
    service.generate(ALICE, "노란 별", "req-b")

    assert balance(shards, ALICE) == before - 10


def test_provider_failure_leaves_no_net_loss(service, shards, provider):
    """**BLOCKER guard.** 만들어 주지 못했으면 조각을 가져가지 않는다."""
    before = balance(shards, ALICE)
    provider.failure = RuntimeError("provider exploded")

    with pytest.raises(Exception):
        service.generate(ALICE, "핑크 리본", "req-boom")

    assert balance(shards, ALICE) == before


def test_any_failure_after_the_debit_refunds(service, shards, provider):
    """차감 뒤의 **어떤** 실패든 net 0이다."""
    for index in range(DEFAULT_ROUNDS):
        service.generate(ALICE, "핑크 리본", f"req-{index}")
    before = balance(shards, ALICE)
    provider.failure = RuntimeError("boom")

    with pytest.raises(Exception):
        service.generate(ALICE, "핑크 리본", "req-boom")

    assert balance(shards, ALICE) == before


def test_insufficient_balance_is_refused_before_the_provider(db, provider, shards):
    """**provider를 부르기 전에** 거절한다. 돈이 없으면 요금이 발생하면 안 된다."""
    poor = "99999999-8888-4777-8666-000000000000"
    fund(shards, poor, 4)   # 5보다 하나 모자라다
    service = AIMirrorService(provider, "test-model", shards=shards)

    with pytest.raises(AIStickerError) as error:
        service.generate(poor, "핑크 리본", "req-poor")

    assert error.value.reason is AIStickerReason.INSUFFICIENT_SHARDS
    assert provider.prompts == []
    assert balance(shards, poor) == 4


def test_refund_writes_one_ledger_line(service, shards, shard_store, provider):
    """되돌리기를 여러 번 시도해도 원장에는 한 줄이다."""
    provider.failure = RuntimeError("boom")
    for _ in range(3):
        with pytest.raises(Exception):
            service.generate(ALICE, "핑크 리본", "req-refund")

    refunds = [x for x in shard_store.entries if x.reason.value == "refund"]
    assert len(refunds) == 1


def test_ledger_records_the_mirror_reason(service, shard_store):
    """원장만 보고 AI 거울에 썼다는 것을 알 수 있다."""
    service.generate(ALICE, "핑크 리본", "req-reason")

    reasons = {x.reason.value for x in shard_store.entries}
    assert "ai_mirror" in reasons
    assert "ai_sticker" not in reasons


def test_price_is_not_taken_from_the_request(service, shards):
    """요청이 값을 정하지 못한다 — `generate`에 금액 인자가 없다."""
    import inspect

    parameters = set(inspect.signature(service.generate).parameters)
    assert "price" not in parameters
    assert "amount" not in parameters
