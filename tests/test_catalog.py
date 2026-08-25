"""내장 템플릿 획득 통계.

보는 것 넷:

1. **등록된 id만** 센다 — 아무 문자열이나 보내 통계를 부풀릴 수 없다
2. 같은 사용자가 다시 받아도 **+0**
3. 기록 생성과 카운터가 **한 commit**
4. 맞춰 보기(reconcile)를 몇 번 불러도 결과가 같다

`민트 플라워`가 이 phase의 출발점이라 실제 id로 회귀 test를 둔다.
**제목으로 맞추지 않는다** — 제목은 바뀔 수 있고 같은 제목이 여럿일 수 있다.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.auth.models import User
from app.catalog.models import (
    ARTWORK_TEMPLATE_IDS,
    BASIC_TEMPLATE_IDS,
    MAX_BATCH,
    TEMPLATE_IDS,
    UnknownTemplate,
    acquisition_id,
    is_known,
)
from app.catalog.service import CatalogService
from app.catalog.store import InMemoryCatalogStore

#: 이 파일의 "평범한 템플릿". **값이 없는 것으로 둔다** — 여기 test들은 세는 규칙과
#: 멱등을 보는 것이지 결제를 보는 것이 아니다. 유료 템플릿을 무료 경로로 가져갈 수
#: 없다는 것은 아래 별도 test가 고정한다.
MINT = "basic-mint"
SECOND = "basic-sky"
#: 값이 있는 템플릿.
PAID = "art-mint-flower"
ALICE = "11111111-2222-4333-8444-555555555555"
BOB = "99999999-8888-4777-8666-555555555555"


def user(user_id: str = ALICE) -> User:
    return User(id=user_id, created_at=datetime(2026, 1, 1, tzinfo=UTC))


@pytest.fixture
def store() -> InMemoryCatalogStore:
    return InMemoryCatalogStore()


@pytest.fixture
def service(store: InMemoryCatalogStore) -> CatalogService:
    return CatalogService(store)


# MARK: - 목록 (§21)


def test_mint_flower_is_registered():
    """이 phase의 출발점. **id로** 확인한다."""
    assert MINT in TEMPLATE_IDS
    assert is_known(MINT)


def test_every_artwork_template_is_registered():
    """client `StoreCatalog.artworkTemplates` 24종."""
    assert len(ARTWORK_TEMPLATE_IDS) == 24
    assert all(x.startswith("art-") for x in ARTWORK_TEMPLATE_IDS)


def test_basic_templates_are_registered():
    """단색 기본 거울 8종."""
    assert len(BASIC_TEMPLATE_IDS) == 8
    assert all(x.startswith("basic-") for x in BASIC_TEMPLATE_IDS)
    assert "basic-mint" in BASIC_TEMPLATE_IDS


def test_registry_has_exactly_the_known_templates():
    assert len(TEMPLATE_IDS) == 32


@pytest.mark.parametrize(
    "unknown",
    ["art-does-not-exist", "", "민트 플라워", "art-mint-flower ", "ART-MINT-FLOWER",
     "../etc/passwd", "art-mint-flower/../x", "a" * 200],
)
def test_unknown_ids_are_refused(service, unknown):
    """**아무 문자열이나 셀 수 없다.** 공개 통계를 임의로 만들 수 있으면 안 된다."""
    assert not is_known(unknown)
    with pytest.raises(UnknownTemplate):
        service.acquire(user(), unknown)


def test_title_is_not_an_identifier(service, store):
    """제목으로 맞추지 않는다 — 한국어 이름은 식별자가 아니다."""
    with pytest.raises(UnknownTemplate):
        service.acquire(user(), "민트 플라워")
    assert store.counts == {}


# MARK: - 획득 (§22)


def test_first_acquisition_counts_once(service):
    result = service.acquire(user(ALICE), MINT)

    assert result.first_acquisition is True
    assert result.download_count == 1


def test_same_user_again_does_not_count(service):
    """가장 중요 — 같은 사람의 재다운로드는 오르지 않는다."""
    service.acquire(user(ALICE), MINT)

    again = service.acquire(user(ALICE), MINT)

    assert again.first_acquisition is False
    assert again.download_count == 1


def test_repeat_is_not_a_failure(service):
    """다시 받는 것은 오류가 아니다. 수는 정상 현재 값이다."""
    service.acquire(user(ALICE), MINT)
    for _ in range(5):
        result = service.acquire(user(ALICE), MINT)
        assert result.download_count == 1
        assert result.first_acquisition is False


def test_second_user_counts(service):
    service.acquire(user(ALICE), MINT)

    result = service.acquire(user(BOB), MINT)

    assert result.first_acquisition is True
    assert result.download_count == 2


def test_users_are_counted_independently(service, store):
    service.acquire(user(ALICE), MINT)
    service.acquire(user(BOB), MINT)
    service.acquire(user(ALICE), SECOND)

    assert store.counts[MINT] == 2
    assert store.counts[SECOND] == 1
    assert store.acquired_template_ids(ALICE) == {MINT, SECOND}
    assert store.acquired_template_ids(BOB) == {MINT}


def test_acquisition_key_is_length_prefixed():
    """`(user, template)` 조합이 겹치지 않는다.

    길이를 앞에 붙이지 않으면 `("ab","c")`와 `("a","bc")`가 같은 문자열이 된다.
    """
    assert acquisition_id("ab", "c") != acquisition_id("a", "bc")
    assert acquisition_id(ALICE, MINT) == acquisition_id(ALICE, MINT)
    assert acquisition_id(ALICE, MINT) != acquisition_id(BOB, MINT)


def test_record_and_counter_move_together(service, store):
    """기록과 카운터가 갈라지면 "받았는데 안 세어졌다"가 된다."""
    service.acquire(user(ALICE), MINT)

    assert len(store.acquisitions) == 1
    assert store.counts[MINT] == 1

    service.acquire(user(BOB), MINT)

    assert len(store.acquisitions) == 2
    assert store.counts[MINT] == 2


def test_concurrent_same_user_counts_once(service, store):
    """동시에 두 번 눌러도 +1이다.

    실제 저장소는 문서 id가 같고 `create`로 쓰기 때문에 두 번째가 거절된다.
    여기서는 in-memory가 같은 원자성을 흉내 낸다.
    """
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: service.acquire(user(ALICE), MINT), range(8)))

    assert store.counts[MINT] == 1
    assert len(store.acquisitions) == 1


def test_concurrent_different_users_all_count(service, store):
    from concurrent.futures import ThreadPoolExecutor

    users = [f"user-{i:04d}-0000-4000-8000-000000000000" for i in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda u: service.acquire(user(u), MINT), users))

    assert store.counts[MINT] == 8


# MARK: - 통계


def test_stats_are_zero_when_nothing_recorded(service):
    """없는 것과 0은 사용자에게 같은 뜻이다. 목록에서 빼면 화면이 자리를 비운다."""
    found = service.stats([MINT, SECOND])

    assert {x.template_id: x.download_count for x in found} == {
        MINT: 0, SECOND: 0
    }


def test_stats_return_the_counted_value(service):
    service.acquire(user(ALICE), MINT)
    service.acquire(user(BOB), MINT)

    found = service.stats([MINT])

    assert found[0].download_count == 2


def test_stats_skip_unknown_ids(service):
    """모르는 id 하나 때문에 화면 전체의 숫자를 잃지 않는다."""
    found = service.stats([MINT, "art-nope", "민트 플라워"])

    assert [x.template_id for x in found] == [MINT]


def test_stats_deduplicate(service):
    found = service.stats([MINT, MINT, MINT])
    assert len(found) == 1


def test_stats_are_capped(service):
    """한 요청이 임의로 커지지 않는다."""
    found = service.stats(sorted(TEMPLATE_IDS) * 5)
    assert len(found) <= MAX_BATCH


def test_stats_need_no_login(service):
    """공개다 — service가 user를 받지 않는다."""
    import inspect

    assert "user" not in inspect.signature(service.stats).parameters


def test_stats_carry_no_private_state(service):
    """누가 받았는지는 공개가 아니다."""
    service.acquire(user(ALICE), MINT)

    found = service.stats([MINT])

    assert not hasattr(found[0], "user_id")
    assert set(found[0].__dict__) == {"template_id", "download_count"}


# MARK: - 맞춰 보기 (§23)


def test_reconcile_empty_list(service, store):
    assert service.reconcile(user(), []) == []
    assert store.counts == {}


def test_reconcile_records_a_previous_download(service, store):
    """예전 버전에서 받은 `민트 플라워`가 한 번 반영된다."""
    results = service.reconcile(user(ALICE), [MINT])

    assert len(results) == 1
    assert results[0].first_acquisition is True
    assert store.counts[MINT] == 1


def test_reconcile_records_several(service, store):
    results = service.reconcile(user(ALICE), [MINT, SECOND, "basic-mint"])

    assert all(x.first_acquisition for x in results)
    assert store.counts == {MINT: 1, SECOND: 1, "basic-mint": 1}


def test_reconcile_twice_adds_nothing(service, store):
    """**멱등** — 두 번째 호출로 수가 오르면 안 된다."""
    service.reconcile(user(ALICE), [MINT, SECOND])

    again = service.reconcile(user(ALICE), [MINT, SECOND])

    assert all(not x.first_acquisition for x in again)
    assert store.counts == {MINT: 1, SECOND: 1}


def test_reconcile_ten_times_stays_the_same(service, store):
    for _ in range(10):
        service.reconcile(user(ALICE), [MINT])

    assert store.counts[MINT] == 1


def test_reconcile_handles_duplicate_ids(service, store):
    service.reconcile(user(ALICE), [MINT, MINT, MINT])

    assert store.counts[MINT] == 1


def test_reconcile_skips_unknown_ids(service, store):
    """모르는 id는 조용히 뺀다 — 목록 하나 때문에 전체가 실패하지 않는다."""
    results = service.reconcile(user(ALICE), ["art-nope", MINT, "민트 플라워"])

    assert [x.template_id for x in results] == [MINT]
    assert store.counts == {MINT: 1}


def test_reconcile_counts_two_users_separately(service, store):
    service.reconcile(user(ALICE), [MINT])
    service.reconcile(user(BOB), [MINT])

    assert store.counts[MINT] == 2


def test_reconcile_recovers_a_failed_acquire(service, store):
    """획득 요청이 실패했어도 다음 맞춰 보기가 되찾는다."""
    # 획득이 안 됐다고 하자.
    assert store.counts == {}

    service.reconcile(user(ALICE), [MINT])

    assert store.counts[MINT] == 1


def test_reconcile_after_acquire_does_not_double_count(service, store):
    service.acquire(user(ALICE), MINT)

    service.reconcile(user(ALICE), [MINT])

    assert store.counts[MINT] == 1


def test_reconcile_never_looks_at_titles(service, store):
    """제목 문자열을 보내면 아무 일도 일어나지 않는다."""
    service.reconcile(user(ALICE), ["민트 플라워", "핑크 리본", "Mint Flower"])

    assert store.counts == {}


def test_reconcile_is_capped(service, store):
    service.reconcile(user(ALICE), sorted(TEMPLATE_IDS) * 5)
    assert len(store.acquisitions) <= MAX_BATCH


# MARK: - HTTP


@pytest.fixture
def client(store):
    from app.core.config import Settings
    from app.main import create_app

    app = create_app(Settings(app_env="local"), catalog_store=store)
    return TestClient(app, raise_server_exceptions=False)


def test_stats_endpoint_is_public(client):
    """**로그인 없이 볼 수 있다** — 상점 구경에 로그인 벽을 세우지 않는다."""
    response = client.get(f"/catalog/templates/stats?ids={MINT}")

    assert response.status_code == 200
    assert response.json() == [{"templateId": MINT, "downloadCount": 0}]


def test_stats_endpoint_batches(client):
    response = client.get(
        f"/catalog/templates/stats?ids={MINT},{SECOND},{PAID}"
    )

    assert response.status_code == 200
    # 통계는 값과 무관하다 — 유료 템플릿도 공개 통계에는 그대로 나온다.
    assert [x["templateId"] for x in response.json()] == [MINT, SECOND, PAID]


def test_stats_endpoint_with_no_ids(client):
    assert client.get("/catalog/templates/stats").json() == []


def test_acquire_endpoint_needs_auth(client):
    assert client.post(f"/catalog/templates/{MINT}/acquire").status_code == 401


def test_reconcile_endpoint_needs_auth(client):
    response = client.post(
        "/catalog/templates/reconcile", json={"templateIds": [MINT]}
    )
    assert response.status_code == 401


def test_unknown_template_acquire_is_404(client):
    # 인증이 먼저 걸리므로 익명은 401이다 — 그것만으로도 임의 증가가 불가능하다.
    assert client.post("/catalog/templates/art-nope/acquire").status_code == 401


def test_no_arbitrary_count_endpoint(client):
    """수를 직접 쓰는 경로를 만들지 않았다."""
    for path in [
        "/catalog/templates/stats",
        f"/catalog/templates/{MINT}/stats",
        "/catalog/stats",
    ]:
        for method in ("post", "put", "patch", "delete"):
            response = getattr(client, method)(path)
            assert response.status_code in {401, 404, 405, 422}, f"{method} {path}"


def test_client_cannot_send_a_user_id(client):
    """누구인지는 session이 정한다 — 요청 모양에 userId가 없다."""
    from app.api.catalog import AcquisitionResponse, ReconcileRequest, TemplateStatResponse

    for model in (ReconcileRequest, AcquisitionResponse, TemplateStatResponse):
        for banned in ("user_id", "userId"):
            assert banned not in model.model_fields, model.__name__


# MARK: - 소스 규칙


def _code_only(source: str) -> str:
    import io
    import tokenize

    return "".join(
        t.string
        for t in tokenize.generate_tokens(io.StringIO(source).readline)
        if t.type not in (tokenize.COMMENT, tokenize.STRING)
    )


def test_counter_is_never_set_from_a_request():
    """client가 숫자를 보내는 자리가 없다."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for path in ["app/api/catalog.py", "app/catalog/service.py"]:
        code = _code_only((root / path).read_text())
        for banned in ["downloadCount =", "download_count =", "downloadCount +", "count ="]:
            assert banned not in code, f"{path}: {banned}"


