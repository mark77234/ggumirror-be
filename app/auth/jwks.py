"""Apple public key(JWKS) 조회 + process-memory cache.

HTTP를 검증 logic에서 떼어 놓는 이유는 하나다 — test에서 Apple을 부르지 않기 위해서다.
`fetch`를 넣어주면 network가 없어도 검증 전체를 시험할 수 있다. DI framework는 쓰지 않는다.

외부 cache(Redis 등)를 쓰지 않는다. key는 몇 개뿐이고 process마다 들고 있어도 된다.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Callable

import jwt

from app.auth.errors import AppleTokenError, AppleTokenReason

logger = logging.getLogger(__name__)

APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"

JWKSFetch = Callable[[], dict]


def http_jwks_fetch(url: str = APPLE_JWKS_URL, timeout: float = 3.0) -> JWKSFetch:
    """stdlib urllib. httpx를 runtime dependency로 올리지 않았다 — 요청이 이거 하나다.

    timeout은 명시한다. Apple이 응답하지 않을 때 Cloud Run worker가
    영원히 매달려 있으면 instance 하나가 통째로 죽는다.
    """

    def fetch() -> dict:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read())

    return fetch


class AppleJWKSProvider:
    """kid로 key를 찾는다. 못 찾으면 한 번 갱신하고 다시 찾는다.

    첫 번째 key를 그냥 쓰지 않는다 — Apple은 여러 key를 동시에 게시하고,
    아무 key로나 검증하면 rotation 중에 엉뚱한 결과가 나온다.
    """

    def __init__(
        self,
        fetch: JWKSFetch | None = None,
        ttl_seconds: float = 600.0,
        # 알 수 없는 kid가 쏟아질 때 Apple을 두드리는 간격의 하한.
        # 이게 없으면 임의의 kid를 넣은 요청이 그대로 Apple 호출로 증폭된다.
        # ponytail: 전역 하나로 충분하다. per-kid backoff는 필요해지면.
        min_refresh_interval: float = 60.0,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch or http_jwks_fetch()
        self._ttl = ttl_seconds
        self._min_refresh_interval = min_refresh_interval
        self._now = now
        self._keys: dict[str, jwt.PyJWK] = {}
        self._loaded_at: float | None = None
        self._attempted_at: float | None = None
        self._rotation_checked_at: float | None = None

    def key_for(self, kid: str) -> jwt.PyJWK:
        if self._is_stale() and self._may_attempt():
            self._refresh()

        key = self._keys.get(kid)
        if key is not None:
            return key

        # key rotation 직후일 수 있다. 창(window)당 한 번만 더 확인한다.
        if self._may_check_rotation():
            self._rotation_checked_at = self._now()
            self._refresh()
            key = self._keys.get(kid)
            if key is not None:
                return key

        logger.warning("apple_jwks_unknown_kid")
        raise AppleTokenError(AppleTokenReason.UNKNOWN_KID)

    def _is_stale(self) -> bool:
        return self._loaded_at is None or self._now() - self._loaded_at >= self._ttl

    def _may_attempt(self) -> bool:
        """Apple 장애 중에 매 요청마다 재시도하지 않는다."""
        return self._attempted_at is None or self._now() - self._attempted_at >= self._min_refresh_interval

    def _may_check_rotation(self) -> bool:
        last = self._rotation_checked_at
        return last is None or self._now() - last >= self._min_refresh_interval

    def _refresh(self) -> None:
        self._attempted_at = self._now()
        try:
            document = self._fetch()
            keys = {
                key.key_id: key
                for key in jwt.PyJWKSet.from_dict(document).keys
                if key.key_id
            }
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, jwt.PyJWKSetError) as error:
            # 갱신 실패가 process를 죽이지 않는다. 아직 cache가 있으면 그걸로 버틴다.
            logger.warning("apple_jwks_fetch_failed error=%s", type(error).__name__)
            if self._keys:
                return
            raise AppleTokenError(AppleTokenReason.JWKS_UNAVAILABLE) from error

        if not keys:
            logger.warning("apple_jwks_empty")
            if not self._keys:
                raise AppleTokenError(AppleTokenReason.JWKS_UNAVAILABLE)
            return

        self._keys = keys
        self._loaded_at = self._now()
        logger.info("apple_jwks_refreshed keys=%d", len(keys))
