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

from app.api.deps import bearer_token, optional_bearer_token, shard_service, store, verifier
from app.auth.apple import AppleTokenVerifier
from app.auth.errors import AppleTokenError
from app.auth.profile import InvalidDisplayName, normalize_display_name
from app.auth.models import APPLE_PROVIDER, User, issue_session_token, new_session, sha256_hex
from app.auth.store import AuthStore, StoreUnavailable
from app.shards.service import ShardLedgerService

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


@router.post("/guest", response_model=SessionPayload, response_model_by_alias=True)
def start_guest_session(
    auth_store: Annotated[AuthStore, Depends(store)],
    guest_token: Annotated[str | None, Depends(optional_bearer_token)] = None,
) -> SessionPayload:
    """**로그인 없이** 쓰는 서버 신원 하나. 조각 구매에 계정을 요구하지 않기 위해서다.

    이름 · 이메일 · Apple Account · 어떤 개인정보도 받지 않는다 — body 자체가 없다.
    id는 **서버가 만든다**: client가 만든 UUID를 지갑 주인으로 인정하면 남의 id를
    적어 남의 지갑을 조회·충전할 수 있다.

    나가는 것은 다른 로그인과 **같은 opaque session**이라, 이 뒤의 모든 경로는
    guest인지 계정인지 따로 알 필요가 없다.

    **아직 살아 있는 guest session을 들고 오면 같은 사용자에게 새 session을 준다**(연장).
    session은 30일이고 조각은 실제로 산 것이라, 오래 안 열었다고 지갑을 잃으면 안 된다.
    옛 session은 **취소하지 않는다** — 응답을 잃은 client가 그 token으로 돌아올 수 있다.
    """
    try:
        # 계정 session으로 부르면 무시하고 새 guest를 만든다(`_guest_for`가 None).
        user = _guest_for(auth_store, guest_token) or auth_store.create_guest_user()
        token = issue_session_token()
        session = new_session(user.id, token)
        auth_store.create_session(session)
    except StoreUnavailable as error:
        # 재시도로 달라지는 실패다 — client는 다음 실행에서 다시 시도한다.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from error

    logger.info("guest_session_created renewed=%s", guest_token is not None)
    return SessionPayload(
        access_token=token,
        expires_at=session.expires_at,
        user=UserPayload(id=user.id),
    )


@router.post("/apple", response_model=SessionPayload, response_model_by_alias=True)
def sign_in_with_apple(
    body: AppleSignInRequest,
    apple: Annotated[AppleTokenVerifier, Depends(verifier)],
    auth_store: Annotated[AuthStore, Depends(store)],
    shards: Annotated[ShardLedgerService, Depends(shard_service)],
    guest_token: Annotated[str | None, Depends(optional_bearer_token)] = None,
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
    #    guest session을 들고 왔다면 그 지갑을 잃지 않게 **먼저 이어 붙인다.**
    try:
        guest = _guest_for(auth_store, guest_token)
        if guest is None:
            user, created = auth_store.user_for_identity(APPLE_PROVIDER, identity.subject)
        else:
            user, created = auth_store.link_identity(
                APPLE_PROVIDER, identity.subject, guest.id
            )
            if not created:
                # 이 Apple 계정에는 이미 User가 있다. guest 지갑은 **원장으로** 옮긴다 —
                # 잔액을 복사하지 않는다. 같은 조합은 두 번 반영되지 않는다.
                moved = shards.claim_guest_wallet(guest.id, user.id)
                # 잔액이 0이어도 적는다 — 늦게 도착한 결제가 이 계정으로 가야 한다.
                auth_store.mark_guest_claimed(guest.id, user.id)
                logger.info("guest_wallet_claimed moved=%d", moved)
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
        if guest is not None and guest_token is not None:
            # **인계가 끝난 뒤에만** 취소한다. 먼저 취소하면 중간에 실패했을 때
            # guest가 자기 지갑에 다시 닿을 방법이 없다.
            auth_store.revoke_session(sha256_hex(guest_token))
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, STORE_FAILED) from error

    logger.info("apple_sign_in_ok new_user=%s", created)
    return SessionPayload(
        access_token=token,
        expires_at=session.expires_at,
        user=UserPayload(id=user.id),
    )


def _guest_for(auth_store: AuthStore, token: str | None) -> User | None:
    """지금 들고 있는 **guest** session의 User. 아니면 `None`.

    - token이 없거나 만료·취소면 `None`이다. **로그인을 실패시키지 않는다** —
      옛 client는 애초에 Authorization을 붙이지 않는다
    - 이미 계정인 session이면 `None`이다. 계정을 다른 Apple identity에 붙이지 않는다
    """
    if not token:
        return None
    session = auth_store.session_by_token_hash(sha256_hex(token))
    if session is None or not session.is_valid():
        return None
    user = auth_store.user(session.user_id)
    if user is None or not user.is_guest:
        return None
    return user


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
