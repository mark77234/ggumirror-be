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

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.ads.service import RewardedAdService
from app.api.deps import CurrentUser, rewarded_ad_service, shard_service, store as auth_store_dep
from app.auth.models import issue_session_token, utcnow
from app.auth.profile import (
    DisplayNameCooldown,
    InvalidDisplayName,
    ProfileView,
    normalize_display_name,
)
from app.auth.store import AuthStore, StoreUnavailable
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


class RewardedAdStatusPayload(BaseModel):
    """오늘 광고 보상 상태. 버튼을 그리는 데만 쓴다."""

    rewarded_today: int = Field(serialization_alias="rewardedToday")
    remaining_today: int = Field(serialization_alias="remainingToday")
    daily_limit: int = Field(serialization_alias="dailyLimit")

    model_config = {"populate_by_name": True}


class RewardContextPayload(BaseModel):
    """광고에 실어 보낼 opaque context. **내부 user id가 아니다.**"""

    context: str

    model_config = {"populate_by_name": True}


class ProfilePayload(BaseModel):
    """내 프로필. **공개 표면에 나가는 것은 `displayName`뿐이다** —
    email · Apple subject · 내부 metadata를 담지 않는다."""

    id: str
    #: 아직 정하지 않았으면 `null`이다. 서버가 기본 이름을 지어내지 않는다.
    display_name: str | None = Field(default=None, serialization_alias="displayName")
    can_change_display_name: bool = Field(serialization_alias="canChangeDisplayName")
    next_display_name_change_at: datetime | None = Field(
        default=None, serialization_alias="nextDisplayNameChangeAt"
    )

    model_config = {"populate_by_name": True}


class DisplayNameRequest(BaseModel):
    display_name: str = Field(alias="displayName", min_length=1, max_length=128)

    model_config = {"populate_by_name": True}


def _profile(user) -> ProfilePayload:
    view = ProfileView(
        display_name=user.display_name,
        display_name_changed_at=user.display_name_changed_at,
        now=utcnow(),
    )
    return ProfilePayload(
        id=user.id,
        display_name=view.display_name,
        can_change_display_name=view.can_change_display_name,
        next_display_name_change_at=view.next_display_name_change_at,
    )


@router.get("/me")
def me(user: CurrentUser) -> dict[str, object]:
    """internal user UUID + 프로필. Apple subject는 나가지 않는다.

    **필드를 더하기만 한다.** 1.0.7 client는 `id`만 읽고 나머지를 무시하므로
    이 응답이 넓어져도 그대로 동작한다(Swift `JSONDecoder`는 모르는 key를 버린다).
    """
    payload = _profile(user)
    return {
        "id": user.id,
        "displayName": payload.display_name,
        "canChangeDisplayName": payload.can_change_display_name,
        "nextDisplayNameChangeAt": (
            payload.next_display_name_change_at.isoformat()
            if payload.next_display_name_change_at
            else None
        ),
    }


@router.patch("/me/profile", response_model=ProfilePayload, response_model_by_alias=True)
def update_profile(
    body: DisplayNameRequest,
    user: CurrentUser,
    auth_store: Annotated[AuthStore, Depends(auth_store_dep)],
) -> ProfilePayload:
    """이름을 바꾼다. **30일 규칙은 서버가 강제한다** — 기기 시계를 믿지 않는다."""
    try:
        name = normalize_display_name(body.display_name)
    except InvalidDisplayName as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "이름을 다시 확인해 주세요.") from error

    try:
        updated = auth_store.set_display_name(user.id, name, utcnow())
    except DisplayNameCooldown as error:
        # 재시도해도 같은 답이라 4xx다. 언제부터 되는지 함께 알려 준다.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"이름은 30일에 한 번 변경할 수 있어요. 다음 변경 가능: {error.available_at.date().isoformat()}",
        ) from error
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "잠시 뒤 다시 시도해 주세요.") from error

    return _profile(updated)


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


# MARK: - 광고 보상


@router.get(
    "/me/rewarded-ads",
    response_model=RewardedAdStatusPayload,
    response_model_by_alias=True,
)
def my_rewarded_ads(
    user: CurrentUser,
    ads: Annotated[RewardedAdService, Depends(rewarded_ad_service)],
) -> RewardedAdStatusPayload:
    """오늘 광고 보상을 몇 번 받았고 몇 번 남았는지. **읽기 전용**이다."""
    state = ads.status(user.id)
    return RewardedAdStatusPayload(
        rewarded_today=state.rewarded_today,
        remaining_today=state.remaining_today,
        daily_limit=state.daily_limit,
    )


@router.post(
    "/me/rewarded-ads/context",
    response_model=RewardContextPayload,
    response_model_by_alias=True,
)
def create_reward_context(
    user: CurrentUser,
    ads: Annotated[RewardedAdService, Depends(rewarded_ad_service)],
) -> RewardContextPayload:
    """광고에 실어 보낼 **short-lived opaque context**를 발급한다.

    **조각을 지급하는 endpoint가 아니다.** 여기서는 아무 잔액도 움직이지 않는다.
    지급은 Google이 서명한 SSV callback이 도착했을 때만 일어난다.

    client는 이 값을 `ServerSideVerificationOptions.customData`에 넣는다.
    session token · Apple token · 내부 user UUID는 **절대 넣지 않는다** —
    callback URL은 로그와 재시도 기록에 남는다.
    """
    # session token과 같은 방식으로 만든다. 서버는 hash만 저장한다.
    token = issue_session_token()
    ads.issue_context(user.id, token)
    return RewardContextPayload(context=token)
