"""reward context의 Firestore 구현.

`ggumirror_` prefix 규칙을 따르고, session과 같은 방식으로 **hash만** 저장한다.
"""

from __future__ import annotations

import logging

from google.api_core import exceptions as gcp_exceptions
from google.cloud import firestore

from app.ads.store import RewardContext
from app.auth.store import StoreUnavailable
from app.shards.models import utcnow

logger = logging.getLogger(__name__)

CONTEXTS = "ggumirror_reward_contexts"


class FirestoreRewardContextStore:
    def __init__(self, client: firestore.Client) -> None:
        self._db = client

    def save(self, context: RewardContext) -> None:
        try:
            self._db.collection(CONTEXTS).document(context.token_hash).set(
                {
                    "userId": context.user_id,
                    "createdAt": context.created_at,
                    "expiresAt": context.expires_at,
                }
            )
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("reward_context_save", error) from error

    def context(self, token_hash: str) -> RewardContext | None:
        """만료 여부로 거르지 않는다 — 부르는 쪽이 판단한다(로그에서 구분하기 위해)."""
        try:
            snapshot = self._db.collection(CONTEXTS).document(token_hash).get()
        except gcp_exceptions.GoogleAPIError as error:
            raise self._unavailable("reward_context_read", error) from error

        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        user_id = str(data.get("userId") or "")
        if not user_id:
            return None
        return RewardContext(
            token_hash=token_hash,
            user_id=user_id,
            # 만료 정보가 없는 문서는 **이미 만료된 것으로** 본다. 열어주지 않는다.
            expires_at=data.get("expiresAt") or utcnow(),
            created_at=data.get("createdAt") or utcnow(),
        )

    def _unavailable(self, operation: str, error: Exception) -> StoreUnavailable:
        # Firestore 오류 문자열에 문서 경로가 들어갈 수 있다. 결과만 남긴다.
        logger.warning("firestore_failed operation=%s error=%s", operation, type(error).__name__)
        return StoreUnavailable(operation)
