"""GET /users/me · GET /users/me/shards

server auth가 실제로 연결됐는지 확인하는 최소 endpoint와, 내 조각 지갑 조회.

**조각을 바꾸는 endpoint는 없다.** `POST /shards/credit` 같은 범용 통로를 만들면
client가 `amount`와 `reason`을 정하는 구조가 된다. 잔액을 바꾸는 것은 서버 내부의
`ShardLedgerService`뿐이고, 각 기능(출석 · 광고 SSV · IAP · 상점)이 자기 사건을
검증한 뒤에만 부른다.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, shard_service
from app.shards.service import ShardLedgerService

router = APIRouter(prefix="/users", tags=["users"])


class ShardWalletPayload(BaseModel):
    """조각 지갑. **userId를 돌려주지 않는다** — 부르는 쪽이 이미 자기 자신이다."""

    balance: int
    lifetime_earned: int = Field(serialization_alias="lifetimeEarned")
    lifetime_spent: int = Field(serialization_alias="lifetimeSpent")

    model_config = {"populate_by_name": True}


@router.get("/me")
def me(user: CurrentUser) -> dict[str, str]:
    """internal user UUID만 돌려준다. Apple subject는 나가지 않는다."""
    return {"id": user.id}


@router.get("/me/shards", response_model=ShardWalletPayload, response_model_by_alias=True)
def my_shards(
    user: CurrentUser,
    shards: Annotated[ShardLedgerService, Depends(shard_service)],
) -> ShardWalletPayload:
    """내 조각 잔액.

    누구인지는 **Bearer session에서만** 온다 — 요청 본문이나 query의 userId를 믿지 않는다.
    지갑이 없으면 0이다. 조회만으로 문서를 만들지 않는다.
    """
    wallet = shards.wallet(user.id)
    return ShardWalletPayload(
        balance=wallet.balance,
        lifetime_earned=wallet.lifetime_earned,
        lifetime_spent=wallet.lifetime_spent,
    )
