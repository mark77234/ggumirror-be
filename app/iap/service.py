"""조각 IAP 지급.

**검사 순서가 곧 보안이다.** 지급(원장 쓰기)은 맨 마지막이고, 그 앞의 모든 검사는
Apple이 서명한 값만 근거로 한다.

    JWS 검증 → bundle → type → environment → catalog → appAccountToken → 원장

`appAccountToken`이 **Apple transaction을 우리 사용자에 묶는 유일한 끈**이다.
"지금 로그인한 사람 + transactionId"로 지급하면, 남의 결제 JWS를 가져와
자기 지갑을 채우는 경로가 열린다.
"""

from __future__ import annotations

import logging

from app.auth.models import User
from app.auth.store import AuthStore, StoreUnavailable
from app.iap.models import (
    CONSUMABLE_TYPE,
    AccountTokenMismatch,
    EnvironmentNotAllowed,
    IAPResult,
    IAPUnavailable,
    InvalidTransaction,
    SCHEMA_VERSION,
    TransactionAlreadyClaimed,
    UnknownProduct,
    VerifiedTransaction,
    same_account_token,
    transaction_claim_id,
    transaction_log_id,
)
from app.iap.verifier import TransactionVerifier
from app.shards.models import ClaimOwnedByAnother, ExclusiveClaim, ShardReason
from app.shards.service import ShardLedgerService

logger = logging.getLogger(__name__)

# 전역 claim collection. 같은 Apple transaction이 **누구 이름으로 오든** 이 자리를 겨룬다.
TRANSACTIONS = "ggumirror_iap_transactions"


