"""AI 스티커 생성 흐름 — **durable server resource**.

A-1A의 두 구멍을 여기서 막는다:

1. **차감 직후 process가 죽으면 환불이 실행되지 않는다** →
   차감과 동시에 `pending` 작업 문서를 만든다. 조각이 움직인 사실이 문서에 남으므로,
   나중에 누구든 그 문서를 보고 되돌릴 수 있다.
2. **응답이 유실되면 그림을 다시 받을 수 없다** →
   응답을 만들기 **전에** 결과를 durable storage에 올린다. 같은 requestId로 다시 오면
   provider를 부르지 않고 저장된 결과를 돌려준다.

## 성공 순서 (바꾸면 안 된다)

    provider 성공 → PNG 검증 → storage upload → status=succeeded → 그 다음 응답

storage가 status보다 **먼저**인 것이 복구의 근거다. 다만 object가 있다는 것은
**증거일 뿐 결론이 아니다** — 무엇을 사용자에게 내보낼지는 Firestore의 terminal state가 정한다.

## late worker — 죽지 않는다는 전제로 설계한다

⚠️ 예전에 "lease > Cloud Run timeout이므로 lease 만료 = worker 사망"이라고 적혀 있었다.
**틀렸다.** Cloud Run의 request timeout은 client 연결을 끊고 504를 줄 뿐,
container instance를 종료하지 않는다. 그 요청을 처리하던 코드는 계속 돌 수 있고,
한참 뒤에 provider 성공을 받아 upload까지 마칠 수도 있다.

그래서 안전은 시간이 아니라 **두 관문**이 만든다:

1. `finish`는 **terminal에서 나가는 전이를 거부한다** —
   `refunded → succeeded`(공짜 결과)도 `succeeded → refunded`(공짜 조각)도 불가능하다
2. `finish`는 **lease 일치**를 요구한다 — 임차권을 뺏긴 worker는 쓰지 못한다

CAS에서 진 worker는 자기가 올린 object를 **직접 치운다**(orphan). 그 결과는
사용자에게 나가지 않으므로 남겨 둘 이유가 없다.

## 언제 환불하는가

| 상황 | 판단 | 왜 |
|---|---|---|
| provider가 명시적으로 거절/실패 | **즉시 환불** | 임차인이 직접 본 결정적 실패다 |
| upload 실패 | **즉시 환불** | 저장 못 한 그림은 없는 것과 같다 |
| lease만 만료 (요청이 끊겼다) | **환불하지 않는다** | worker가 아직 돌고 있을 수 있다 |
| `RECOVERY_GRACE`(15분)까지 지남 | 결과 유무를 보고 정리 | 정상 worker 수명을 한참 넘겼다 |

시간만으로 성급히 환불하지 않는 것이 §5의 핵심이다.

**grace가 지나면 "정리된다"가 아니라 "정리 대상이 된다"(recovery eligible)이다.**
정리는 lazy하게, 그 작업을 건드리는 다음 요청에서 일어난다 —
같은 requestId 재시도 · 상태 조회 · 앱 시작의 `config` sweep.
아무도 오지 않으면 문서는 `pending`으로 남아 있고 조각도 묶여 있다.
그전까지 사용자에게는 "아직 만드는 중"으로 보인다.

## worker를 두지 않은 이유

Cloud Run은 요청 밖에서 CPU를 보장하지 않는다(`min-instances=0`). background thread는
응답 직후 얼어붙을 수 있어 복구를 맡길 수 없고, Cloud Tasks / Pub/Sub / Scheduler는
새 infra · 새 IAM · 새 실패 모드를 들여온다. 대신 **복구가 필요한 바로 그 순간**
(같은 requestId 재시도 · 상태 조회 · 앱 시작의 config 조회)에 lazy하게 정리한다.
**사용자가 돌아오지 않으면 조각은 계속 묶여 있다** — 시간이 지났다고 저절로 풀리지 않는다.
돌아오는 순간(재시도 · 조회 · 앱 시작) 정확히 정리될 뿐이다.
자동 정리가 필요해지면 그때 scheduler를 붙인다.
"""

from __future__ import annotations

import logging
import uuid

