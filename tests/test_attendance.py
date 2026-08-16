"""출석 — 하루 한 번 조각 +1.

여기서 지키는 것:
1. 하루의 기준은 **server 시계의 Asia/Seoul 날짜**다 — UTC도, client 시각도 아니다
2. 같은 사용자 · 같은 KST 날짜는 **정확히 한 번만** 지급된다 (동시 요청 포함)
3. client가 userId · date · amount · reason을 정할 방법이 없다

실제 Firestore에 붙지 않는다 — `InMemoryShardStore`가 transaction의 의미를 흉내 낸다.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.auth.models import sha256_hex
from app.auth.store import InMemoryAuthStore
from app.core.config import Settings
from app.main import create_app
from app.shards import attendance
from app.shards.models import ShardReason, idempotency_hash
from app.shards.service import ShardLedgerService
from app.shards.store import InMemoryShardStore
from tests.conftest import CLIENT_ID, apple_claims

USER = "internal-user-1"
OTHER = "internal-user-2"

KST = timezone(timedelta(hours=9))


@pytest.fixture
def store() -> InMemoryShardStore:
    return InMemoryShardStore()


@pytest.fixture
def shards(store: InMemoryShardStore) -> ShardLedgerService:
    return ShardLedgerService(store)


# MARK: - 하루의 기준은 KST


def test_kst_daytime_is_that_day():
    assert attendance.attendance_date(datetime(2026, 8, 16, 10, 0, tzinfo=KST)) == "2026-08-16"


def test_last_second_of_the_day_is_still_that_day():
    assert attendance.attendance_date(datetime(2026, 8, 16, 23, 59, 59, tzinfo=KST)) == "2026-08-16"


def test_midnight_is_the_next_day():
    assert attendance.attendance_date(datetime(2026, 8, 17, 0, 0, 0, tzinfo=KST)) == "2026-08-17"


def test_uses_kst_not_utc():
    """UTC 날짜와 KST 날짜가 다를 때 **KST를 쓴다.**

    UTC 2026-08-13 15:01 = KST 2026-08-14 00:01 → 출석일은 8월 14일이다.
    UTC로 계산하면 한국 사용자가 자정 직후에 어제 몫을 다시 받거나 못 받는다.
    """
    utc_evening = datetime(2026, 8, 13, 15, 1, tzinfo=timezone.utc)
    assert utc_evening.strftime("%Y-%m-%d") == "2026-08-13"
    assert attendance.attendance_date(utc_evening) == "2026-08-14"

    # 반대 방향: KST 아침은 아직 UTC 전날이다.
    utc_morning = datetime(2026, 8, 16, 1, 0, tzinfo=timezone.utc)
    assert attendance.attendance_date(utc_morning) == "2026-08-16"


def test_naive_datetime_is_rejected():
    """timezone 없는 시각을 조용히 UTC로 가정하지 않는다 — 하루가 통째로 어긋난다."""
    with pytest.raises(ValueError):
        attendance.attendance_date(datetime(2026, 8, 16, 10, 0))


def test_production_uses_server_clock():
    """인자 없이 부르면 server 시계를 쓴다. client가 넘긴 시각을 쓰는 경로가 없다."""
    today = attendance.attendance_date()
    assert attendance.attendance_date(datetime.now(timezone.utc)) == today


# MARK: - 경제


def test_first_claim_gives_one(shards: ShardLedgerService):
    assert shards.wallet(USER).balance == 0

    result = attendance.claim(shards, USER, datetime(2026, 8, 16, 10, 0, tzinfo=KST))

    assert (result.claimed, result.reward, result.balance) == (True, 1, 1)
    assert result.date == "2026-08-16"


def test_second_claim_same_day_gives_nothing(shards: ShardLedgerService):
    now = datetime(2026, 8, 16, 10, 0, tzinfo=KST)
    attendance.claim(shards, USER, now)

    result = attendance.claim(shards, USER, datetime(2026, 8, 16, 22, 0, tzinfo=KST))

    # 오류가 아니라 idempotent success다.
    assert (result.claimed, result.reward, result.balance) == (False, 0, 1)
    assert shards.wallet(USER).balance == 1


def test_ten_requests_pay_once(shards: ShardLedgerService, store: InMemoryShardStore):
    now = datetime(2026, 8, 16, 10, 0, tzinfo=KST)
    for _ in range(10):
        attendance.claim(shards, USER, now)

    assert shards.wallet(USER).balance == 1
    assert len(store.entries) == 1


def test_next_kst_day_pays_again(shards: ShardLedgerService, store: InMemoryShardStore):
    attendance.claim(shards, USER, datetime(2026, 8, 16, 23, 59, 59, tzinfo=KST))
    result = attendance.claim(shards, USER, datetime(2026, 8, 17, 0, 0, 1, tzinfo=KST))

    assert (result.claimed, result.reward, result.balance) == (True, 1, 2)
    assert len(store.entries) == 2


def test_two_users_same_day_each_get_one(shards: ShardLedgerService, store: InMemoryShardStore):
    """출석 event id는 날짜뿐이다. user scope가 없으면 하루에 한 사람만 받게 된다."""
    now = datetime(2026, 8, 16, 10, 0, tzinfo=KST)

    assert attendance.claim(shards, USER, now).balance == 1
    assert attendance.claim(shards, OTHER, now).balance == 1

    assert shards.wallet(USER).balance == 1
    assert shards.wallet(OTHER).balance == 1
    assert len(store.entries) == 2


def test_lifetime_counters_are_exact(shards: ShardLedgerService):
    for day in (16, 17, 18):
        attendance.claim(shards, USER, datetime(2026, 8, day, 10, 0, tzinfo=KST))
        # 같은 날 중복 호출이 섞여도 합계가 부풀지 않는다.
        attendance.claim(shards, USER, datetime(2026, 8, day, 11, 0, tzinfo=KST))

    wallet = shards.wallet(USER)
    assert (wallet.balance, wallet.lifetime_earned, wallet.lifetime_spent) == (3, 3, 0)


def test_ledger_entry_shape(shards: ShardLedgerService, store: InMemoryShardStore):
    attendance.claim(shards, USER, datetime(2026, 8, 16, 10, 0, tzinfo=KST))

    entry = store.entries[0]
    assert entry.reason == ShardReason.DAILY_ATTENDANCE
    assert entry.delta == 1
    assert entry.balance_after == 1
    # 사건 식별자는 **user + reason + KST 날짜**다. raw 값은 남지 않는다.
    assert entry.idempotency_key_hash == idempotency_hash(
        USER, ShardReason.DAILY_ATTENDANCE, "2026-08-16"
    )
    assert "2026-08-16" not in entry.idempotency_key_hash
    assert USER not in entry.idempotency_key_hash


def test_balance_after_follows_other_movements(shards: ShardLedgerService):
    shards.credit(USER, 5, ShardReason.IAP_PURCHASE)
    result = attendance.claim(shards, USER, datetime(2026, 8, 16, 10, 0, tzinfo=KST))
    assert result.balance == 6


def test_exactly_one_entry_per_user_per_day(shards: ShardLedgerService, store: InMemoryShardStore):
    for day in (16, 17):
        for user in (USER, OTHER):
            for _ in range(3):
                attendance.claim(shards, user, datetime(2026, 8, day, 12, 0, tzinfo=KST))

    assert len(store.entries) == 4  # 2일 × 2명
    assert shards.wallet(USER).balance == 2
    assert shards.wallet(OTHER).balance == 2


# MARK: - 상태 조회


def test_status_before_and_after(shards: ShardLedgerService, store: InMemoryShardStore):
    now = datetime(2026, 8, 16, 10, 0, tzinfo=KST)

    assert attendance.status(shards, USER, now) == ("2026-08-16", False)
    # 조회만으로는 아무것도 지급하지 않는다.
    assert store.entries == []

    attendance.claim(shards, USER, now)
    assert attendance.status(shards, USER, now) == ("2026-08-16", True)
    # 다음 날은 다시 받을 수 있다.
    assert attendance.status(shards, USER, datetime(2026, 8, 17, 0, 1, tzinfo=KST))[1] is False


def test_status_is_per_user(shards: ShardLedgerService):
    now = datetime(2026, 8, 16, 10, 0, tzinfo=KST)
    attendance.claim(shards, USER, now)

    assert attendance.status(shards, OTHER, now)[1] is False


# MARK: - 동시성
#
# `InMemoryShardStore.apply`는 lock 안에서 통째로 일어난다(Firestore transaction과 같은 의미).
# test가 스스로 직렬화 장치를 끼우지 않는다 — 그러면 저장소가 아니라 test가 만든 lock을
# 시험하게 된다.


def concurrently(count: int, work) -> list:
    """`count`개를 **최대한 같은 순간에** 시작시키고 결과를 모은다."""
    barrier = threading.Barrier(count)
    results: list = [None] * count

    def run(index: int) -> None:
        barrier.wait()
        results[index] = work(index)

    threads = [threading.Thread(target=run, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def test_ten_concurrent_claims_report_exactly_one_winner(store: InMemoryShardStore):
    """10개가 동시에 들어오면 **정확히 하나만** "내가 지급했다"고 답한다.

    잔액만 맞고 응답이 전부 `claimed=true`면, client 10개가 각자 "오늘 +1 받았다"고
    믿는다. 지급 여부는 원장 transaction의 결과에서만 나와야 한다.
    """
    shards = ShardLedgerService(store)
    now = datetime(2026, 8, 16, 10, 0, tzinfo=KST)

    results = concurrently(10, lambda _: attendance.claim(shards, USER, now))

    assert len(results) == 10
    assert sum(1 for r in results if r.claimed) == 1, "동시 요청이 둘 이상 지급했다고 답했다"
    assert sum(1 for r in results if not r.claimed) == 9
    assert sum(r.reward for r in results) == 1
    assert [r.reward for r in results if r.claimed] == [1]
    assert all(r.reward == 0 for r in results if not r.claimed)
    # 모든 응답이 같은 날짜를 말한다.
    assert {r.date for r in results} == {"2026-08-16"}

    wallet = shards.wallet(USER)
    assert (wallet.balance, wallet.lifetime_earned, wallet.lifetime_spent) == (1, 1, 0)
    assert len(store.entries) == 1


def test_concurrent_claims_of_two_users_each_have_one_winner(store: InMemoryShardStore):
    """사용자마다 정확히 하나씩 이긴다. 남의 요청이 내 지급을 가로채지 않는다."""
    shards = ShardLedgerService(store)
    now = datetime(2026, 8, 16, 10, 0, tzinfo=KST)
    users = [USER, OTHER] * 5

    results = concurrently(10, lambda index: (users[index], attendance.claim(shards, users[index], now)))

    for user in (USER, OTHER):
        mine = [result for owner, result in results if owner == user]
        assert sum(1 for r in mine if r.claimed) == 1, f"{user}의 지급 응답이 하나가 아니다"
        assert sum(r.reward for r in mine) == 1
        assert shards.wallet(user).balance == 1

    assert len(store.entries) == 2


def test_slow_ledger_still_has_one_winner(store: InMemoryShardStore):
    """원장 쓰기가 느릴 때 — race window를 **일부러 크게 벌린다.**

    첫 쓰기가 끝나기 전에 열 요청이 전부 "아직 없다"를 볼 수 있는 상황이다.
    지급 여부를 미리 조회해서 정하는 구현이면 여기서 열 개 모두 `claimed=true`가 된다.
    (CPython에서는 그냥 동시 실행만으로 이 틈이 잘 열리지 않아, 시간을 늘려 재현한다.)
    """
    shards = ShardLedgerService(store)
    now = datetime(2026, 8, 16, 10, 0, tzinfo=KST)
    original = store.apply

    def slow(*args, **kwargs):
        time.sleep(0.02)
        return original(*args, **kwargs)

    store.apply = slow  # type: ignore[method-assign]

    results = concurrently(10, lambda _: attendance.claim(shards, USER, now))

    assert sum(1 for r in results if r.claimed) == 1
    assert sum(r.reward for r in results) == 1
    assert shards.wallet(USER).balance == 1
    assert len(store.entries) == 1


def test_claim_does_not_check_before_acting(store: InMemoryShardStore):
    """지급 여부를 **미리 조회해서 짐작하지 않는다.**

    `event_applied`를 부르면 터지는 저장소로도 출석이 정상 동작해야 한다.
    조회-후-쓰기 구조로 되돌아가면 이 test가 먼저 깨진다.
    """
    shards = ShardLedgerService(store)
    now = datetime(2026, 8, 16, 10, 0, tzinfo=KST)

    def forbidden(*args, **kwargs):
        raise AssertionError("claim이 지급 전에 원장을 조회했다 — check-then-act는 race를 만든다")

    store.event_applied = forbidden  # type: ignore[method-assign]

    assert attendance.claim(shards, USER, now).claimed is True
    assert attendance.claim(shards, USER, now).claimed is False


# MARK: - 재시도 (응답을 잃어버린 client)


def test_lost_response_retry_reports_already_claimed(shards: ShardLedgerService):
    """server는 성공했는데 응답이 client에 닿지 못한 경우.

    재시도는 `claimed=false, reward=0`이고 **balance는 이미 오른 값**이다.
    client는 이것만 보고 "오늘 출석 완료"로 복구할 수 있다.
    """
    now = datetime(2026, 8, 16, 10, 0, tzinfo=KST)

    first = attendance.claim(shards, USER, now)  # 이 응답이 유실됐다고 가정
    assert (first.claimed, first.reward, first.balance) == (True, 1, 1)

    retry = attendance.claim(shards, USER, now)
    assert (retry.claimed, retry.reward, retry.balance) == (False, 0, 1)
    assert retry.date == first.date


# MARK: - HTTP


@pytest.fixture
def client(store: InMemoryShardStore, apple_key, jwks_of, monkeypatch) -> TestClient:
    from app.auth import jwks as jwks_module

    document = jwks_of(apple_key)
    monkeypatch.setattr(jwks_module, "http_jwks_fetch", lambda *a, **k: lambda: document)

    app = create_app(
        Settings(app_env="local", apple_client_id=CLIENT_ID),
        auth_store=InMemoryAuthStore(),
        shard_store=store,
    )
    return TestClient(app)


def sign_in(client: TestClient, apple_key, subject: str = "001234.abcdef0123456789.1234") -> str:
    nonce = f"nonce-{subject}"
    token = apple_key.token(apple_claims(sub=subject, nonce=sha256_hex(nonce)))
    response = client.post("/auth/apple", json={"identityToken": token, "nonce": nonce})
    return response.json()["accessToken"]


def test_requires_authentication(client: TestClient):
    assert client.get("/users/me/attendance").status_code == 401
    assert client.post("/users/me/attendance").status_code == 401

    bad = {"Authorization": "Bearer nope"}
    assert client.get("/users/me/attendance", headers=bad).status_code == 401
    assert client.post("/users/me/attendance", headers=bad).status_code == 401


def test_status_then_claim_then_duplicate(client: TestClient, apple_key):
    headers = {"Authorization": f"Bearer {sign_in(client, apple_key)}"}
    today = attendance.attendance_date()

    status = client.get("/users/me/attendance", headers=headers)
    assert status.status_code == 200
    assert status.json() == {"attendanceDate": today, "claimed": False}

    first = client.post("/users/me/attendance", headers=headers)
    assert first.status_code == 200
    assert first.json() == {"attendanceDate": today, "claimed": True, "reward": 1, "balance": 1}

    again = client.post("/users/me/attendance", headers=headers)
    assert again.status_code == 200
    assert again.json() == {"attendanceDate": today, "claimed": False, "reward": 0, "balance": 1}

    assert client.get("/users/me/attendance", headers=headers).json()["claimed"] is True
    # 지갑도 정확히 1이다.
    wallet = client.get("/users/me/shards", headers=headers).json()
    assert wallet == {"balance": 1, "lifetimeEarned": 1, "lifetimeSpent": 0}


def test_client_cannot_choose_amount_user_or_date(client: TestClient, apple_key):
    """body에 무엇을 넣어도 보상이 달라지지 않는다 — 받는 자리가 없다."""
    headers = {"Authorization": f"Bearer {sign_in(client, apple_key)}"}

    response = client.post(
        "/users/me/attendance",
        headers=headers,
        json={
            "userId": "someone-else",
            "date": "1999-01-01",
            "amount": 9999,
            "reason": "admin_adjustment",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["reward"], body["balance"]) == (1, 1)
    assert body["attendanceDate"] == attendance.attendance_date()

    # query로 우겨넣어도 같다.
    assert client.post(
        "/users/me/attendance?amount=9999&userId=someone-else", headers=headers
    ).json() == {
        "attendanceDate": attendance.attendance_date(),
        "claimed": False,
        "reward": 0,
        "balance": 1,
    }


def test_each_user_gets_their_own(client: TestClient, apple_key):
    first = {"Authorization": f"Bearer {sign_in(client, apple_key)}"}
    second = {"Authorization": f"Bearer {sign_in(client, apple_key, subject='000999.other.5678')}"}

    assert client.post("/users/me/attendance", headers=first).json()["balance"] == 1
    assert client.post("/users/me/attendance", headers=second).json()["balance"] == 1
    # 남의 출석이 내 잔액을 건드리지 않는다.
    assert client.get("/users/me/shards", headers=first).json()["balance"] == 1


def test_ten_concurrent_posts_have_exactly_one_winner(client: TestClient, apple_key, store):
    """endpoint까지 통과하는 진짜 동시 요청 10개.

    HTTP는 10개 모두 성공이고, 그중 **정확히 하나만** 지급했다고 답한다.
    """
    headers = {"Authorization": f"Bearer {sign_in(client, apple_key)}"}

    responses = concurrently(10, lambda _: client.post("/users/me/attendance", headers=headers))

    assert [r.status_code for r in responses] == [200] * 10
    bodies = [r.json() for r in responses]

    assert sum(1 for b in bodies if b["claimed"]) == 1
    assert sum(1 for b in bodies if not b["claimed"]) == 9
    assert sum(b["reward"] for b in bodies) == 1
    assert all(b["reward"] == 0 for b in bodies if not b["claimed"])
    # 늦게 온 응답도 실제 잔액을 말한다.
    assert {b["balance"] for b in bodies} == {1}

    assert client.get("/users/me/shards", headers=headers).json() == {
        "balance": 1, "lifetimeEarned": 1, "lifetimeSpent": 0
    }
    assert len(store.entries) == 1


def test_attendance_is_not_a_generic_mutation_endpoint(client: TestClient):
    """전용 통로 하나가 생겼을 뿐, 범용 통로는 여전히 없다."""
    paths = {route.path for route in client.app.routes if hasattr(route, "path")}
    assert "/users/me/attendance" in paths
    for forbidden in ["/shards/credit", "/shards/debit", "/shards/add", "/wallet/add", "/wallet/set"]:
        assert forbidden not in paths


def test_logs_have_no_sensitive_values(client: TestClient, apple_key, caplog):
    import logging

    token = sign_in(client, apple_key)
    with caplog.at_level(logging.DEBUG):
        client.post("/users/me/attendance", headers={"Authorization": f"Bearer {token}"})

    assert "shard_ledger_credit" in caplog.text
    assert token not in caplog.text
    # 날짜(external event id)와 idempotency key도 로그에 남기지 않는다.
    assert attendance.attendance_date() not in caplog.text
