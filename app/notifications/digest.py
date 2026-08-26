"""새 거울 모아 보기와 다시 둘러보기 (I-12 · I-13).

**무엇이 공개인지 다시 정하지 않는다.** `marketplace.list_published()`가
`_is_public`을 지나므로, 운영자가 내린 것 · 판매자가 지운 것 · 아직 안 올린 것은
애초에 여기까지 오지 않는다. 여기서 상태를 다시 판단하면 규칙이 두 벌이 되고,
그 둘은 반드시 갈라진다.

날짜는 조각 출석과 **같은 달력**을 쓴다(`attendance_date`, KST). 사용자가 하루를
세는 방식이 기능마다 다르면 안 된다.

보낼 것이 없으면 보내지 않는다. "오늘 새 거울 0개"는 알림이 아니라 소음이다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.marketplace.models import ContentType
from app.notifications.delivery import DeliveryRecord, delivery_id
from app.notifications.models import NotificationEvent, NotificationType
from app.notifications.preferences import DigestFrequency
from app.shards.attendance import attendance_date

logger = logging.getLogger(__name__)

#: 다시 둘러보기는 **최대 주 1회**다. 재방문 알림이 잦으면 그건 알림이 아니라 광고다.
RECOMMENDATION_WINDOW_DAYS = 7


@dataclass(frozen=True)
class DigestOutcome:
    """한 번의 job 결과. 숫자만 남긴다 — 누구에게 보냈는지는 로그에 남기지 않는다."""

    considered: int = 0
    sent: int = 0
    skipped_duplicate: int = 0
    skipped_empty: int = 0


class MirrorDigestService:
    def __init__(self, marketplace, notifications, deliveries, preferences, pushes) -> None:
        self._marketplace = marketplace
        self._notifications = notifications
        self._deliveries = deliveries
        self._preferences = preferences
        self._pushes = pushes

    # MARK: - 세기

    def new_mirrors(self, since: datetime, now: datetime | None = None) -> int:
        """`since` 이후 **공개된 거울** 수.

        스티커는 세지 않는다 — "새 거울 소식"이라고 말하고 스티커를 세면 거짓말이다.
        """
        listings = self._marketplace.browse(content_type=ContentType.MIRROR)
        return sum(
            1 for x in listings if x.published_at is not None and x.published_at >= since
        )

    # MARK: - job

    def run_daily(self, user_ids: list[str], now: datetime | None = None) -> DigestOutcome:
        """어제 이후 올라온 거울을 **매일 받겠다고 한 사람에게만**."""
        moment = now or _utcnow()
        window = attendance_date(moment)
        count = self.new_mirrors(since=moment - timedelta(days=1), now=moment)
        return self._deliver(
            user_ids=user_ids,
            frequency=DigestFrequency.DAILY,
            kind="mirror_digest_daily",
            window=window,
            count=count,
            headline="새로운 거울이 올라왔어요 🪞",
            body=f"오늘 새 거울 {count}개를 구경해보세요.",
        )

    def run_weekly(self, user_ids: list[str], now: datetime | None = None) -> DigestOutcome:
        """지난 7일. **매주 받겠다고 한 사람에게만** — 매일 받는 사람은 여기 없다."""
        moment = now or _utcnow()
        window = _iso_week(moment)
        count = self.new_mirrors(since=moment - timedelta(days=7), now=moment)
        return self._deliver(
            user_ids=user_ids,
            frequency=DigestFrequency.WEEKLY,
            kind="mirror_digest_weekly",
            window=window,
            count=count,
            headline="이번 주 새로운 거울이 올라왔어요 🪞",
            body=f"이번 주 새 거울 {count}개를 구경해보세요.",
        )

    def run_recommendation(
        self, user_ids: list[str], now: datetime | None = None
    ) -> DigestOutcome:
        """다시 둘러보라는 소식. **켠 사람에게만, 주 1회.**

        세어 보지 않은 것을 말하지 않는다 — "지금 가장 인기" 같은 문구를 쓰지 않는다.
        """
        moment = now or _utcnow()
        window = _iso_week(moment)
        outcome = DigestOutcome()

        for user_id in user_ids:
            outcome = _counted(outcome, considered=1)
            if not self._preferences.preferences(user_id).recommendation_enabled:
                continue
            if not self._claim(user_id, "recommendation", window):
                outcome = _counted(outcome, skipped_duplicate=1)
                continue

            event = NotificationEvent(
                id=NotificationEvent.new_id(),
                user_id=user_id,
                type=NotificationType.RECOMMENDATION,
                headline="새로운 거울이 기다리고 있어요 🪞",
                body="꾸미러를 다시 둘러보세요.",
            )
            # **알림센터에 남기지 않는다.** 홍보성 알림이 쌓이면 판매 소식이 묻힌다.
            self._push(event)
            outcome = _counted(outcome, sent=1)

        logger.info(
            "recommendation_job considered=%d sent=%d duplicate=%d",
            outcome.considered, outcome.sent, outcome.skipped_duplicate,
        )
        return outcome

    def subscriber_ids(self) -> list[str]:
        """보낼 후보. **기기를 등록한 사람만** — token이 없으면 보낼 곳이 없다.

        전체 사용자를 훑지 않는다. 설정은 그다음에 각자 확인한다.
        """
        return self._pushes.registered_user_ids()

    # MARK: - 내부

    def _deliver(
        self, *, user_ids, frequency, kind, window, count, headline, body
    ) -> DigestOutcome:
        outcome = DigestOutcome()
        for user_id in user_ids:
            outcome = _counted(outcome, considered=1)
            if self._preferences.preferences(user_id).digest_frequency is not frequency:
                continue
            if count <= 0:
                # 보낼 것이 없다. **자리를 잡지도 않는다** — 나중에 올라오면 보낸다.
                outcome = _counted(outcome, skipped_empty=1)
                continue
            if not self._claim(user_id, kind, window):
                outcome = _counted(outcome, skipped_duplicate=1)
                continue

            event = NotificationEvent(
                id=NotificationEvent.new_id(),
                user_id=user_id,
                type=NotificationType.MIRROR_DIGEST,
                headline=headline,
                body=body,
            )
            # 모아 보기는 기록으로 남긴다 — 나중에 열어 봐도 무엇이 있었는지 안다.
            self._notifications.create(event)
            self._push(event)
            outcome = _counted(outcome, sent=1)

        logger.info(
            "%s considered=%d sent=%d duplicate=%d empty=%d",
            kind, outcome.considered, outcome.sent,
            outcome.skipped_duplicate, outcome.skipped_empty,
        )
        return outcome

    def _claim(self, user_id: str, kind: str, window: str) -> bool:
        return self._deliveries.claim(
            DeliveryRecord(
                id=delivery_id(user_id, kind, window),
                user_id=user_id, kind=kind, window=window,
            )
        )

    def _push(self, event: NotificationEvent) -> None:
        """**한 사람의 실패가 나머지를 막지 않는다.**"""
        try:
            self._pushes.notify(event)
        except Exception:   # noqa: BLE001
            logger.warning("digest_push_failed type=%s", event.type.value)


def _counted(outcome: DigestOutcome, **delta: int) -> DigestOutcome:
    return DigestOutcome(
        considered=outcome.considered + delta.get("considered", 0),
        sent=outcome.sent + delta.get("sent", 0),
        skipped_duplicate=outcome.skipped_duplicate + delta.get("skipped_duplicate", 0),
        skipped_empty=outcome.skipped_empty + delta.get("skipped_empty", 0),
    )


def _iso_week(moment: datetime) -> str:
    """KST 기준 ISO 주. 주가 바뀌면 새 자리가 되고, 같은 주면 한 번만 간다."""
    day = attendance_date(moment)
    year, month, date = (int(x) for x in day.split("-"))
    iso = datetime(year, month, date).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _utcnow() -> datetime:
    from app.shards.models import utcnow

    return utcnow()
