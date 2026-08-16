"""출석 — 하루 한 번 조각 +1.

하루의 기준은 **server 시계의 Asia/Seoul 날짜**다. client가 보낸 날짜 · timezone ·
device 시각은 근거로 쓰지 않는다. 앱을 지웠다 깔아도, 기기 시계를 바꿔도 결과가 같다.

지급은 B-3 원장이 한다 — 여기서 잔액을 직접 만지지 않는다.
`external_event_id`가 KST 날짜라서 같은 날 몇 번을 불러도 원장에는 한 줄만 남는다
(user scope는 `ShardLedgerService`가 붙인다).

**출석 전용 collection을 만들지 않았다.** "오늘 받았나"는 원장에게 묻는다 —
같은 경제 사실의 authority가 두 곳이 되면 언젠가 서로 다른 답을 한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.shards.models import ShardReason, utcnow
from app.shards.service import ShardLedgerService

# 출석 하루 = Asia/Seoul calendar day.
# ponytail: 고정 offset이다. 한국은 1988년 이후 DST가 없어 ZoneInfo와 결과가 같고,
# container에 tzdata가 들어 있는지에 의존하지 않는다. 한국이 DST를 도입하면 ZoneInfo로 바꾼다.
KST = timezone(timedelta(hours=9), "KST")

DAILY_REWARD = 1


def attendance_date(now: datetime | None = None) -> str:
    """server 시각 → KST 날짜(`YYYY-MM-DD`).

    `now`를 받는 이유는 **test가 시간을 고정**하기 위해서다.
    production은 언제나 `utcnow()` — server 시계다.

    UTC 2026-08-13 15:01 → KST 2026-08-14 00:01 → `2026-08-14`.
    """
    moment = now or utcnow()
    if moment.tzinfo is None:
        # naive datetime을 조용히 UTC로 가정하지 않는다. 하루가 통째로 어긋난다.
        raise ValueError("now must be timezone-aware")
    return moment.astimezone(KST).strftime("%Y-%m-%d")


@dataclass(frozen=True)
class Attendance:
    """오늘의 출석 결과. `balance`는 언제나 **서버 원장이 계산한 값**이다."""

    date: str
    claimed: bool
    reward: int
    balance: int


def status(
    shards: ShardLedgerService,
    user_id: str,
    now: datetime | None = None,
) -> tuple[str, bool]:
    """오늘 날짜와 **이미 받았는지**. 지갑을 읽지 않는다 — 잔액은 지갑 endpoint가 답한다."""
    date = attendance_date(now)
    return date, shards.has_event(user_id, ShardReason.DAILY_ATTENDANCE, date)


def claim(
    shards: ShardLedgerService,
    user_id: str,
    now: datetime | None = None,
) -> Attendance:
    """오늘 조각을 받는다. 이미 받았으면 **오류가 아니라 그대로 성공**이다.

    중복 출석을 400으로 만들면 client가 "네트워크 실패 후 재시도"와
    "정말 두 번 눌렀다"를 구분해야 한다. 재시도는 정상 동작이므로 idempotent success로 둔다.

    **`has_event`로 먼저 확인하지 않는다.** 확인과 지급 사이에 다른 요청이 끼어들면
    둘 다 "내가 지급했다"고 답한다. 지급 여부는 **원장 transaction의 결과**(`applied`)만이
    안다 — 10개가 동시에 들어와도 `applied=True`는 정확히 하나다.
    """
    date = attendance_date(now)

    result = shards.credit(
        user_id,
        DAILY_REWARD,
        ShardReason.DAILY_ATTENDANCE,
        external_event_id=date,
    )
    return Attendance(
        date=date,
        claimed=result.applied,
        reward=DAILY_REWARD if result.applied else 0,
        balance=result.wallet.balance,
    )
