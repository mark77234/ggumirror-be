"""생성 작업의 Firestore 구현.

collection 이름은 기존 규칙(`ggumirror_` prefix)을 따른다.

**모든 상태 전이가 transaction 하나다.** 읽기 → 조건 확인 → 쓰기가 전부 성공하거나
전부 실패한다. `lease_expires_at`을 조건으로 쓰기 때문에, 늦게 끝난 요청이
이미 복구된 작업을 되돌릴 수 없다.
"""

from __future__ import annotations

import logging
from datetime import datetime

from google.api_core import exceptions as gcp_exceptions
from google.cloud import firestore

from app.ai.models import (
    GENERATION_LEASE,
    AIStickerReason,
    Generation,
    GenerationStatus,
    can_transition,
)
from app.auth.store import StoreUnavailable
from app.shards.models import utcnow

logger = logging.getLogger(__name__)

GENERATIONS = "ggumirror_ai_generations"
#: AI 거울 하루 횟수. **원장이 아니다** — 돈이 아니라 비용을 막는 계수기다.
AI_MIRROR_QUOTAS = "ggumirror_ai_mirror_quotas"


class FirestoreGenerationStore:
    def __init__(self, client: firestore.Client) -> None:
        self._db = client

    def get(self, generation_id: str) -> Generation | None:
        try:
            snapshot = self._db.collection(GENERATIONS).document(generation_id).get()
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("generation_read", error) from error
        if not snapshot.exists:
            return None
        return _generation(snapshot.id, snapshot.to_dict() or {})

    def create_pending(self, generation: Generation) -> tuple[Generation, bool]:
        """`create()`로 쓴다 — 같은 id가 있으면 실패하고, 그게 곧 중복 판정이다."""
        reference = self._db.collection(GENERATIONS).document(generation.id)

        @firestore.transactional
        def run(transaction: firestore.Transaction) -> tuple[Generation, bool]:
            snapshot = reference.get(transaction=transaction)
            if snapshot.exists:
                return _generation(snapshot.id, snapshot.to_dict() or {}), False
            transaction.create(reference, _document(generation))
            return generation, True

        try:
            return run(self._db.transaction())
        except gcp_exceptions.AlreadyExists:
            # 동시에 같은 requestId가 둘 들어왔고 상대가 먼저 commit했다.
            # **실패가 아니다** — 이미 있다는 것이 정확히 "내가 만들지 않았다"는 답이다.
            existing = self.get(generation.id)
            if existing is None:  # 있을 수 없지만, 있으면 상위가 5xx로 다룬다.
                raise self._unavailable("generation_create_race", RuntimeError("vanished"))
            return existing, False
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("generation_create", error) from error

    def finish(
        self,
        generation_id: str,
        expected_lease: datetime | None,
        status: GenerationStatus,
        result_object: str | None = None,
        result_expires_at: datetime | None = None,
        failure_reason: AIStickerReason | None = None,
        refund_entry_id: str | None = None,
    ) -> Generation | None:
        reference = self._db.collection(GENERATIONS).document(generation_id)

        @firestore.transactional
        def run(transaction: firestore.Transaction) -> Generation | None:
            snapshot = reference.get(transaction=transaction)
            if not snapshot.exists:
                return None
            current = _generation(snapshot.id, snapshot.to_dict() or {})
            # **여기가 경제를 지키는 지점이다.** 두 관문을 모두 통과해야 쓴다:
            # (1) terminal에서 나가는 전이는 없다 — 늦게 돌아온 worker가
            #     refunded를 succeeded로, succeeded를 refunded로 뒤집지 못한다.
            # (2) lease가 내 것이 아니면 그 사이에 다른 쪽이 가져간 것이다.
            if not can_transition(current.status, status):
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
                result_expires_at=result_expires_at,
                failure_reason=failure_reason,
            )
            transaction.set(reference, _document(updated))
            return updated

        try:
            return run(self._db.transaction())
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("generation_finish", error) from error

    def steal_expired(self, generation_id: str) -> Generation | None:
        reference = self._db.collection(GENERATIONS).document(generation_id)

        @firestore.transactional
        def run(transaction: firestore.Transaction) -> Generation | None:
            snapshot = reference.get(transaction=transaction)
            if not snapshot.exists:
                return None
            current = _generation(snapshot.id, snapshot.to_dict() or {})
            if not current.is_lease_expired:
                # 아직 살아 있거나 이미 끝났다. 건드리지 않는다.
                return None

            now = utcnow()
            stolen = Generation(
                id=current.id,
                user_id=current.user_id,
                status=GenerationStatus.PENDING,
                price=current.price,
                created_at=current.created_at,
                updated_at=now,
                lease_expires_at=now + GENERATION_LEASE,
                debit_entry_id=current.debit_entry_id,
                refund_entry_id=current.refund_entry_id,
                result_object=current.result_object,
                result_expires_at=current.result_expires_at,
                failure_reason=current.failure_reason,
            )
            transaction.set(reference, _document(stolen))
            return stolen

        try:
            return run(self._db.transaction())
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("generation_steal", error) from error

    def stale_pending(self, user_id: str, limit: int = 10) -> list[Generation]:
        """이 사용자의 `pending` 작업들. 정리 대상인지는 부르는 쪽이 판단한다.

        등호 두 개짜리 질의라 단일 필드 index만으로 처리된다 — composite index가 필요 없다.
        """
        try:
            snapshots = (
                self._db.collection(GENERATIONS)
                .where(filter=firestore.FieldFilter("userId", "==", user_id))
                .where(filter=firestore.FieldFilter("status", "==", GenerationStatus.PENDING.value))
                .limit(limit)
                .get()
            )
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("generation_stale_query", error) from error

        # 시각 비교는 읽은 뒤에 한다 — 질의에 넣으면 composite index가 필요해진다.
        return [_generation(snapshot.id, snapshot.to_dict() or {}) for snapshot in snapshots]

    def _unavailable(self, operation: str, error: Exception) -> StoreUnavailable:
        logger.warning("firestore_failed operation=%s error=%s", operation, type(error).__name__)
        return StoreUnavailable(operation)


# MARK: - 문서 변환


def _document(generation: Generation) -> dict:
    """**프롬프트가 없다.** 저장하지 않기로 한 값은 문서 모양에도 자리가 없다."""
    return {
        "userId": generation.user_id,
        "type": "sticker",
        "status": generation.status.value,
        "price": generation.price,
        "createdAt": generation.created_at,
        "updatedAt": generation.updated_at,
        "leaseExpiresAt": generation.lease_expires_at,
        "debitEntryId": generation.debit_entry_id,
        "refundEntryId": generation.refund_entry_id,
        "resultObject": generation.result_object,
        "resultExpiresAt": generation.result_expires_at,
        "failureReason": generation.failure_reason.value if generation.failure_reason else None,
        "schemaVersion": 1,
    }


def _generation(generation_id: str, data: dict) -> Generation:
    raw_reason = data.get("failureReason")
    return Generation(
        id=generation_id,
        user_id=str(data.get("userId") or ""),
        status=GenerationStatus(data.get("status") or GenerationStatus.PENDING.value),
        price=int(data.get("price") or 0),
        created_at=data.get("createdAt") or utcnow(),
        updated_at=data.get("updatedAt") or utcnow(),
        lease_expires_at=data.get("leaseExpiresAt"),
        debit_entry_id=data.get("debitEntryId"),
        refund_entry_id=data.get("refundEntryId"),
        result_object=data.get("resultObject"),
        result_expires_at=data.get("resultExpiresAt"),
        failure_reason=AIStickerReason(raw_reason) if raw_reason else None,
    )
