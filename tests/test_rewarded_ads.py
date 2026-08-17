"""AdMob Rewarded + Server-Side Verification.

여기서 지키는 것:

1. **Google 서명만이 보상 권위다** — 서명 없이는 어떤 경로로도 조각이 늘지 않는다
2. Google이 서명한 **원본 query 바이트**를 검증한다 — 재조립한 문자열이 아니다
3. 서명이 맞아도 **우리 제품의 보상이 아니면** 지급하지 않는다
4. 하루 5회 상한과 중복 방지가 **지급과 같은 transaction**에서 원자적으로 걸린다
5. 보상 날짜는 **서명된 timestamp**로 정한다 — callback이 늦게 와도 그 날의 몫이다

실제 Google private key는 존재하지 않는다. test용 EC key pair를 실행 중에 만든다.
"""

from __future__ import annotations

import base64
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from app.ads.models import (
    DAILY_REWARD_LIMIT,
    RewardedAdError,
    RewardedAdReason,
)
from app.ads.service import RewardedAdConfig, RewardedAdService
from app.ads.store import InMemoryRewardContextStore, new_context
from app.ads.verifier import AdMobKeyProvider
from app.auth.models import sha256_hex
from app.auth.store import InMemoryAuthStore
from app.core.config import Settings
from app.main import create_app
from app.shards.models import ShardReason
from app.shards.service import ShardLedgerService
from app.shards.store import InMemoryShardStore
from tests.conftest import CLIENT_ID, apple_claims

USER = "internal-user-1"
OTHER = "internal-user-2"

AD_UNIT = "ca-app-pub-0000000000000000/1111111111"
REWARD_ITEM = "mirror_shard"

KST = timezone(timedelta(hours=9))


# MARK: - Google을 흉내 내는 서명 도구


class FakeAdMobKey:
    """AdMob verifier key 하나. Google과 같은 방식으로 query에 서명한다."""

    def __init__(self, key_id: str = "3335741209") -> None:
        self.key_id = key_id
        self._private = ec.generate_private_key(ec.SECP256R1())

    @property
    def document(self) -> dict:
        pem = self._private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        # Google은 keyId를 **숫자**로 보낸다. query에는 문자열로 온다.
        return {"keys": [{"keyId": int(self.key_id), "pem": pem, "base64": "unused"}]}

    def sign(self, content: str) -> str:
        signature = self._private.sign(content.encode(), ec.ECDSA(hashes.SHA256()))
        return base64.urlsafe_b64encode(signature).decode().rstrip("=")

    def callback_query(self, content: str) -> str:
        """Google 규약: 마지막 두 parameter는 항상 `signature`, `key_id` 순서다."""
        return f"{content}&signature={self.sign(content)}&key_id={self.key_id}"


def content_for(
    transaction_id: str = "txn-1",
    custom_data: str = "context-token",
    timestamp: datetime | None = None,
    ad_unit: str = AD_UNIT,
    reward_amount: int = 1,
    reward_item: str = REWARD_ITEM,
) -> str:
    """Google이 실제로 보내는 순서를 흉내 낸다(알파벳 순이 아니다).

    기본 timestamp는 고정 날짜다 — 대부분의 test가 그 날짜로 상태를 직접 조회하므로
    결정적이어야 한다. 다만 **서버의 "오늘"을 쓰는 경로**(HTTP `GET /users/me/rewarded-ads`)를
    지나는 test는 이 기본값을 쓰면 자정을 넘기는 순간 깨진다 — 그쪽은 `timestamp=today_kst()`를
    명시적으로 넘긴다.
    """
    moment = timestamp or datetime(2026, 8, 16, 10, 0, tzinfo=KST)
    milliseconds = int(moment.timestamp() * 1000)
    return (
        f"ad_network=5450213213286189855"
        f"&ad_unit={ad_unit}"
        f"&custom_data={urllib.parse.quote(custom_data, safe='')}"
        f"&reward_amount={reward_amount}"
        f"&reward_item={urllib.parse.quote(reward_item, safe='')}"
        f"&timestamp={milliseconds}"
        f"&transaction_id={transaction_id}"
    )


def call(ads: RewardedAdService, query: str):
    """service는 **ASGI raw bytes**만 받는다.

    test는 문자열로 조립하는 편이 읽기 쉬우므로 경계에서 한 번만 bytes로 바꾼다.
    (문자열을 그대로 넘기면 verifier가 `TypeError`로 거절한다 — 그것도 test로 고정한다.)
    """
    return ads.handle_callback(query.encode())


@pytest.fixture
def google() -> FakeAdMobKey:
    return FakeAdMobKey()


@pytest.fixture
def keys(google: FakeAdMobKey) -> AdMobKeyProvider:
    return AdMobKeyProvider(fetch=lambda: google.document)


@pytest.fixture
def shard_store() -> InMemoryShardStore:
    return InMemoryShardStore()


@pytest.fixture
def contexts() -> InMemoryRewardContextStore:
    store = InMemoryRewardContextStore()
    store.save(new_context(USER, "context-token"))
    store.save(new_context(OTHER, "other-token"))
    return store


@pytest.fixture
def ads(shard_store, contexts, keys) -> RewardedAdService:
    return RewardedAdService(
        shards=ShardLedgerService(shard_store),
        contexts=contexts,
        keys=keys,
        config=RewardedAdConfig(ad_unit=AD_UNIT, reward_item=REWARD_ITEM, reward_amount=1),
    )


