"""GET /admob/rewarded/ssv — Google AdMob Server-Side Verification callback.

**이 endpoint에는 Bearer가 없다.** Google이 우리 세션을 갖고 있지 않기 때문이다.
authentication은 Google의 **ECDSA 서명 검증**이 한다(`app/ads/verifier.py`).

client가 부를 수 있는 지급 통로가 아니다 — 서명을 만들 수 없으면 아무 일도 일어나지 않는다.

## raw query

검증 입력은 **ASGI scope의 `query_string`(bytes) 그대로**다.
`Request.url.query`도 쓰지 않는다 — URL 객체를 거치는 순간 parsing과 재직렬화를
한 번 지나므로, 검증 대상이 "받은 바이트"가 아니게 될 여지가 생긴다.

## 응답 코드가 곧 Google에 대한 지시

| 상황 | 응답 | 이유 |
|---|---|---|
| 지급함 · 중복 · 하루 상한 | **200** | 처리가 끝났다. 재시도하지 말라 |
| 서명 유효하지만 지급 대상 아님 | **200** | 우리가 판단해서 안 준 것이다. 다시 보내도 같다 |
| 서명 실패 · 형식 오류 | **400** | Google이 보낸 것이 아니거나 우리가 모르는 모양이다 |
| Google key 조회 실패 · 저장소 오류 | **5xx** | 일시적이다. 재시도하면 복구된다 |

"서명은 맞는데 줄 수 없다"(context 없음 · 만료 · 하루 상한 · 중복 · ad unit 불일치)를
4xx로 돌려주면 Google은 전달 실패로 보고 한동안 재시도한다. 결과가 바뀌지 않는데도.
그래서 **처리 완료(200)**로 답하고, 왜 안 줬는지는 로그로만 구분한다.

**AdMob SSV Test Tool은 사용자 context 없이 호출한다** — 서명이 유효하므로 200이고,
조각은 하나도 움직이지 않는다.

반대로 일시적 실패에 200을 주면 정당한 보상이 조용히 사라진다.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.ads.models import RewardedAdError
from app.ads.service import RewardedAdService
from app.api.deps import rewarded_ad_service
from app.auth.store import StoreUnavailable

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admob", tags=["admob"])


@router.get("/rewarded/ssv")
def rewarded_ssv(
    request: Request,
    ads: Annotated[RewardedAdService, Depends(rewarded_ad_service)],
) -> Response:
    """Google만 성공시킬 수 있는 지급 통로.

    응답 본문을 비워 둔다 — Google은 status code만 본다. 실패 이유를 밖으로 알려주면
    서명을 맞춰가며 시도하는 쪽에 힌트가 된다.
    """
    try:
        # ASGI가 받은 **원본 bytes**. 재구성하지 않는다.
        ads.handle_callback(request.scope["query_string"])
    except RewardedAdError as error:
        if error.is_upstream_failure:
            # Google 공개키를 못 받았다. 우리 문제이므로 재시도를 받아야 한다.
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE) from error
        if error.is_permanent_non_reward:
            # 서명은 유효하다. 지급하지 않기로 **판단이 끝난** 것이므로 재시도를 받지 않는다.
            logger.info("admob_ssv_no_reward reason=%s", error.reason.value)
            return Response(status_code=status.HTTP_200_OK)
        logger.info("admob_ssv_rejected reason=%s", error.reason.value)
        raise HTTPException(status.HTTP_400_BAD_REQUEST) from error
    except StoreUnavailable as error:
        # Firestore 일시 오류. Google 재시도로 복구된다 — transaction_id가 중복을 막는다.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE) from error

    # 지급 · 중복 · 하루 상한 전부 "처리 완료"다.
    return Response(status_code=status.HTTP_200_OK)
