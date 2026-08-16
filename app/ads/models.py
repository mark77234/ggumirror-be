"""AdMob Rewarded 보상의 값과 실패 분류.

**보상 권위는 Google SSV callback 하나뿐이다.** client의 `onUserEarnedReward`는
"광고 UX가 끝났다"는 신호일 뿐 조각을 지급하는 근거가 아니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

# PRODUCT RULE. **callback이 보낸 숫자를 그대로 쓰지 않는다** —
# reward_amount는 검증 대상이지 지급액의 출처가 아니다.
REWARD_PER_AD = 1
DAILY_REWARD_LIMIT = 5


class RewardedAdReason(StrEnum):
    """callback을 지급하지 않은 이유. **응답 본문에 담지 않는다** — 로그 분류용이다."""

    # 암호학적 실패 — 이 callback은 Google이 서명한 것이 아니거나 형식이 깨졌다.
    MISSING_SIGNATURE = "missing_signature"
    MISSING_KEY_ID = "missing_key_id"
    MALFORMED_SIGNATURE = "malformed_signature"
    UNKNOWN_KEY_ID = "unknown_key_id"
    INVALID_SIGNATURE = "invalid_signature"
    # 서명은 맞는데 우리가 모르는 모양이다. Google이 보낼 리 없는 값이라 시끄럽게 거절한다.
    MISSING_FIELD = "missing_field"
    INVALID_TIMESTAMP = "invalid_timestamp"
    # 일시적 실패 — 다시 보내면 될 수도 있다.
    KEYS_UNAVAILABLE = "keys_unavailable"
    # 서명은 유효하지만 지급 대상이 아니다. **다시 보내도 결과가 같다.**
    NOT_CONFIGURED = "not_configured"
    UNEXPECTED_AD_UNIT = "unexpected_ad_unit"
    UNEXPECTED_REWARD = "unexpected_reward"
    MISSING_CONTEXT = "missing_context"
    UNKNOWN_CONTEXT = "unknown_context"
    EXPIRED_CONTEXT = "expired_context"


# 서명은 유효하지만 조각을 줄 수 없는 상태. **재전송으로 해결되지 않는다.**
#
# 이런 callback에 4xx를 주면 Google이 "전달 실패"로 보고 한동안 재시도한다.
# 실제로는 우리가 정상적으로 판단해서 "안 준다"고 결론 낸 것이므로 **처리 완료(200)**다.
# AdMob SSV Test Tool은 사용자 context 없이 호출하는데, 그것도 여기에 해당한다.
PERMANENT_NON_REWARD = frozenset(
    {
        RewardedAdReason.NOT_CONFIGURED,
        RewardedAdReason.UNEXPECTED_AD_UNIT,
        RewardedAdReason.UNEXPECTED_REWARD,
        RewardedAdReason.MISSING_CONTEXT,
        RewardedAdReason.UNKNOWN_CONTEXT,
        RewardedAdReason.EXPIRED_CONTEXT,
    }
)


class RewardedAdError(Exception):
    """callback을 지급으로 이어가지 않는다. `reason`은 로그에만 남는다."""

    def __init__(self, reason: RewardedAdReason) -> None:
        # message에 callback 값을 담지 않는다 — 그대로 로그로 새어 나간다.
        super().__init__(reason.value)
        self.reason = reason

    @property
    def is_upstream_failure(self) -> bool:
        """우리 잘못도 Google 서명 문제도 아닌, 일시적인 조회 실패인가.

        이 경우에만 5xx로 답해서 **Google이 재시도하게** 한다.
        서명이 틀린 callback을 5xx로 돌려주면 영원히 재시도를 받는다.
        """
        return self.reason is RewardedAdReason.KEYS_UNAVAILABLE

    @property
    def is_permanent_non_reward(self) -> bool:
        """서명은 유효한데 지급 대상이 아닌가. 그렇다면 재시도를 받을 이유가 없다."""
        return self.reason in PERMANENT_NON_REWARD


@dataclass(frozen=True)
class VerifiedRewardCallback:
    """**서명 검증을 통과한** callback의 값.

    이 타입이 존재한다는 것 자체가 "Google이 서명했다"는 뜻이다.
    검증 전 값은 이 경계를 넘어오지 못한다 — 특히 `timestamp`는
    검증 전에 읽으면 공격자가 보상 날짜를 고를 수 있다.
    """

    transaction_id: str
    ad_unit: str
    reward_amount: int
    reward_item: str
    # Google이 보낸 event 시각(epoch milliseconds 기준의 UTC datetime).
    timestamp: datetime
    # 우리가 발급한 short-lived reward context. 내부 user UUID가 아니다.
    #
    # **없을 수 있다.** AdMob SSV Test Tool은 사용자 없이 호출하고, 그 경우에도
    # 서명은 유효하다. "값이 없다"와 "값이 우리 것이 아니다"를 구분해야
    # 실제 광고에서 context가 빠지는 사고를 로그로 알아챌 수 있다.
    custom_data: str


@dataclass(frozen=True)
class RewardOutcome:
    """callback 처리 결과. Google에게는 전부 성공(200)으로 답한다."""

    granted: bool
    duplicate: bool
    limit_reached: bool
