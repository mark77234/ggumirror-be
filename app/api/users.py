"""GET /users/me · GET /users/me/shards · GET·POST /users/me/attendance

server auth가 실제로 연결됐는지 확인하는 최소 endpoint와, 내 조각 지갑 / 출석.

**범용으로 조각을 바꾸는 endpoint는 없다.** `POST /shards/credit` 같은 통로를 만들면
client가 `amount`와 `reason`을 정하는 구조가 된다. 잔액을 바꾸는 것은 서버 내부의
`ShardLedgerService`뿐이고, 각 기능(출석 · 광고 SSV · IAP · 상점)이 자기 사건을
검증한 뒤에만 부른다.

`POST /users/me/attendance`가 그 첫 번째 전용 통로다. **request body를 받지 않는다** —
누구인지는 Bearer session에서, 며칠인지는 server 시계에서, 얼마인지는 서버 상수에서 온다.
client가 정할 수 있는 값이 하나도 없다.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, shard_service
from app.shards import attendance
from app.shards.service import ShardLedgerService

router = APIRouter(prefix="/users", tags=["users"])


class ShardWalletPayload(BaseModel):
    """조각 지갑. **userId를 돌려주지 않는다** — 부르는 쪽이 이미 자기 자신이다."""

    balance: int
    lifetime_earned: int = Field(serialization_alias="lifetimeEarned")
    lifetime_spent: int = Field(serialization_alias="lifetimeSpent")

    model_config = {"populate_by_name": True}


class AttendanceStatusPayload(BaseModel):
    """오늘 출석을 받을 수 있는지. `attendanceDate`는 **server KST 날짜**다."""

    attendance_date: str = Field(serialization_alias="attendanceDate")
    claimed: bool

    model_config = {"populate_by_name": True}


class AttendanceClaimPayload(BaseModel):
    """출석 결과.

    `claimed`는 **이번 호출이 지급했는가**다. 같은 날 두 번째 호출은
    `claimed=false, reward=0`이고 오류가 아니다 — 재시도는 정상 동작이다.
    `balance`는 언제나 서버 원장이 계산한 현재 잔액이다.
    """

    attendance_date: str = Field(serialization_alias="attendanceDate")
    claimed: bool
    reward: int
    balance: int

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


# MARK: - 출석


@router.get(
    "/me/attendance",
    response_model=AttendanceStatusPayload,
    response_model_by_alias=True,
)
def my_attendance(
    user: CurrentUser,
    shards: Annotated[ShardLedgerService, Depends(shard_service)],
) -> AttendanceStatusPayload:
    """오늘 출석 조각을 받을 수 있는지. 날짜는 **server 시계의 KST 날짜**다."""
    date, claimed = attendance.status(shards, user.id)
    return AttendanceStatusPayload(attendance_date=date, claimed=claimed)


@router.post(
    "/me/attendance",
    response_model=AttendanceClaimPayload,
    response_model_by_alias=True,
)
def claim_attendance(
    user: CurrentUser,
    shards: Annotated[ShardLedgerService, Depends(shard_service)],
) -> AttendanceClaimPayload:
    """오늘 출석 조각 +1.

    **body를 읽지 않는다.** `userId` · `date` · `amount` · `reason`을 보내도 아무 영향이 없다 —
    받을 자리 자체가 없다. 하루 한 번이라는 규칙은 원장의 idempotency가 지킨다
    (`daily_attendance` + KST 날짜 + user).
    """
    result = attendance.claim(shards, user.id)
    return AttendanceClaimPayload(
        attendance_date=result.date,
        claimed=result.claimed,
        reward=result.reward,
        balance=result.balance,
    )
