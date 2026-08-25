"""Firestore 구현."""

from __future__ import annotations

import logging

from google.api_core import exceptions as gcp_exceptions
from google.cloud import firestore

from app.auth.store import StoreUnavailable
from app.notifications.models import (
    NotificationEvent,
    NotificationNotFound,
    NotificationType,
)
from app.notifications.store import NOTIFICATIONS
from app.shards.models import utcnow

logger = logging.getLogger(__name__)


class FirestoreNotificationStore:
    def __init__(self, client: firestore.Client) -> None:
        self._db = client

    def create(self, event: NotificationEvent) -> NotificationEvent:
        try:
            self._db.collection(NOTIFICATIONS).document(event.id).create(_document(event))
        except gcp_exceptions.AlreadyExists:
            # 같은 판매에 알림은 하나다. 재시도는 오류가 아니다.
            return event
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("notification_create", error) from error
        return event

    def page(
        self, user_id: str, cursor: str | None, limit: int
    ) -> tuple[list[NotificationEvent], str | None]:
        """`userId` 하나로 질의하고 **정렬은 application에서 한다.**

        `where` + `order_by`는 composite index를 요구한다. 한 사람의 알림 수는
        정렬해도 되는 규모이고, 커지면 그때 index와 함께 넣는다.
        """
        try:
            found = (
                self._db.collection(NOTIFICATIONS)
                .where(filter=firestore.FieldFilter("userId", "==", user_id))
                .stream()
            )
            events = [_event_from(x.id, x.to_dict() or {}) for x in found]
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("notification_list", error) from error

        events.sort(key=lambda x: (x.created_at, x.id), reverse=True)
        if cursor is not None:
            ids = [x.id for x in events]
            events = events[ids.index(cursor) + 1:] if cursor in ids else []
        page = events[:limit]
        return page, (page[-1].id if page and len(events) > limit else None)

    def mark_read(self, user_id: str, event_id: str) -> NotificationEvent:
        reference = self._db.collection(NOTIFICATIONS).document(event_id)

        @firestore.transactional
        def run(transaction) -> NotificationEvent:
            found = reference.get(transaction=transaction)
            if not found.exists:
                raise NotificationNotFound(event_id)
            event = _event_from(event_id, found.to_dict() or {})
            if event.user_id != user_id:
                # 남의 알림이다. 없는 것과 구분해 알려주지 않는다.
                raise NotificationNotFound(event_id)
            if event.is_read:
                return event
            now = utcnow()
            transaction.update(reference, {"readAt": now})
            return NotificationEvent(
                id=event.id, user_id=event.user_id, type=event.type,
                listing_id=event.listing_id, content_type=event.content_type,
                title_snapshot=event.title_snapshot, shard_amount=event.shard_amount,
                created_at=event.created_at, read_at=now,
            )

        try:
            return run(self._db.transaction())
        except NotificationNotFound:
            raise
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("notification_read", error) from error

    def delete_for_user(self, user_id: str) -> int:
        try:
            found = list(
                self._db.collection(NOTIFICATIONS)
                .where(filter=firestore.FieldFilter("userId", "==", user_id))
                .stream()
            )
            for snapshot in found:
                snapshot.reference.delete()
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("notification_delete", error) from error
        return len(found)

    def _unavailable(self, operation: str, error: Exception) -> StoreUnavailable:
        logger.warning("firestore_failed operation=%s error=%s", operation, type(error).__name__)
        return StoreUnavailable(operation)


def _document(event: NotificationEvent) -> dict:
    return {
        "userId": event.user_id,
        "type": event.type.value,
        "listingId": event.listing_id,
        "contentType": event.content_type,
        "titleSnapshot": event.title_snapshot,
        "shardAmount": event.shard_amount,
        "createdAt": event.created_at,
        "readAt": event.read_at,
        "schemaVersion": event.schema_version,
    }


def _event_from(event_id: str, data: dict) -> NotificationEvent:
    return NotificationEvent(
        id=event_id,
        user_id=str(data.get("userId") or ""),
        type=NotificationType(data.get("type") or NotificationType.MARKETPLACE_SALE.value),
        listing_id=str(data.get("listingId") or ""),
        content_type=str(data.get("contentType") or "mirror"),
        title_snapshot=str(data.get("titleSnapshot") or ""),
        shard_amount=int(data.get("shardAmount") or 0),
        created_at=data.get("createdAt") or utcnow(),
        read_at=data.get("readAt"),
        schema_version=int(data.get("schemaVersion") or 1),
    )
