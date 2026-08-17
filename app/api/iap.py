"""조각 IAP endpoint.

**통로는 하나뿐이고 body는 서명된 JWS 하나다.** `amount` · `price` · `productId` ·
`userId`를 받는 자리를 만들지 않는다 — 하나라도 열면 client가 조각을 정하는 구조가 된다
(B-3의 generic mutation 금지 규칙).
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, iap_service
from app.iap.models import (
    AccountTokenMismatch,
    EnvironmentNotAllowed,
    IAPUnavailable,
    InvalidTransaction,
    TransactionAlreadyClaimed,
    UnknownProduct,
)
from app.iap.service import IAPService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["iap"])

UNAVAILABLE = "지금은 조각을 충전할 수 없어요. 잠시 뒤 다시 시도해 주세요."
INVALID = "결제를 확인하지 못했어요."
ALREADY_USED = "이 결제는 이미 사용됐어요."


class ShardPurchaseRequest(BaseModel):
    """**서명된 transaction 하나뿐이다.**

    `model_config`에 `extra="forbid"`를 걸어 `amount` 같은 field를 몰래 얹지 못하게 한다.
    """

    model_config = {"extra": "forbid"}

    signed_transaction: str = Field(alias="signedTransaction", min_length=1)


class ShardPurchasePayload(BaseModel):
    """`credited`는 **이번 요청이 지급했는가**다.

    같은 transaction 재전송이면 `false`이고 그때도 `balance`는 정상 현재 잔액이다 —
    실패가 아니다(B-4 `claimed`와 같은 계약).
    """

    credited: bool
    amount: int
    balance: int


@router.post(
    "/me/iap/shards",
    response_model=ShardPurchasePayload,
    response_model_by_alias=True,
)
def credit_shards(
    request: ShardPurchaseRequest,
    user: CurrentUser,
    iap: Annotated[IAPService, Depends(iap_service)],
) -> ShardPurchasePayload:
    """Apple이 서명한 consumable 결제를 조각으로 바꾼다.

    지급 수량은 **서버 catalog**가 정하고, 결제의 주인은 서명된 `appAccountToken`으로 본다.
    """
    try:
        result = iap.credit(user, request.signed_transaction)
    except IAPUnavailable as error:
        # 설정 문제다. client 잘못이 아니므로 재시도 가능한 503으로 준다.
        logger.error("iap_unavailable")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from error
    except TransactionAlreadyClaimed as error:
        # 다른 사용자가 이미 쓴 transaction이다. 존재를 자세히 알려주지 않는다.
        raise HTTPException(status.HTTP_409_CONFLICT, ALREADY_USED) from error
    except (
        InvalidTransaction,
        UnknownProduct,
        EnvironmentNotAllowed,
        AccountTokenMismatch,
    ) as error:
        # 어떤 검사에서 걸렸는지 client에 나눠 알려주지 않는다 —
        # 공격자에게 어느 값을 고치면 되는지 가르쳐 주는 셈이다. 로그에만 분류가 남는다.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, INVALID) from error

    return ShardPurchasePayload(
        credited=result.credited, amount=result.amount, balance=result.balance
    )
