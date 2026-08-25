"""알림센터 service (Phase F)."""

from __future__ import annotations

import logging

from app.auth.models import User
from app.notifications.models import (
    MAX_PAGE,
    PAGE_SIZE,
    NotificationEvent,
    SaleStat,
)
from app.notifications.store import NotificationStore

logger = logging.getLogger(__name__)


class NotificationService:
    def __init__(self, store: NotificationStore, marketplace=None) -> None:
        self._store = store
        # 판매 현황은 **판매자의 listing에서 읽는다** — 새 counter를 만들지 않는다.
        self._marketplace = marketplace

    def page(
        self, user: User, *, cursor: str | None = None, limit: int = PAGE_SIZE
    ) -> tuple[list[NotificationEvent], str | None]:
        """**authenticated caller가 곧 주인이다.** userId를 받을 자리가 없다."""
        return self._store.page(user.id, cursor, max(1, min(limit, MAX_PAGE)))

    def mark_read(self, user: User, event_id: str) -> NotificationEvent:
        return self._store.mark_read(user.id, event_id)

    def sale_stats(self, user: User) -> list[SaleStat]:
        """내 상품이 각각 몇 번 팔렸는가.

        **알림 기록을 세지 않는다.** 알림은 페이지로 끊어 읽으므로 그것을 세면
        "총 판매 횟수"가 화면에 몇 장을 불러왔는지에 따라 달라진다.

        `listing.downloadCount`가 이미 정확한 값이다 — 소유권이 새로 생길 때만,
        구매와 **같은 transaction 안에서** 오른다. 두 번째 counter를 만들면
        둘이 어긋날 자리가 새로 생긴다.

        팔린 적 없는 상품은 빼고, 많이 팔린 것부터 준다.
        """
        if self._marketplace is None:
            return []
        stats = [
            SaleStat(
                listing_id=x.id,
                content_type=x.content_type.value,
                title=x.title,
                sale_count=x.download_count,
                price_shards=x.price_shards,
            )
            for x in self._marketplace.seller_listings(user)
            if x.download_count > 0
        ]
        return sorted(stats, key=lambda x: (-x.sale_count, x.listing_id))

    def delete_for_user(self, user_id: str) -> int:
        return self._store.delete_for_user(user_id)
