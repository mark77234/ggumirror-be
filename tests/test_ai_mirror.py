"""AI 거울 생성.

**실제 provider를 부르지 않는다.** test가 돈을 쓰면 안 된다 —
모든 test는 fake provider를 쓴다.

지키는 것 셋:
1. 인증 없이는 못 부른다
2. 하루 몫을 넘길 수 없다(동시에 들어와도)
3. 고칠 수 있는 실패는 **몫을 쓰기 전에** 걸러서 비용이 새지 않는다
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.ai.mirror import (
    DEFAULT_DAILY_LIMIT,
    MIRROR_PROMPT_TEMPLATE,
    AIMirrorQuota,
    AIMirrorService,
    DailyLimitReached,
)
from app.ai.models import AIStickerError, AIStickerReason
from tests.test_auth_api import client, store  # noqa: F401

ALICE = "11111111-2222-4333-8444-555555555555"
BOB = "99999999-8888-4777-8666-555555555555"

KST = timezone(timedelta(hours=9))
#: KST로 2026-03-02 오전 0시 30분. UTC로는 전날이다 — 하루 경계 test에 쓴다.
MORNING = datetime(2026, 3, 1, 15, 30, tzinfo=timezone.utc)


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
    return AIMirrorService(
        provider, AIMirrorQuota(db, "quotas"), "test-model", shards=shards
    )


# MARK: - 프롬프트


def test_server_owns_the_geometry_instructions(service, provider):
    service.generate(ALICE, "핑크 리본", "req-1", MORNING)
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
        service.generate(ALICE, "   ", "req-1", MORNING)
    # 고칠 수 있는 실패다 — 비용도 몫도 쓰지 않는다.
    assert provider.prompts == []
    assert db.data == {}


def test_oversized_prompt_never_reaches_the_provider(service, provider):
    with pytest.raises(AIStickerError):
        service.generate(ALICE, "가" * 5000, "req-1", MORNING)
    assert provider.prompts == []


def test_unconfigured_provider_is_refused(db):
    service = AIMirrorService(UnconfiguredProvider(), AIMirrorQuota(db, "quotas"), "m")
    assert service.is_available is False
    with pytest.raises(AIStickerError):
        service.generate(ALICE, "핑크 리본", "req-1", MORNING)
    assert db.data == {}


# MARK: - 하루 몫


def test_generation_succeeds_within_the_limit(service, provider):
    for _ in range(DEFAULT_DAILY_LIMIT):
        result = service.generate(ALICE, "핑크 리본", "req-1", MORNING)
        assert result.png == b"PNG-BYTES"
    assert len(provider.prompts) == DEFAULT_DAILY_LIMIT


def test_one_more_is_refused(service, provider):
    for _ in range(DEFAULT_DAILY_LIMIT):
        service.generate(ALICE, "핑크 리본", "req-1", MORNING)
    with pytest.raises(DailyLimitReached):
        service.generate(ALICE, "핑크 리본", "req-1", MORNING)
    # **provider에 가지 않는다** — 거절이 곧 비용 절약이다.
    assert len(provider.prompts) == DEFAULT_DAILY_LIMIT


def test_the_next_day_starts_over(service, provider):
    for _ in range(DEFAULT_DAILY_LIMIT):
        service.generate(ALICE, "핑크 리본", "req-1", MORNING)
    tomorrow = MORNING + timedelta(days=1)
    service.generate(ALICE, "핑크 리본", "req-1", tomorrow)
    assert len(provider.prompts) == DEFAULT_DAILY_LIMIT + 1


def test_the_day_boundary_is_seoul_not_utc(service):
    """한국 서비스라 하루는 **KST 자정**에 바뀐다."""
    # KST 2026-03-02 00:30 (UTC로는 03-01 15:30)
    late = datetime(2026, 3, 1, 15, 30, tzinfo=timezone.utc)
    # 같은 KST 날의 저녁 (UTC로는 03-02 09:00)
    evening = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
    for _ in range(DEFAULT_DAILY_LIMIT):
        service.generate(ALICE, "핑크 리본", "req-1", late)
    # UTC로는 날짜가 바뀌었지만 KST로는 같은 날이다 — 여전히 막혀야 한다.
    with pytest.raises(DailyLimitReached):
        service.generate(ALICE, "핑크 리본", "req-1", evening)


def test_users_have_separate_quotas(service, provider):
    for _ in range(DEFAULT_DAILY_LIMIT):
        service.generate(ALICE, "핑크 리본", "req-1", MORNING)
    # BOB은 자기 몫이 그대로 있다.
    service.generate(BOB, "Y2K", "req-1", MORNING)
    assert len(provider.prompts) == DEFAULT_DAILY_LIMIT + 1


def test_remaining_counts_down(service):
    assert service.remaining(ALICE, MORNING) == DEFAULT_DAILY_LIMIT
    service.generate(ALICE, "핑크 리본", "req-1", MORNING)
    assert service.remaining(ALICE, MORNING) == DEFAULT_DAILY_LIMIT - 1


def test_quota_claim_is_atomic(db):
    """확인과 증가가 한 transaction이어야 동시 요청이 상한을 넘기지 못한다."""
    quota = AIMirrorQuota(db, "quotas", limit=2)
    assert quota.claim(ALICE, MORNING) == 1
    assert quota.claim(ALICE, MORNING) == 2
    with pytest.raises(DailyLimitReached):
        quota.claim(ALICE, MORNING)


def test_limit_is_configurable_by_the_server(db, provider):
    service = AIMirrorService(provider, AIMirrorQuota(db, "quotas", limit=1), "m")
    assert service.daily_limit == 1
    service.generate(ALICE, "핑크 리본", "req-1", MORNING)
    with pytest.raises(DailyLimitReached):
        service.generate(ALICE, "핑크 리본", "req-1", MORNING)


# MARK: - 비용


def test_provider_is_called_exactly_once_per_generation(service, provider):
    service.generate(ALICE, "핑크 리본", "req-1", MORNING)
    # **자동 재시도가 없다.** 정말 만들어졌는데 응답만 잃었을 수도 있어서,
    # 다시 부르면 비용이 두세 배가 된다.
    assert len(provider.prompts) == 1


def test_provider_failure_still_consumed_the_attempt(db):
    """provider가 실제로 일했을 수 있다. 공짜 재시도로 비용이 새지 않게 한다."""
    provider = FakeProvider(failure=AIStickerError(AIStickerReason.PROVIDER_UNAVAILABLE))
    service = AIMirrorService(provider, AIMirrorQuota(db, "quotas"), "m")

    with pytest.raises(AIStickerError):
        service.generate(ALICE, "핑크 리본", "req-1", MORNING)
    assert service.remaining(ALICE, MORNING) == DEFAULT_DAILY_LIMIT - 1


def test_nothing_is_stored_about_the_prompt(service, db):
    service.generate(ALICE, "아주 개인적인 문장", "req-1", MORNING)
    # 프롬프트도 그림도 남기지 않는다 — 남길 이유가 없다.
    assert "아주 개인적인 문장" not in str(db.data)
    assert "PNG-BYTES" not in str(db.data)


def test_price_is_ten_shards(service):
    """**한 장에 10조각이다.** 값의 authority는 서버다 — 요청에 실을 자리가 없다."""
    from app.ai.models import DEFAULT_MIRROR_PRICE

    assert DEFAULT_MIRROR_PRICE == 10
    assert service.price == 10


def test_sticker_price_is_untouched():
    """거울 값을 정하면서 **스티커 값이 따라 움직이면 안 된다.**"""
    from app.ai.models import DEFAULT_STICKER_PRICE

    assert DEFAULT_STICKER_PRICE == 6


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


def test_success_costs_exactly_ten(service, shards, provider):
    """성공하면 정확히 10조각. 한 번만."""
    before = balance(shards, ALICE)

    service.generate(ALICE, "핑크 리본", "req-success", MORNING)

    assert balance(shards, ALICE) == before - 10
    assert len(provider.prompts) == 1


def test_retry_with_the_same_request_id_does_not_charge_twice(service, shards, provider):
    """**BLOCKER guard.** 응답을 잃고 다시 부른 것은 새 구매가 아니다.

    그림은 다시 만들어야 하므로 provider는 다시 부르지만, **조각은 다시 빠지지 않는다** —
    원장 멱등 키가 같기 때문이다.
    """
    before = balance(shards, ALICE)

    service.generate(ALICE, "핑크 리본", "req-same", MORNING)
    service.generate(ALICE, "핑크 리본", "req-same", MORNING)
    service.generate(ALICE, "핑크 리본", "req-same", MORNING)

    assert balance(shards, ALICE) == before - 10


def test_different_requests_each_cost_ten(service, shards):
    """다른 요청은 각각 값을 낸다 — 멱등이 공짜를 뜻하지 않는다."""
    before = balance(shards, ALICE)

    service.generate(ALICE, "핑크 리본", "req-a", MORNING)
    service.generate(ALICE, "노란 별", "req-b", MORNING)

    assert balance(shards, ALICE) == before - 20


def test_provider_failure_leaves_no_net_loss(service, shards, provider):
    """**BLOCKER guard.** 만들어 주지 못했으면 조각을 가져가지 않는다."""
    before = balance(shards, ALICE)
    provider.failure = RuntimeError("provider exploded")

    with pytest.raises(Exception):
        service.generate(ALICE, "핑크 리본", "req-boom", MORNING)

    assert balance(shards, ALICE) == before


def test_daily_limit_after_debit_also_refunds(service, shards):
    """하루 몫이 끝나서 실패해도 net 0이다 — 차감 뒤의 어떤 실패든 되돌린다."""
    for index in range(DEFAULT_DAILY_LIMIT):
        service.generate(ALICE, "핑크 리본", f"req-{index}", MORNING)
    before = balance(shards, ALICE)

    with pytest.raises(DailyLimitReached):
        service.generate(ALICE, "핑크 리본", "req-over", MORNING)

    assert balance(shards, ALICE) == before


def test_insufficient_balance_is_refused_before_the_provider(db, provider, shards):
    """**provider를 부르기 전에** 거절한다. 돈이 없으면 요금이 발생하면 안 된다."""
    poor = "99999999-8888-4777-8666-000000000000"
    fund(shards, poor, 9)   # 10보다 하나 모자라다
    service = AIMirrorService(
        provider, AIMirrorQuota(db, "quotas"), "test-model", shards=shards
    )

    with pytest.raises(AIStickerError) as error:
        service.generate(poor, "핑크 리본", "req-poor", MORNING)

    assert error.value.reason is AIStickerReason.INSUFFICIENT_SHARDS
    assert provider.prompts == []
    # 잔액도 하루 몫도 건드리지 않았다.
    assert balance(shards, poor) == 9
    assert service.remaining(poor, MORNING) == DEFAULT_DAILY_LIMIT


def test_refund_writes_one_ledger_line(service, shards, shard_store, provider):
    """되돌리기를 여러 번 시도해도 원장에는 한 줄이다."""
    provider.failure = RuntimeError("boom")
    for _ in range(3):
        with pytest.raises(Exception):
            service.generate(ALICE, "핑크 리본", "req-refund", MORNING)

    refunds = [x for x in shard_store.entries if x.reason.value == "refund"]
    assert len(refunds) == 1


def test_ledger_records_the_mirror_reason(service, shard_store):
    """원장만 보고 AI 거울에 썼다는 것을 알 수 있다."""
    service.generate(ALICE, "핑크 리본", "req-reason", MORNING)

    reasons = {x.reason.value for x in shard_store.entries}
    assert "ai_mirror" in reasons
    assert "ai_sticker" not in reasons


def test_price_is_not_taken_from_the_request(service, shards):
    """요청이 값을 정하지 못한다 — `generate`에 금액 인자가 없다."""
    import inspect

    parameters = set(inspect.signature(service.generate).parameters)
    assert "price" not in parameters
    assert "amount" not in parameters