# MARK: - 서명 검증


def test_valid_signature_is_rewarded(ads, google, shard_store):
    outcome = call(ads, google.callback_query(content_for()))

    assert outcome.granted is True
    assert shard_store.wallet(USER).balance == 1
    assert shard_store.entries[0].reason == ShardReason.REWARDED_AD


def test_one_byte_signature_mutation_is_rejected(ads, google, shard_store):
    query = google.callback_query(content_for())
    head, _, tail = query.partition("&signature=")
    signature, _, key_part = tail.partition("&key_id=")
    # 한 글자만 바꾼다.
    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
    broken = f"{head}&signature={flipped}&key_id={key_part}"

    with pytest.raises(RewardedAdError) as error:
        call(ads, broken)

    assert error.value.reason in {
        RewardedAdReason.INVALID_SIGNATURE,
        RewardedAdReason.MALFORMED_SIGNATURE,
    }
    assert shard_store.entries == []


def test_signed_content_mutation_is_rejected(ads, google, shard_store):
    """서명은 그대로 두고 내용만 바꾼다 — 보상을 부풀리려는 전형적인 시도."""
    signed = content_for(reward_amount=1)
    query = google.callback_query(signed).replace("reward_amount=1", "reward_amount=999")

    with pytest.raises(RewardedAdError) as error:
        call(ads, query)

    assert error.value.reason is RewardedAdReason.INVALID_SIGNATURE
    assert shard_store.entries == []


def test_transaction_id_swap_is_rejected(ads, google, shard_store):
    """정상 callback 하나를 잡아 transaction_id만 바꿔 재사용할 수 없다."""
    query = google.callback_query(content_for(transaction_id="txn-1"))
    forged = query.replace("transaction_id=txn-1", "transaction_id=txn-2")

    with pytest.raises(RewardedAdError):
        call(ads, forged)
    assert shard_store.entries == []


def test_custom_data_swap_to_another_user_is_rejected(ads, google, shard_store):
    """남의 보상을 내 계정으로 돌릴 수 없다 — custom_data도 서명 대상이다."""
    query = google.callback_query(content_for(custom_data="context-token"))
    forged = query.replace("custom_data=context-token", "custom_data=other-token")

    with pytest.raises(RewardedAdError):
        call(ads, forged)
    assert shard_store.wallet(OTHER).balance == 0


def test_missing_signature_is_rejected(ads):
    with pytest.raises(RewardedAdError) as error:
        call(ads, content_for())
    assert error.value.reason is RewardedAdReason.MISSING_SIGNATURE


def test_missing_key_id_is_rejected(ads, google):
    content = content_for()
    with pytest.raises(RewardedAdError) as error:
        call(ads, f"{content}&signature={google.sign(content)}")
    assert error.value.reason is RewardedAdReason.MISSING_KEY_ID


def test_malformed_signature_is_rejected(ads):
    content = content_for()
    with pytest.raises(RewardedAdError) as error:
        call(ads, f"{content}&signature=!!!not-base64!!!&key_id=1")
    assert error.value.reason in {
        RewardedAdReason.MALFORMED_SIGNATURE,
        RewardedAdReason.UNKNOWN_KEY_ID,
    }


def test_empty_signature_is_rejected(ads):
    content = content_for()
    with pytest.raises(RewardedAdError):
        call(ads, f"{content}&signature=&key_id=3335741209")


def test_unknown_key_id_refreshes_once_then_rejects(google, shard_store, contexts):
    """모르는 key_id면 **한 번 갱신**하고 다시 찾는다. rotation 대응이다."""
    fetches = 0

    def fetch() -> dict:
        nonlocal fetches
        fetches += 1
        return google.document

    provider = AdMobKeyProvider(fetch=fetch)
    ads = RewardedAdService(
        shards=ShardLedgerService(shard_store),
        contexts=contexts,
        keys=provider,
        config=RewardedAdConfig(ad_unit=AD_UNIT, reward_item=REWARD_ITEM),
    )

    content = content_for()
    query = f"{content}&signature={google.sign(content)}&key_id=999999"

    with pytest.raises(RewardedAdError) as error:
        call(ads, query)

    assert error.value.reason is RewardedAdReason.UNKNOWN_KEY_ID
    # 최초 적재 + rotation 확인 1회.
    assert fetches == 2


def test_rotated_key_is_found_after_refresh(shard_store, contexts):
    """갱신하면 새 key로 검증에 성공한다."""
    old, new = FakeAdMobKey("111"), FakeAdMobKey("222")
    published = [old.document]

    provider = AdMobKeyProvider(fetch=lambda: published[0])
    ads = RewardedAdService(
        shards=ShardLedgerService(shard_store),
        contexts=contexts,
        keys=provider,
        config=RewardedAdConfig(ad_unit=AD_UNIT, reward_item=REWARD_ITEM),
    )

    # 먼저 옛 key를 cache에 올린다.
    call(ads, old.callback_query(content_for(transaction_id="txn-old")))

    # Google이 key를 교체했다.
    published[0] = new.document
    assert call(ads, new.callback_query(content_for(transaction_id="txn-new"))).granted