class IAPService:
    def __init__(
        self,
        verifier: TransactionVerifier,
        shards: ShardLedgerService,
        *,
        bundle_id: str,
        allowed_environments: frozenset[str],
        users: AuthStore | None = None,
    ) -> None:
        self._verifier = verifier
        self._shards = shards
        self._bundle_id = bundle_id
        self._allowed_environments = allowed_environments
        # guest 지갑을 넘겨받은 계정인지 확인할 때만 읽는다(_check_owner의 실패 갈래).
        self._users = users

    @property
    def is_available(self) -> bool:
        """검증기와 허용 환경이 **둘 다** 있어야 켠다.

        환경 목록이 비어 있으면 어떤 결제도 통과할 수 없으므로 꺼진 것과 같다 —
        "켜져 있는데 전부 거절"보다 꺼져 있다고 말하는 것이 정직하다.
        """
        return self._verifier.is_configured and bool(self._allowed_environments)

    def credit(self, user: User, signed_transaction: str) -> IAPResult:
        """서명된 transaction 하나로 조각을 지급한다.

        client는 **JWS 하나만** 보낸다. 수량 · 가격 · productId · userId를 받지 않는다.
        """
        if not self._verifier.is_configured:
            raise IAPUnavailable("transaction verifier is not configured")
        if not signed_transaction or not signed_transaction.strip():
            raise InvalidTransaction("signedTransaction is empty")

        transaction = self._verifier.verify(signed_transaction.strip())
        short = transaction_log_id(transaction.transaction_id)

        self._check_shape(transaction, short)
        amount = self._check_product(transaction, short)
        self._check_owner(transaction, user, short)

        return self._apply(transaction, user, amount, short)

    # MARK: - 검사 (서명된 값만 본다)

    def _check_shape(self, transaction: VerifiedTransaction, short: str) -> None:
        if self._bundle_id and transaction.bundle_id != self._bundle_id:
            # 다른 앱의 결제다. 서명이 유효해도 우리 조각을 주지 않는다.
            logger.warning("iap_rejected reason=bundle_mismatch transaction=%s", short)
            raise InvalidTransaction("bundle id does not match")

        if transaction.transaction_type != CONSUMABLE_TYPE:
            # 구독 · 영구 entitlement를 조각으로 바꾸지 않는다.
            logger.warning("iap_rejected reason=not_consumable transaction=%s", short)
            raise InvalidTransaction("transaction is not a consumable")

        if transaction.environment not in self._allowed_environments:
            # Xcode 로컬 서명은 어떤 설정으로도 여기 들어오지 못한다
            # (`parse_allowed_environments`가 값 자체를 버린다).
            logger.warning(
                "iap_rejected reason=environment_not_allowed observed_environment=%s transaction=%s",
                transaction.environment, short,
            )
            raise EnvironmentNotAllowed(f"environment {transaction.environment} is not allowed")

    def _check_product(self, transaction: VerifiedTransaction, short: str) -> int:
        """수량은 **서버 catalog**가 정한다. client가 말한 값을 쓰지 않는다."""
        amount = transaction.shard_amount
        if amount is None:
            # 모르는 product면 추측해서 지급하지 않는다. 값이 스스로 드러나게 남긴다.
            logger.warning(
                "iap_rejected reason=unknown_product observed_product=%s transaction=%s",
                transaction.product_id, short,
            )
            raise UnknownProduct(f"unknown product {transaction.product_id}")
        return amount

    def _check_owner(self, transaction: VerifiedTransaction, user: User, short: str) -> None:
        """`appAccountToken`이 지금 로그인한 사용자여야 한다.

        없으면 거절한다 — "없으면 현재 사용자로 본다"로 두면 남의 결제 JWS로
        자기 지갑을 채울 수 있고, 그게 정확히 막으려는 것이다.
        """
        if same_account_token(transaction.app_account_token, user.id):
            return

        # 로그인 전에 산 결제가 늦게 도착할 수 있다 — guest로 결제하고, 서버 지급이
        # 실패한 채로 로그인하면 token은 그 guest를 가리킨다. **그 guest 지갑을
        # 넘겨받은 계정이 지금 이 사람일 때만** 받아 준다(서버가 적어 둔 값이 근거다).
        if self._claimed_guest_of(transaction.app_account_token, user):
            logger.info("iap_owner_via_claimed_guest transaction=%s", short)
            return

        logger.warning(
            "iap_rejected reason=account_token_mismatch present=%s transaction=%s",
            transaction.app_account_token is not None, short,
        )
        raise AccountTokenMismatch("appAccountToken does not match the signed-in user")

    def _claimed_guest_of(self, token: str | None, user: User) -> bool:
        if not token or self._users is None:
            return False
        try:
            owner = self._users.user(str(token))
        except StoreUnavailable:
            # 읽지 못한 것을 "주인이 아니다"로 단정하지 않는다 — 원래 오류 그대로 간다.
            return False
        return (
            owner is not None
            and owner.is_guest
            and same_account_token(owner.claimed_by_user_id, user.id)
        )

    # MARK: - 지급

    def _apply(
        self, transaction: VerifiedTransaction, user: User, amount: int, short: str
    ) -> IAPResult:
        claim = ExclusiveClaim(
            collection=TRANSACTIONS,
            key=transaction_claim_id(transaction.transaction_id),
            # 민감하지 않은 최소 metadata만. **raw transaction id를 넣지 않는다.**
            document={
                "productId": transaction.product_id,
                "amount": amount,
                "environment": transaction.environment,
                "schemaVersion": SCHEMA_VERSION,
            },
        )

        try:
            result = self._shards.credit(
                user.id,
                amount,
                ShardReason.IAP_PURCHASE,
                external_event_id=transaction.transaction_id,
                claim=claim,
            )
        except ClaimOwnedByAnother as error:
            # 같은 Apple transaction을 다른 사용자가 이미 썼다. **아무것도 기록되지 않았다.**
            logger.warning("iap_rejected reason=claimed_by_another transaction=%s", short)
            raise TransactionAlreadyClaimed("transaction already credited") from error

        logger.info(
            "iap_credit transaction=%s amount=%d balance=%d applied=%s environment=%s",
            short, amount, result.wallet.balance, result.applied, transaction.environment,
        )
        return IAPResult(
            credited=result.applied, amount=amount, balance=result.wallet.balance
        )
