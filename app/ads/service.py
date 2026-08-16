"""검증된 SSV callback → 조각 지급.

순서가 곧 보안이다:

    1. Google 서명 검증        ← 이걸 통과하기 전에는 어떤 값도 믿지 않는다
    2. 제품 설정과 대조         ← 서명만 맞다고 지급하지 않는다
    3. context → 내부 user     ← client가 보낸 user id를 믿지 않는다
    4. 원장 transaction        ← 하루 상한 · 중복 방지 · 잔액 갱신이 한 번에

지급액은 **`REWARD_PER_AD` 상수**다. callback의 `reward_amount`는 지급액의 출처가 아니라
**검증 대상**이다 — AdMob 설정이 바뀌었거나 남의 ad unit이면 지급하지 않는다.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime

from app.ads.models import (
    DAILY_REWARD_LIMIT,
    REWARD_PER_AD,
    RewardedAdError,
    RewardedAdReason,
    RewardOutcome,
    VerifiedRewardCallback,
)
from app.ads.store import RewardContextStore, new_context
from app.ads.verifier import AdMobKeyProvider, verify
from app.auth.models import sha256_hex
from app.shards.attendance import attendance_date
from app.shards.models import QuotaExceeded, ShardReason
from app.shards.service import ShardLedgerService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RewardedAdConfig:
    """production AdMob 설정. **없으면 지급하지 않는다(fail closed).**

    ad unit이 비어 있는 채로 배포되면 endpoint는 살아 있되 아무에게도 조각을 주지 않는다.
    추측한 ID를 넣어두고 "언젠가 맞겠지" 하는 것보다 안전하다.
    """

    ad_unit: str = ""
    reward_item: str = ""
    reward_amount: int = REWARD_PER_AD

    @property
    def is_configured(self) -> bool:
        return bool(self.ad_unit and self.reward_item)


@dataclass(frozen=True)
class RewardedAdStatus:
    rewarded_today: int
    remaining_today: int
    daily_limit: int


class RewardedAdService:
    def __init__(
        self,
        shards: ShardLedgerService,
        contexts: RewardContextStore,
        keys: AdMobKeyProvider,
        config: RewardedAdConfig,
    ) -> None:
        self._shards = shards
        self._contexts = contexts
        self._keys = keys
        self._config = config

    # MARK: - client가 부르는 쪽 (authenticated)

    def status(self, user_id: str, now: datetime | None = None) -> RewardedAdStatus:
        """오늘 몇 번 받았고 몇 번 남았는지. **표시용**이고 지급 판단에 쓰지 않는다."""
        used = self._shards.quota_used(user_id, ShardReason.REWARDED_AD, attendance_date(now))
        capped = min(used, DAILY_REWARD_LIMIT)
        return RewardedAdStatus(
            rewarded_today=capped,
            remaining_today=DAILY_REWARD_LIMIT - capped,
            daily_limit=DAILY_REWARD_LIMIT,
        )

    def issue_context(self, user_id: str, token: str) -> None:
        """광고에 실어 보낼 short-lived context를 저장한다. raw token은 호출부가 만든다."""
        self._contexts.save(new_context(user_id, token))
        logger.info("admob_ssv_context_issued")

    # MARK: - Google이 부르는 쪽 (서명이 곧 인증)

    def handle_callback(self, raw_query: str) -> RewardOutcome:
        """서명 검증부터 지급까지. 실패는 전부 `RewardedAdError`로 나간다."""
        callback = verify(raw_query, self._keys)
        self._check_product(callback)
        user_id = self._resolve_user(callback)
        return self._grant(user_id, callback)

    # MARK: - 내부

    def _check_product(self, callback: VerifiedRewardCallback) -> None:
        """서명이 맞아도 **우리 제품의 보상이 아니면** 지급하지 않는다."""
        if not self._config.is_configured:
            # production ad unit이 아직 없다. 서명이 맞아도 지급하지 않는다.
            logger.warning("admob_ssv_not_configured")
            raise RewardedAdError(RewardedAdReason.NOT_CONFIGURED)

        if callback.ad_unit != self._config.ad_unit:
            logger.warning("admob_ssv_unexpected_ad_unit")
            raise RewardedAdError(RewardedAdReason.UNEXPECTED_AD_UNIT)

        if (
            callback.reward_item != self._config.reward_item
            or callback.reward_amount != self._config.reward_amount
        ):
            # AdMob 설정이 바뀌었거나 우리가 모르는 보상이다. 조용히 지급하지 않는다.
            logger.warning("admob_ssv_unexpected_reward")
            raise RewardedAdError(RewardedAdReason.UNEXPECTED_REWARD)

    def _resolve_user(self, callback: VerifiedRewardCallback) -> str:
        """서명은 유효하다. 이제 **누구에게 줄지**를 정한다.

        세 가지 실패를 구분해서 로그에 남긴다 — 셋 다 지급하지 않지만 뜻이 다르다:

        - `missing_context`  — 아예 안 왔다. **SSV Test Tool이 정상적으로 이 상태다**
        - `unknown_context`  — 값은 있는데 우리가 발급한 적이 없다. 조작이거나 다른 앱이다
        - `expired_context`  — 우리가 준 것이지만 시간이 지났다. 흐름이 너무 느렸다는 뜻

        실제 광고에서 `missing_context`가 보이기 시작하면 client가 context를
        안 싣고 있다는 신호다. 하나로 뭉뚱그리면 그걸 알아챌 수 없다.
        """
        if not callback.custom_data:
            logger.info("admob_ssv_missing_context")
            raise RewardedAdError(RewardedAdReason.MISSING_CONTEXT)

        context = self._contexts.context(sha256_hex(callback.custom_data))
        if context is None:
            logger.warning("admob_ssv_unknown_context")
            raise RewardedAdError(RewardedAdReason.UNKNOWN_CONTEXT)
        if not context.is_valid():
            logger.warning("admob_ssv_expired_context")
            raise RewardedAdError(RewardedAdReason.EXPIRED_CONTEXT)
        return context.user_id

    def _grant(self, user_id: str, callback: VerifiedRewardCallback) -> RewardOutcome:
        """하루 상한 · 중복 방지 · 잔액 갱신이 **한 transaction**에서 끝난다.

        보상 날짜는 **서명된 `timestamp`**로 정한다. callback이 늦게 도착해도
        광고를 본 그 날의 몫이다 — 23:59:58에 본 광고가 00:00:02에 도착했다고
        다음 날 quota를 깎으면 안 된다.
        """
        reward_day = attendance_date(callback.timestamp)
        short = _short(callback.transaction_id)

        try:
            result = self._shards.credit(
                user_id,
                REWARD_PER_AD,
                ShardReason.REWARDED_AD,
                external_event_id=callback.transaction_id,
                period=reward_day,
                limit=DAILY_REWARD_LIMIT,
            )
        except QuotaExceeded:
            # 정상적인 callback이지만 오늘 몫을 다 받았다. 오류가 아니다.
            logger.info("admob_ssv_daily_limit_reached transaction=%s", short)
            return RewardOutcome(granted=False, duplicate=False, limit_reached=True)

        if not result.applied:
            # Google 재전송. 이미 지급했으므로 아무것도 하지 않는다.
            logger.info("admob_ssv_duplicate transaction=%s", short)
            return RewardOutcome(granted=False, duplicate=True, limit_reached=False)

        logger.info("admob_ssv_reward_applied transaction=%s balance=%d", short, result.wallet.balance)
        return RewardOutcome(granted=True, duplicate=False, limit_reached=False)


def _short(transaction_id: str) -> str:
    """로그에 raw transaction id를 남기지 않는다. 추적에 충분한 만큼만."""
    return hashlib.sha256(transaction_id.encode()).hexdigest()[:12]