def test_key_fetch_failure_is_not_a_signature_failure(shard_store, contexts, google):
    """조회 실패는 **재시도 가능한 실패**다. 서명이 틀린 것과 구분한다."""

    def broken_fetch() -> dict:
        raise OSError("network down")

    provider = AdMobKeyProvider(fetch=broken_fetch)
    ads = RewardedAdService(
        shards=ShardLedgerService(shard_store),
        contexts=contexts,
        keys=provider,
        config=RewardedAdConfig(ad_unit=AD_UNIT, reward_item=REWARD_ITEM),
    )

    with pytest.raises(RewardedAdError) as error:
        call(ads, google.callback_query(content_for()))

    assert error.value.reason is RewardedAdReason.KEYS_UNAVAILABLE
    assert error.value.is_upstream_failure is True


# MARK: - raw query를 그대로 쓴다


def test_parameter_order_is_preserved(ads, google, shard_store):
    """Google이 보낸 **순서 그대로** 검증한다.

    정렬하거나 dict로 재조립하면 서명이 깨진다. 순서를 바꾼 query는 통과하면 안 된다.
    """
    content = content_for()
    query = google.callback_query(content)
    assert call(ads, query).granted is True

    # 같은 값, 다른 순서 → 다른 바이트열 → 검증 실패.
    fields = urllib.parse.parse_qsl(content)
    reordered = urllib.parse.urlencode(sorted(fields))
    assert reordered != content, "이 test가 의미를 가지려면 순서가 실제로 달라야 한다"
    scrambled = f"{reordered}&signature={google.sign(content)}&key_id={google.key_id}"

    with pytest.raises(RewardedAdError) as error:
        call(ads, scrambled)
    assert error.value.reason is RewardedAdReason.INVALID_SIGNATURE


def test_percent_encoding_is_preserved(ads, google, shard_store):
    """encoding이 살짝 달라져도 서명은 깨진다 — 그래서 재인코딩하지 않는다."""
    content = content_for(custom_data="context-token")
    query = google.callback_query(content)
    assert call(ads, query).granted is True

    # `-`를 percent encoding으로 바꾼다. 값은 같지만 바이트열이 다르다.
    tampered = query.replace("custom_data=context-token", "custom_data=context%2Dtoken")
    with pytest.raises(RewardedAdError) as error:
        call(ads, tampered)
    assert error.value.reason is RewardedAdReason.INVALID_SIGNATURE


def test_verification_input_must_be_raw_bytes(ads, google):
    """검증 입력은 **ASGI raw bytes**여야 한다.

    문자열이 들어온다는 것은 어디선가 URL 객체나 dict를 거쳐 재구성했다는 뜻이다.
    조용히 encode해서 받아주면 그 경로가 굳어버리므로 여기서 막는다.
    """
    with pytest.raises(TypeError):
        ads.handle_callback(google.callback_query(content_for()))


def test_plus_and_percent20_are_not_interchangeable(ads, google, shard_store):
    """`+`와 `%20`은 decode하면 같은 값이지만 **서명 대상 바이트는 다르다.**

    parse 후 재인코딩하는 구현이면 둘 중 하나가 통과해버린다.
    """
    content = content_for(custom_data="context token")  # quote → `context%20token`
    assert "%20" in content

    swapped = content.replace("%20", "+")
    assert urllib.parse.parse_qs(swapped)["custom_data"] == ["context token"]

    # 서명은 원본(`%20`)에 대해 만들고, 보내는 것은 `+` 버전이다.
    forged = f"{swapped}&signature={google.sign(content)}&key_id={google.key_id}"

    with pytest.raises(RewardedAdError) as error:
        call(ads, forged)
    assert error.value.reason is RewardedAdReason.INVALID_SIGNATURE
    assert shard_store.entries == []


def test_single_byte_change_anywhere_breaks_verification(ads, google, shard_store):
    """서명 대상 바이트 **한 개**만 바뀌어도 검증에 실패한다."""
    content = content_for()
    signature = google.sign(content)

    for index in range(0, len(content), 7):  # 전부 돌 필요는 없다
        mutated = list(content)
        mutated[index] = "0" if content[index] != "0" else "1"
        forged = f"{''.join(mutated)}&signature={signature}&key_id={google.key_id}"
        with pytest.raises(RewardedAdError):
            call(ads, forged)

    assert shard_store.entries == []


def test_signed_content_excludes_signature_and_key_id(google):
    """서명 대상은 `signature` **앞의 전부**다."""
    from app.ads.verifier import signed_content

    content = content_for()
    query = google.callback_query(content)
    assert signed_content(query.encode()) == content.encode()
    assert b"signature=" not in signed_content(query.encode())
    assert b"key_id=" not in signed_content(query.encode())


# MARK: - 제품 설정 대조 (서명만 맞다고 지급하지 않는다)


def test_wrong_ad_unit_is_not_rewarded(ads, google, shard_store):
    with pytest.raises(RewardedAdError) as error:
        call(ads, google.callback_query(content_for(ad_unit="ca-app-pub-9999/8888")))
    assert error.value.reason is RewardedAdReason.UNEXPECTED_AD_UNIT
    assert shard_store.entries == []


def test_wrong_reward_amount_is_not_rewarded(ads, google, shard_store):
    with pytest.raises(RewardedAdError) as error:
        call(ads, google.callback_query(content_for(reward_amount=100)))
    assert error.value.reason is RewardedAdReason.UNEXPECTED_REWARD
    assert shard_store.entries == []


def test_wrong_reward_item_is_not_rewarded(ads, google, shard_store):
    with pytest.raises(RewardedAdError) as error:
        call(ads, google.callback_query(content_for(reward_item="gold")))
    assert error.value.reason is RewardedAdReason.UNEXPECTED_REWARD
    assert shard_store.entries == []


