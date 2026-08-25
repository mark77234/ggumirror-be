"""Firestore 구현.

**raw token이 문서 자리에 오지 않는다** — 자리는 hash다(`push_device_id`).
"""

from __future__ import annotations

import logging

from google.api_core import exceptions as gcp_exceptions
from google.cloud import firestore

from app.auth.store import StoreUnavailable
from app.push.models import PushDevice, PushEnvironment, push_device_id
from app.push.store import PUSH_DEVICES
from app.shards.models import utcnow

logger = logging.getLogger(__name__)


class FirestorePushStore:
    def __init__(self, client: firestore.Client) -> None:
        self._db = client

    def register(self, device: PushDevice) -> PushDevice:
        """`set`이다 — **재등록이 곧 주인 교체다.**

        읽고-없으면-만들기로 하면 그 사이에 다른 계정이 끼어들 수 있고, 그때
        이전 주인의 문서가 남아 **엉뚱한 사람에게 판매 알림이 간다.**
        """
        reference = self._db.collection(PUSH_DEVICES).document(device.id)

        @firestore.transactional
        def run(transaction) -> PushDevice:
            found = reference.get(transaction=transaction)
            created = (found.to_dict() or {}).get("createdAt") if found.exists else None
            saved = PushDevice(
                id=device.id,
                user_id=device.user_id,
                token=device.token,
                environment=device.environment,
                platform=device.platform,
                enabled=True,
                created_at=created or device.created_at,
                updated_at=utcnow(),
            )
            transaction.set(reference, _document(saved))
            return saved

        try:
            return run(self._db.transaction())
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("push_device_register", error) from error

    def unregister(self, token: str, user_id: str) -> bool:
        reference = self._db.collection(PUSH_DEVICES).document(push_device_id(token))

        @firestore.transactional
        def run(transaction) -> bool:
            found = reference.get(transaction=transaction)
            if not found.exists or (found.to_dict() or {}).get("userId") != user_id:
                # 남의 등록을 지울 수 없다. 없는 것과 구분해 알려주지도 않는다.
                return False
            transaction.delete(reference)
            return True

        try:
            return run(self._db.transaction())
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("push_device_unregister", error) from error

    def devices(self, user_id: str) -> list[PushDevice]:
        try:
            found = (
                self._db.collection(PUSH_DEVICES)
                .where(filter=firestore.FieldFilter("userId", "==", user_id))
                .stream()
            )
            devices = [_device_from(x.id, x.to_dict() or {}) for x in found]
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("push_device_list", error) from error
        # 꺼진 기기는 여기서 뺀다 — query에 조건을 하나 더 걸면 composite index가 필요해진다.
        return sorted((x for x in devices if x.enabled), key=lambda x: x.id)

    def disable(self, device_id: str) -> None:
        try:
            self._db.collection(PUSH_DEVICES).document(device_id).update(
                {"enabled": False, "updatedAt": utcnow()}
            )
        except gcp_exceptions.NotFound:
            # 이미 없다. 지우려던 것이 없는 것은 실패가 아니다.
            return
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("push_device_disable", error) from error

    def _unavailable(self, operation: str, error: Exception) -> StoreUnavailable:
        logger.warning("firestore_failed operation=%s error=%s", operation, type(error).__name__)
        return StoreUnavailable(operation)


def _document(device: PushDevice) -> dict:
    return {
        "userId": device.user_id,
        # token 자체는 보내려면 있어야 한다. **문서 자리에는 오지 않는다.**
        "token": device.token,
        "environment": device.environment.value,
        "platform": device.platform,
        "enabled": device.enabled,
        "createdAt": device.created_at,
        "updatedAt": device.updated_at,
        "schemaVersion": device.schema_version,
    }


def _device_from(device_id: str, data: dict) -> PushDevice:
    return PushDevice(
        id=device_id,
        user_id=str(data.get("userId") or ""),
        token=str(data.get("token") or ""),
        environment=PushEnvironment(data.get("environment") or PushEnvironment.PRODUCTION.value),
        platform=str(data.get("platform") or "ios"),
        # 옛 문서에 값이 없으면 켜진 것으로 읽는다.
        enabled=bool(data.get("enabled", True)),
        created_at=data.get("createdAt") or utcnow(),
        updated_at=data.get("updatedAt") or utcnow(),
        schema_version=int(data.get("schemaVersion") or 1),
    )
