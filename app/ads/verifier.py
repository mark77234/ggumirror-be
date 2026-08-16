"""Google AdMob SSV callback 서명 검증.

**여기가 이 기능의 authentication이다.** endpoint에 Bearer가 없는 이유는
Google이 우리 세션을 갖고 있지 않기 때문이고, 대신 Google의 ECDSA 서명이
"이 요청은 진짜 Google이 보냈다"를 증명한다.

## raw query bytes를 그대로 쓴다

Google은 **자기가 보낸 query string의 바이트열**에 서명한다.
그래서 검증 입력은 ASGI scope의 `query_string`(**bytes**) 그대로다.

dict로 재조립하는 것은 물론이고, `Request.url`처럼 **URL 객체를 거쳐 재구성한 문자열도
쓰지 않는다** — URL 재구성은 parsing과 재직렬화를 한 번 거치는 경로라
언젠가 정규화가 끼어들 여지가 있다. 검증 대상은 "받은 바이트" 하나여야 한다.

decode는 **검증에 성공한 뒤** 값을 해석할 때만 한다.

규칙(Google 문서): query의 마지막 두 parameter는 **항상** `signature`와 `key_id`이고,
그 앞의 전부가 서명된 내용이다.

    ...&reward_item=shard&timestamp=1700000000000&signature=<sig>&key_id=<id>
    ^-------------- 서명 대상 --------------^

decode도 정렬도 재인코딩도 하지 않는다. **검증에 성공한 뒤에야** 값을 해석한다.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicKey

from app.ads.models import RewardedAdError, RewardedAdReason, VerifiedRewardCallback

logger = logging.getLogger(__name__)

ADMOB_KEY_URL = "https://www.gstatic.com/admob/reward/verifier-keys.json"

KeyFetch = Callable[[], dict]

SIGNATURE_DELIMITER = b"&signature="


def http_key_fetch(url: str = ADMOB_KEY_URL, timeout: float = 3.0) -> KeyFetch:
    """stdlib urllib. Apple JWKS와 같은 이유로 httpx를 runtime dependency로 올리지 않는다.

    timeout을 명시한다 — Google이 응답하지 않을 때 Cloud Run worker가 매달리면
    instance 하나가 통째로 죽는다.
    """

    def fetch() -> dict:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read())

    return fetch


class AdMobKeyProvider:
    """Google 공개키 조회 + process-memory cache.

    `AppleJWKSProvider`와 같은 모양이다 — key rotation을 만나면 한 번 갱신하고 다시 찾되,
    임의의 key_id가 쏟아져도 Google 호출로 증폭되지 않게 갱신 간격에 하한을 둔다.
    """

    def __init__(
        self,
        fetch: KeyFetch | None = None,
        # Google key rotation을 고려해 하루보다 길게 신뢰하지 않는다.
        ttl_seconds: float = 3600.0,
        min_refresh_interval: float = 60.0,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch or http_key_fetch()
        self._ttl = ttl_seconds
        self._min_refresh_interval = min_refresh_interval
        self._now = now
        self._keys: dict[str, EllipticCurvePublicKey] = {}
        self._loaded_at: float | None = None
        self._attempted_at: float | None = None
        self._rotation_checked_at: float | None = None

    def key_for(self, key_id: str) -> EllipticCurvePublicKey:
        if self._is_stale() and self._may_attempt():
            self._refresh()

        if key := self._keys.get(key_id):
            return key

        # rotation 직후일 수 있다. 창(window)당 한 번만 더 확인한다.
        if self._may_check_rotation():
            self._rotation_checked_at = self._now()
            self._refresh()
            if key := self._keys.get(key_id):
                return key

        logger.warning("admob_ssv_unknown_key_id")
        raise RewardedAdError(RewardedAdReason.UNKNOWN_KEY_ID)

    def _is_stale(self) -> bool:
        return self._loaded_at is None or self._now() - self._loaded_at >= self._ttl

    def _may_attempt(self) -> bool:
        return self._attempted_at is None or self._now() - self._attempted_at >= self._min_refresh_interval

    def _may_check_rotation(self) -> bool:
        last = self._rotation_checked_at
        return last is None or self._now() - last >= self._min_refresh_interval

    def _refresh(self) -> None:
        self._attempted_at = self._now()
        try:
            document = self._fetch()
            keys = _parse_keys(document)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as error:
            # 갱신 실패가 검증 실패와 **같은 값으로 취급되면 안 된다.**
            # 조회를 못 한 것은 Google에게 재시도를 받아야 하는 상황이다.
            logger.warning("admob_ssv_key_fetch_failed error=%s", type(error).__name__)
            if self._keys:
                return
            raise RewardedAdError(RewardedAdReason.KEYS_UNAVAILABLE) from error

        if not keys:
            logger.warning("admob_ssv_keys_empty")
            if not self._keys:
                raise RewardedAdError(RewardedAdReason.KEYS_UNAVAILABLE)
            return

        self._keys = keys
        self._loaded_at = self._now()
        logger.info("admob_ssv_keys_refreshed keys=%d", len(keys))


def _parse_keys(document: dict) -> dict[str, EllipticCurvePublicKey]:
    """`{"keys":[{"keyId":123,"pem":"-----BEGIN PUBLIC KEY-----…"}]}`.

    `keyId`는 JSON에서 숫자지만 query에는 문자열로 온다. 문자열로 통일해 둔다.
    """
    keys: dict[str, EllipticCurvePublicKey] = {}
    for entry in document.get("keys") or []:
        key_id = entry.get("keyId")
        pem = entry.get("pem")
        if key_id is None or not pem:
            continue
        try:
            public_key = serialization.load_pem_public_key(pem.encode())
        except (ValueError, TypeError):
            # 하나가 깨졌다고 나머지 key까지 버리지 않는다.
            logger.warning("admob_ssv_key_unreadable")
            continue
        if isinstance(public_key, EllipticCurvePublicKey):
            keys[str(key_id)] = public_key
    return keys


def signed_content(raw_query: bytes) -> bytes:
    """서명 대상 = `signature` parameter **앞의 전부**, **바이트 그대로**.

    Google이 보낸 바이트열을 자르기만 한다. decode도 재조립도 하지 않는다.
    """
    marker = raw_query.find(SIGNATURE_DELIMITER)
    if marker < 0:
        # `signature`가 첫 parameter인 경우는 Google 규약상 없다.
        raise RewardedAdError(RewardedAdReason.MISSING_SIGNATURE)
    return raw_query[:marker]


def verify(raw_query: bytes, keys: AdMobKeyProvider) -> VerifiedRewardCallback:
    """서명을 검증하고, **성공한 경우에만** 값을 해석해서 돌려준다.

    입력은 ASGI `scope["query_string"]` 그대로의 **bytes**다.
    """
    if not isinstance(raw_query, bytes):
        # 문자열이 흘러들어오면 어디선가 재구성을 거쳤다는 뜻이다. 조용히 받아주지 않는다.
        raise TypeError("raw_query must be the raw ASGI query_string bytes")

    content = signed_content(raw_query)

    # signature · key_id는 **서명 대상이 아니므로** 여기서 해석해도 안전하다.
    tail = _fields(raw_query[len(content):].lstrip(b"&"))
    signature = tail.get("signature")
    key_id = tail.get("key_id")
    if not signature:
        raise RewardedAdError(RewardedAdReason.MISSING_SIGNATURE)
    if not key_id:
        raise RewardedAdError(RewardedAdReason.MISSING_KEY_ID)

    try:
        # Google은 web-safe base64로 보낸다. padding이 빠져 있을 수 있다.
        der = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    except (binascii.Error, ValueError) as error:
        raise RewardedAdError(RewardedAdReason.MALFORMED_SIGNATURE) from error
    if not der:
        raise RewardedAdError(RewardedAdReason.MALFORMED_SIGNATURE)

    public_key = keys.key_for(key_id)
    try:
        # **받은 바이트 그대로** 검증한다.
        public_key.verify(der, content, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as error:
        logger.warning("admob_ssv_verification_failed key_id=%s", key_id)
        raise RewardedAdError(RewardedAdReason.INVALID_SIGNATURE) from error
    except (ValueError, TypeError) as error:
        raise RewardedAdError(RewardedAdReason.MALFORMED_SIGNATURE) from error

    # 검증을 통과했으니 이제 값을 해석해도 된다.
    return _callback(_fields(content))


def _fields(raw: bytes) -> dict[str, str]:
    """검증 **후**(또는 서명 대상 밖) 값 해석용.

    `parse_qsl`에 bytes를 주면 percent decoding과 `+` → 공백 변환을 해서 돌려준다.
    이 결과는 **의미 해석에만** 쓰고, 서명 검증에는 절대 쓰지 않는다.
    """
    return {
        key.decode("utf-8", "replace"): value.decode("utf-8", "replace")
        for key, value in urllib.parse.parse_qsl(raw, keep_blank_values=True)
    }


def _callback(fields: dict[str, str]) -> VerifiedRewardCallback:
    transaction_id = fields.get("transaction_id", "").strip()
    ad_unit = fields.get("ad_unit", "").strip()
    reward_item = fields.get("reward_item", "").strip()
    # **custom_data는 필수가 아니다.** SSV Test Tool은 사용자 없이 호출하고
    # 그때도 서명은 유효하다. 없는 것은 형식 오류가 아니라 "줄 사람이 없다"는 뜻이고,
    # 그 판단은 service가 한다(로그에서 구분할 수 있어야 한다).
    custom_data = fields.get("custom_data", "").strip()
    if not transaction_id or not ad_unit or not reward_item:
        raise RewardedAdError(RewardedAdReason.MISSING_FIELD)

    try:
        reward_amount = int(fields["reward_amount"])
    except (KeyError, ValueError) as error:
        raise RewardedAdError(RewardedAdReason.MISSING_FIELD) from error

    # Google `timestamp`는 epoch **milliseconds**다. 이 값으로 보상 날짜가 정해지므로
    # 서명 검증을 통과한 뒤에만 읽는다.
    try:
        milliseconds = int(fields["timestamp"])
        timestamp = datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
    except (KeyError, ValueError, OverflowError, OSError) as error:
        raise RewardedAdError(RewardedAdReason.INVALID_TIMESTAMP) from error

    return VerifiedRewardCallback(
        transaction_id=transaction_id,
        ad_unit=ad_unit,
        reward_amount=reward_amount,
        reward_item=reward_item,
        timestamp=timestamp,
        custom_data=custom_data,
    )
