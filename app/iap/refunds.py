"""Apple 환불 반영 (B-6F-B).

**금액의 authority는 원본 구매 claim이다.** 알림이 말한 값도, 지금의 catalog 값도
쓰지 않는다 — catalog는 나중에 바뀔 수 있고, 그러면 예전 구매를 잘못된 금액으로
되돌리게 된다. 우리가 지급할 때 남긴 `ggumirror_iap_transactions/{hash}`의
`amount`가 그 결제로 실제 나간 조각이고, 되돌릴 수 있는 최대치다.

되돌리는 양(`requested`)과 실제로 뺀 양(`recovered`)은 **다른 값**이다.
잔액이 모자라면 있는 만큼만 빼고 나머지는 기록으로만 남긴다 —
**빚으로 만들지 않는다.** 나중에 번 조각에서 자동 상계하지 않는다.
"""

from __future__ import annotations

import logging

from app.iap.models import (
    FAMILY_REVOKE,
    REFUND_FULL,
    REFUND_PRORATED,
    SCHEMA_VERSION,
    InvalidTransaction,
    RefundMismatch,
    VerifiedNotification,
    VerifiedTransaction,
    account_token_user_id,
    refund_record_id,
    transaction_claim_id,
    transaction_log_id,
)
from app.iap.service import TRANSACTIONS
from app.shards.models import DocumentKey, PurchaseClaimMissing, RefundPlan
from app.shards.service import ShardLedgerService

logger = logging.getLogger(__name__)

# 환불 business record. **generic notification history가 아니다** —
# 원본 구매 transaction 하나당 한 줄이고, payload를 저장하지 않는다.
REFUNDS = "ggumirror_iap_refunds"


# Apple `revocationPercentage`의 단위. **100% = 100000 milliunits**이고
# 0..100 퍼센트가 아니다 — `0.015%`는 `15`로 온다.
#
# ⚠️ 이 상수가 있는 이유: 처음에 100으로 나눠 실제 환불의 **1/1000만** 회수하는
# 버그를 냈다. Apple 공식 문서와 library field 주석이 milliunits라고 말한다.
MILLIUNITS_PER_UNIT = 100_000


