"""App Store Server Notifications V2 endpoint.

**Apple server-to-server다.** Bearer session이 없고, 인증은 오직
**Apple이 서명한 payload**다 — AdMob SSV(B-5)에서 Google 서명이 곧 인증이던 것과 같다.

body는 `signedPayload` 하나뿐이고 다른 authority field를 받는 자리가 없다.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import app_store_notifications
from app.auth.store import StoreUnavailable
from app.iap.models import (
    AccountTokenMismatch,
    EnvironmentNotAllowed,
    IAPUnavailable,
    InvalidTransaction,
    NotificationNotHandled,
    RefundMismatch,
)
from app.iap.notifications import AppStoreNotificationService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/app-store", tags=["appstore"])

INVALID = "notification could not be verified"
DEFERRED = "notification is not handled yet"
UNAVAILABLE = "notification handling is unavailable"


class NotificationRequest(BaseModel):
    """Apple이 보내는 것은 서명된 payload 하나뿐이다."""

    model_config = {"extra": "forbid"}

    signed_payload: str = Field(alias="signedPayload", min_length=1)


@router.post("/notifications/v2", status_code=status.HTTP_200_OK)
def receive_notification(
    request: NotificationRequest,
    notifications: Annotated[AppStoreNotificationService, Depends(app_store_notifications)],
) -> dict[str, str]:
    """검증하고 분류한다. 조각을 움직이는 알림은 **`REFUND` 하나뿐**이다(B-6F-B).

    응답 규칙은 B-5 SSV와 같다 — **재시도로 결과가 달라지는 것만 5xx**다:

    | 상황 | status |
    |---|---|
    | 검증됨 + 우리가 할 일 없음 | 200 |
    | **REFUND 처리 완료 · 중복 · 원본 기록 없음 · 가족 회수 · percentage 없음** | **200** |
    | 서명/형식/bundle/environment 오류 · **원본 구매와 불일치** | 400 (재시도해도 같다) |
    | 검증기 미설정 · 인증서 조회 실패 · **Firestore 장애** | 503 |
    | **REFUND_REVERSED · 모르는 타입** | **503 — Apple이 다시 보내게 둔다** |

    "되돌릴 것이 없다"와 "되돌리지 못했다"는 다르다. 앞의 것은 재시도해도 답이 같으므로
    200이고, 뒤의 것만 Apple의 재전송을 받는다.
    """
    try:
        outcome = notifications.handle(request.signed_payload)
    except StoreUnavailable as error:
        # 일시적 장애다. **다시 보내면 될 수도 있다** — 삼키지 않는다.
        logger.warning("app_store_notification_store_unavailable")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from error
    except NotificationNotHandled as error:
        # **200으로 삼키지 않는다.** 여기서 소비하면 환불 알림이 영영 사라진다.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, DEFERRED) from error
    except IAPUnavailable as error:
        logger.error("app_store_notification_unavailable")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from error
    except (InvalidTransaction, EnvironmentNotAllowed, AccountTokenMismatch, RefundMismatch) as error:
        # 영구 실패다. 재시도해도 같으므로 400으로 끝낸다.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, INVALID) from error

    return {"outcome": outcome.value}
