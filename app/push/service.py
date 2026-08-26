"""Push 전송 service (Phase F).

**최선을 다하지만 아무것도 되돌리지 않는다.** 이 파일의 어떤 실패도 구매 ·
지갑 · 원장 · 소유권 · 알림 기록을 바꾸지 않는다 — 그러려면 애초에 그것들을
만질 방법이 없어야 하고, 실제로 여기 들어오는 것은 기기 저장소와 provider뿐이다.
"""

from __future__ import annotations

import logging

from app.auth.store import StoreUnavailable
from app.notifications.models import NotificationEvent, NotificationType
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
    def __init__(
        self, store: PushStore, provider: PushProvider, preferences=None
    ) -> None:
        self._store = store
        self._provider = provider
        # 사람의 설정. 없으면(구성 전) 예전처럼 전부 보낸다 — 기존 동작이 기본이다.
        self._preferences = preferences

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

    def registered_user_ids(self) -> list[str]:
        """기기를 등록한 사람. **보낼 곳이 있는 사람만** 후보다."""
        try:
            return self._store.registered_user_ids()
        except Exception:   # noqa: BLE001
            logger.warning("push_registered_users_unavailable")
            return []

    def notify(self, event: NotificationEvent) -> int:
        """알림 하나를 그 사람의 모든 기기로. **종류에 맞는 문구를 고른다.**

        판매 알림은 예전 경로(`notify_sale`)가 그대로 부른다.
        """
        return self._send(event, message_for(event))

    def notify_sale(self, event: NotificationEvent) -> int:
        """판매 알림을 판매자의 모든 기기로 보낸다. **transaction 밖이다.**

        한 기기가 실패해도 나머지를 보낸다 — 기기 하나가 나머지를 막으면
        아이폰을 바꾼 사람이 알림을 아예 못 받는다.

        어떤 실패도 위로 던지지 않는다. 부르는 쪽은 이미 commit이 끝났고,
        여기서 예외가 나가면 구매 응답이 실패로 보인다.
        """
        # **판매 알림을 끈 사람에게는 보내지 않는다.**
        #
        # 기록은 이미 남았다(구매 transaction 안에서). 여기서 막는 것은 전달뿐이고,
        # 구매 · 소유권 · 판매자 지급은 이 판단과 아무 상관이 없다.
        if not self._sales_allowed(event.user_id):
            logger.info("push_sale_skipped_by_preference")
            return 0
        return self._send(event, sale_message(event))

    def _sales_allowed(self, user_id: str) -> bool:
        if self._preferences is None:
            return True
        try:
            return self._preferences.preferences(user_id).sales_enabled
        except Exception:   # noqa: BLE001 — 설정을 못 읽었다고 판매 알림을 잃지 않는다
            logger.warning("push_preferences_unavailable")
            return True

    def _send(self, event: NotificationEvent, message: PushMessage) -> int:
        if not self._provider.is_available:
            return 0
        try:
            devices = self._store.devices(event.user_id)
        except StoreUnavailable:
            logger.warning("push_devices_unavailable notification=%s", event.id[:12])
            return 0

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
            "push_sent type=%s notification=%s devices=%d sent=%d",
            event.type.value, event.id[:12], len(devices), sent,
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


def message_for(event: NotificationEvent) -> PushMessage:
    """종류에 맞는 문구. **판매만 상품 이름으로 문장을 만든다.**

    모아 보기와 추천 소식은 상품 하나에 매이지 않으므로 서버가 적어 둔
    `headline` / `body`를 그대로 쓴다.
    """
    if event.type is NotificationType.MARKETPLACE_SALE:
        return sale_message(event)
    return PushMessage(
        title=event.headline or "꾸미러",
        body=event.body or "새로운 소식이 있어요.",
        kind=event.type.value,
    )
