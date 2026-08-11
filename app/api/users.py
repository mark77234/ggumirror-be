"""GET /users/me

server auth가 실제로 연결됐는지 확인하는 최소 endpoint.
shard / profile / 통계를 여기에 얹지 않는다 — 아직 그런 개념이 서버에 없다.
"""

from fastapi import APIRouter

from app.api.deps import CurrentUser

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me")
def me(user: CurrentUser) -> dict[str, str]:
    """internal user UUID만 돌려준다. Apple subject는 나가지 않는다."""
    return {"id": user.id}