def test_missing_transaction_id_is_not_rewarded(ads, google, shard_store):
    with pytest.raises(RewardedAdError) as error:
        call(ads, google.callback_query(content_for(transaction_id="")))
    assert error.value.reason is RewardedAdReason.MISSING_FIELD
    assert shard_store.entries == []


def test_invalid_timestamp_is_not_rewarded(ads, google, shard_store):
    content = content_for().replace("&timestamp=", "&timestamp=", 1)
    content = "&".join(
        part if not part.startswith("timestamp=") else "timestamp=not-a-number"
        for part in content.split("&")
    )
    with pytest.raises(RewardedAdError) as error:
        call(ads, google.callback_query(content))
    assert error.value.reason is RewardedAdReason.INVALID_TIMESTAMP
    assert shard_store.entries == []


def test_unknown_context_is_not_rewarded(ads, google, shard_store):
    with pytest.raises(RewardedAdError) as error:
        call(ads, google.callback_query(content_for(custom_data="never-issued")))
    assert error.value.reason is RewardedAdReason.UNKNOWN_CONTEXT
    assert shard_store.entries == []


def test_expired_context_is_not_rewarded(google, shard_store, keys):
    contexts = InMemoryRewardContextStore()
    stale = new_context(USER, "context-token", now=datetime.now(timezone.utc) - timedelta(days=2))
    contexts.save(stale)
    ads = RewardedAdService(
        shards=ShardLedgerService(shard_store),
        contexts=contexts,
        keys=keys,
        config=RewardedAdConfig(ad_unit=AD_UNIT, reward_item=REWARD_ITEM),
    )

    with pytest.raises(RewardedAdError) as error:
        call(ads, google.callback_query(content_for()))
    # "우리가 준 적 없다"와 구분한다 — 만료는 흐름이 너무 느렸다는 뜻이다.
    assert error.value.reason is RewardedAdReason.EXPIRED_CONTEXT
    assert shard_store.entries == []


def test_missing_context_is_distinguished(ads, google, shard_store):
    """context가 아예 없는 것과 우리 것이 아닌 것은 **다른 사건**이다.

    SSV Test Tool은 사용자 없이 호출하므로 정상적으로 `missing_context`가 된다.
    실제 광고에서 이게 보이기 시작하면 client가 context를 안 싣고 있다는 신호다.
    """
    with pytest.raises(RewardedAdError) as error:
        call(ads, google.callback_query(content_for(custom_data="")))

    assert error.value.reason is RewardedAdReason.MISSING_CONTEXT
    assert shard_store.entries == []


def test_unconfigured_ad_unit_never_rewards(google, shard_store, contexts, keys):
    """production ad unit이 아직 없으면 서명이 맞아도 지급하지 않는다(fail closed)."""
    ads = RewardedAdService(
        shards=ShardLedgerService(shard_store),
        contexts=contexts,
        keys=keys,
        config=RewardedAdConfig(),  # 비어 있음
    )

    with pytest.raises(RewardedAdError) as error:
        call(ads, google.callback_query(content_for()))
    assert error.value.reason is RewardedAdReason.NOT_CONFIGURED
    assert shard_store.entries == []


# MARK: - idempotency


def test_same_transaction_ten_times_rewards_once(ads, google, shard_store):
    query = google.callback_query(content_for(transaction_id="txn-repeat"))
    outcomes = [call(ads, query) for _ in range(10)]

    assert sum(1 for o in outcomes if o.granted) == 1
    assert sum(1 for o in outcomes if o.duplicate) == 9
    assert shard_store.wallet(USER).balance == 1
    assert len(shard_store.entries) == 1
    # 재전송이 남은 횟수를 깎지 않는다.
    assert ads.status(USER, datetime(2026, 8, 16, 12, 0, tzinfo=KST)).rewarded_today == 1


# MARK: - 하루 5회


def test_five_per_day_then_no_more(ads, google, shard_store):
    day = datetime(2026, 8, 16, 10, 0, tzinfo=KST)

    for index in range(DAILY_REWARD_LIMIT):
        outcome = call(ads, 
            google.callback_query(content_for(transaction_id=f"txn-{index}", timestamp=day))
        )
        assert outcome.granted is True

    sixth = call(ads, 
        google.callback_query(content_for(transaction_id="txn-6th", timestamp=day))
    )

    assert sixth.granted is False
    assert sixth.limit_reached is True
    assert shard_store.wallet(USER).balance == DAILY_REWARD_LIMIT
    rewarded = [e for e in shard_store.entries if e.reason == ShardReason.REWARDED_AD]
    assert len(rewarded) == DAILY_REWARD_LIMIT

    state = ads.status(USER, day)
    assert (state.rewarded_today, state.remaining_today) == (5, 0)


def test_next_day_resets_the_limit(ads, google, shard_store):
    today = datetime(2026, 8, 16, 10, 0, tzinfo=KST)
    tomorrow = datetime(2026, 8, 17, 10, 0, tzinfo=KST)

    for index in range(DAILY_REWARD_LIMIT):
        call(ads, 
            google.callback_query(content_for(transaction_id=f"a-{index}", timestamp=today))
        )
    assert call(ads, 
        google.callback_query(content_for(transaction_id="b-0", timestamp=tomorrow))
    ).granted is True

    assert shard_store.wallet(USER).balance == DAILY_REWARD_LIMIT + 1
    assert ads.status(USER, tomorrow).rewarded_today == 1


