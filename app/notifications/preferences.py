"""알림 설정 (I-11).

**기기가 아니라 사람의 설정이다.** `ggumirror_push_devices`에 섞지 않는다 —
그쪽은 "어디로 보낼까"이고 이쪽은 "무엇을 보낼까"다. 섞으면 기기를 바꿀 때
설정이 따라 사라지고, 한 사람이 기기 두 대를 쓰면 설정이 둘로 갈라진다.

**없는 값의 뜻을 여기서 정한다.** 지금 production에는 이 문서가 한 개도 없으므로
기본값이 곧 기존 사용자의 경험이다:

- 판매 알림은 **켜짐**이 기본이다. 지금 판매 알림을 받고 있는 사람이 이 기능
  하나 때문에 갑자기 못 받게 되면 그건 기능이 아니라 사고다.
- 모아 보기와 추천 소식은 **꺼짐**이 기본이다. 새로 생긴 홍보성 알림을 기존
  계정에 몰래 켜 두지 않는다. 받고 싶은 사람이 켠다.

Firestore를 손으로 고치지 않는다 — 문서가 없으면 기본값으로 읽는다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from app.shards.models import utcnow

SCHEMA_VERSION = 1

NOTIFICATION_PREFERENCES = "ggumirror_notification_preferences"


class DigestFrequency(StrEnum):
    """새 거울 소식을 얼마나 자주 받을까."""

    OFF = "off"
    DAILY = "daily"
    WEEKLY = "weekly"

    @classmethod
    def of(cls, raw) -> "DigestFrequency":
        """**모르는 값은 끔이다.** 알 수 없을 때 더 보내는 쪽으로 기울지 않는다."""
        if not raw:
            return cls.OFF
        try:
            return cls(raw)
        except ValueError:
            return cls.OFF


@dataclass(frozen=True)
class NotificationPreferences:
    """한 사람의 알림 설정.

    **전달 상태(마지막으로 언제 보냈는지)를 여기 담지 않는다.** 그것은 서버가
    쓰는 값이고 사용자가 고칠 수 있는 값이 아니다 — 같은 문서에 두면 설정을
    저장하는 요청이 그 값을 덮어쓸 수 있다.
    """

    user_id: str
    #: 내 상품이 팔렸을 때. **기본 켜짐** — 기존 동작을 그대로 유지한다.
    sales_enabled: bool = True
    #: 새 거울 모아 보기. **기본 꺼짐** — opt-in이다.
    digest_frequency: DigestFrequency = DigestFrequency.OFF
    #: 다시 둘러보라는 소식. **기본 꺼짐** — opt-in이다.
    recommendation_enabled: bool = False
    updated_at: datetime = field(default_factory=utcnow)
    schema_version: int = SCHEMA_VERSION

    @staticmethod
    def default(user_id: str) -> "NotificationPreferences":
        """문서가 없는 사람의 설정. **읽기만 하고 만들지 않는다.**"""
        return NotificationPreferences(user_id=user_id)


class PreferenceStore(Protocol):
    def preferences(self, user_id: str) -> NotificationPreferences:
        """없으면 기본값. **문서를 만들지 않는다** — 읽기가 쓰기를 일으키지 않는다."""

    def save(self, preferences: NotificationPreferences) -> NotificationPreferences: ...

    def delete(self, user_id: str) -> None:
        """계정 삭제용."""


class InMemoryPreferenceStore:
    def __init__(self) -> None:
        self.saved: dict[str, NotificationPreferences] = {}

    def preferences(self, user_id: str) -> NotificationPreferences:
        return self.saved.get(user_id) or NotificationPreferences.default(user_id)

    def save(self, preferences: NotificationPreferences) -> NotificationPreferences:
        stored = NotificationPreferences(
            user_id=preferences.user_id,
            sales_enabled=preferences.sales_enabled,
            digest_frequency=preferences.digest_frequency,
            recommendation_enabled=preferences.recommendation_enabled,
            updated_at=utcnow(),
        )
        self.saved[preferences.user_id] = stored
        return stored

    def delete(self, user_id: str) -> None:
        self.saved.pop(user_id, None)


logger = logging.getLogger(__name__)


class NotificationPreferenceService:
    def __init__(self, store: PreferenceStore) -> None:
        self._store = store

    def preferences(self, user_id: str) -> NotificationPreferences:
        return self._store.preferences(user_id)

    def update(
        self,
        user_id: str,
        *,
        sales_enabled: bool | None = None,
        digest_frequency: str | None = None,
        recommendation_enabled: bool | None = None,
    ) -> NotificationPreferences:
        """보낸 값만 바꾼다. **보내지 않은 값은 건드리지 않는다.**

        화면이 토글 하나를 바꿀 때 나머지를 함께 보내지 않아도 되고, 그래서
        오래된 화면이 새 설정을 실수로 되돌리지 않는다.
        """
        current = self._store.preferences(user_id)
        updated = NotificationPreferences(
            user_id=user_id,
            sales_enabled=current.sales_enabled if sales_enabled is None else sales_enabled,
            digest_frequency=(
                current.digest_frequency
                if digest_frequency is None
                else DigestFrequency.of(digest_frequency)
            ),
            recommendation_enabled=(
                current.recommendation_enabled
                if recommendation_enabled is None
                else recommendation_enabled
            ),
        )
        saved = self._store.save(updated)
        logger.info(
            "notification_preferences_updated sales=%s digest=%s recommendation=%s",
            saved.sales_enabled, saved.digest_frequency.value, saved.recommendation_enabled,
        )
        return saved

    def delete(self, user_id: str) -> None:
        self._store.delete(user_id)
