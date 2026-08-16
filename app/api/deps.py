"""FastAPI dependency.

verifier / store를 app.state에 담아두고 여기서 꺼낸다. DI container를 만들지 않는다.

Firestore client는 **처음 auth 요청 때** 만든다 — /health가 Firestore에 의존하면
credential이 없는 곳에서 container가 죽었다고 잘못 판정된다.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.ads.service import RewardedAdService
from app.auth.apple import AppleTokenVerifier
from app.auth.models import User, sha256_hex
from app.auth.store import AuthStore, StoreUnavailable
from app.shards.service import ShardLedgerService

logger = logging.getLogger(__name__)

UNAUTHENTICATED = "인증이 필요해요."
UNAVAILABLE = "지금은 로그인할 수 없어요. 잠시 뒤 다시 시도해 주세요."


def verifier(request: Request) -> AppleTokenVerifier:
    try:
        return request.app.state.apple_verifier()
    except ValueError as error:
        # APPLE_CLIENT_ID가 없다. 설정 문제이므로 client 잘못이 아니다.
        logger.error("apple_verifier_unavailable")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from error


def store(request: Request) -> AuthStore:
    try:
        return request.app.state.auth_store()
    except Exception as error:  # Firestore client 생성 실패 (credential / network)
        logger.error("auth_store_unavailable error=%s", type(error).__name__)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from error


def shard_service(request: Request) -> ShardLedgerService:
    """조각 원장 서비스. store와 같은 이유로 처음 쓰일 때 만든다."""
    try:
        return request.app.state.shard_service()
    except Exception as error:  # Firestore client 생성 실패 (credential / network)
        logger.error("shard_service_unavailable error=%s", type(error).__name__)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from error


def rewarded_ad_service(request: Request) -> RewardedAdService:
    """광고 보상 service. store와 같은 이유로 처음 쓰일 때 만든다."""
    try:
        return request.app.state.rewarded_ad_service()
    except Exception as error:  # Firestore client 생성 실패 (credential / network)
        logger.error("rewarded_ad_service_unavailable error=%s", type(error).__name__)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from error


def bearer_token(request: Request) -> str:
    """`Authorization: Bearer <token>`. **header를 로그에 남기지 않는다.**"""
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            UNAUTHENTICATED,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token.strip()


def current_user(
    token: Annotated[str, Depends(bearer_token)],
    auth_store: Annotated[AuthStore, Depends(store)],
) -> User:
    """token → hash → session → 만료/취소 확인 → internal User."""
    try:
        session = auth_store.session_by_token_hash(sha256_hex(token))
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from error

    if session is None or not session.is_valid():
        # 없음 / 만료 / 취소를 구분해서 알려주지 않는다.
        logger.info("session_rejected")
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            UNAUTHENTICATED,
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        user = auth_store.user(session.user_id)
    except StoreUnavailable as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from error

    if user is None:
        logger.warning("session_without_user")
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            UNAUTHENTICATED,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


CurrentUser = Annotated[User, Depends(current_user)]
