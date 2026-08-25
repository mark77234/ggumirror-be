"""내장 템플릿 획득 저장소.

**획득 기록과 통계가 한 commit이다.** 갈라지면 "받았는데 안 세어졌다"거나
"세었는데 기록이 없다"가 되고, 둘 다 나중에 고칠 방법이 없다.

동시에 같은 사용자가 두 번 눌러도 +1이다 — 문서 id가 `(userId, templateId)`이고
`create`로 쓰기 때문에 두 번째는 저장소가 거절한다(B-7E 소유권과 같은 규칙).
"""

from __future__ import annotations

import logging
from threading import RLock
from typing import Protocol

from app.auth.store import StoreUnavailable
from app.shards.models import ShardReason
from app.catalog.models import (
    AcquisitionResult,
    TemplateAcquisition,
    TemplateStat,
    acquisition_id,
)

logger = logging.getLogger(__name__)

ACQUISITIONS = "ggumirror_catalog_acquisitions"
STATS = "ggumirror_catalog_stats"


class CatalogStore(Protocol):
    def purchase(
        self, shards, user_id: str, template_id: str, price: int
    ) -> "AcquisitionResult":
        """조각을 내고 내장 템플릿을 갖는다. **하나의 transaction이다.**

        멱등의 열쇠는 **`(사용자, 템플릿)` 기록 자체**다 — 그것이 있으면 값을 내지
        않는다. 별도 operation id를 두지 않는 이유가 그것이다: 다른 id로 다시
        보내도 같은 기록을 겨루므로 두 번 빠질 수 없다.
        """

    def acquire(self, user_id: str, template_id: str) -> AcquisitionResult:
        """**최초 획득만 센다.** 이미 있으면 `first_acquisition=False`이고 수는 그대로다.

        기록 생성과 카운터 증가가 **한 transaction**이어야 한다.
        """

    def stats(self, template_ids: list[str]) -> list[TemplateStat]:
        """공개 통계. 기록이 없는 템플릿은 **0으로 돌려준다** —
        없는 것과 0은 사용자에게 같은 뜻이고, 빠뜨리면 화면이 자리를 비운다."""

    def acquired_template_ids(self, user_id: str) -> set[str]:
        """이 사용자가 이미 기록한 템플릿. 맞춰 보기(reconcile)가 쓴다."""


class InMemoryCatalogStore:
    """test / local용. 실제 store와 **같은 원자성**을 흉내 낸다."""

    def __init__(self) -> None:
        self.acquisitions: dict[str, TemplateAcquisition] = {}
        self.counts: dict[str, int] = {}
        self._lock = RLock()

    def acquire(self, user_id: str, template_id: str) -> AcquisitionResult:
        with self._lock:
            key = acquisition_id(user_id, template_id)
            if key in self.acquisitions:
                return AcquisitionResult(
                    template_id=template_id,
                    first_acquisition=False,
                    download_count=self.counts.get(template_id, 0),
                )
            self.acquisitions[key] = TemplateAcquisition(
                user_id=user_id, template_id=template_id
            )
            self.counts[template_id] = self.counts.get(template_id, 0) + 1
            return AcquisitionResult(
                template_id=template_id,
                first_acquisition=True,
                download_count=self.counts[template_id],
            )

    def purchase(self, shards, user_id: str, template_id: str, price: int) -> AcquisitionResult:
        with self._lock:
            key = acquisition_id(user_id, template_id)
            if key in self.acquisitions:
                # 이미 가지고 있다. **값을 내지 않는다.**
                return AcquisitionResult(
                    template_id=template_id, first_acquisition=False,
                    download_count=self.counts.get(template_id, 0),
                )
            if price > 0:
                shards.debit(
                    user_id, price, ShardReason.CATALOG_TEMPLATE_PURCHASE,
                    external_event_id=key,
                )
            self.acquisitions[key] = TemplateAcquisition(
                user_id=user_id, template_id=template_id
            )
            self.counts[template_id] = self.counts.get(template_id, 0) + 1
            return AcquisitionResult(
                template_id=template_id, first_acquisition=True,
                download_count=self.counts[template_id],
            )

    def stats(self, template_ids: list[str]) -> list[TemplateStat]:
        with self._lock:
            return [
                TemplateStat(template_id=x, download_count=self.counts.get(x, 0))
                for x in template_ids
            ]

    def acquired_template_ids(self, user_id: str) -> set[str]:
        with self._lock:
            return {
                x.template_id for x in self.acquisitions.values() if x.user_id == user_id
            }


