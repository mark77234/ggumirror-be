"""내장 템플릿 획득 · 통계.

**등록된 id만 받는다.** client가 보낸 문자열을 그대로 세면 아무 값이나 보내
공개 통계를 부풀릴 수 있다.

Marketplace 경로와 섞지 않는다 — 저쪽은 소유권·조각·판매자가 있고 이쪽은 없다.
"""

from __future__ import annotations

import logging

from app.auth.models import User
from app.catalog.models import (
    MAX_BATCH,
    TEMPLATE_IDS,
    AcquisitionResult,
    TemplateStat,
    UnknownTemplate,
    is_known,
)
from app.catalog.store import CatalogStore

logger = logging.getLogger(__name__)


class CatalogService:
    def __init__(self, store: CatalogStore) -> None:
        self._store = store

    def acquire(self, user: User, template_id: str) -> AcquisitionResult:
        """이 사용자가 이 템플릿을 받았다고 기록한다.

        **최초 한 번만 센다.** 다시 받아도 `first_acquisition=False`이고 실패가 아니다 —
        수는 정상 현재 값이다.
        """
        if not is_known(template_id):
            raise UnknownTemplate(template_id)
        result = self._store.acquire(user.id, template_id)
        logger.info(
            "catalog_acquire template=%s first=%s count=%d",
            template_id, result.first_acquisition, result.download_count,
        )
        return result

    def stats(self, template_ids: list[str]) -> list[TemplateStat]:
        """공개 통계. 로그인 없이 볼 수 있다.

        **모르는 id는 조용히 뺀다** — 거절하면 client가 목록 하나 때문에 화면 전체의
        숫자를 잃는다. 대신 없는 것을 만들어내지도 않는다.

        중복은 합치고 상한을 넘으면 자른다. 한 요청이 임의로 커지지 않게 한다.
        """
        wanted: list[str] = []
        for template_id in template_ids:
            if template_id in wanted or not is_known(template_id):
                continue
            wanted.append(template_id)
            if len(wanted) >= MAX_BATCH:
                break
        if not wanted:
            return []
        return self._store.stats(wanted)

    def reconcile(self, user: User, template_ids: list[str]) -> list[AcquisitionResult]:
        """앱에 이미 있는 내장 템플릿을 **한 번씩** 기록으로 남긴다.

        예전 버전에서 받은 것은 서버 기록이 없다(그때는 세는 곳이 아예 없었다).
        로그인한 뒤 이 요청 하나로 따라잡는다.

        **몇 번을 불러도 결과가 같다.** 이미 있는 것은 `first_acquisition=False`이고
        수가 오르지 않는다 — 그래서 실패한 획득의 복구 수단으로도 쓸 수 있다.

        모르는 id는 조용히 뺀다. 중복도 합친다.
        """
        wanted: list[str] = []
        for template_id in template_ids:
            if template_id in wanted or not is_known(template_id):
                continue
            wanted.append(template_id)
            if len(wanted) >= MAX_BATCH:
                break

        results = [self._store.acquire(user.id, x) for x in wanted]
        added = sum(1 for x in results if x.first_acquisition)
        logger.info("catalog_reconcile requested=%d new=%d", len(wanted), added)
        return results

    @staticmethod
    def known_template_ids() -> frozenset[str]:
        return TEMPLATE_IDS