def test_two_users_have_separate_limits(ads, google, shard_store):
    day = datetime(2026, 8, 16, 10, 0, tzinfo=KST)

    for index in range(DAILY_REWARD_LIMIT):
        call(ads, 
            google.callback_query(content_for(transaction_id=f"a-{index}", timestamp=day))
        )
    # USER는 끝났지만 OTHER는 그대로다.
    assert call(ads, 
        google.callback_query(
            content_for(transaction_id="b-0", custom_data="other-token", timestamp=day)
        )
    ).granted is True

    assert shard_store.wallet(USER).balance == DAILY_REWARD_LIMIT
    assert shard_store.wallet(OTHER).balance == 1


# MARK: - 자정 (서명된 timestamp 기준)


def test_reward_day_comes_from_signed_timestamp(ads, google, shard_store):
    """23:59:58에 본 광고가 00:00:02에 도착해도 **전날 몫**이다.

    서버가 받은 시각으로 정하면 자정 근처에 quota가 두 번 열리거나 하루가 통째로 밀린다.
    """
    late_night = datetime(2026, 8, 16, 23, 59, 58, tzinfo=KST)
    just_after = datetime(2026, 8, 17, 0, 0, 0, tzinfo=KST)

    # 전날 이미 4개를 받았다.
    for index in range(4):
        call(ads, 
            google.callback_query(content_for(transaction_id=f"prev-{index}", timestamp=late_night))
        )

    # 자정 직전에 본 5번째 → 전날 quota의 마지막 한 칸.
    assert call(ads, 
        google.callback_query(content_for(transaction_id="prev-5", timestamp=late_night))
    ).granted is True

    # 같은 전날 timestamp로 하나 더 → 전날은 이미 5개다.
    assert call(ads, 
        google.callback_query(content_for(transaction_id="prev-6", timestamp=late_night))
    ).limit_reached is True

    # 자정을 넘긴 timestamp → 새 날의 첫 칸.
    assert call(ads, 
        google.callback_query(content_for(transaction_id="next-1", timestamp=just_after))
    ).granted is True

    assert ads.status(USER, late_night).rewarded_today == 5
    assert ads.status(USER, just_after).rewarded_today == 1


# MARK: - 동시성 (B-5 핵심)


