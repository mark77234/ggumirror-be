"""Scheduler가 부르는 자리 (I-14).

**아무나 부를 수 있으면 안 된다.** 이 경로는 사용자에게 알림을 보내므로,
열려 있으면 누구나 우리 사용자에게 push를 쏘게 하는 버튼이 된다.

인증은 Cloud Run의 것을 그대로 쓴다 — Cloud Scheduler가 OIDC token을 달고
호출하고, Cloud Run이 `roles/run.invoker`를 확인한 뒤에야 여기 코드가 돈다.
앱이 두 번째 인증 체계를 만들지 않는다.

그 위에 **한 겹 더** 둔다: `JOBS_TOKEN`이 설정돼 있으면 header로 확인한다.
Cloud Run이 잘못 열려 있어도(`allUsers` invoker) 그것만으로는 돌지 않는다.

이번 phase에서 실제 Cloud Scheduler job은 만들지 않는다 — 코드만 준비한다.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.auth.store import StoreUnavailable

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])


class JobResult(BaseModel):
    """**누구에게 보냈는지는 담지 않는다.** 숫자만 남긴다."""

    considered: int
    sent: int
    skipped_duplicate: int = Field(serialization_alias="skippedDuplicate")
    skipped_empty: int = Field(serialization_alias="skippedEmpty")


def _authorized(request: Request, token: str | None) -> None:
    """설정돼 있으면 확인한다. **값을 로그에 남기지 않는다.**"""
    expected = getattr(request.app.state.settings, "jobs_token", "")
    if not expected:
        # 설정하지 않았다면 Cloud Run invoker IAM 하나에 기댄다.
        return
    if token != expected:
        logger.warning("job_rejected")
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
    x_ggumirror_job_token: Annotated[str | None, Header()] = None,
) -> JobResult:
    """매일 받겠다고 한 사람에게. **같은 날 두 번 불러도 한 번만 간다.**"""
    _authorized(request, x_ggumirror_job_token)
    service = _digest(request)
    try:
        return _result(service.run_daily(service.subscriber_ids()))
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error


@router.post("/mirror-digest/weekly")
def run_weekly_digest(
    request: Request,
    x_ggumirror_job_token: Annotated[str | None, Header()] = None,
) -> JobResult:
    _authorized(request, x_ggumirror_job_token)
    service = _digest(request)
    try:
        return _result(service.run_weekly(service.subscriber_ids()))
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error


@router.post("/recommendation/weekly")
def run_recommendation(
    request: Request,
    x_ggumirror_job_token: Annotated[str | None, Header()] = None,
) -> JobResult:
    """다시 둘러보라는 소식. **켠 사람에게만, 주 1회.**"""
    _authorized(request, x_ggumirror_job_token)
    service = _digest(request)
    try:
        return _result(service.run_recommendation(service.subscriber_ids()))
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage unavailable") from error
