"""내장 템플릿 획득 통계 endpoint.

앱에 들어 있는 공식 템플릿 32종이 **몇 명에게 받아졌는지**만 다룬다.
조각도 소유권도 판매자도 없다 — Marketplace(B-7E)와 다른 것이고 그 경로를 섞지 않는다.

`downloadCount`의 뜻은 Marketplace와 **같다**: 서로 다른 사용자의 최초 획득 수.
같은 사람이 다시 받아도, 구경만 해도 오르지 않는다.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.api.deps import catalog_service, current_user
from app.auth.models import User
from app.auth.store import StoreUnavailable
from app.catalog.models import PurchaseRequired, UnknownTemplate
from app.shards.models import InsufficientShards
from app.catalog.service import CatalogService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/catalog", tags=["catalog"])

UNAVAILABLE = "storage unavailable"


class TemplateStatResponse(BaseModel):
    """공개 통계 하나. **사용자별 상태를 담지 않는다** — 누가 받았는지는 공개가 아니다."""

    template_id: str = Field(serialization_alias="templateId")
    download_count: int = Field(serialization_alias="downloadCount")

    model_config = {"populate_by_name": True}


class AcquisitionResponse(BaseModel):
    template_id: str = Field(serialization_alias="templateId")
    #: **이 요청이 처음 기록했는가.** `false`는 실패가 아니다 — 이미 받은 것이고
    #: `downloadCount`는 정상 현재 값이다.
    first_acquisition: bool = Field(serialization_alias="firstAcquisition")
    download_count: int = Field(serialization_alias="downloadCount")

    model_config = {"populate_by_name": True}


class ReconcileRequest(BaseModel):
    """앱이 이미 가지고 있는 내장 템플릿 id 목록.

    **client가 userId를 보내지 않는다** — 누구인지는 session이 정한다.
    """

    template_ids: list[str] = Field(alias="templateIds")

    model_config = {"populate_by_name": True}


@router.get("/templates/stats", response_model_by_alias=True)
def template_stats(
    service: Annotated[CatalogService, Depends(catalog_service)],
    ids: Annotated[str, Query(description="쉼표로 나눈 template id")] = "",
) -> list[TemplateStatResponse]:
    """**공개다 — 로그인 없이 볼 수 있다.**

    카드마다 요청을 하나씩 만들지 않도록 **한 번에** 묻는다.
    모르는 id는 조용히 빼고, 기록이 없는 것은 0으로 돌려준다 —
    없는 것과 0은 사용자에게 같은 뜻이고 빠뜨리면 화면이 자리를 비운다.
    """
    wanted = [x.strip() for x in ids.split(",") if x.strip()]
    if not wanted:
        return []
    try:
        found = service.stats(wanted)
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from error
    return [
        TemplateStatResponse(template_id=x.template_id, download_count=x.download_count)
        for x in found
    ]


@router.post("/templates/{template_id}/acquire", response_model_by_alias=True)
def acquire_template(
    template_id: str,
    user: Annotated[User, Depends(current_user)],
    service: Annotated[CatalogService, Depends(catalog_service)],
) -> AcquisitionResponse:
    """이 사용자가 내장 템플릿을 받았다고 기록한다. **body가 없다.**

    수량 · userId · count를 실을 자리가 없다 — 누구인지는 session이 정하고
    얼마나 오르는지는 서버가 정한다(최초 한 번만 +1).

    **등록된 id만 받는다.** 아무 문자열이나 세면 공개 통계를 부풀릴 수 있다.
    """
    try:
        result = service.acquire(user, template_id)
    except UnknownTemplate as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "template not found") from error
    except PurchaseRequired as error:
        # **값이 생긴 템플릿을 이 경로로 받으려 한 것이다.**
        #
        # 이 경로는 모든 손그림이 공짜였던 1.0.7의 것이다. Phase B로 값이 붙은
        # 뒤에도 구버전 앱은 계속 여기를 부른다 — 예상된 일이고 오류가 아니다.
        # 잡지 않으면 500이 나가고, 정상적인 구버전 사용 하나하나가 서버 장애처럼
        # 기록돼 진짜 장애가 묻힌다.
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED, "template requires purchase"
        ) from error
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from error
    return AcquisitionResponse(
        template_id=result.template_id,
        first_acquisition=result.first_acquisition,
        download_count=result.download_count,
    )


@router.get("/templates/mine")
def my_templates(
    user: Annotated[User, Depends(current_user)],
    service: Annotated[CatalogService, Depends(catalog_service)],
) -> dict[str, list[str]]:
    """내가 가진 내장 템플릿 id. **id만 나간다** — 값도 시각도 필요 없다."""
    try:
        return {"templateIds": service.owned_template_ids(user)}
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from error


@router.post("/templates/{template_id}/purchases", response_model_by_alias=True)
def purchase_template(
    template_id: str,
    user: Annotated[User, Depends(current_user)],
    service: Annotated[CatalogService, Depends(catalog_service)],
) -> AcquisitionResponse:
    """조각을 내고 내장 템플릿을 갖는다.

    **body가 없다.** 가격도 수량도 사용자도 client가 정하는 자리를 만들지 않는다 —
    값은 서버 표가, 사람은 token이 정한다.

    이미 가진 것은 값을 내지 않고 그대로 돌려준다(`firstAcquisition=false`).
    같은 요청을 여러 번 보내도 조각은 한 번만 빠진다 —
    멱등의 열쇠가 `(사용자, 템플릿)` 기록 자체이기 때문이다.
    """
    try:
        result = service.purchase(user, template_id)
    except UnknownTemplate as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "template not found") from error
    except InsufficientShards as error:
        raise HTTPException(status.HTTP_409_CONFLICT, "not enough shards") from error
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from error
    return AcquisitionResponse(
        template_id=result.template_id,
        first_acquisition=result.first_acquisition,
        download_count=result.download_count,
    )


@router.post("/templates/reconcile", response_model_by_alias=True)
def reconcile_templates(
    request: ReconcileRequest,
    user: Annotated[User, Depends(current_user)],
    service: Annotated[CatalogService, Depends(catalog_service)],
) -> list[AcquisitionResponse]:
    """앱에 이미 있는 내장 템플릿을 한 번씩 따라잡는다.

    예전 버전에서 받은 것은 서버 기록이 없다 — 그때는 세는 곳이 아예 없었다.
    로그인한 뒤 이 요청 하나로 반영한다.

    **몇 번을 불러도 결과가 같다.** 이미 있는 것은 수가 오르지 않으므로,
    실패한 획득의 복구 수단으로도 쓸 수 있다.
    """
    try:
        results = service.reconcile(user, request.template_ids)
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from error
    return [
        AcquisitionResponse(
            template_id=x.template_id,
            first_acquisition=x.first_acquisition,
            download_count=x.download_count,
        )
        for x in results
    ]