def test_concurrent_callbacks_cannot_exceed_the_daily_cap(ads, google, shard_store):
    """quota가 4일 때 서로 다른 정상 callback 10개가 **동시에** 도착한다.

    "세어보고 5보다 작으면 준다"였다면 열 개가 전부 통과해 하루에 14개를 지급한다.
    확인과 증가가 지급과 같은 transaction에 있어야 정확히 하나만 통과한다.
    """
    day = datetime(2026, 8, 16, 10, 0, tzinfo=KST)
    for index in range(4):
        call(ads, 
            google.callback_query(content_for(transaction_id=f"seed-{index}", timestamp=day))
        )
    assert shard_store.wallet(USER).balance == 4

    queries = [
        google.callback_query(content_for(transaction_id=f"race-{index}", timestamp=day))
        for index in range(10)
    ]
    outcomes: list = [None] * 10
    barrier = threading.Barrier(10)

    def run(index: int) -> None:
        barrier.wait()
        outcomes[index] = call(ads, queries[index])

    threads = [threading.Thread(target=run, args=(index,)) for index in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(1 for o in outcomes if o.granted) == 1, "상한을 넘겨 지급했다"
    assert sum(1 for o in outcomes if o.limit_reached) == 9
    assert shard_store.wallet(USER).balance == DAILY_REWARD_LIMIT
    rewarded = [e for e in shard_store.entries if e.reason == ShardReason.REWARDED_AD]
    assert len(rewarded) == DAILY_REWARD_LIMIT
    assert ads.status(USER, day).remaining_today == 0


def test_concurrent_duplicates_reward_once(ads, google, shard_store):
    """같은 transaction이 동시에 10번 도착해도 한 번만 지급하고 quota도 한 칸만 쓴다."""
    day = datetime(2026, 8, 16, 10, 0, tzinfo=KST)
    query = google.callback_query(content_for(transaction_id="same-txn", timestamp=day))

    outcomes: list = [None] * 10
    barrier = threading.Barrier(10)

    def run(index: int) -> None:
        barrier.wait()
        outcomes[index] = call(ads, query)

    threads = [threading.Thread(target=run, args=(index,)) for index in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(1 for o in outcomes if o.granted) == 1
    assert shard_store.wallet(USER).balance == 1
    assert len(shard_store.entries) == 1
    assert ads.status(USER, day).rewarded_today == 1


# MARK: - HTTP


@pytest.fixture
def client(shard_store, contexts, google, apple_key, jwks_of, monkeypatch) -> TestClient:
    from app.auth import jwks as jwks_module

    document = jwks_of(apple_key)
    monkeypatch.setattr(jwks_module, "http_jwks_fetch", lambda *a, **k: lambda: document)

    app = create_app(
        Settings(
            app_env="local",
            apple_client_id=CLIENT_ID,
            admob_ssv_expected_ad_unit=AD_UNIT,
            admob_reward_item=REWARD_ITEM,
        ),
        auth_store=InMemoryAuthStore(),
        shard_store=shard_store,
        reward_context_store=contexts,
        admob_keys=AdMobKeyProvider(fetch=lambda: google.document),
    )
    return TestClient(app)


def sign_in(client: TestClient, apple_key) -> str:
    nonce = "nonce-abc"
    token = apple_key.token(apple_claims(nonce=sha256_hex(nonce)))
    return client.post("/auth/apple", json={"identityToken": token, "nonce": nonce}).json()[
        "accessToken"
    ]


def test_ssv_endpoint_needs_no_bearer(client, google, shard_store):
    """Google은 우리 세션을 갖고 있지 않다. 인증은 **서명**이 한다."""
    response = client.get(f"/admob/rewarded/ssv?{google.callback_query(content_for())}")
    assert response.status_code == 200
    assert shard_store.wallet(USER).balance == 1


def test_ssv_endpoint_rejects_bad_signature(client, google, shard_store):
    query = google.callback_query(content_for()).replace("reward_amount=1", "reward_amount=5")
    response = client.get(f"/admob/rewarded/ssv?{query}")

    # 재시도해도 결과가 같다 — 5xx로 돌려주면 Google이 영원히 재시도한다.
    assert response.status_code == 400
    assert shard_store.entries == []


def test_ssv_endpoint_without_query_is_rejected(client):
    assert client.get("/admob/rewarded/ssv").status_code == 400


def test_ssv_duplicate_and_cap_are_success(client, google, shard_store):
    """중복과 하루 상한은 **처리 완료(200)**다. Google이 재시도할 이유가 없다."""
    day = datetime(2026, 8, 16, 10, 0, tzinfo=KST)
    first = client.get(
        f"/admob/rewarded/ssv?{google.callback_query(content_for(transaction_id='t1', timestamp=day))}"
    )
    duplicate = client.get(
        f"/admob/rewarded/ssv?{google.callback_query(content_for(transaction_id='t1', timestamp=day))}"
    )
    assert (first.status_code, duplicate.status_code) == (200, 200)
    assert shard_store.wallet(USER).balance == 1

    for index in range(1, DAILY_REWARD_LIMIT):
        client.get(
            f"/admob/rewarded/ssv?"
            f"{google.callback_query(content_for(transaction_id=f'fill-{index}', timestamp=day))}"
        )
    capped = client.get(
        f"/admob/rewarded/ssv?{google.callback_query(content_for(transaction_id='over', timestamp=day))}"
    )
    assert capped.status_code == 200
    assert shard_store.wallet(USER).balance == DAILY_REWARD_LIMIT


def test_key_fetch_failure_asks_google_to_retry(shard_store, contexts, google, apple_key, jwks_of, monkeypatch):
    from app.auth import jwks as jwks_module

    monkeypatch.setattr(jwks_module, "http_jwks_fetch", lambda *a, **k: lambda: jwks_of(apple_key))

    def broken() -> dict:
        raise OSError("network down")

    app = create_app(
        Settings(app_env="local", apple_client_id=CLIENT_ID,
                 admob_ssv_expected_ad_unit=AD_UNIT, admob_reward_item=REWARD_ITEM),
        auth_store=InMemoryAuthStore(),
        shard_store=shard_store,
        reward_context_store=contexts,
        admob_keys=AdMobKeyProvider(fetch=broken),
    )
    client = TestClient(app)

    response = client.get(f"/admob/rewarded/ssv?{google.callback_query(content_for())}")
    # 일시적 실패다 — Google이 다시 보내면 복구된다.
    assert response.status_code == 503
    assert shard_store.entries == []


def test_valid_signature_without_context_is_success_with_no_mutation(client, google, shard_store):
    """**AdMob SSV Test Tool이 이 경로다.** 사용자 context 없이 호출한다.

    서명이 유효하므로 우리가 정상 처리한 것이고, 재시도해도 결과가 같다 → 200.
    조각은 하나도 움직이지 않는다.
    """
    query = google.callback_query(content_for(custom_data=""))
    response = client.get(f"/admob/rewarded/ssv?{query}")

    assert response.status_code == 200
    assert shard_store.entries == []
    assert shard_store.wallets == {}


def test_valid_signature_with_unknown_context_is_success_with_no_mutation(client, google, shard_store):
    query = google.callback_query(content_for(custom_data="never-issued-by-us"))
    response = client.get(f"/admob/rewarded/ssv?{query}")

    assert response.status_code == 200
    assert shard_store.entries == []


def test_valid_signature_with_expired_context_is_success_with_no_mutation(
    shard_store, google, apple_key, jwks_of, monkeypatch
):
    from app.auth import jwks as jwks_module

    monkeypatch.setattr(jwks_module, "http_jwks_fetch", lambda *a, **k: lambda: jwks_of(apple_key))

    contexts = InMemoryRewardContextStore()
    contexts.save(new_context(USER, "context-token", now=datetime.now(timezone.utc) - timedelta(days=2)))

    app = create_app(
        Settings(app_env="local", apple_client_id=CLIENT_ID,
                 admob_ssv_expected_ad_unit=AD_UNIT, admob_reward_item=REWARD_ITEM),
        auth_store=InMemoryAuthStore(),
        shard_store=shard_store,
        reward_context_store=contexts,
        admob_keys=AdMobKeyProvider(fetch=lambda: google.document),
    )
    client = TestClient(app)

    response = client.get(f"/admob/rewarded/ssv?{google.callback_query(content_for())}")

    assert response.status_code == 200
    assert shard_store.entries == []


def test_unconfigured_service_is_success_with_no_mutation(
    shard_store, contexts, google, apple_key, jwks_of, monkeypatch
):
    """ad unit이 아직 없는 지금 상태. 서명이 맞아도 지급하지 않고, 재시도를 받지 않는다."""
    from app.auth import jwks as jwks_module

    monkeypatch.setattr(jwks_module, "http_jwks_fetch", lambda *a, **k: lambda: jwks_of(apple_key))

    app = create_app(
        # admob 설정이 비어 있다 — 배포 직후의 실제 상태다.
        Settings(app_env="local", apple_client_id=CLIENT_ID),
        auth_store=InMemoryAuthStore(),
        shard_store=shard_store,
        reward_context_store=contexts,
        admob_keys=AdMobKeyProvider(fetch=lambda: google.document),
    )
    client = TestClient(app)

    response = client.get(f"/admob/rewarded/ssv?{google.callback_query(content_for())}")

    assert response.status_code == 200
    assert shard_store.entries == []


def test_wrong_ad_unit_is_success_with_no_mutation(client, google, shard_store):
    """서명은 유효하지만 우리 광고가 아니다. 재시도해도 같으므로 200이다."""
    query = google.callback_query(content_for(ad_unit="ca-app-pub-9999/8888"))
    response = client.get(f"/admob/rewarded/ssv?{query}")

    assert response.status_code == 200
    assert shard_store.entries == []


def test_transient_firestore_failure_asks_google_to_retry(
    contexts, google, apple_key, jwks_of, monkeypatch
):
    """저장소 오류는 **일시적**이다. 5xx로 답해서 Google 재시도로 복구한다."""
    from app.auth import jwks as jwks_module
    from app.auth.store import StoreUnavailable

    monkeypatch.setattr(jwks_module, "http_jwks_fetch", lambda *a, **k: lambda: jwks_of(apple_key))

    store = InMemoryShardStore()

    def broken(*args, **kwargs):
        raise StoreUnavailable("ledger_apply")

    store.apply = broken  # type: ignore[method-assign]

    app = create_app(
        Settings(app_env="local", apple_client_id=CLIENT_ID,
                 admob_ssv_expected_ad_unit=AD_UNIT, admob_reward_item=REWARD_ITEM),
        auth_store=InMemoryAuthStore(),
        shard_store=store,
        reward_context_store=contexts,
        admob_keys=AdMobKeyProvider(fetch=lambda: google.document),
    )
    client = TestClient(app)

    response = client.get(f"/admob/rewarded/ssv?{google.callback_query(content_for())}")

    assert response.status_code == 503
    assert store.entries == []


def test_status_requires_authentication(client):
    assert client.get("/users/me/rewarded-ads").status_code == 401
    assert client.post("/users/me/rewarded-ads/context").status_code == 401


def test_status_reports_remaining(client, apple_key, shard_store):
    headers = {"Authorization": f"Bearer {sign_in(client, apple_key)}"}

    body = client.get("/users/me/rewarded-ads", headers=headers).json()
    assert body == {"rewardedToday": 0, "remainingToday": 5, "dailyLimit": 5}


def test_context_endpoint_never_moves_shards(client, apple_key, shard_store):
    headers = {"Authorization": f"Bearer {sign_in(client, apple_key)}"}

    response = client.post("/users/me/rewarded-ads/context", headers=headers)

    assert response.status_code == 200
    context = response.json()["context"]
    assert context, "context가 비어 있다"
    # 조각은 하나도 움직이지 않는다.
    assert shard_store.entries == []
    assert client.get("/users/me/shards", headers=headers).json()["balance"] == 0


def test_context_is_not_the_internal_user_id(client, apple_key):
    headers = {"Authorization": f"Bearer {sign_in(client, apple_key)}"}
    user_id = client.get("/users/me", headers=headers).json()["id"]
    token = sign_in(client, apple_key)

    context = client.post("/users/me/rewarded-ads/context", headers=headers).json()["context"]

    # 내부 UUID도, session token도 그대로 나가지 않는다.
    assert context != user_id
    assert user_id not in context
    assert context != token


def test_issued_context_resolves_to_its_owner(client, apple_key, google, shard_store):
    """발급 → 광고 → SSV까지 실제 경로 한 바퀴."""
    headers = {"Authorization": f"Bearer {sign_in(client, apple_key)}"}
    user_id = client.get("/users/me", headers=headers).json()["id"]
    context = client.post("/users/me/rewarded-ads/context", headers=headers).json()["context"]

    # 상태 조회는 **서버의 오늘**을 본다. 고정 날짜로 지급하면 자정을 넘기는 순간
    # "지급은 어제 몫, 조회는 오늘 몫"이 되어 시계 때문에 깨진다.
    query = google.callback_query(content_for(
        transaction_id="real-1", custom_data=context, timestamp=datetime.now(KST)
    ))
    assert client.get(f"/admob/rewarded/ssv?{query}").status_code == 200

    assert shard_store.wallet(user_id).balance == 1
    assert client.get("/users/me/shards", headers=headers).json()["balance"] == 1
    assert client.get("/users/me/rewarded-ads", headers=headers).json()["rewardedToday"] == 1


def test_no_client_facing_reward_claim_endpoint(client):
    """client가 광고 보상을 직접 청구하는 통로는 없다."""
    paths = {route.path for route in client.app.routes if hasattr(route, "path")}
    for forbidden in [
        "/rewarded/claim", "/users/me/rewarded-ads/claim", "/ads/reward",
        "/shards/credit", "/shards/add",
    ]:
        assert forbidden not in paths

    headers = {"Authorization": "Bearer whatever"}
    for method in ["post", "put", "patch", "delete"]:
        response = getattr(client, method)("/users/me/rewarded-ads", headers=headers)
        assert response.status_code in (401, 404, 405)


# MARK: - 로그


def test_logs_have_no_sensitive_values(client, apple_key, google, caplog):
    import logging

    token = sign_in(client, apple_key)
    headers = {"Authorization": f"Bearer {token}"}
    context = client.post("/users/me/rewarded-ads/context", headers=headers).json()["context"]

    with caplog.at_level(logging.DEBUG):
        query = google.callback_query(
            content_for(transaction_id="secret-transaction", custom_data=context)
        )
        client.get(f"/admob/rewarded/ssv?{query}")

    assert "admob_ssv_reward_applied" in caplog.text
    # raw callback · signature · context · session token · transaction id 전부 남지 않는다.
    for secret in (token, context, "secret-transaction", "signature="):
        assert secret not in caplog.text
    # 경로는 남는다 — callback이 왔다는 사실은 운영에 필요하다.
    assert "/admob/rewarded/ssv?<redacted>" in caplog.text


def test_access_log_redacts_the_callback_query():
    """access log는 우리가 부르는 것이 아니다 — filter로 막는다.

    uvicorn access logger는 요청 줄을 query까지 통째로 남긴다. 그대로 두면
    Google signature와 reward context가 Cloud Run 로그에 적힌다.
    """
    import logging

    from app.core.config import RedactSensitiveQuery

    record = logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname="", lineno=0,
        msg='%s - "%s %s HTTP/%s" %d', args=(
            "1.2.3.4:0", "GET",
            "/admob/rewarded/ssv?custom_data=secret-context&signature=SECRETSIG&key_id=1",
            "1.1", 200,
        ),
        exc_info=None,
    )

    assert RedactSensitiveQuery().filter(record) is True
    rendered = record.getMessage()
    assert "secret-context" not in rendered
    assert "SECRETSIG" not in rendered
    assert "/admob/rewarded/ssv?<redacted>" in rendered


def test_redaction_leaves_other_paths_alone():
    """다른 경로의 access log까지 지우지 않는다 — 운영 가시성을 잃으면 안 된다."""
    import logging

    from app.core.config import RedactSensitiveQuery

    record = logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname="", lineno=0,
        msg="%s", args=("/users/me/shards?userId=someone",), exc_info=None,
    )
    RedactSensitiveQuery().filter(record)
    assert record.getMessage() == "/users/me/shards?userId=someone"


