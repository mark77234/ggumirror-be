"""Firestore 구현 — 알림 설정과 발송 기록."""

from __future__ import annotations

import logging

from google.api_core import exceptions as gcp_exceptions
from google.cloud import firestore

from app.auth.store import StoreUnavailable
from app.notifications.delivery import DIGEST_DELIVERIES, DeliveryRecord
from app.notifications.preferences import (
    NOTIFICATION_PREFERENCES,
    DigestFrequency,
    NotificationPreferences,
)
from app.shards.models import utcnow

logger = logging.getLogger(__name__)


class FirestorePreferenceStore:
    def __init__(self, client: firestore.Client) -> None:
        self._db = client

    def preferences(self, user_id: str) -> NotificationPreferences:
        """**문서가 없어도 만들지 않는다.** 기본값으로 읽는다(migration 없음)."""
        try:
            found = self._db.collection(NOTIFICATION_PREFERENCES).document(user_id).get()
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("preferences_read", error) from error
        if not found.exists:
            return NotificationPreferences.default(user_id)
        data = found.to_dict() or {}
        return NotificationPreferences(
            user_id=user_id,
            # 없는 값의 뜻은 도메인이 정한다 — 판매는 켜짐, 나머지는 꺼짐.
            sales_enabled=bool(data.get("salesEnabled", True)),
            digest_frequency=DigestFrequency.of(data.get("mirrorDigestFrequency")),
            recommendation_enabled=bool(data.get("recommendationEnabled", False)),
            updated_at=data.get("updatedAt") or utcnow(),
        )

    def save(self, preferences: NotificationPreferences) -> NotificationPreferences:
        now = utcnow()
        try:
            self._db.collection(NOTIFICATION_PREFERENCES).document(preferences.user_id).set(
                {
                    "salesEnabled": preferences.sales_enabled,
                    "mirrorDigestFrequency": preferences.digest_frequency.value,
                    "recommendationEnabled": preferences.recommendation_enabled,
                    "updatedAt": now,
                    "schemaVersion": preferences.schema_version,
                }
            )
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("preferences_save", error) from error
        return NotificationPreferences(
            user_id=preferences.user_id,
            sales_enabled=preferences.sales_enabled,
            digest_frequency=preferences.digest_frequency,
            recommendation_enabled=preferences.recommendation_enabled,
            updated_at=now,
        )

    def delete(self, user_id: str) -> None:
        try:
            self._db.collection(NOTIFICATION_PREFERENCES).document(user_id).delete()
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("preferences_delete", error) from error

    def _unavailable(self, operation: str, error: Exception) -> StoreUnavailable:
        logger.warning("firestore_failed operation=%s error=%s", operation, type(error).__name__)
        return StoreUnavailable(operation)


class FirestoreDeliveryStore:
    def __init__(self, client: firestore.Client) -> None:
        self._db = client

    def claim(self, record: DeliveryRecord) -> bool:
        """`create`다. 자리가 이미 차 있으면 진 쪽이 조용히 물러난다.

        읽고-비교하고-쓰지 않는다 — 그 사이에 다른 실행이 끼어들면 둘 다 보낸다.
        """
        try:
            self._db.collection(DIGEST_DELIVERIES).document(record.id).create(
                {
                    "userId": record.user_id,
                    "kind": record.kind,
                    "window": record.window,
                    "createdAt": record.created_at,
                }
            )
        except gcp_exceptions.AlreadyExists:
            return False
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("delivery_claim", error) from error
        return True

    def delete_for_user(self, user_id: str) -> int:
        try:
            found = list(
                self._db.collection(DIGEST_DELIVERIES)
                .where(filter=firestore.FieldFilter("userId", "==", user_id))
                .stream()
            )
            for snapshot in found:
                snapshot.reference.delete()
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("delivery_delete", error) from error
        return len(found)

    def _unavailable(self, operation: str, error: Exception) -> StoreUnavailable:
        logger.warning("firestore_failed operation=%s error=%s", operation, type(error).__name__)
        return StoreUnavailable(operation)