from app.ai.models import (
    DEFAULT_STICKER_PRICE,
    MAX_REQUEST_ID_LENGTH,
    RESULT_RETENTION,
    AIStickerError,
    AIStickerReason,
    Generation,
    GenerationStatus,
    generation_id,
)
from app.ai.prompt import normalize_prompt
from app.ai.provider import ImageProvider
from app.ai.storage import GenerationStorage, object_name
from app.ai.store import GenerationStore, leased
from app.shards.models import InsufficientShards, ShardReason, utcnow
from app.shards.service import ShardLedgerService

logger = logging.getLogger(__name__)


def short(generation: str) -> str:
    """로그용 짧은 형태. 전체 id를 남기지 않는다."""
    return generation[:8]


class AIStickerService:
    def __init__(
        self,
        shards: ShardLedgerService,
        provider: ImageProvider,
        store: GenerationStore,
        storage: GenerationStorage,
        price: int = DEFAULT_STICKER_PRICE,
    ) -> None:
        self._shards = shards
        self._provider = provider
        self._store = store
        self._storage = storage
        self._price = price

    @property
    def price(self) -> int:
        return self._price

    @property
    def retention_days(self) -> int:
        """결과를 며칠 동안 다시 받을 수 있는지. client 안내 문구가 이 값을 쓴다."""
        return RESULT_RETENTION.days

    def balance(self, user_id: str) -> int:
        """지금 잔액. **원장이 계산한 값**을 그대로 읽어 응답에 싣는다."""
        return self._shards.wallet(user_id).balance

    @property
    def is_available(self) -> bool:
        """provider **와** 보관소가 둘 다 있어야 켠다.

        보관소 없이 생성하면 응답 유실을 복구할 수 없다 — 그건 A-1A의 구멍 그대로다.
        """
        return self._provider.is_configured and self._storage.is_configured

    # MARK: - 생성

    def generate(self, user_id: str, request_id: str, raw_prompt: str) -> Generation:
        """프롬프트 → 투명 PNG. **같은 `(user, requestId)`는 몇 번을 불러도 한 번만 만든다.**

        재시도는 provider를 다시 부르지 않고 지금 상태를 그대로 돌려준다.
        """
        request_id = self._checked_request_id(request_id)
        identifier = generation_id(user_id, request_id)

        # **재시도를 먼저 본다.** 프롬프트 검사보다 앞이어야 한다 —
        # 응답을 잃은 client는 무엇을 적었는지 다시 보낼 수 없다(우리도 저장하지 않는다).
        # 이미 있는 작업이면 프롬프트 없이도 이어받을 수 있어야 한다.
        existing = self._store.get(identifier)
        if existing is not None:
            # **provider를 부르지 않는다.**
            return self._resume(existing)

        # 새로 만드는 길. 고칠 수 있는 실패는 **차감 전에** 전부 거른다.
        prompt = normalize_prompt(raw_prompt)
        if not self.is_available:
            raise AIStickerError(AIStickerReason.NOT_CONFIGURED)

        # 차감과 작업 문서 생성. 순서가 중요하다 — 문서를 **먼저** 만들어
        # "차감했는데 아무 흔적이 없는" 상태를 만들지 않는다.
        created, is_new = self._store.create_pending(
            leased(Generation(
                id=identifier,
                user_id=user_id,
                status=GenerationStatus.PENDING,
                price=self._price,
                # 차감의 원장 키는 generation id 그 자체다. 문서만 보고 원장 줄을 찾을 수 있게 적어 둔다.
                debit_entry_id=identifier,
            ))
        )
        if not is_new:
            # 같은 requestId가 동시에 들어왔고 상대가 먼저 만들었다. 우리는 차감하지 않는다.
            logger.info("ai_generation_duplicate id=%s", short(identifier))
            return self._resume(created)

        try:
            result = self._shards.debit(
                user_id, self._price, ShardReason.AI_STICKER, external_event_id=identifier
            )
        except InsufficientShards as error:
            # 차감하지 못했으므로 이 작업은 없던 것으로 닫는다. 환불할 것도 없다.
            self._store.finish(
                identifier, created.lease_expires_at, GenerationStatus.REFUNDED,
                failure_reason=AIStickerReason.INSUFFICIENT_SHARDS,
            )
            logger.info("ai_sticker_insufficient_shards price=%d", self._price)
            raise AIStickerError(AIStickerReason.INSUFFICIENT_SHARDS) from error

        logger.info(
            "ai_generation_started id=%s prompt_length=%d applied=%s",
            short(identifier), len(prompt), result.applied,
        )
        return self._run(created, prompt)

    def _run(self, generation: Generation, prompt: str) -> Generation:
        """임차 중인 작업 하나를 끝까지 돌린다. 실패하면 환불까지 한다."""
        started = utcnow()
        try:
            png = self._provider.generate(prompt)
        except AIStickerError as error:
            return self._fail(generation, error.reason)

        name = object_name(generation.id)
        try:
            # **응답보다 먼저 durable하게 둔다.** 이 줄이 응답 유실 복구의 전부다.
            self._storage.put(name, png)
        except Exception as error:  # noqa: BLE001 — 올리지 못했으면 없는 것과 같다
            logger.error("ai_storage_put_failed id=%s error=%s", short(generation.id), type(error).__name__)
            return self._fail(generation, AIStickerReason.STORAGE_FAILED)

        finished = self._store.finish(
            generation.id, generation.lease_expires_at, GenerationStatus.SUCCEEDED,
            result_object=name, result_expires_at=utcnow() + RESULT_RETENTION,
        )
        if finished is None:
            # CAS에서 졌다. 그 사이에 다른 쪽이 이 작업을 끝냈다는 뜻이고,
            # **이 결과는 사용자에게 나가지 않는다.** 올려 둔 object를 치운다.
            return self._abandon(generation, name)

        logger.info(
            "ai_generation_succeeded id=%s bytes=%d duration_bucket=%s",
            short(generation.id), len(png), _duration_bucket(started),
        )
        return finished

    def _abandon(self, generation: Generation, name: str) -> Generation:
        """CAS에서 진 worker의 뒷정리. **자기가 만든 object만** 치운다.

        지금 상태가 `succeeded`이고 바로 그 object를 가리키고 있으면 **지우지 않는다** —
        복구가 이 object를 보고 성공으로 확정했을 수 있고, 그걸 지우면
        조각을 쓴 사용자가 그림을 못 받는다.
        """
        current = self._store.get(generation.id) or generation
        logger.warning(
            "ai_generation_late_finish_rejected id=%s status=%s",
            short(generation.id), current.status.value,
        )
        if current.status is GenerationStatus.SUCCEEDED and current.result_object == name:
            return current
        try:
            self._storage.delete(name)
            logger.info("ai_orphan_object_deleted id=%s", short(generation.id))
        except Exception as error:  # noqa: BLE001 — best effort. lifecycle이 결국 지운다
            logger.warning("ai_orphan_delete_failed id=%s error=%s",
                           short(generation.id), type(error).__name__)
        return current

    def _fail(self, generation: Generation, reason: AIStickerReason) -> Generation:
        """실패를 확정하고 조각을 되돌린다. **환불 키는 결정적이라 두 번 들어가지 않는다.**"""
        refund_entry_id = self._refund(generation)
        status = GenerationStatus.REFUNDED if refund_entry_id else GenerationStatus.FAILED
        finished = self._store.finish(
            generation.id, generation.lease_expires_at, status,
            failure_reason=reason, refund_entry_id=refund_entry_id,
        )
        if finished is None:
            # CAS에서 졌다. 환불은 결정적 키라 이미 반영됐거나 무시됐고, 어느 쪽이든
            # 원장에는 한 줄뿐이다. 상태는 이긴 쪽 것을 그대로 읽는다.
            logger.warning("ai_generation_late_fail_rejected id=%s", short(generation.id))
            return self._store.get(generation.id) or generation
        logger.info("ai_generation_failed id=%s reason=%s status=%s",
                    short(generation.id), reason.value, status.value)
        return finished

    def _refund(self, generation: Generation) -> str | None:
        """되돌린다. 실패하면 `None` — 상태가 `failed`로 남아 나중에 다시 시도된다.

        `refund_key`가 generation id에서 결정적으로 나오므로, 몇 번을 불러도
        원장에는 한 줄만 들어간다(두 번째부터는 `applied=False`).
        """
        try:
            result = self._shards.credit(
                generation.user_id, generation.price, ShardReason.REFUND,
                external_event_id=generation.refund_event_id,
            )
        except Exception as error:  # noqa: BLE001 — 환불 실패가 원래 오류를 덮으면 안 된다
            logger.error("ai_refund_failed id=%s error=%s", short(generation.id), type(error).__name__)
            return None
        logger.info("ai_refunded id=%s amount=%d applied=%s",
                    short(generation.id), generation.price, result.applied)
        # 재시도로 `applied=False`여도 같은 줄을 가리킨다 — 키가 결정적이기 때문이다.
        return generation.refund_event_id

    # MARK: - 복구

    def _resume(self, generation: Generation) -> Generation:
        """이미 있는 작업을 어떻게 답할지 정한다. 필요하면 여기서 정리한다."""
        if generation.status is not GenerationStatus.PENDING:
            # 끝난 작업이다. `failed`는 환불이 남아 있으므로 한 번 더 시도한다.
            if generation.status is GenerationStatus.FAILED:
                return self._retry_refund(generation)
            return generation
        # **lease가 만료됐다고 곧바로 정리하지 않는다.** worker가 아직 살아 있을 수 있다
        # (Cloud Run timeout은 container를 죽이지 않는다). grace를 넘겨야 손댄다.
        if not generation.is_recoverable:
            return generation
        return self.reconcile(generation)

    def reconcile(self, generation: Generation) -> Generation:
        """grace를 넘긴 `pending`을 정리한다. **결과가 있으면 성공, 없으면 환불.**

        `exists`는 **증거일 뿐 권위가 아니다.** 확인한 뒤에 늦은 upload가 도착할 수 있고,
        그 경우 그 object는 orphan이 된다 — 사용자에게 나가는 것은
        `finish`가 확정한 terminal state뿐이다.
        """
        stolen = self._store.steal_expired(generation.id)
        if stolen is None:
            # 다른 요청이 먼저 가져갔거나 이미 끝났다. 지금 상태를 그대로 읽어 답한다.
            return self._store.get(generation.id) or generation

        name = object_name(stolen.id)
        try:
            has_result = self._storage.exists(name)
        except Exception as error:  # noqa: BLE001
            logger.warning("ai_storage_exists_failed id=%s error=%s",
                           short(stolen.id), type(error).__name__)
            # 확인하지 못했으면 **되돌리지 않는다.** 성공한 결과를 환불하는 것이
            # 조금 더 기다리는 것보다 나쁘다. lease를 다시 만료시켜 다음에 재시도한다.
            return stolen

        if has_result:
            logger.info("ai_generation_recovered_success id=%s", short(stolen.id))
            finished = self._store.finish(
                stolen.id, stolen.lease_expires_at, GenerationStatus.SUCCEEDED,
                result_object=name, result_expires_at=utcnow() + RESULT_RETENTION,
            )
            return finished or (self._store.get(stolen.id) or stolen)

        logger.info("ai_generation_recovered_interrupted id=%s", short(stolen.id))
        recovered = self._fail(stolen, AIStickerReason.INTERRUPTED)
        # 환불로 끝났다면 그 뒤에 늦은 upload가 도착했을 수 있다. 그 object는 orphan이다 —
        # 사용자는 조각을 돌려받았으므로 결과를 함께 받으면 공짜가 된다.
        if recovered.status is GenerationStatus.REFUNDED:
            self._sweep_orphan(recovered, name)
        return recovered

    def _sweep_orphan(self, generation: Generation, name: str) -> None:
        """환불된 작업 자리에 남은 object를 치운다. **best effort.**"""
        try:
            if self._storage.exists(name):
                self._storage.delete(name)
                logger.info("ai_orphan_object_deleted id=%s", short(generation.id))
        except Exception as error:  # noqa: BLE001 — lifecycle이 결국 지운다
            logger.warning("ai_orphan_delete_failed id=%s error=%s",
                           short(generation.id), type(error).__name__)

    def _retry_refund(self, generation: Generation) -> Generation:
        """`failed`(환불 못 함)를 다시 되돌린다."""
        refund_entry_id = self._refund(generation)
        if refund_entry_id is None:
            return generation
        # lease가 None인 상태에서 끝내므로 expected도 None이다.
        finished = self._store.finish(
            generation.id, None, GenerationStatus.REFUNDED,
            failure_reason=generation.failure_reason, refund_entry_id=refund_entry_id,
        )
        return finished or generation

    def sweep(self, user_id: str) -> int:
        """이 사용자의 묶인 조각을 푼다. 앱이 켜질 때 `config` 조회가 부른다.

        worker를 두지 않은 대신, **사용자가 돌아오는 그 순간** 정리한다.
        실패해도 조용히 넘어간다 — 이건 부수 작업이고 config 응답을 막으면 안 된다.
        """
        try:
            stale = self._store.stale_pending(user_id)
        except Exception as error:  # noqa: BLE001
            logger.warning("ai_sweep_query_failed error=%s", type(error).__name__)
            return 0

        recovered = 0
        for generation in stale:
            # grace를 넘긴 것만 손댄다 — 아직 도는 중일 수 있는 작업을 정리하지 않는다.
            if not generation.is_recoverable:
                continue
            try:
                self.reconcile(generation)
                recovered += 1
            except Exception as error:  # noqa: BLE001
                logger.warning("ai_sweep_failed id=%s error=%s",
                               short(generation.id), type(error).__name__)
        if recovered:
            logger.info("ai_sweep_recovered count=%d", recovered)
        return recovered

    # MARK: - 조회

    def status(self, user_id: str, identifier: str) -> Generation:
        """상태 조회. **남의 작업은 없는 것으로 답한다.**"""
        generation = self._store.get(identifier)
        if generation is None or generation.user_id != user_id:
            # 있는지 없는지도 알려주지 않는다 — 404와 403을 구분하면 존재가 새어 나간다.
            raise AIStickerError(AIStickerReason.NOT_FOUND)
        return self._resume(generation)

    def result(self, user_id: str, identifier: str) -> bytes:
        """결과 이미지. **Firestore의 terminal state가 유일한 권위다.**

        object가 bucket에 있어도 상태가 `succeeded`가 아니면 내보내지 않는다 —
        환불받은 사용자가 결과까지 받으면 공짜가 된다.
        """
        generation = self.status(user_id, identifier)
        if generation.status is GenerationStatus.PENDING:
            raise AIStickerError(AIStickerReason.STILL_PENDING)
        if generation.status is not GenerationStatus.SUCCEEDED:
            raise AIStickerError(generation.failure_reason or AIStickerReason.NOT_FOUND)
        if not generation.is_result_available:
            raise AIStickerError(AIStickerReason.RESULT_EXPIRED)

        png = self._storage.get(generation.result_object or object_name(identifier))
        if png is None:
            # lifecycle이 이미 지웠다. 조각은 이미 그림으로 바뀐 뒤라 환불 대상이 아니다.
            raise AIStickerError(AIStickerReason.RESULT_EXPIRED)
        return png

    # MARK: - 내부

    @staticmethod
    def _checked_request_id(request_id: str) -> str:
        """client가 만든 멱등 키. **모양만 본다** — 값의 의미는 client 것이다."""
        if not isinstance(request_id, str):
            raise AIStickerError(AIStickerReason.INVALID_REQUEST_ID)
        trimmed = request_id.strip()
        if not trimmed or len(trimmed) > MAX_REQUEST_ID_LENGTH:
            raise AIStickerError(AIStickerReason.INVALID_REQUEST_ID)
        # 제어문자 · 공백이 섞인 키는 거절한다. 로그와 문서 ID 계산이 지저분해진다.
        if any(character.isspace() or ord(character) < 32 for character in trimmed):
            raise AIStickerError(AIStickerReason.INVALID_REQUEST_ID)
        return trimmed


def new_request_id() -> str:
    """test / 도구용. production에서는 **client가 만든다.**"""
    return str(uuid.uuid4())


def _duration_bucket(started) -> str:
    """정확한 시간을 남기지 않는다 — 대략적인 구간만."""
    seconds = (utcnow() - started).total_seconds()
    for limit in (5, 10, 20, 40, 80):
        if seconds < limit:
            return f"<{limit}s"
    return ">=80s"