def requested_amount(
    original: int, revocation_type: str, percentage_milliunits: int | None
) -> int:
    """되돌릴 양. **원본 지급량을 넘지 않는다.**

    `percentage_milliunits`는 Apple이 서명해 보낸 raw 값이다(**milliunits**).
    prorated는 `floor(original * p / 100000)`이되, `p > 0`인데 결과가 0이면 **최소 1**이다 —
    Apple이 일부를 돌려줬는데 우리는 아무것도 회수하지 않는 상태를 만들지 않는다
    (10조각 × 0.015%가 정확히 그 경우다).

    `REFUND_FULL`은 percentage를 **금액 authority로 쓰지 않는다** — 값이 있든 없든
    원본 지급량 전체다.
    """
    if revocation_type == REFUND_FULL:
        return original

    p = percentage_milliunits
    if p is None or p <= 0 or p > MILLIUNITS_PER_UNIT:
        # 여기 오면 payload가 우리가 아는 어떤 모양도 아니다. **추측해서 빼지 않는다.**
        raise InvalidTransaction("revocationPercentage is not usable")
    return min(original, max(1, original * p // MILLIUNITS_PER_UNIT))


class IAPRefundService:
    def __init__(self, shards: ShardLedgerService) -> None:
        self._shards = shards

    def handle(self, notification: VerifiedNotification) -> None:
        """검증된 `REFUND` 알림 하나를 반영한다.

        **조각을 되돌릴 수 없는 경우에도 예외를 올리지 않는다** — 재시도해도 답이
        같은 상황(원본 기록 없음 · 가족 회수 · percentage 없음)은 *할 일이 없는 것*이지
        실패가 아니다. 진단만 남기고 조용히 끝낸다(호출부가 200으로 답한다).

        어긋난 것(product · environment · 주인)은 다르다 — 그건 올린다.
        """
        transaction = notification.transaction
        if transaction is None:
            # 되돌릴 대상을 알 수 없다. 서명은 맞지만 안쪽 transaction이 없다.
            logger.warning("app_store_refund_skipped reason=missing_transaction")
            return

        short = transaction_log_id(transaction.transaction_id)
        revocation = transaction.revocation_type

        if revocation == FAMILY_REVOKE:
            # 가족 공유 회수다. consumable 조각에서는 되돌릴 대상이 다르므로
            # **일반 환불로 매핑하지 않는다.** 정책이 정해지면 그때 붙인다.
            logger.warning("app_store_refund_skipped reason=family_revoke transaction=%s", short)
            return

        if revocation == REFUND_PRORATED and transaction.revocation_percentage_milliunits is None:
            # 얼마를 되돌려야 하는지 Apple이 알려주지 않았다. **추측하지 않는다.**
            logger.warning("app_store_refund_skipped reason=missing_percentage transaction=%s", short)
            return

        if revocation not in {REFUND_FULL, REFUND_PRORATED}:
            # 모르는 회수 종류. 값이 스스로 드러나게 남기고 조각은 건드리지 않는다.
            logger.warning(
                "app_store_refund_skipped reason=unknown_revocation_type "
                "observed_revocation_type=%s transaction=%s",
                revocation, short,
            )
            return

        user_id = account_token_user_id(transaction.app_account_token)
        if user_id is None:
            # 주인을 알 수 없는 환불이다. 아무 지갑에서도 빼지 않는다.
            logger.warning("app_store_refund_skipped reason=missing_account_token transaction=%s", short)
            return

        self._recover(notification, transaction, user_id, short)

    # MARK: - 회수

    def _recover(
        self,
        notification: VerifiedNotification,
        transaction: VerifiedTransaction,
        user_id: str,
        short: str,
    ) -> None:
        claim_key = transaction_claim_id(transaction.transaction_id)

        try:
            result = self._shards.refund_iap(
                user_id,
                external_event_id=transaction.transaction_id,
                purchase=DocumentKey(collection=TRANSACTIONS, key=claim_key),
                record=DocumentKey(collection=REFUNDS, key=refund_record_id(transaction.transaction_id)),
                # payload · raw id는 남기지 않는다. 남는 것은 business state뿐이다.
                document={
                    "productId": transaction.product_id,
                    "environment": transaction.environment,
                    "purchaseTransactionClaimId": claim_key,
                    "revocationType": transaction.revocation_type,
                    # Apple이 보낸 **raw milliunit integer**를 그대로 남긴다.
                    # 50%는 `50000`이다 — 50으로 정규화하면 원본 의미를 잃는다.
                    "revocationPercentage": transaction.revocation_percentage_milliunits,
                    "schemaVersion": SCHEMA_VERSION,
                },
                plan=self._plan(transaction, user_id),
            )
        except PurchaseClaimMissing:
            # 우리가 조각을 준 적 없는 결제다. 되돌릴 것이 없고, 다시 와도 답이 같다.
            logger.warning("app_store_refund_skipped reason=unknown_purchase transaction=%s", short)
            return

        logger.info(
            "app_store_refund environment=%s transaction=%s requested=%d recovered=%d "
            "unrecovered=%d applied=%s",
            notification.environment, short, result.requested, result.recovered,
            result.unrecovered, result.applied,
        )

    def _plan(self, transaction: VerifiedTransaction, user_id: str):
        """원본 구매 claim을 보고 되돌릴 양을 정한다. **transaction 안에서 불린다.**

        여기서 예외가 나가면 아무것도 기록되지 않는다 — 대조 실패가 곧 mutation 0이다.
        """

        def plan(claim: dict) -> RefundPlan:
            original = int(claim.get("amount") or 0)

            if original <= 0:
                logger.warning("app_store_refund_rejected reason=empty_original_amount")
                raise RefundMismatch("original purchase amount is not usable")

            if claim.get("productId") != transaction.product_id:
                # 다른 상품의 환불을 이 결제에 적용하지 않는다.
                logger.warning("app_store_refund_rejected reason=product_mismatch")
                raise RefundMismatch("product does not match the original purchase")

            if claim.get("environment") != transaction.environment:
                # Sandbox 환불로 Production 지급을 되돌리는 경로를 만들지 않는다.
                logger.warning("app_store_refund_rejected reason=environment_mismatch")
                raise RefundMismatch("environment does not match the original purchase")

            if claim.get("userId") != user_id:
                # **문자열이 정확히 같아야 한다.** 지갑 문서 ID가 문자열이라,
                # 표기만 다른 값을 통과시키면 엉뚱한 지갑에서 빼게 된다.
                logger.warning("app_store_refund_rejected reason=owner_mismatch")
                raise RefundMismatch("appAccountToken does not own the original purchase")

            return RefundPlan(
                requested=requested_amount(
                    original,
                    transaction.revocation_type or "",
                    transaction.revocation_percentage_milliunits,
                ),
                # 원본 지급량을 record에 함께 남긴다 — requested가 어디서 나왔는지
                # 나중에 claim을 다시 읽지 않고도 읽을 수 있다.
                fields={"originalAmount": original},
            )

        return plan
