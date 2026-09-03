"""Scheduler가 부르는 자리 (I-14).

**아무나 부를 수 있으면 안 된다.** 이 경로는 사용자에게 알림을 보내므로,
열려 있으면 누구나 우리 사용자 전체에게 push를 쏘는 버튼이 된다.

⚠️ 예전에 여기 "Cloud Run invoker IAM 뒤에 있다"고 적혀 있었다. **틀렸다.**
`ggumirror-api`는 `allUsers`에게 `roles/run.invoker`가 열린 공개 service다 —
로그인 없이 상점을 볼 수 있어야 하기 때문이다. 그래서 IAM은 이 경로에 아무런
문이 되지 못했고, `JOBS_TOKEN`도 설정돼 있지 않으면 그냥 통과였다.

이제 **앱이 직접** 부른 쪽을 확인한다(`scheduler_identity`):
Google 서명 · issuer · audience · exp에 더해 **우리가 정한 그 scheduler 계정인지**
까지 본다. 서명만 보면 Google 계정을 가진 누구나 통과한다.

설정이 없으면 **막는다.** 공개 service에서 "설정 안 했으니 통과"는 문을 여는 것이다.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.scheduler_identity import SchedulerIdentityError, verify_scheduler
from app.auth.store import StoreUnavailable

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])


class JobResult(BaseModel):
    """**누구에게 보냈는지는 담지 않는다.** 숫자만 남긴다."""

    considered: int
    sent: int
    skipped_duplicate: int = Field(serialization_alias="skippedDuplicate")
    skipped_empty: int = Field(serialization_alias="skippedEmpty")


def _authorized(
    request: Request, authorization: str | None, job_token: str | None
) -> None:
    """부른 쪽이 우리 scheduler인가. **아니면 아무것도 하지 않는다.**

    실패 이유를 응답에 담지 않는다 — 무엇이 틀렸는지 알려 주면 맞출 때까지
    시도하게 된다.
    """
    settings = request.app.state.settings
    scheme, _, token = (authorization or "").partition(" ")
    try:
        verify_scheduler(
            token.strip() if scheme.lower() == "bearer" else None,
            expected_service_account=getattr(settings, "scheduler_service_account", ""),
            expected_audience=getattr(settings, "scheduler_audience", ""),
            verifier=request.app.state.scheduler_verifier(),
        )
    except SchedulerIdentityError as error:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from error

    # 2차 자물쇠(선택). 설정돼 있을 때만 본다.
    expected = getattr(settings, "jobs_token", "")
    if expected and job_token != expected:
        logger.warning("job_rejected reason=token")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")


def _digest(request: Request):
    try:
        return request.app.state.mirror_digest_service()
    except Exception as error:  # Firestore client 생성 실패
        logger.error("digest_service_unavailable error=%s", type(error).__name__)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "unavailable") from error


def _result(outcome) -> JobResult:
    return JobResult(
        considered=outcome.considered,
        sent=outcome.sent,
        skipped_duplicate=outcome.skipped_duplicate,
        skipped_empty=outcome.skipped_empty,
    )


@router.post("/mirror-digest/daily")
def run_daily_digest(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_ggumirror_job_token: Annotated[str | None, Header()] = None,
) -> JobResult:
    """매일 받겠다고 한 사람에게. **같은 날 두 번 불러도 한 번만 간다.**"""
    _authorized(request, authorization, x_ggumirror_job_token)
    service = _digest(request)
    try:
        return _result(service.run_daily(service.subscriber_ids()))
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error


@router.post("/mirror-digest/weekly")
def run_weekly_digest(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_ggumirror_job_token: Annotated[str | None, Header()] = None,
) -> JobResult:
    _authorized(request, authorization, x_ggumirror_job_token)
    service = _digest(request)
    try:
        return _result(service.run_weekly(service.subscriber_ids()))
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error


@router.post("/recommendation/weekly")
def run_recommendation(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_ggumirror_job_token: Annotated[str | None, Header()] = None,
) -> JobResult:
    """다시 둘러보라는 소식. **켠 사람에게만, 주 1회.**"""
    _authorized(request, authorization, x_ggumirror_job_token)
    service = _digest(request)
    try:
        return _result(service.run_recommendation(service.subscriber_ids()))
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error
