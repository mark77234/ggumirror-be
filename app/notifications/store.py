"""알림 기록 저장소.

**전체를 한 번에 읽지 않는다** — cursor로 끊어 읽는다(marketplace 운영 목록과 같은 규칙).
"""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Protocol

from app.notifications.models import NotificationEvent, NotificationNotFound
from app.shards.models import utcnow

NOTIFICATIONS = "ggumirror_user_notifications"


class NotificationStore(Protocol):
    def create(self, event: NotificationEvent) -> NotificationEvent:
        """**`create`다.** 같은 자리에 두 번 쓰지 않는다 — 그것이 알림이
        한 판매에 하나뿐인 이유다."""

    def page(
        self, user_id: str, cursor: str | None, limit: int
    ) -> tuple[list[NotificationEvent], str | None]:
        """내 알림 한 장. 최신이 먼저다."""

    def mark_read(self, user_id: str, event_id: str) -> NotificationEvent:
        """**내 알림만** 읽음으로 바꾼다. 남의 것이면 `NotificationNotFound`."""

    def delete_for_user(self, user_id: str) -> int:
        """계정 삭제용. 그 사람의 알림 전부."""


class InMemoryNotificationStore:
    def __init__(self) -> None:
        self.events: dict[str, NotificationEvent] = {}
        self._lock = threading.RLock()

    def create(self, event: NotificationEvent) -> NotificationEvent:
        with self._lock:
            if event.id in self.events:
                # 이미 있다. **덮어쓰지 않는다** — 같은 판매에 알림은 하나다.
                return self.events[event.id]
            self.events[event.id] = event
            return event

    def page(
        self, user_id: str, cursor: str | None, limit: int
    ) -> tuple[list[NotificationEvent], str | None]:
        ordered = sorted(
            (x for x in self.events.values() if x.user_id == user_id),
            key=lambda x: (x.created_at, x.id),
            reverse=True,
        )
        if cursor is not None:
            ids = [x.id for x in ordered]
            ordered = ordered[ids.index(cursor) + 1:] if cursor in ids else []
        page = ordered[:limit]
        return page, (page[-1].id if page and len(ordered) > limit else None)

    def mark_read(self, user_id: str, event_id: str) -> NotificationEvent:
        with self._lock:
            found = self.events.get(event_id)
            if found is None or found.user_id != user_id:
                raise NotificationNotFound(event_id)
            if found.is_read:
                return found   # 이미 읽었다. 다시 써도 같은 결과다(멱등).
            updated = replace(found, read_at=utcnow())
            self.events[event_id] = updated
            return updated

    def delete_for_user(self, user_id: str) -> int:
        with self._lock:
            gone = [k for k, v in self.events.items() if v.user_id == user_id]
            for key in gone:
                del self.events[key]
            return len(gone)
