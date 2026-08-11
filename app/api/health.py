"""Health / root.

이 Phase의 public API는 이 두 개뿐이다.
/auth/apple, /users, /shards, /store, /listings, /purchases를 미리 만들지 않는다.
"""

from fastapi import APIRouter

from app.core.config import SERVICE_NAME

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    """process health. DB나 외부 API를 건드리지 않는다.

    Cloud Run / uptime check가 부르는 곳이라 의존성을 넣으면
    외부 장애 때 멀쩡한 container가 죽는다.
    """
    return {"status": "ok"}


@router.get("/")
def root() -> dict[str, str]:
    return {"service": SERVICE_NAME, "status": "ok"}
