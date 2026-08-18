"""App Store Server Notifications V2 (B-6F-A · B-6F-B · B-6F-C).

이 파일은 **검증하고 분류하는 곳**이다. 처리할 수 없는 알림을 **200으로 삼키지 않는다** —
Apple이 다시 보내게 둔다.

**조각을 움직이는 코드는 여전히 여기 없다.** 환불조차 `IAPRefundService`에 맡기고
이 파일은 어느 알림을 그리로 보낼지만 정한다 — 지급/차감 경로가 여기 섞이면
"알림이 곧 authority"가 되고, 그게 한 결제에 두 번 지급되는 길이다.
테스트가 그것을 소스 수준에서 고정한다.
"""

from __future__ import annotations

import logging

from app.iap.models import (
    ACKNOWLEDGED_NOTIFICATIONS,
    DEFERRED_NOTIFICATIONS,
    REFUND_NOTIFICATION,
    REFUND_REVERSED_NOTIFICATION,
    AccountTokenMismatch,
    EnvironmentNotAllowed,
    IAPUnavailable,
    InvalidTransaction,
    NotificationNotHandled,
    NotificationOutcome,
    VerifiedNotification,
    transaction_log_id,
)

logger = logging.getLogger(__name__)


class AppStoreNotificationService:
    def __init__(
        self,
        verifier,
        *,
        bundle_id: str,
        allowed_environments: frozenset[str],
        app_apple_id: int | None,
        refunds=None,
    ) -> None:
        self._verifier = verifier
        # 없으면 `REFUND`를 **deferred로 되돌린다**(fail closed) — 처리기가 없는데
        # 200으로 답하면 그 환불은 영영 사라진다.
        self._refunds = refunds
        self._bundle_id = bundle_id
        self._allowed_environments = allowed_environments
        self._app_apple_id = app_apple_id

    @property
    def is_available(self) -> bool:
        return getattr(self._verifier, "is_configured", False) and bool(self._allowed_environments)

    def handle(self, signed_payload: str) -> NotificationOutcome:
        """서명된 알림 하나를 처리한다. **검증 전에는 어떤 값도 믿지 않는다.**"""
        if not getattr(self._verifier, "is_configured", False):
            raise IAPUnavailable("notification verifier is not configured")
        if not signed_payload or not signed_payload.strip():
            raise InvalidTransaction("signedPayload is empty")

        verify = getattr(self._verifier, "verify_notification", None)
        if verify is None:
            raise IAPUnavailable("verifier cannot decode notifications")

        notification = verify(signed_payload.strip())
        self._check(notification)
        return self._route(notification)

    # MARK: - 검증된 값만 본다

    def _check(self, notification: VerifiedNotification) -> None:
        short = self._short(notification)

        if self._bundle_id and notification.bundle_id != self._bundle_id:
            logger.warning(
                "app_store_notification_rejected reason=bundle_mismatch type=%s",
                notification.notification_type,
            )
            raise InvalidTransaction("bundle id does not match")

        if notification.environment not in self._allowed_environments:
            # Xcode · LocalTesting은 애초에 verifier가 없어 여기까지 오지 못한다.
            logger.warning(
                "app_store_notification_rejected reason=environment_not_allowed "
                "observed_environment=%s type=%s",
                notification.environment, notification.notification_type,
            )
            raise EnvironmentNotAllowed(f"environment {notification.environment} is not allowed")

        # Production 알림은 우리 앱의 것이어야 한다. library도 대조하지만 한 번 더 본다.
        if (
            notification.environment == "Production"
            and self._app_apple_id is not None
            and notification.app_apple_id is not None
            and notification.app_apple_id != self._app_apple_id
        ):
            logger.warning("app_store_notification_rejected reason=app_id_mismatch type=%s",
                           notification.notification_type)
            raise AccountTokenMismatch("appAppleId does not match")

        logger.info(
            "app_store_notification_verified type=%s subtype=%s environment=%s transaction=%s",
            notification.notification_type, notification.subtype or "-",
            notification.environment, short,
        )

    # MARK: - 분류

    def _route(self, notification: VerifiedNotification) -> NotificationOutcome:
        kind = notification.notification_type

        if kind in (REFUND_NOTIFICATION, REFUND_REVERSED_NOTIFICATION):
            if self._refunds is None:
                logger.warning("app_store_notification_deferred type=%s reason=no_refund_handler", kind)
                raise NotificationNotHandled(f"{kind} handling is not configured")
            # 조각을 움직이는 것은 저쪽이다. 여기서는 **어디로 보낼지만** 정한다.
            if kind == REFUND_NOTIFICATION:
                self._refunds.handle(notification)
            else:
                self._refunds.handle_reversal(notification)
            return NotificationOutcome.ACKNOWLEDGED

        if kind in ACKNOWLEDGED_NOTIFICATIONS:
            # **경제를 건드리지 않는다.** 특히 CONSUMPTION_REQUEST는 환불 승인이 아니고,
            # Apple로 소비 정보를 보내지도 않는다(동의 흐름이 없다).
            logger.info(
                "app_store_notification_acknowledged type=%s transaction=%s",
                kind, self._short(notification),
            )
            return NotificationOutcome.ACKNOWLEDGED

        if kind in DEFERRED_NOTIFICATIONS:
            # 아직 구현하지 않았다. **200으로 소비하면 환불이 영영 사라진다.**
            logger.warning(
                "app_store_notification_deferred type=%s reason=not_implemented transaction=%s",
                kind, self._short(notification),
            )
            raise NotificationNotHandled(f"{kind} is not handled yet")

        # 모르는/새 타입. 조각에 영향이 있는지 알 수 없으므로 **삼키지 않는다.**
        logger.warning(
            "app_store_notification_deferred type=%s reason=unknown_type transaction=%s",
            kind, self._short(notification),
        )
        raise NotificationNotHandled(f"{kind} is not recognised")

    @staticmethod
    def _short(notification: VerifiedNotification) -> str:
        """transaction이 없는 알림(TEST 등)에 hash를 만들려고 하지 않는다."""
        transaction = notification.transaction
        return transaction_log_id(transaction.transaction_id) if transaction else "-"
