"""A-1B — AI 생성의 내구성.

A-1A에는 두 구멍이 있었다. 여기서 그 둘이 막혔는지만 본다:

1. **차감 후 process가 죽으면 환불이 안 된다** → 작업이 문서로 남고 나중에 정리된다
2. **응답이 유실되면 그림을 다시 못 받는다** → 결과가 응답보다 먼저 durable하게 저장된다

crash는 "service를 중간에 버리고 새 service로 이어서 부른다"로 흉내 낸다 —
실제 Cloud Run이 죽는 것과 서버 입장에서 구분되지 않는다.
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.ai.models import (
    DEFAULT_STICKER_PRICE,
    RECOVERY_GRACE,
    RESULT_RETENTION,
    AIStickerError,
    AIStickerReason,
    Generation,
    GenerationStatus,
    generation_id,
)
from app.ai.service import AIStickerService
from app.ai.storage import InMemoryGenerationStorage, object_name
from app.ai.store import InMemoryGenerationStore
from app.auth.models import sha256_hex
from app.auth.store import InMemoryAuthStore
from app.core.config import Settings
from app.main import create_app
from app.shards.models import ShardReason, utcnow
from app.shards.service import ShardLedgerService
from app.shards.store import InMemoryShardStore
from tests.conftest import CLIENT_ID, apple_claims

USER = "internal-user-1"
OTHER = "internal-user-2"
REQUEST = "11111111-2222-3333-4444-555555555555"

PNG = b"\x89PNG\r\n\x1a\n" + b"transparent-pixels"


class FakeProvider:
    is_configured = True

    def __init__(self, png: bytes = PNG, error: AIStickerReason | None = None) -> None:
        self.png = png
        self.error = error
        self.calls = 0
        self.gate: threading.Event | None = None

    def generate(self, prompt: str) -> bytes:
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(timeout=5)
        if self.error is not None:
            raise AIStickerError(self.error)
        return self.png


@pytest.fixture
def shard_store() -> InMemoryShardStore:
    return InMemoryShardStore()


@pytest.fixture
def shards(shard_store: InMemoryShardStore) -> ShardLedgerService:
    return ShardLedgerService(shard_store)


@pytest.fixture
def store() -> InMemoryGenerationStore:
    return InMemoryGenerationStore()


@pytest.fixture
def storage() -> InMemoryGenerationStorage:
    return InMemoryGenerationStorage()


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def service(shards, provider, store, storage) -> AIStickerService:
    return AIStickerService(shards=shards, provider=provider, store=store, storage=storage)


def fund(shards: ShardLedgerService, amount: int, user: str = USER) -> None:
    shards.credit(user, amount, ShardReason.ADMIN_ADJUSTMENT, external_event_id=f"seed-{user}-{amount}")


def expire_lease(store: InMemoryGenerationStore, identifier: str, *, aged: bool = False) -> None:
    """임차권을 만료시킨다. **worker가 죽었다는 뜻이 아니다** — 소유권만 풀린다.

    `aged=True`면 `RECOVERY_GRACE`까지 넘겨 정리 대상으로 만든다.
    """
    current = store.generations[identifier]
    created = current.created_at - (RECOVERY_GRACE + timedelta(minutes=1)) if aged else current.created_at
    store.generations[identifier] = Generation(
        id=current.id, user_id=current.user_id, status=current.status, price=current.price,
        created_at=created, updated_at=current.updated_at,
        lease_expires_at=utcnow() - timedelta(seconds=1),
        debit_entry_id=current.debit_entry_id, refund_entry_id=current.refund_entry_id,
        result_object=current.result_object, result_expires_at=current.result_expires_at,
        failure_reason=current.failure_reason,
    )


# MARK: - 멱등성


def test_same_request_id_calls_provider_once(service, provider, shards):
    fund(shards, 100)

    first = service.generate(USER, REQUEST, "고양이")
    second = service.generate(USER, REQUEST, "고양이")

    assert provider.calls == 1
    assert first.id == second.id
    assert second.status is GenerationStatus.SUCCEEDED


def test_same_request_id_debits_once(service, provider, shards):
    fund(shards, 100)

    service.generate(USER, REQUEST, "고양이")
    service.generate(USER, REQUEST, "고양이")
    service.generate(USER, REQUEST, "고양이")

    assert shards.wallet(USER).balance == 100 - DEFAULT_STICKER_PRICE


def test_different_request_ids_are_different_generations(service, provider, shards):
    fund(shards, 100)

    first = service.generate(USER, "req-a", "고양이")
    second = service.generate(USER, "req-b", "고양이")

    assert first.id != second.id
    assert provider.calls == 2
    assert shards.wallet(USER).balance == 100 - DEFAULT_STICKER_PRICE * 2


def test_request_id_is_scoped_per_user(service, shards):
    """다른 사용자가 같은 requestId를 써도 서로의 작업이 되지 않는다."""
    fund(shards, 100)
    fund(shards, 100, OTHER)

    mine = service.generate(USER, REQUEST, "고양이")
    theirs = service.generate(OTHER, REQUEST, "고양이")

    assert mine.id != theirs.id


def test_concurrent_same_request_id_calls_provider_once(shards, store, storage):
    """동시에 같은 requestId 10개 → provider 1회 · 차감 1회."""
    fund(shards, 100)
    provider = FakeProvider()
    provider.gate = threading.Event()
    service = AIStickerService(shards=shards, provider=provider, store=store, storage=storage)

    results: list = []
    errors: list = []

    def attempt():
        try:
            results.append(service.generate(USER, REQUEST, "고양이"))
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=attempt) for _ in range(10)]
    for thread in threads:
        thread.start()
    provider.gate.set()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert provider.calls == 1
    assert shards.wallet(USER).balance == 100 - DEFAULT_STICKER_PRICE
    assert len({result.id for result in results}) == 1


def test_invalid_request_id_is_rejected_before_any_charge(service, shards, provider):
    fund(shards, 100)
    for bad in ("", "   ", "a" * 65, "has space", "tab\there"):
        with pytest.raises(AIStickerError) as error:
            service.generate(USER, bad, "고양이")
        assert error.value.reason is AIStickerReason.INVALID_REQUEST_ID
    assert provider.calls == 0
    assert shards.wallet(USER).balance == 100


def test_resume_needs_no_prompt(service, provider, shards):
    """응답을 잃은 client는 무엇을 적었는지 다시 보낼 수 없다 — 그래도 이어받아야 한다."""
    fund(shards, 100)
    first = service.generate(USER, REQUEST, "고양이")

    resumed = service.generate(USER, REQUEST, "")

    assert resumed.id == first.id
    assert resumed.status is GenerationStatus.SUCCEEDED
    assert provider.calls == 1


def test_empty_prompt_on_a_new_request_is_rejected(service, provider, shards):
    """이어받을 것이 없으면 빈 프롬프트는 오류다 — 그리고 아무것도 차감되지 않았다."""
    fund(shards, 100)
    with pytest.raises(AIStickerError) as error:
        service.generate(USER, "never-created", "")
    assert error.value.reason is AIStickerReason.EMPTY_PROMPT
    assert provider.calls == 0
    assert shards.wallet(USER).balance == 100


def test_generation_id_is_deterministic():
    assert generation_id(USER, REQUEST) == generation_id(USER, REQUEST)
    assert generation_id(USER, REQUEST) != generation_id(OTHER, REQUEST)
    # raw user id / requestId가 id에 남지 않는다.
    assert USER not in generation_id(USER, REQUEST)
    assert REQUEST not in generation_id(USER, REQUEST)


# MARK: - 응답 유실 복구


def test_result_is_durable_before_the_response(service, shards, storage):
    """응답을 만들기 **전에** 결과가 저장돼 있어야 한다."""
    fund(shards, 100)
    generation = service.generate(USER, REQUEST, "고양이")
    assert storage.get(object_name(generation.id)) == PNG


def test_lost_response_can_be_recovered_by_retry(service, provider, shards):
    """응답이 유실됐다 → 같은 requestId로 다시 → 같은 결과, provider 재호출 없음."""
    fund(shards, 100)
    first = service.generate(USER, REQUEST, "고양이")

    retried = service.generate(USER, REQUEST, "고양이")

    assert retried.id == first.id
    assert retried.status is GenerationStatus.SUCCEEDED
    assert provider.calls == 1
    assert service.result(USER, first.id) == PNG


def test_image_can_be_downloaded_repeatedly(service, shards):
    """'한 번 응답하면 사라지는 이미지'가 아니다."""
    fund(shards, 100)
    generation = service.generate(USER, REQUEST, "고양이")

    for _ in range(3):
        assert service.result(USER, generation.id) == PNG


def test_status_survives_a_new_service_instance(shards, store, storage, provider):
    """process가 재시작돼도 작업은 서버에 남아 있다."""
    fund(shards, 100)
    first = AIStickerService(shards=shards, provider=provider, store=store, storage=storage)
    generation = first.generate(USER, REQUEST, "고양이")

    fresh = AIStickerService(shards=shards, provider=FakeProvider(), store=store, storage=storage)
    assert fresh.status(USER, generation.id).status is GenerationStatus.SUCCEEDED
    assert fresh.result(USER, generation.id) == PNG


# MARK: - 소유자


def test_other_user_cannot_read_status(service, shards):
    fund(shards, 100)
    generation = service.generate(USER, REQUEST, "고양이")

    with pytest.raises(AIStickerError) as error:
        service.status(OTHER, generation.id)
    # 403이 아니라 not_found — 존재 여부를 알려주지 않는다.
    assert error.value.reason is AIStickerReason.NOT_FOUND


def test_other_user_cannot_read_image(service, shards):
    fund(shards, 100)
    generation = service.generate(USER, REQUEST, "고양이")

    with pytest.raises(AIStickerError) as error:
        service.result(OTHER, generation.id)
    assert error.value.reason is AIStickerReason.NOT_FOUND


def test_unknown_generation_is_not_found(service):
    with pytest.raises(AIStickerError) as error:
        service.status(USER, "does-not-exist")
    assert error.value.reason is AIStickerReason.NOT_FOUND


# MARK: - 보관 기간


def test_expired_result_is_gone_not_refunded(service, shards, store, storage):
    """보관 기간이 지나면 못 받는다. **조각을 되돌리지 않는다** — 이미 그림으로 바꿔 갔다."""
    fund(shards, 100)
    generation = service.generate(USER, REQUEST, "고양이")

    current = store.generations[generation.id]
    store.generations[generation.id] = Generation(
        id=current.id, user_id=current.user_id, status=current.status, price=current.price,
        created_at=current.created_at, updated_at=current.updated_at,
        result_object=current.result_object,
        result_expires_at=utcnow() - timedelta(seconds=1),
    )

    with pytest.raises(AIStickerError) as error:
        service.result(USER, generation.id)
    assert error.value.reason is AIStickerReason.RESULT_EXPIRED
    assert shards.wallet(USER).balance == 100 - DEFAULT_STICKER_PRICE


def test_missing_object_reports_expired(service, shards, storage):
    """lifecycle이 이미 지운 경우."""
    fund(shards, 100)
    generation = service.generate(USER, REQUEST, "고양이")
    storage.objects.clear()

    with pytest.raises(AIStickerError) as error:
        service.result(USER, generation.id)
    assert error.value.reason is AIStickerReason.RESULT_EXPIRED


def test_retention_is_reported_to_the_client(service):
    assert service.retention_days == RESULT_RETENTION.days


# MARK: - 실패 · 환불


def test_provider_failure_refunds_exactly_once(service, shards, shard_store, provider):
    fund(shards, 100)
    provider.error = AIStickerReason.PROVIDER_UNAVAILABLE

    generation = service.generate(USER, REQUEST, "고양이")

    assert generation.status is GenerationStatus.REFUNDED
    assert shards.wallet(USER).balance == 100
    refunds = [entry for entry in shard_store.entries if entry.reason is ShardReason.REFUND]
    assert len(refunds) == 1


def test_retrying_a_refunded_generation_does_not_credit_again(service, shards, shard_store, provider):
    fund(shards, 100)
    provider.error = AIStickerReason.PROVIDER_REJECTED

    service.generate(USER, REQUEST, "고양이")
    service.generate(USER, REQUEST, "고양이")
    service.status(USER, generation_id(USER, REQUEST))

    assert shards.wallet(USER).balance == 100
    refunds = [entry for entry in shard_store.entries if entry.reason is ShardReason.REFUND]
    assert len(refunds) == 1


def test_storage_failure_refunds(service, shards, storage):
    """저장하지 못한 그림은 없는 것과 같다 — 응답만 주면 복구할 수 없다."""
    fund(shards, 100)
    storage.put_failure = RuntimeError("bucket down")

    generation = service.generate(USER, REQUEST, "고양이")

    assert generation.status is GenerationStatus.REFUNDED
    assert generation.failure_reason is AIStickerReason.STORAGE_FAILED
    assert shards.wallet(USER).balance == 100


def test_failed_refund_is_retried_later(shards, store, storage, monkeypatch):
    """환불까지 실패하면 `failed`로 남고, 다음에 조회할 때 되돌린다."""
    fund(shards, 100)
    provider = FakeProvider(error=AIStickerReason.PROVIDER_UNAVAILABLE)
    service = AIStickerService(shards=shards, provider=provider, store=store, storage=storage)

    real_credit = shards.credit
    monkeypatch.setattr(
        shards, "credit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("firestore down"))
    )
    generation = service.generate(USER, REQUEST, "고양이")
    assert generation.status is GenerationStatus.FAILED
    assert shards.wallet(USER).balance == 100 - DEFAULT_STICKER_PRICE

    monkeypatch.setattr(shards, "credit", real_credit)
    recovered = service.status(USER, generation.id)

    assert recovered.status is GenerationStatus.REFUNDED
    assert shards.wallet(USER).balance == 100


def test_insufficient_shards_never_calls_the_provider(service, provider, shards):
    with pytest.raises(AIStickerError) as error:
        service.generate(USER, REQUEST, "고양이")
    assert error.value.reason is AIStickerReason.INSUFFICIENT_SHARDS
    assert provider.calls == 0


def test_insufficient_shards_closes_the_generation(service, store, shards):
    """차감하지 못한 작업이 pending으로 남아 있으면 안 된다."""
    with pytest.raises(AIStickerError):
        service.generate(USER, REQUEST, "고양이")
    generation = store.generations[generation_id(USER, REQUEST)]
    assert generation.status is GenerationStatus.REFUNDED
    assert generation.failure_reason is AIStickerReason.INSUFFICIENT_SHARDS


# MARK: - crash / stale 복구


def test_crash_before_upload_refunds(shards, store, storage):
    """차감 직후 죽었다 → 결과가 없다 → 되돌린다."""
    fund(shards, 100)
    identifier = generation_id(USER, REQUEST)
    # provider 호출 중에 죽은 상태를 만든다: pending + 차감됨 + 결과 없음.
    service = AIStickerService(
        shards=shards, provider=FakeProvider(error=AIStickerReason.PROVIDER_UNAVAILABLE),
        store=store, storage=storage,
    )
    store.create_pending(Generation(
        id=identifier, user_id=USER, status=GenerationStatus.PENDING,
        price=DEFAULT_STICKER_PRICE, lease_expires_at=utcnow() + timedelta(seconds=300),
        debit_entry_id=identifier,
    ))
    shards.debit(USER, DEFAULT_STICKER_PRICE, ShardReason.AI_STICKER, external_event_id=identifier)
    assert shards.wallet(USER).balance == 100 - DEFAULT_STICKER_PRICE

    expire_lease(store, identifier, aged=True)
    recovered = service.status(USER, identifier)

    assert recovered.status is GenerationStatus.REFUNDED
    assert recovered.failure_reason is AIStickerReason.INTERRUPTED
    assert shards.wallet(USER).balance == 100


def test_crash_after_upload_succeeds_without_refund(shards, store, storage):
    """upload 후 status 쓰기 전에 죽었다 → 결과가 있다 → **성공으로 확정한다.**

    이것이 upload를 status보다 먼저 하는 이유다.
    """
    fund(shards, 100)
    identifier = generation_id(USER, REQUEST)
    service = AIStickerService(
        shards=shards, provider=FakeProvider(), store=store, storage=storage
    )
    store.create_pending(Generation(
        id=identifier, user_id=USER, status=GenerationStatus.PENDING,
        price=DEFAULT_STICKER_PRICE, lease_expires_at=utcnow() + timedelta(seconds=300),
        debit_entry_id=identifier,
    ))
    shards.debit(USER, DEFAULT_STICKER_PRICE, ShardReason.AI_STICKER, external_event_id=identifier)
    storage.put(object_name(identifier), PNG)  # upload는 끝났다

    expire_lease(store, identifier, aged=True)
    recovered = service.status(USER, identifier)

    assert recovered.status is GenerationStatus.SUCCEEDED
    assert service.result(USER, identifier) == PNG
    # 성공했으므로 조각은 돌아오지 않는다.
    assert shards.wallet(USER).balance == 100 - DEFAULT_STICKER_PRICE


def test_live_generation_is_not_reconciled(service, store, shards):
    """lease가 살아 있으면 건드리지 않는다 — 느린 요청을 죽었다고 오판하지 않는다."""
    fund(shards, 100)
    identifier = generation_id(USER, REQUEST)
    store.create_pending(Generation(
        id=identifier, user_id=USER, status=GenerationStatus.PENDING,
        price=DEFAULT_STICKER_PRICE, lease_expires_at=utcnow() + timedelta(seconds=300),
    ))

    assert service.status(USER, identifier).status is GenerationStatus.PENDING
    assert shards.wallet(USER).balance == 100


def test_concurrent_recovery_refunds_once(shards, store, storage, shard_store):
    """복구가 동시에 열 번 들어와도 환불은 한 번이다."""
    fund(shards, 100)
    identifier = generation_id(USER, REQUEST)
    service = AIStickerService(
        shards=shards, provider=FakeProvider(), store=store, storage=storage
    )
    store.create_pending(Generation(
        id=identifier, user_id=USER, status=GenerationStatus.PENDING,
        price=DEFAULT_STICKER_PRICE, lease_expires_at=utcnow() + timedelta(seconds=300),
    ))
    shards.debit(USER, DEFAULT_STICKER_PRICE, ShardReason.AI_STICKER, external_event_id=identifier)
    expire_lease(store, identifier, aged=True)

    threads = [threading.Thread(target=lambda: service.status(USER, identifier)) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    refunds = [entry for entry in shard_store.entries if entry.reason is ShardReason.REFUND]
    assert len(refunds) == 1
    assert shards.wallet(USER).balance == 100


def test_late_writer_cannot_undo_a_recovered_generation(shards, store, storage):
    """lease를 뺏긴 요청이 뒤늦게 끝나도 이미 정리된 결과를 덮어쓰지 못한다."""
    fund(shards, 100)
    identifier = generation_id(USER, REQUEST)
    stale_lease = utcnow() - timedelta(seconds=1)
    store.create_pending(Generation(
        id=identifier, user_id=USER, status=GenerationStatus.PENDING,
        price=DEFAULT_STICKER_PRICE,
        created_at=utcnow() - (RECOVERY_GRACE + timedelta(minutes=1)),
        lease_expires_at=stale_lease,
    ))
    service = AIStickerService(
        shards=shards, provider=FakeProvider(), store=store, storage=storage
    )
    service.status(USER, identifier)  # 복구가 가져가서 정리한다

    # 죽은 줄 알았던 요청이 예전 lease로 끝내려 한다.
    assert store.finish(identifier, stale_lease, GenerationStatus.SUCCEEDED) is None
    assert store.generations[identifier].status is not GenerationStatus.SUCCEEDED

def test_storage_check_failure_does_not_refund(shards, store, storage, monkeypatch):
    """결과가 있는지 확인하지 못했으면 되돌리지 않는다 — 성공을 환불하는 쪽이 더 나쁘다."""
    fund(shards, 100)
    identifier = generation_id(USER, REQUEST)
    store.create_pending(Generation(
        id=identifier, user_id=USER, status=GenerationStatus.PENDING,
        price=DEFAULT_STICKER_PRICE, created_at=utcnow() - (RECOVERY_GRACE + timedelta(minutes=1)),
        lease_expires_at=utcnow() - timedelta(seconds=1),
    ))
    shards.debit(USER, DEFAULT_STICKER_PRICE, ShardReason.AI_STICKER, external_event_id=identifier)
    service = AIStickerService(
        shards=shards, provider=FakeProvider(), store=store, storage=storage
    )
    monkeypatch.setattr(
        storage, "exists", lambda name: (_ for _ in ()).throw(RuntimeError("gcs down"))
    )

    result = service.status(USER, identifier)

    assert result.status is GenerationStatus.PENDING
    assert shards.wallet(USER).balance == 100 - DEFAULT_STICKER_PRICE


# MARK: - sweep


def test_sweep_recovers_stuck_shards(shards, store, storage):
    """앱을 켜면(config 조회) 묶인 조각이 풀린다."""
    fund(shards, 100)
    identifier = generation_id(USER, REQUEST)
    store.create_pending(Generation(
        id=identifier, user_id=USER, status=GenerationStatus.PENDING,
        price=DEFAULT_STICKER_PRICE, created_at=utcnow() - (RECOVERY_GRACE + timedelta(minutes=1)),
        lease_expires_at=utcnow() - timedelta(seconds=1),
    ))
    shards.debit(USER, DEFAULT_STICKER_PRICE, ShardReason.AI_STICKER, external_event_id=identifier)
    service = AIStickerService(
        shards=shards, provider=FakeProvider(), store=store, storage=storage
    )

    assert service.sweep(USER) == 1
    assert shards.wallet(USER).balance == 100


def test_sweep_ignores_other_users(shards, store, storage):
    fund(shards, 100, OTHER)
    identifier = generation_id(OTHER, REQUEST)
    store.create_pending(Generation(
        id=identifier, user_id=OTHER, status=GenerationStatus.PENDING,
        price=DEFAULT_STICKER_PRICE, lease_expires_at=utcnow() - timedelta(seconds=1),
    ))
    service = AIStickerService(
        shards=shards, provider=FakeProvider(), store=store, storage=storage
    )
    assert service.sweep(USER) == 0


def test_sweep_never_raises(shards, store, storage, monkeypatch):
    """복구는 부수 작업이다. 실패해도 config 응답을 막지 않는다."""
    service = AIStickerService(
        shards=shards, provider=FakeProvider(), store=store, storage=storage
    )
    monkeypatch.setattr(
        store, "stale_pending", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    )
    assert service.sweep(USER) == 0


# MARK: - fail closed


def test_missing_storage_disables_the_feature(shards, store, provider):
    """bucket이 없으면 켜지 않는다 — 복구할 수 없는 생성은 하지 않는다."""
    from app.ai.storage import UnconfiguredStorage

    service = AIStickerService(
        shards=shards, provider=provider, store=store, storage=UnconfiguredStorage()
    )
    assert not service.is_available

    fund(shards, 100)
    with pytest.raises(AIStickerError) as error:
        service.generate(USER, REQUEST, "고양이")
    assert error.value.reason is AIStickerReason.NOT_CONFIGURED
    assert provider.calls == 0
    assert shards.wallet(USER).balance == 100


def test_object_name_has_no_user_information():
    name = object_name(generation_id(USER, REQUEST))
    assert name.startswith("ai/stickers/")
    assert USER not in name
    assert REQUEST not in name


# MARK: - 로그


def test_logs_never_contain_prompt_or_ids(service, shards, caplog):
    fund(shards, 100)
    with caplog.at_level(logging.DEBUG):
        generation = service.generate(USER, REQUEST, "아주 비밀스러운 프롬프트")
        service.result(USER, generation.id)

    assert "아주 비밀스러운" not in caplog.text
    assert USER not in caplog.text
    assert REQUEST not in caplog.text
    # 전체 generation id도 남기지 않는다 — 앞 8자만.
    assert generation.id not in caplog.text


# MARK: - HTTP


@pytest.fixture
def client(shard_store, store, storage, provider, apple_key, jwks_of, monkeypatch) -> TestClient:
    from app.auth import jwks as jwks_module

    document = jwks_of(apple_key)
    monkeypatch.setattr(jwks_module, "http_jwks_fetch", lambda *a, **k: lambda: document)

    app = create_app(
        Settings(app_env="local", apple_client_id=CLIENT_ID),
        auth_store=InMemoryAuthStore(),
        shard_store=shard_store,
        image_provider=provider,
        generation_store=store,
        generation_storage=storage,
    )
    return TestClient(app)


def sign_in(client: TestClient, apple_key, subject: str = "001234.abcdef0123456789.1234") -> str:
    nonce = f"nonce-{subject}"
    token = apple_key.token(apple_claims(sub=subject, nonce=sha256_hex(nonce)))
    return client.post(
        "/auth/apple", json={"identityToken": token, "nonce": nonce}
    ).json()["accessToken"]


def auth(client: TestClient, apple_key, subject: str = "001234.abcdef0123456789.1234") -> dict:
    return {"Authorization": f"Bearer {sign_in(client, apple_key, subject)}"}


def seed(client: TestClient, headers: dict, shard_store: InMemoryShardStore, amount: int) -> str:
    user_id = client.get("/users/me", headers=headers).json()["id"]
    ShardLedgerService(shard_store).credit(
        user_id, amount, ShardReason.ADMIN_ADJUSTMENT, external_event_id=f"seed-{user_id}"
    )
    return user_id


def test_http_requires_authentication(client: TestClient):
    assert client.get("/ai/stickers/config").status_code == 401
    assert client.post("/ai/stickers", json={"requestId": REQUEST, "prompt": "x"}).status_code == 401
    assert client.get("/ai/stickers/abc").status_code == 401
    assert client.get("/ai/stickers/abc/image").status_code == 401


def test_http_post_returns_status_not_image(client, apple_key, shard_store):
    headers = auth(client, apple_key)
    seed(client, headers, shard_store, 100)

    response = client.post(
        "/ai/stickers", json={"requestId": REQUEST, "prompt": "고양이"}, headers=headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["generationId"]
    assert body["balance"] == 100 - DEFAULT_STICKER_PRICE
    # A-1A와 달리 이미지가 응답에 없다.
    assert "imagePng" not in body


def test_http_image_endpoint_streams_png(client, apple_key, shard_store):
    headers = auth(client, apple_key)
    seed(client, headers, shard_store, 100)
    generation = client.post(
        "/ai/stickers", json={"requestId": REQUEST, "prompt": "고양이"}, headers=headers
    ).json()

    image = client.get(f"/ai/stickers/{generation['generationId']}/image", headers=headers)

    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.headers["cache-control"] == "no-store"
    assert image.content == PNG


def test_http_retry_returns_same_generation(client, apple_key, shard_store, provider):
    headers = auth(client, apple_key)
    seed(client, headers, shard_store, 100)

    first = client.post(
        "/ai/stickers", json={"requestId": REQUEST, "prompt": "고양이"}, headers=headers
    ).json()
    second = client.post(
        "/ai/stickers", json={"requestId": REQUEST, "prompt": "고양이"}, headers=headers
    ).json()

    assert first["generationId"] == second["generationId"]
    assert provider.calls == 1
    assert second["balance"] == 100 - DEFAULT_STICKER_PRICE


def test_http_other_user_gets_404(client, apple_key, shard_store):
    mine = auth(client, apple_key)
    seed(client, mine, shard_store, 100)
    generation = client.post(
        "/ai/stickers", json={"requestId": REQUEST, "prompt": "고양이"}, headers=mine
    ).json()["generationId"]

    theirs = auth(client, apple_key, subject="000999.other.5678")
    assert client.get(f"/ai/stickers/{generation}", headers=theirs).status_code == 404
    assert client.get(f"/ai/stickers/{generation}/image", headers=theirs).status_code == 404


def test_http_config_reports_retention(client, apple_key):
    body = client.get("/ai/stickers/config", headers=auth(client, apple_key)).json()
    assert body["available"] is True
    assert body["price"] == DEFAULT_STICKER_PRICE
    assert body["resultRetentionDays"] == RESULT_RETENTION.days


def test_http_client_cannot_choose_price_or_user(client, apple_key, shard_store):
    headers = auth(client, apple_key)
    seed(client, headers, shard_store, 100)

    body = client.post(
        "/ai/stickers",
        json={"requestId": REQUEST, "prompt": "고양이", "price": 0, "amount": 0,
              "userId": "someone-else", "balance": 9999, "status": "succeeded"},
        headers=headers,
    ).json()

    assert body["balance"] == 100 - DEFAULT_STICKER_PRICE


def test_http_missing_request_id_is_rejected(client, apple_key, shard_store):
    headers = auth(client, apple_key)
    seed(client, headers, shard_store, 100)
    response = client.post("/ai/stickers", json={"prompt": "고양이"}, headers=headers)
    assert response.status_code == 422


def test_no_generic_mutation_endpoint(client: TestClient):
    paths = client.app.openapi()["paths"]
    assert "/ai/stickers" in paths
    assert "/ai/stickers/{generation_id}/image" in paths
    for forbidden in ("/shards/credit", "/shards/debit", "/ai/refund", "/ai/stickers/{generation_id}/refund"):
        assert forbidden not in paths


# MARK: - late worker (A-1B.1)
#
# **Cloud Run request timeout은 container를 죽이지 않는다.** client 연결을 끊고 504를
# 돌려줄 뿐이고, 그 요청을 처리하던 worker는 계속 돌 수 있다. 아래 test들은 전부
# "예전 worker가 아직 살아 있다"를 전제로 한다.


def running_generation(store, shards, identifier: str, age: timedelta = timedelta(0)) -> Generation:
    """차감까지 끝나고 provider를 기다리는 중인 작업을 만든다."""
    created = utcnow() - age
    store.generations[identifier] = Generation(
        id=identifier, user_id=USER, status=GenerationStatus.PENDING,
        price=DEFAULT_STICKER_PRICE, created_at=created, updated_at=created,
        lease_expires_at=created + timedelta(seconds=300), debit_entry_id=identifier,
    )
    shards.debit(USER, DEFAULT_STICKER_PRICE, ShardReason.AI_STICKER, external_event_id=identifier)
    return store.generations[identifier]


def test_lease_expiry_alone_does_not_refund(service, store, shards):
    """lease가 끊겼다고 환불하지 않는다 — worker가 아직 돌고 있을 수 있다."""
    fund(shards, 100)
    identifier = generation_id(USER, REQUEST)
    running_generation(store, shards, identifier)
    expire_lease(store, identifier)

    result = service.status(USER, identifier)

    assert result.status is GenerationStatus.PENDING
    assert shards.wallet(USER).balance == 100 - DEFAULT_STICKER_PRICE


def test_refund_only_after_the_recovery_grace(service, store, shards):
    """정상 worker 수명을 한참 넘긴 뒤에야 정리한다."""
    fund(shards, 100)
    identifier = generation_id(USER, REQUEST)
    running_generation(store, shards, identifier, age=RECOVERY_GRACE + timedelta(minutes=1))
    expire_lease(store, identifier)

    result = service.status(USER, identifier)

    assert result.status is GenerationStatus.REFUNDED
    assert shards.wallet(USER).balance == 100


def test_late_provider_success_cannot_undo_a_refund(shards, store, storage, shard_store):
    """A: provider 호출 중 → B: 환불 → C: A가 늦게 성공.

    기대: 지갑 변화 없음 · refunded 유지 · 이미지 제공 안 함 · object는 orphan.
    """
    fund(shards, 100)
    identifier = generation_id(USER, REQUEST)
    service = AIStickerService(
        shards=shards, provider=FakeProvider(), store=store, storage=storage
    )
    # A가 들고 있는 lease.
    worker_a = running_generation(store, shards, identifier, age=RECOVERY_GRACE + timedelta(minutes=1))
    expire_lease(store, identifier)
    lease_a = store.generations[identifier].lease_expires_at

    # B: 정리 → 환불.
    refunded = service.status(USER, identifier)
    assert refunded.status is GenerationStatus.REFUNDED
    assert shards.wallet(USER).balance == 100

    # C: A가 늦게 provider 성공을 받아 upload하고 끝내려 한다.
    name = object_name(identifier)
    storage.put(name, PNG)
    late = service._abandon(  # worker A가 CAS에서 지는 경로
        Generation(
            id=identifier, user_id=USER, status=GenerationStatus.PENDING,
            price=DEFAULT_STICKER_PRICE, lease_expires_at=lease_a,
        ),
        name,
    )

    assert late.status is GenerationStatus.REFUNDED, "환불이 성공으로 뒤집혔다"
    assert shards.wallet(USER).balance == 100, "지갑이 또 움직였다"
    refunds = [entry for entry in shard_store.entries if entry.reason is ShardReason.REFUND]
    assert len(refunds) == 1
    # orphan object는 치워진다.
    assert not storage.exists(name)
    # 그리고 어떤 경우에도 내보내지 않는다.
    with pytest.raises(AIStickerError) as error:
        service.result(USER, identifier)
    assert error.value.reason is AIStickerReason.INTERRUPTED


def test_refunded_cannot_become_succeeded(store, shards, storage):
    """terminal 권위: `refunded → succeeded`는 어떤 lease로도 불가능하다."""
    fund(shards, 100)
    identifier = generation_id(USER, REQUEST)
    store.generations[identifier] = Generation(
        id=identifier, user_id=USER, status=GenerationStatus.REFUNDED,
        price=DEFAULT_STICKER_PRICE, failure_reason=AIStickerReason.INTERRUPTED,
    )

    # lease가 우연히 맞아떨어져도(둘 다 None) 막힌다.
    assert store.finish(identifier, None, GenerationStatus.SUCCEEDED, result_object="x") is None
    assert store.generations[identifier].status is GenerationStatus.REFUNDED


def test_succeeded_cannot_become_refunded(store):
    """terminal 권위: `succeeded → refunded`(공짜 조각)도 불가능하다."""
    identifier = generation_id(USER, REQUEST)
    store.generations[identifier] = Generation(
        id=identifier, user_id=USER, status=GenerationStatus.SUCCEEDED,
        price=DEFAULT_STICKER_PRICE, result_object=object_name(identifier),
    )

    assert store.finish(identifier, None, GenerationStatus.REFUNDED) is None
    assert store.generations[identifier].status is GenerationStatus.SUCCEEDED


def test_transition_table():
    from app.ai.models import can_transition as allowed

    assert allowed(GenerationStatus.PENDING, GenerationStatus.SUCCEEDED)
    assert allowed(GenerationStatus.PENDING, GenerationStatus.REFUNDED)
    assert allowed(GenerationStatus.PENDING, GenerationStatus.FAILED)
    assert allowed(GenerationStatus.FAILED, GenerationStatus.REFUNDED)
    # terminal에서 나가는 길은 없다.
    assert not allowed(GenerationStatus.SUCCEEDED, GenerationStatus.REFUNDED)
    assert not allowed(GenerationStatus.REFUNDED, GenerationStatus.SUCCEEDED)
    assert not allowed(GenerationStatus.SUCCEEDED, GenerationStatus.FAILED)
    # 실패한 것이 뒤늦게 성공이 되지 않는다.
    assert not allowed(GenerationStatus.FAILED, GenerationStatus.SUCCEEDED)
    # pending으로 돌아가지 않는다.
    assert not allowed(GenerationStatus.PENDING, GenerationStatus.PENDING)


def test_object_exists_but_refunded_is_not_served(service, store, shards, storage):
    """object 존재는 증거일 뿐이다. 상태가 권위다."""
    fund(shards, 100)
    identifier = generation_id(USER, REQUEST)
    store.generations[identifier] = Generation(
        id=identifier, user_id=USER, status=GenerationStatus.REFUNDED,
        price=DEFAULT_STICKER_PRICE, failure_reason=AIStickerReason.INTERRUPTED,
    )
    storage.put(object_name(identifier), PNG)  # 늦은 upload가 남긴 orphan

    with pytest.raises(AIStickerError) as error:
        service.result(USER, identifier)
    assert error.value.reason is AIStickerReason.INTERRUPTED


def test_object_exists_but_pending_is_not_served(service, store, shards, storage):
    fund(shards, 100)
    identifier = generation_id(USER, REQUEST)
    running_generation(store, shards, identifier)
    storage.put(object_name(identifier), PNG)

    with pytest.raises(AIStickerError) as error:
        service.result(USER, identifier)
    assert error.value.reason is AIStickerReason.STILL_PENDING


def test_late_worker_does_not_delete_a_recovered_success(shards, store, storage):
    """복구가 그 object를 보고 성공으로 확정했다면 **지우면 안 된다.**

    지우면 조각을 쓴 사용자가 그림을 못 받는다.
    """
    fund(shards, 100)
    identifier = generation_id(USER, REQUEST)
    service = AIStickerService(
        shards=shards, provider=FakeProvider(), store=store, storage=storage
    )
    running_generation(store, shards, identifier, age=RECOVERY_GRACE + timedelta(minutes=1))
    expire_lease(store, identifier)
    lease_a = store.generations[identifier].lease_expires_at

    name = object_name(identifier)
    storage.put(name, PNG)              # A가 upload를 마쳤다
    recovered = service.status(USER, identifier)  # B가 그것을 보고 성공 확정
    assert recovered.status is GenerationStatus.SUCCEEDED

    # A가 뒤늦게 finish를 시도했다가 CAS에서 진다.
    late = service._abandon(
        Generation(
            id=identifier, user_id=USER, status=GenerationStatus.PENDING,
            price=DEFAULT_STICKER_PRICE, lease_expires_at=lease_a,
        ),
        name,
    )

    assert late.status is GenerationStatus.SUCCEEDED
    assert storage.exists(name), "복구된 결과를 늦은 worker가 지웠다"
    assert service.result(USER, identifier) == PNG


def test_upload_after_existence_check_becomes_orphan(shards, store, storage, shard_store):
    """reconciler가 확인한 **뒤에** upload가 도착하는 순서."""
    fund(shards, 100)
    identifier = generation_id(USER, REQUEST)
    name = object_name(identifier)
    service = AIStickerService(
        shards=shards, provider=FakeProvider(), store=store, storage=storage
    )
    running_generation(store, shards, identifier, age=RECOVERY_GRACE + timedelta(minutes=1))
    expire_lease(store, identifier)

    real_exists = storage.exists

    def check_then_upload(target: str) -> bool:
        answer = real_exists(target)
        # 확인 직후 A가 upload를 마친다.
        storage.objects[name] = PNG
        return answer

    storage.exists = check_then_upload  # type: ignore[method-assign]
    result = service.status(USER, identifier)
    storage.exists = real_exists  # type: ignore[method-assign]

    assert result.status is GenerationStatus.REFUNDED
    assert shards.wallet(USER).balance == 100
    refunds = [entry for entry in shard_store.entries if entry.reason is ShardReason.REFUND]
    assert len(refunds) == 1
    # 환불로 끝났으므로 늦게 올라온 object는 치워진다.
    assert not storage.exists(name)


def test_repeated_late_finishes_never_move_the_wallet(shards, store, storage, shard_store):
    """늦은 worker가 몇 번을 돌아와도 지갑은 그대로다."""
    fund(shards, 100)
    identifier = generation_id(USER, REQUEST)
    service = AIStickerService(
        shards=shards, provider=FakeProvider(), store=store, storage=storage
    )
    running_generation(store, shards, identifier, age=RECOVERY_GRACE + timedelta(minutes=1))
    expire_lease(store, identifier)
    lease_a = store.generations[identifier].lease_expires_at
    service.status(USER, identifier)
    balance = shards.wallet(USER).balance

    ghost = Generation(
        id=identifier, user_id=USER, status=GenerationStatus.PENDING,
        price=DEFAULT_STICKER_PRICE, lease_expires_at=lease_a,
    )
    for _ in range(5):
        service._abandon(ghost, object_name(identifier))
        assert store.finish(identifier, lease_a, GenerationStatus.SUCCEEDED) is None

    assert shards.wallet(USER).balance == balance
    entries = [e for e in shard_store.entries if e.reason in (ShardReason.AI_STICKER, ShardReason.REFUND)]
    assert len(entries) == 2, "차감 1 · 환불 1이 아니다"


def test_concurrent_reconcilers_refund_exactly_once_after_grace(shards, store, storage, shard_store):
    fund(shards, 100)
    identifier = generation_id(USER, REQUEST)
    service = AIStickerService(
        shards=shards, provider=FakeProvider(), store=store, storage=storage
    )
    running_generation(store, shards, identifier, age=RECOVERY_GRACE + timedelta(minutes=1))
    expire_lease(store, identifier)

    threads = [threading.Thread(target=lambda: service.status(USER, identifier)) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    refunds = [entry for entry in shard_store.entries if entry.reason is ShardReason.REFUND]
    assert len(refunds) == 1
    assert shards.wallet(USER).balance == 100
    assert store.generations[identifier].status is GenerationStatus.REFUNDED


def test_deterministic_provider_failure_still_refunds_immediately(shards, store, storage):
    """결정적 실패는 grace를 기다리지 않는다 — 임차인이 직접 본 결과다."""
    fund(shards, 100)
    service = AIStickerService(
        shards=shards, provider=FakeProvider(error=AIStickerReason.PROVIDER_REJECTED),
        store=store, storage=storage,
    )

    generation = service.generate(USER, REQUEST, "고양이")

    assert generation.status is GenerationStatus.REFUNDED
    assert shards.wallet(USER).balance == 100


def test_provider_timeout_is_shorter_than_cloud_run_timeout():
    """provider(90s) < Cloud Run(180s) < client(200s).

    Cloud Run timeout은 container를 죽이지 않으므로, provider 쪽이 **먼저** 끊겨야
    application이 실패를 직접 보고 정리까지 마칠 수 있다.
    """
    from app.ai.provider import DEFAULT_TIMEOUT

    cloud_run_timeout = 180
    assert DEFAULT_TIMEOUT < cloud_run_timeout


def test_no_source_claims_cloud_run_kills_the_worker():
    """틀린 전제가 코드에 다시 들어오지 않게 고정한다."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    for path in (root / "app" / "ai").glob("*.py"):
        text = path.read_text()
        # 주석에서 "틀렸다"고 설명하는 문장은 허용하되, 보장으로 쓰는 표현을 막는다.
        assert "= worker 사망" not in text or "틀렸다" in text, path.name
