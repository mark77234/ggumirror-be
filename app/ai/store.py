"""생성 작업 저장소.

`ShardStore`와 같은 방식이다 — Protocol 하나 + Firestore 구현 하나 + test fake 하나.

**상태 전이는 전부 조건부 transaction이다.** "읽고 → 판단하고 → 쓴다"를 나누면
느린 요청과 복구 로직이 서로의 결과를 덮어쓴다. 그래서 protocol이
`claim` / `finish` / `steal` 같은 **하나의 원자적 연산**만 노출한다.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import Protocol

from app.ai.models import (
    GENERATION_LEASE,
    AIStickerReason,
    Generation,
    GenerationStatus,
    can_transition,
)
from app.auth.store import StoreUnavailable  # noqa: F401  (같은 실패 타입을 쓴다)
from app.shards.models import utcnow


class GenerationStore(Protocol):
    def get(self, generation_id: str) -> Generation | None:
        """없으면 None. **소유자 검증은 부르는 쪽이 한다.**"""

    def create_pending(self, generation: Generation) -> tuple[Generation, bool]:
        """`pending` 작업을 만든다.

        두 번째 값은 **이번 호출이 실제로 만들었는가**다. 같은 id가 이미 있으면
        `(기존 작업, False)`다 — 그때 provider를 다시 부르면 안 되고 차감도 하면 안 된다.

        `create()`로 쓰므로 동시 요청 둘 중 정확히 하나만 True를 받는다.
        조회해서 없으면 만드는 방식으로 바꾸지 않는다.
        """

    def finish(
        self,
        generation_id: str,
        expected_lease: object,
        status: GenerationStatus,
        result_object: str | None = None,
        result_expires_at: object = None,
        failure_reason: AIStickerReason | None = None,
        refund_entry_id: str | None = None,
    ) -> Generation | None:
        """작업을 끝낸다. **두 관문을 모두 통과해야 쓴다.**

        1. **terminal 금지** — 지금 상태가 `succeeded`나 `refunded`면 무엇도 쓰지 않는다.
           `refunded → succeeded`(공짜 결과)와 `succeeded → refunded`(공짜 조각)를
           여기서 구조적으로 막는다. lease가 어쩌다 맞아도 통과하지 못한다.
        2. **lease 일치** — `expected_lease`가 문서의 현재 값과 다르면 쓰지 않는다.
           그 사이에 다른 쪽이 임차권을 가져갔다는 뜻이다.

        통과하지 못하면 `None`. 부르는 쪽은 **자기가 만든 부산물(업로드한 object)을
        치워야 한다** — 그 결과는 사용자에게 나가지 않기 때문이다.
        """

    def steal_expired(self, generation_id: str) -> Generation | None:
        """만료된 `pending` 작업의 임차권을 가져온다. 복구가 쓴다.

        가져오면 lease가 새로 연장된 작업을 돌려주고, 이미 누가 가져갔거나
        아직 살아 있으면 `None`이다. 두 복구 시도가 동시에 들어와도
        **정확히 하나만** 처리한다.
        """

    def stale_pending(self, user_id: str, limit: int = 10) -> list[Generation]:
        """이 사용자의 `pending` 작업들. 정리 대상인지는 부르는 쪽이 판단한다."""


class InMemoryGenerationStore:
    """test / local용. Firestore transaction의 **의미**를 그대로 흉내 낸다."""

    def __init__(self) -> None:
        self.generations: dict[str, Generation] = {}
        self._lock = threading.Lock()

    def get(self, generation_id: str) -> Generation | None:
        return self.generations.get(generation_id)

    def create_pending(self, generation: Generation) -> tuple[Generation, bool]:
        with self._lock:
            if existing := self.generations.get(generation.id):
                return existing, False
            self.generations[generation.id] = generation
            return generation, True

    def finish(
        self,
        generation_id: str,
        expected_lease: object,
        status: GenerationStatus,
        result_object: str | None = None,
        result_expires_at: object = None,
        failure_reason: AIStickerReason | None = None,
        refund_entry_id: str | None = None,
    ) -> Generation | None:
        with self._lock:
            current = self.generations.get(generation_id)
            if current is None or not can_transition(current.status, status):
                return None
            if current.lease_expires_at != expected_lease:
                return None
            updated = Generation(
                id=current.id,
                user_id=current.user_id,
                status=status,
                price=current.price,
                created_at=current.created_at,
                updated_at=utcnow(),
                lease_expires_at=None,
                debit_entry_id=current.debit_entry_id,
                refund_entry_id=refund_entry_id or current.refund_entry_id,
                result_object=result_object,
                result_expires_at=result_expires_at,  # type: ignore[arg-type]
                failure_reason=failure_reason,
            )
            self.generations[generation_id] = updated
            return updated

    def steal_expired(self, generation_id: str) -> Generation | None:
        with self._lock:
            current = self.generations.get(generation_id)
            if current is None or not current.is_lease_expired:
                return None
            stolen = Generation(
                id=current.id,
                user_id=current.user_id,
                status=GenerationStatus.PENDING,
                price=current.price,
                created_at=current.created_at,
                updated_at=utcnow(),
                lease_expires_at=utcnow() + GENERATION_LEASE,
                debit_entry_id=current.debit_entry_id,
                refund_entry_id=current.refund_entry_id,
                result_object=current.result_object,
                result_expires_at=current.result_expires_at,
                failure_reason=current.failure_reason,
            )
            self.generations[generation_id] = stolen
            return stolen

    def stale_pending(self, user_id: str, limit: int = 10) -> list[Generation]:
        found = [
            generation
            for generation in self.generations.values()
            if generation.user_id == user_id and generation.status is GenerationStatus.PENDING
        ]
        return found[:limit]


def leased(generation: Generation, lease: timedelta = GENERATION_LEASE) -> Generation:
    """`pending` + 새 lease. 만드는 쪽이 쓴다."""
    now = utcnow()
    return Generation(
        id=generation.id,
        user_id=generation.user_id,
        status=GenerationStatus.PENDING,
        price=generation.price,
        created_at=now,
        updated_at=now,
        lease_expires_at=now + lease,
        debit_entry_id=generation.debit_entry_id,
    )
