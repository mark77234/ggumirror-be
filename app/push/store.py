"""기기 push token 저장소."""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Protocol

from app.push.models import PushDevice, push_device_id
from app.shards.models import utcnow

PUSH_DEVICES = "ggumirror_push_devices"


class PushStore(Protocol):
    def register(self, device: PushDevice) -> PushDevice:
        """등록하거나 **주인을 바꾼다.**

        자리가 token hash 하나이므로, 다른 계정이 같은 기기에서 등록하면 이전
        주인이 남지 않는다 — 그것이 A가 B의 판매 알림을 받지 않는 이유다.
        """

    def unregister(self, token: str, user_id: str) -> bool:
        """내 기기만 지운다. 남의 등록을 지울 수 없다."""

    def devices(self, user_id: str) -> list[PushDevice]:
        """그 사람에게 보낼 수 있는 기기 전부. **꺼진 것은 빼고** 준다."""

    def disable(self, device_id: str) -> None:
        """APNs가 **끝났다**고 한 token을 끈다. 일시적 실패에는 부르지 않는다."""

    def registered_user_ids(self) -> list[str]:
        """기기를 등록한 사람 전부. 정기 발송의 후보를 여기서 얻는다."""


class InMemoryPushStore:
    def __init__(self) -> None:
        self.devices_by_id: dict[str, PushDevice] = {}
        self._lock = threading.RLock()

    def register(self, device: PushDevice) -> PushDevice:
        with self._lock:
            existing = self.devices_by_id.get(device.id)
            saved = replace(
                device,
                # 처음 등록한 시각은 유지한다 — 재등록이 기록을 지우지 않는다.
                created_at=existing.created_at if existing else device.created_at,
                updated_at=utcnow(),
                enabled=True,
            )
            self.devices_by_id[device.id] = saved
            return saved

    def unregister(self, token: str, user_id: str) -> bool:
        with self._lock:
            key = push_device_id(token)
            found = self.devices_by_id.get(key)
            if found is None or found.user_id != user_id:
                return False
            del self.devices_by_id[key]
            return True

    def devices(self, user_id: str) -> list[PushDevice]:
        return sorted(
            (x for x in self.devices_by_id.values() if x.user_id == user_id and x.enabled),
            key=lambda x: x.id,
        )

    def registered_user_ids(self) -> list[str]:
        return sorted({x.user_id for x in self.devices_by_id.values() if x.enabled})

    def disable(self, device_id: str) -> None:
        with self._lock:
            found = self.devices_by_id.get(device_id)
            if found is not None:
                self.devices_by_id[device_id] = replace(found, enabled=False, updated_at=utcnow())