class FirestoreCatalogStore:
    """실제 저장소. 기록 생성과 카운터 증가가 **한 transaction**이다."""

    def __init__(self, db) -> None:
        self._db = db

    def purchase(self, shards, user_id: str, template_id: str, price: int) -> AcquisitionResult:
        """조각을 내고 갖는다. **읽기를 전부 먼저 한다** — Firestore transaction은
        쓰기 뒤에 읽을 수 없다(유료 Marketplace 구매에서 실제로 터졌던 자리다).

        멱등의 열쇠는 `(사용자, 템플릿)` 기록이다. 이미 있으면 값을 내지 않고,
        원장 열쇠도 같은 값이라 다른 요청 id로 다시 보내도 두 번 빠지지 않는다.
        """
        from google.api_core import exceptions as gcp_exceptions
        from google.cloud import firestore

        key = acquisition_id(user_id, template_id)
        record_ref = self._db.collection(ACQUISITIONS).document(key)
        stat_ref = self._db.collection(STATS).document(template_id)

        @firestore.transactional
        def run(transaction) -> AcquisitionResult:
            scoped = shards.context(transaction)

            # **읽기 먼저.**
            existing = record_ref.get(transaction=transaction)
            stat = stat_ref.get(transaction=transaction)
            count = int((stat.to_dict() or {}).get("downloadCount") or 0) if stat.exists else 0

            if existing.exists:
                # 이미 가지고 있다. **조각을 빼지 않는다** — 값이 오른 뒤에도 그대로다.
                return AcquisitionResult(
                    template_id=template_id, first_acquisition=False, download_count=count
                )

            if price > 0:
                # 잔액이 모자라면 여기서 끝나고 **기록도 수도 남지 않는다.**
                shards.apply_in_transaction(
                    scoped, user_id, -price,
                    ShardReason.CATALOG_TEMPLATE_PURCHASE, key,
                )

            # 읽기가 끝났다. 조각 쓰기를 먼저 내려보낸다.
            scoped.flush()

            record = TemplateAcquisition(user_id=user_id, template_id=template_id)
            # `create`라 동시에 두 번 사도 하나만 남는다 — 마지막 방어선.
            transaction.create(
                record_ref,
                {
                    "userId": record.user_id,
                    "templateId": record.template_id,
                    "createdAt": record.created_at,
                    "pricePaid": price,
                },
            )
            if stat.exists:
                transaction.update(stat_ref, {"downloadCount": count + 1})
            else:
                transaction.set(stat_ref, {"downloadCount": count + 1})
            return AcquisitionResult(
                template_id=template_id, first_acquisition=True, download_count=count + 1
            )

        try:
            return run(self._db.transaction())
        except gcp_exceptions.GoogleAPIError as error:
            logger.error("catalog_purchase_unavailable error=%s", type(error).__name__)
            raise StoreUnavailable("catalog purchase failed") from error

    def acquire(self, user_id: str, template_id: str) -> AcquisitionResult:
        from google.api_core import exceptions as gcp_exceptions
        from google.cloud import firestore

        record_ref = self._db.collection(ACQUISITIONS).document(
            acquisition_id(user_id, template_id)
        )
        stat_ref = self._db.collection(STATS).document(template_id)

        @firestore.transactional
        def run(transaction) -> AcquisitionResult:
            # **읽기가 먼저다.** Firestore transaction은 쓰기 뒤에 읽을 수 없다.
            existing = record_ref.get(transaction=transaction)
            stat = stat_ref.get(transaction=transaction)
            count = int((stat.to_dict() or {}).get("downloadCount") or 0) if stat.exists else 0

            if existing.exists:
                # 같은 사용자가 다시 받았다. **세지 않는다** — 실패도 아니다.
                return AcquisitionResult(
                    template_id=template_id, first_acquisition=False, download_count=count
                )

            record = TemplateAcquisition(user_id=user_id, template_id=template_id)
            # `create`라 같은 문서가 두 번 만들어질 수 없다 — 동시 요청의 마지막 방어선.
            transaction.create(
                record_ref,
                {
                    "userId": record.user_id,
                    "templateId": record.template_id,
                    "createdAt": record.created_at,
                },
            )
            if stat.exists:
                transaction.update(stat_ref, {"downloadCount": count + 1})
            else:
                transaction.set(
                    stat_ref, {"templateId": template_id, "downloadCount": count + 1}
                )
            return AcquisitionResult(
                template_id=template_id, first_acquisition=True, download_count=count + 1
            )

        try:
            return run(self._db.transaction())
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("catalog_acquire", error) from error

    def stats(self, template_ids: list[str]) -> list[TemplateStat]:
        from google.api_core import exceptions as gcp_exceptions

        try:
            found = {
                doc.id: int((doc.to_dict() or {}).get("downloadCount") or 0)
                for doc in self._db.get_all(
                    [self._db.collection(STATS).document(x) for x in template_ids]
                )
                if doc.exists
            }
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("catalog_stats", error) from error
        # 기록이 없으면 0이다 — 목록에서 빼면 화면이 자리를 비운다.
        return [
            TemplateStat(template_id=x, download_count=found.get(x, 0))
            for x in template_ids
        ]

    def acquired_template_ids(self, user_id: str) -> set[str]:
        from google.api_core import exceptions as gcp_exceptions
        from google.cloud import firestore

        try:
            found = (
                self._db.collection(ACQUISITIONS)
                .where(filter=firestore.FieldFilter("userId", "==", user_id))
                .stream()
            )
            return {str((x.to_dict() or {}).get("templateId") or "") for x in found} - {""}
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("catalog_acquired", error) from error

    @staticmethod
    def _unavailable(operation: str, error: Exception) -> StoreUnavailable:
        logger.error("catalog_store_unavailable op=%s error=%s", operation, type(error).__name__)
        return StoreUnavailable(operation)