# MARK: - ad unit 진단 로그 (설정값을 확정하기 위한 최소 노출)


def test_unconfigured_logs_observed_ad_unit(shard_store, contexts, google, caplog):
    """설정 전이라도 **검증된 ad_unit 값은** 한 번 봐야 채울 수 있다.

    이 로그가 `ADMOB_SSV_EXPECTED_AD_UNIT`을 확정하는 유일한 근거다 —
    platform request log는 exclusion으로 저장되지 않기 때문이다.
    """
    import logging

    ads = RewardedAdService(
        shards=ShardLedgerService(shard_store),
        contexts=contexts,
        keys=AdMobKeyProvider(fetch=lambda: google.document),
        config=RewardedAdConfig(ad_unit="", reward_item=""),  # 미설정 = 현재 production 상태
    )

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(RewardedAdError) as error:
            call(ads, google.callback_query(content_for(ad_unit="1234567890")))

    assert error.value.reason is RewardedAdReason.NOT_CONFIGURED
    assert "observed_ad_unit=1234567890" in caplog.text
    assert shard_store.entries == []


def test_unexpected_ad_unit_logs_the_observed_value(ads, google, shard_store, caplog):
    import logging

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(RewardedAdError):
            call(ads, google.callback_query(content_for(ad_unit="9999999999")))

    assert "observed_ad_unit=9999999999" in caplog.text
    assert shard_store.entries == []


