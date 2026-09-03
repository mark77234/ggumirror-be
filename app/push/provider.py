"""APNs 전송 (Phase F).

**FCM을 쓰지 않는다.** Firebase project를 새로 만들 이유가 없고, 우리는 iOS 하나만
보낸다 — Cloud Run에서 APNs HTTP/2로 바로 보낸다.

private key는 **Secret Manager에만** 있다. client · repo · Firestore · 로그
어디에도 없다. 이 파일은 key를 서명에만 쓰고 값을 로그로 내보내지 않는다.

전송은 `APNsTransport`라는 이음매 하나 뒤에 있다. 그래서 **자동 test가 실제
Apple로 나가지 않는다** — JWT · topic · host · payload · 응답 해석을 전부
fake transport로 확인한다.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Protocol

import jwt

from app.push.models import (
    PushDevice,
    PushEnvironment,
    PushMessage,
    PushOutcome,
    token_fingerprint,
)

logger = logging.getLogger(__name__)

#: APNs provider token은 최대 1시간이다. 조금 일찍 새로 만든다 —
#: 만료 직전 token으로 보내면 403 `ExpiredProviderToken`이 난다.
TOKEN_LIFETIME = 45 * 60

#: 이 응답들은 **이 token이 앞으로도 안 된다**는 뜻이다.
#: 그 밖의 실패(5xx · timeout · 429)는 일시적이고, 기기 등록을 지우지 않는다.
TERMINAL_REASONS = frozenset({"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"})


class PushProvider(Protocol):
    """보낼 수 있는 것. test는 여기에 fake를 끼운다."""

    @property
    def is_available(self) -> bool:
        """자격 증명이 설정돼 있는가. **없다고 앱이 죽지 않는다** —
        판매는 되고 알림만 안 간다."""

    def send(self, device: PushDevice, message: PushMessage) -> PushOutcome: ...


class APNsTransport(Protocol):
    """HTTP/2 한 번. **이 이음매가 있어서 test가 network에 나가지 않는다.**"""

    def post(
        self, host: str, path: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, bytes]: ...


class NullPushProvider:
    """자격 증명이 없을 때. **조용히 성공했다고 하지 않는다.**

    `is_available`이 False이므로 service가 아예 부르지 않고, 실수로 불러도
    "보내지 못했다"고 정확히 답한다 — 보냈다고 거짓말하면 왜 알림이 안 오는지
    아무도 찾을 수 없다.
    """

    @property
    def is_available(self) -> bool:
        return False

    def send(self, device: PushDevice, message: PushMessage) -> PushOutcome:
        return PushOutcome(device_id=device.id, delivered=False, terminal=False)


class APNsPushProvider:
    """실제 APNs.

    자격 증명 넷이 모두 있어야 동작한다. 하나라도 없으면 `is_available`이 False다 —
    반쯤 설정된 상태로 보내려다 실패하는 것보다 안 보내는 것이 낫다.
    """

    def __init__(
        self,
        *,
        key_id: str,
        team_id: str,
        private_key: str,
        bundle_id: str,
        transport: APNsTransport | None = None,
    ) -> None:
        self._key_id = key_id.strip()
        self._team_id = team_id.strip()
        # **값을 로그로 내보내지 않는다.** 여기 담아두고 서명에만 쓴다.
        self._private_key = private_key
        self._bundle_id = bundle_id.strip()
        self._transport = transport or HTTPXAPNsTransport()
        self._token: tuple[str, float] | None = None

    @property
    def is_available(self) -> bool:
        return all((self._key_id, self._team_id, self._private_key, self._bundle_id))

    def send(self, device: PushDevice, message: PushMessage) -> PushOutcome:
        if not self.is_available:
            return PushOutcome(device_id=device.id, delivered=False)

        headers = {
            "authorization": f"bearer {self._provider_token()}",
            # topic은 **번들 id다.** 다른 앱의 token으로는 애초에 통과하지 않는다.
            "apns-topic": self._bundle_id,
            "apns-push-type": "alert",
            # 판매 알림은 즉시성이 중요하지 않지만 화면에 뜨는 알림은 10이어야 한다.
            "apns-priority": "10",
            "content-type": "application/json",
        }
        try:
            status, body = self._transport.post(
                device.environment.host,
                f"/3/device/{device.token}",
                headers,
                _payload(message),
            )
        except Exception as error:   # noqa: BLE001 — 어떤 network 실패도 판매를 깨뜨리지 않는다
            logger.warning(
                "apns_send_failed device=%s error=%s",
                token_fingerprint(device.token), type(error).__name__,
            )
            return PushOutcome(device_id=device.id, delivered=False)

        if status == 200:
            logger.info("apns_sent device=%s", token_fingerprint(device.token))
            return PushOutcome(device_id=device.id, delivered=True, status=status)

        reason = _reason(body)
        terminal = status in (400, 410) and reason in TERMINAL_REASONS
        # **token도 payload도 찍지 않는다.** 지문 · status · 사유만 남긴다.
        logger.warning(
            "apns_rejected device=%s status=%d reason=%s terminal=%s",
            token_fingerprint(device.token), status, reason, terminal,
        )
        return PushOutcome(
            device_id=device.id, delivered=False, terminal=terminal, status=status
        )

    def _provider_token(self) -> str:
        """ES256으로 서명한 provider token. **요청마다 새로 만들지 않는다** —
        Apple이 잦은 재발급을 거절한다(429 `TooManyProviderTokenUpdates`)."""
        now = time.time()
        if self._token is not None and now < self._token[1]:
            return self._token[0]
        token = jwt.encode(
            {"iss": self._team_id, "iat": int(now)},
            self._private_key,
            algorithm="ES256",
            headers={"kid": self._key_id},
        )
        self._token = (token, now + TOKEN_LIFETIME)
        return token


def _payload(message: PushMessage) -> bytes:
    return json.dumps(
        {
            "aps": {
                "alert": {"title": message.title, "body": message.body},
                "sound": "default",
            },
            # 앱이 탭을 받아 알림센터로 간다. deep link 체계를 새로 만들지 않는다.
            "kind": message.kind,
        },
        ensure_ascii=False,
    ).encode()


def _reason(body: bytes) -> str:
    try:
        return str(json.loads(body).get("reason") or "")
    except (ValueError, AttributeError):
        return ""


class HTTPXAPNsTransport:
    """실제 HTTP/2. **APNs는 HTTP/1.1을 받지 않는다.**

    client를 만드는 것을 여기까지 미룬다 — import 시점에 만들면 자격 증명이 없는
    환경(그리고 test)에서도 연결이 준비된다.
    """

    def __init__(self, timeout: float = 10.0) -> None:
        self._timeout = timeout
        self._client = None

    def post(
        self, host: str, path: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, bytes]:
        if self._client is None:
            import httpx

            self._client = httpx.Client(http2=True, timeout=self._timeout)
        response = self._client.post(
            f"https://{host}{path}", headers=headers, content=body
        )
        return response.status_code, response.content


def build_push_provider(
    *, key_id: str, team_id: str, private_key: str, bundle_id: str
) -> PushProvider:
    """자격 증명이 다 있으면 진짜, 아니면 아무것도 보내지 않는 것.

    **여기서 예외를 던지지 않는다** — 알림 자격 증명이 없다고 앱이 뜨지 않으면
    상점도 로그인도 함께 죽는다. 알림만 안 가면 된다.
    """
    provider = APNsPushProvider(
        key_id=key_id, team_id=team_id, private_key=private_key, bundle_id=bundle_id
    )
    return provider if provider.is_available else NullPushProvider()