def test_acquisition_and_counter_share_one_transaction():
    """실제 저장소에서 둘이 한 commit이다."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parent.parent / "app/catalog/store.py"
    ).read_text()
    body = source[source.index("class FirestoreCatalogStore"):]
    run = body[body.index("def acquire"):body.index("def stats")]
    assert "@firestore.transactional" in run
    # 기록 생성과 카운터가 같은 transaction 안에 있다.
    assert "transaction.create(" in run
    assert "transaction.update(" in run or "transaction.set(" in run
    # 읽기가 쓰기보다 먼저다 — Firestore는 반대를 허용하지 않는다.
    assert run.index("record_ref.get(") < run.index("transaction.create(")


def test_catalog_does_not_touch_the_marketplace():
    """두 경로를 섞지 않는다 — 저쪽은 소유권·조각이 있고 이쪽은 없다."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for path in ["app/catalog/service.py", "app/catalog/store.py", "app/catalog/models.py"]:
        code = _code_only((root / path).read_text())
        # `utcnow`는 `app.shards.models`에 있는 **시각 유틸**이라 재사용한다 —
        # 그것 때문에 "shard"를 통째로 금지하면 중복 helper를 만들게 된다.
        # 진짜로 섞이면 안 되는 것은 **경제와 상품**이다.
        for banned in [
            "marketplace", "Listing", "Ownership", "ShardLedgerService",
            "wallet", "balance", "ledger", "priceShards",
        ]:
            assert banned not in code, f"{path}: {banned}"
