"""POST /auth/apple · POST /auth/logout

응답 key는 client의 Swift Codable 이름과 그대로 맞춘다(camelCase) —
decoder에 keyDecodingStrategy를 걸어두면 나중에 한쪽만 바꿨을 때 조용히 깨진다.

**Apple subject는 절대 응답에 담지 않는다.** 나가는 것은 internal user UUID뿐이다.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import bearer_token, store, verifier
from app.auth.apple import AppleTokenVerifier
from app.auth.errors import AppleTokenError
from app.auth.profile import InvalidDisplayName, normalize_display_name
from app.auth.models import APPLE_PROVIDER, issue_session_token, new_session, sha256_hex
from app.auth.store import AuthStore, StoreUnavailable

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

INVALID_CREDENTIAL = "Apple 로그인 정보를 확인하지 못했어요. 다시 시도해 주세요."
UNAVAILABLE = "지금은 로그인할 수 없어요. 잠시 뒤 다시 시도해 주세요."
STORE_FAILED = "로그인 정보를 저장하지 못했어요. 잠시 뒤 다시 시도해 주세요."


class AppleSignInRequest(BaseModel):
    """client가 보내는 것은 token · nonce, 그리고 **선택적인 이름**뿐이다.

    `displayName`은 **optional이다** — 1.0.7 client는 보내지 않고 그래도 정상이다.
    Apple은 보통 최초 authorization에서만 이름을 주므로 이후 로그인에는 없다.
    """

    identity_token: str = Field(alias="identityToken", min_length=1, max_length=8192)
    # client가 만든 raw nonce. server가 SHA-256으로 바꿔 token claim과 비교한다.
    nonce: str = Field(min_length=1, max_length=256)
    # **서명된 값이 아니다.** 신원·권한 판단에 쓰지 않고 첫 이름을 채우는 데만 쓴다.
    display_name: str | None = Field(default=None, alias="displayName", max_length=128)

    model_config = {"populate_by_name": True}


class UserPayload(BaseModel):
    id: str


class SessionPayload(BaseModel):
    access_token: str = Field(serialization_alias="accessToken")
    token_type: str = Field(default="Bearer", serialization_alias="tokenType")
    expires_at: datetime = Field(serialization_alias="expiresAt")
    user: UserPayload

    model_config = {"populate_by_name": True}


@router.post("/apple", response_model=SessionPayload, response_model_by_alias=True)
def sign_in_with_apple(
    body: AppleSignInRequest,
    apple: Annotated[AppleTokenVerifier, Depends(verifier)],
    auth_store: Annotated[AuthStore, Depends(store)],
) -> SessionPayload:
    # 1. Apple identity token 검증 (B-2A). client가 "검증됐다"고 하는 말은 믿지 않는다.
    #    client는 raw nonce를 보내고, Apple token에는 그 SHA-256이 들어 있다.
    try:
        identity = apple.verify(body.identity_token, expected_nonce=sha256_hex(body.nonce))
    except AppleTokenError as error:
        if error.is_upstream_failure:
            # Apple JWKS에 닿지 못했다. client 잘못이 아니므로 401이 아니다.
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from error
        # 어떤 검증에서 걸렸는지 알려주지 않는다.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, INVALID_CREDENTIAL) from error

    # 2. identity → internal User (없으면 생성). 같은 subject면 항상 같은 User다.
    try:
        user, created = auth_store.user_for_identity(APPLE_PROVIDER, identity.subject)
        # 3. Apple이 이름을 줬고 **아직 이름이 없을 때만** 채운다.
        #    이미 사용자가 정한 이름이 있으면 건드리지 않는다 — 로그인할 때마다
        #    Apple 이름으로 되돌아가면 사용자가 바꾼 의미가 사라진다.
        #    이름 하나 때문에 로그인이 실패하면 안 되므로 검증 실패는 조용히 넘긴다.
        if body.display_name is not None and user.display_name is None:
            try:
                user = auth_store.seed_display_name(user.id, normalize_display_name(body.display_name))
            except InvalidDisplayName:
                logger.info("apple_sign_in_display_name_rejected")
        token = issue_session_token()
        session = new_session(user.id, token)
        auth_store.create_session(session)
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, STORE_FAILED) from error

    logger.info("apple_sign_in_ok new_user=%s", created)
    return SessionPayload(
        access_token=token,
        expires_at=session.expires_at,
        user=UserPayload(id=user.id),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    token: Annotated[str, Depends(bearer_token)],
    auth_store: Annotated[AuthStore, Depends(store)],
) -> None:
    """이 기기의 session만 취소한다.

    Apple authorization 자체를 revoke하지 않는다 — 그건 사용자가 설정에서 할 일이다.
    client의 거울 / 스티커 / 등록 준비와는 아무 관계가 없다.
    """
    try:
        revoked = auth_store.revoke_session(sha256_hex(token))
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from error

    # 이미 없거나 만료된 session이어도 성공으로 둔다 — client는 어차피 지운다.
    logger.info("logout revoked=%s", revoked)