def test_invalid_signature_never_logs_an_ad_unit(ads, google, shard_store, caplog):
    """**서명이 틀리면 ad_unit을 읽지도 남기지도 않는다.**

    검증 전 값을 로그에 남기면, 아무나 우리 로그에 원하는 문자열을 적을 수 있다.
    """
    import logging

    content = content_for(ad_unit="ATTACKER_CONTROLLED")
    forged = f"{content}&signature=AAAA&key_id={google.key_id}"

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(RewardedAdError):
            call(ads, forged)

    assert "observed_ad_unit" not in caplog.text
    assert "ATTACKER_CONTROLLED" not in caplog.text
    assert shard_store.entries == []


def test_diagnostic_log_never_leaks_credentials(shard_store, contexts, google, caplog):
    """ad_unit 하나만 늘었을 뿐, 나머지는 그대로 로그에 없다."""
    import logging

    ads = RewardedAdService(
        shards=ShardLedgerService(shard_store),
        contexts=contexts,
        keys=AdMobKeyProvider(fetch=lambda: google.document),
        config=RewardedAdConfig(ad_unit="", reward_item=""),
    )
    query = google.callback_query(
        content_for(ad_unit="1234567890", custom_data="secret-context", transaction_id="secret-txn")
    )

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(RewardedAdError):
            call(ads, query)

    assert "observed_ad_unit=1234567890" in caplog.text
    for secret in ("secret-context", "secret-txn", "signature=", "custom_data"):
        assert secret not in caplog.text, f"로그에 {secret}이 새어 나갔다"
