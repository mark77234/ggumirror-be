"""보낸 기록 (I-12 · I-13 dedup).

**scheduler는 같은 일을 두 번 부를 수 있다.** 재시도 · 겹친 실행 · 손으로 한 번 더 —
전부 정상이다. 그래서 "언제 보냈는지 읽고 비교"로는 부족하다. 두 실행이 같은 순간에
읽으면 둘 다 "아직 안 보냈다"를 보고 둘 다 보낸다.

자리 하나를 **선점**하는 방식으로 막는다. 열쇠가 곧 그 발송이고, 이미 있으면
진 쪽은 조용히 물러난다. 조각 원장 · 소유권 · IAP claim과 같은 규칙이다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from app.shards.models import utcnow

DIGEST_DELIVERIES = "ggumirror_notification_deliveries"


def delivery_id(user_id: str, kind: str, window: str) -> str:
    """이 사람에게 이 종류를 이 기간에 보냈는가.

    raw user id를 문서 이름에 노출하지 않는다 — 길이 접두사 canonical encoding은
    원장 · 소유권과 같은 규칙이다.
    """
    canonical = "|".join(
        f"{len(part.encode())}:{part}"
        for part in ("notification_delivery", user_id, kind, window)
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class DeliveryRecord:
    id: str
    user_id: str
    kind: str
    window: str
    created_at: datetime = field(default_factory=utcnow)


class DeliveryStore(Protocol):
    def claim(self, record: DeliveryRecord) -> bool:
        """이번 실행이 **처음인가.**

        처음이면 자리를 잡고 `True`. 이미 있으면 아무것도 쓰지 않고 `False`다 —
        그때는 보내지 않는다.
        """

    def delete_for_user(self, user_id: str) -> int:
        """계정 삭제용."""


class InMemoryDeliveryStore:
    def __init__(self) -> None:
        self.records: dict[str, DeliveryRecord] = {}

    def claim(self, record: DeliveryRecord) -> bool:
        if record.id in self.records:
            return False
        self.records[record.id] = record
        return True

    def delete_for_user(self, user_id: str) -> int:
        gone = [k for k, v in self.records.items() if v.user_id == user_id]
        for key in gone:
            del self.records[key]
        return len(gone)
