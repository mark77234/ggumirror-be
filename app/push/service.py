"""Push 전송 service (Phase F).

**최선을 다하지만 아무것도 되돌리지 않는다.** 이 파일의 어떤 실패도 구매 ·
지갑 · 원장 · 소유권 · 알림 기록을 바꾸지 않는다 — 그러려면 애초에 그것들을
만질 방법이 없어야 하고, 실제로 여기 들어오는 것은 기기 저장소와 provider뿐이다.
"""

from __future__ import annotations

import logging

from app.auth.store import StoreUnavailable
from app.notifications.models import NotificationEvent
from app.push.models import (
    PushDevice,
    PushEnvironment,
    PushMessage,
    checked_token,
    push_device_id,
)
from app.push.provider import PushProvider
from app.push.store import PushStore

logger = logging.getLogger(__name__)


class PushService:
    def __init__(self, store: PushStore, provider: PushProvider) -> None:
        self._store = store
        self._provider = provider

    @property
    def is_available(self) -> bool:
        return self._provider.is_available

    def register(self, user_id: str, *, token: str, environment: str) -> PushDevice:
        """기기를 등록한다. **환경을 client 문자열로 믿지 않는다** —
        아는 값이 아니면 거절한다(모르는 값을 production으로 넘겨짚지 않는다)."""
        checked = checked_token(token)
        return self._store.register(
            PushDevice(
                id=push_device_id(checked),
                user_id=user_id,
                token=checked,
                environment=PushEnvironment(environment),
            )
        )

    def unregister(self, user_id: str, token: str) -> bool:
        return self._store.unregister(checked_token(token), user_id)

    def notify_sale(self, event: NotificationEvent) -> int:
        """판매 알림을 판매자의 모든 기기로 보낸다. **transaction 밖이다.**

        한 기기가 실패해도 나머지를 보낸다 — 기기 하나가 나머지를 막으면
        아이폰을 바꾼 사람이 알림을 아예 못 받는다.

        어떤 실패도 위로 던지지 않는다. 부르는 쪽은 이미 commit이 끝났고,
        여기서 예외가 나가면 구매 응답이 실패로 보인다.
        """
        if not self._provider.is_available:
            return 0
        try:
            devices = self._store.devices(event.user_id)
        except StoreUnavailable:
            logger.warning("push_devices_unavailable notification=%s", event.id[:12])
            return 0

        message = sale_message(event)
        sent = 0
        for device in devices:
            try:
                outcome = self._provider.send(device, message)
            except Exception:   # noqa: BLE001 — provider가 무엇을 던지든 판매는 끝났다
                logger.warning("push_send_failed notification=%s", event.id[:12])
                continue
            if outcome.delivered:
                sent += 1
            elif outcome.terminal:
                # **끝난 token만 끈다.** 5xx · timeout에는 손대지 않는다.
                try:
                    self._store.disable(device.id)
                except StoreUnavailable:
                    pass
        logger.info(
            "push_sale_sent notification=%s devices=%d sent=%d",
            event.id[:12], len(devices), sent,
        )
        return sent


def sale_message(event: NotificationEvent) -> PushMessage:
    """잠금화면에 뜰 말. **구매자가 누구인지 담지 않는다.**

    금액은 원장이 확정한 값의 사본이다 — listing 가격을 다시 읽어 쓰지 않는다.
    무료 상품이면 조각 이야기를 하지 않는다(0조각을 받았다는 말은 이상하다).
    """
    kind = "거울" if event.content_type == "mirror" else "스티커"
    body = (
        f"{event.shard_amount}조각을 받았어요."
        if event.shard_amount > 0
        else "누군가 받아 갔어요."
    )
    return PushMessage(
        title=f"내 {kind} '{event.title_snapshot}'이 판매됐어요!", body=body
    )
