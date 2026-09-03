"""Admin Shard CLI.

**Firestore를 부르지 않는다.** 기존 B-3 fake(`InMemoryShardStore`)와
auth fake(`InMemoryAuthStore`)를 그대로 쓴다 — CLI가 새 경제 로직을 만들지 않았다는 것을
같은 저장소로 확인하는 것이 요점이다.
"""

from __future__ import annotations

import inspect
import io
import tokenize
from pathlib import Path

import pytest

from scripts import admin_shards

from app.auth.models import User
from app.auth.store import InMemoryAuthStore
from app.shards.models import ShardReason, idempotency_hash
from app.shards.service import ShardLedgerService
from app.shards.store import InMemoryShardStore
from scripts.admin_shards import (
    PROJECT,
    AdminError,
    Adjustment,
    adjustment_from_args,
    build_parser,
    format_plan,
    new_event_id,
    resolve_project,
    run,
    validated_user_id,
)

USER_ID = "6f1c9a2e-3b4d-4a5e-8c7f-0d1e2f3a4b5c"


def _world(balance: int = 0) -> tuple[ShardLedgerService, InMemoryShardStore, InMemoryAuthStore]:
    store = InMemoryShardStore()
    service = ShardLedgerService(store)
    users = InMemoryAuthStore()
    users.users[USER_ID] = User(id=USER_ID)
    if balance:
        service.credit(USER_ID, balance, ShardReason.DAILY_ATTENDANCE, external_event_id="seed")
    return service, store, users


def _adjustment(delta: int, event_id: str | None = None, user_id: str = USER_ID) -> Adjustment:
    return Adjustment(
        user_id=user_id,
        delta=delta,
        note="AI sticker E2E",
        event_id=event_id or new_event_id(),
        project=PROJECT,
        database="(default)",
    )


def _recorded_delta(store: InMemoryShardStore):
    """원장에 이미 적힌 delta를 읽기만 한다. 열쇠는 B-3의 `idempotency_hash` 그대로."""

    def lookup(user_id: str, reason: ShardReason, event_id: str) -> int | None:
        key = idempotency_hash(user_id, reason, event_id)
        for entry in store.entries:
            if entry.idempotency_key_hash == key:
                return entry.delta
        return None

    return lookup


def _run(adjustment, service, users, *, store=None, confirm=lambda: True, dry_run=False):
    out = io.StringIO()
    code = run(
        adjustment,
        service=service,
        users=users,
        recorded_delta=_recorded_delta(store) if store is not None else (lambda *_: None),
        confirm=confirm,
        dry_run=dry_run,
        out=out,
    )
    return code, out.getvalue()


# MARK: - 지급 / 회수


def test_positive_adjustment_credits_through_ledger():
    service, store, users = _world()

    code, output = _run(_adjustment(100), service, users)

    assert code == 0
    assert service.wallet(USER_ID).balance == 100
    assert len(store.entries) == 1
    entry = store.entries[0]
    assert entry.delta == 100
    assert entry.reason is ShardReason.ADMIN_ADJUSTMENT
    assert "Applied. balance=100" in output


def test_negative_adjustment_debits_through_ledger():
    service, store, users = _world(balance=100)

    code, _ = _run(_adjustment(-20), service, users)

    assert code == 0
    assert service.wallet(USER_ID).balance == 80
    admin_entries = [e for e in store.entries if e.reason is ShardReason.ADMIN_ADJUSTMENT]
    assert len(admin_entries) == 1
    assert admin_entries[0].delta == -20


def test_reversal_appends_instead_of_editing():
    """"원복"은 과거 줄을 지우지 않는다 — 반대 부호 줄이 하나 더 쌓인다."""
    service, store, users = _world()

    _run(_adjustment(100), service, users)
    _run(_adjustment(-20), service, users)

    deltas = [e.delta for e in store.entries]
    assert deltas == [100, -20]
    assert service.wallet(USER_ID).balance == 80


def test_debit_below_zero_is_refused_by_ledger():
    """잔액 음수 금지는 기존 정책이다. CLI가 바꾸지 않는다."""
    from app.shards.models import InsufficientShards

    service, store, users = _world(balance=2)

    with pytest.raises(InsufficientShards):
        _run(_adjustment(-50), service, users)

    assert service.wallet(USER_ID).balance == 2
    assert not [e for e in store.entries if e.reason is ShardReason.ADMIN_ADJUSTMENT]


# MARK: - 거절


def test_zero_delta_rejected():
    parser = build_parser()
    args = parser.parse_args(
        ["--user-id", USER_ID, "--delta", "0", "--note", "nope"]
    )
    with pytest.raises(AdminError):
        adjustment_from_args(args, {})


def test_nonexistent_user_rejected_and_writes_nothing():
    service, store, users = _world()
    unknown = "11111111-2222-4333-8444-555555555555"

    with pytest.raises(AdminError):
        _run(_adjustment(100, user_id=unknown), service, users)

    assert store.entries == []
    assert service.wallet(unknown).balance == 0


def test_malformed_user_id_rejected():
    for bad in ["", "not-a-uuid", "6F1C9A2E-3B4D-4A5E-8C7F-0D1E2F3A4B5C", "../escape"]:
        with pytest.raises(AdminError):
            validated_user_id(bad)


def test_empty_note_rejected():
    parser = build_parser()
    args = parser.parse_args(["--user-id", USER_ID, "--delta", "5", "--note", "   "])
    with pytest.raises(AdminError):
        adjustment_from_args(args, {})


# MARK: - idempotency


def test_same_event_id_same_delta_is_idempotent():
    """CASE 1 — 정상 재시도. 딱 한 번만 반영된다."""
    service, store, users = _world()
    event_id = "admin:fixed-event"

    first_code, first_output = _run(_adjustment(100, event_id), service, users, store=store)
    second_code, second_output = _run(_adjustment(100, event_id), service, users, store=store)

    assert first_code == second_code == 0
    assert "Applied. balance=100" in first_output
    assert "Already applied" in second_output
    assert service.wallet(USER_ID).balance == 100
    assert len([e for e in store.entries if e.reason is ShardReason.ADMIN_ADJUSTMENT]) == 1


def test_same_event_id_different_delta_fails_closed():
    """CASE 2 — 같은 event id에 다른 금액. 헷갈릴 자리를 만들지 않는다."""
    service, store, users = _world()
    event_id = "admin:reused"
    _run(_adjustment(10, event_id), service, users, store=store)

    with pytest.raises(AdminError) as error:
        _run(_adjustment(100, event_id), service, users, store=store)

    message = str(error.value)
    assert "admin:reused" in message
    assert "+10" in message
    assert "+100" in message
    assert "new event id" in message


def test_mismatched_delta_leaves_balance_and_ledger_unchanged():
    service, store, users = _world()
    event_id = "admin:reused"
    _run(_adjustment(10, event_id), service, users, store=store)
    entries_before = list(store.entries)

    with pytest.raises(AdminError):
        _run(_adjustment(100, event_id), service, users, store=store)

    assert service.wallet(USER_ID).balance == 10
    assert store.entries == entries_before
    assert len(store.entries) == 1


def test_mismatched_delta_is_caught_even_in_dry_run():
    service, store, users = _world()
    event_id = "admin:reused"
    _run(_adjustment(10, event_id), service, users, store=store)

    with pytest.raises(AdminError):
        _run(_adjustment(100, event_id), service, users, store=store, dry_run=True)


def test_opposite_sign_reuse_is_rejected():
    """`+100` 뒤에 같은 event id로 `-100`을 넣는 것도 다른 조정이다."""
    service, store, users = _world()
    event_id = "admin:same"
    _run(_adjustment(100, event_id), service, users, store=store)

    with pytest.raises(AdminError):
        _run(_adjustment(-100, event_id), service, users, store=store)

    assert service.wallet(USER_ID).balance == 100


def test_guard_never_blocks_a_fresh_event_id():
    service, store, users = _world()
    _run(_adjustment(10, "admin:one"), service, users, store=store)

    code, _ = _run(_adjustment(100, "admin:two"), service, users, store=store)

    assert code == 0
    assert service.wallet(USER_ID).balance == 110


def test_recorded_delta_lookup_uses_the_b3_key_rule():
    """열쇠를 CLI가 새로 만들지 않는다 — 어긋나면 guard가 조용히 무력해진다."""
    service, store, users = _world()
    _run(_adjustment(10, "admin:keyed"), service, users, store=store)

    key = idempotency_hash(USER_ID, ShardReason.ADMIN_ADJUSTMENT, "admin:keyed")
    assert [e.idempotency_key_hash for e in store.entries] == [key]
    assert _recorded_delta(store)(USER_ID, ShardReason.ADMIN_ADJUSTMENT, "admin:keyed") == 10


def test_generated_event_ids_are_unique():
    assert new_event_id() != new_event_id()
    assert new_event_id().startswith("admin:")


def test_event_id_is_shown_before_mutation():
    """네트워크가 끊겨도 같은 event id로 재실행할 수 있어야 한다."""
    service, _, users = _world()
    adjustment = _adjustment(100, "admin:printed")

    _, output = _run(adjustment, service, users, dry_run=True)

    assert "admin:printed" in output


def test_plan_warns_when_event_already_applied():
    service, store, users = _world()
    event_id = "admin:already"
    _run(_adjustment(100, event_id), service, users, store=store)

    _, output = _run(_adjustment(100, event_id), service, users, store=store, dry_run=True)

    assert "이미 반영" in output


# MARK: - ledger / wallet 일관성


def test_ledger_and_wallet_projection_stay_consistent():
    service, store, users = _world()

    for delta in (100, -20, 7, -1):
        _run(_adjustment(delta), service, users)

    replayed = sum(entry.delta for entry in store.entries)
    wallet = service.wallet(USER_ID)
    assert wallet.balance == replayed
    assert store.entries[-1].balance_after == wallet.balance


def test_lifetime_counters_follow_direction():
    service, _, users = _world()

    _run(_adjustment(100), service, users)
    _run(_adjustment(-30), service, users)

    wallet = service.wallet(USER_ID)
    assert wallet.lifetime_earned == 100
    assert wallet.lifetime_spent == 30


# MARK: - mutation 0 경로


def test_dry_run_writes_nothing():
    service, store, users = _world()

    code, output = _run(_adjustment(100), service, users, dry_run=True)

    assert code == 0
    assert "Dry run" in output
    assert store.entries == []
    assert service.wallet(USER_ID).balance == 0


def test_dry_run_never_asks_for_confirmation():
    service, _, users = _world()

    def explode() -> bool:
        raise AssertionError("dry run은 물어보지 않는다")

    _run(_adjustment(100), service, users, confirm=explode, dry_run=True)


def test_declined_confirmation_writes_nothing():
    service, store, users = _world()

    code, output = _run(_adjustment(100), service, users, confirm=lambda: False)

    assert code == 1
    assert "Aborted" in output
    assert store.entries == []
    assert service.wallet(USER_ID).balance == 0


def test_confirmation_is_required_by_default():
    """`--yes`가 없으면 항상 물어본다."""
    args = build_parser().parse_args(["--user-id", USER_ID, "--delta", "1", "--note", "n"])
    assert args.yes is False


def test_yes_flag_is_wired_to_the_confirm_gate():
    """`--yes`를 파싱만 하고 쓰지 않으면 자동 실행이 조용히 abort된다."""
    source = inspect.getsource(admin_shards.main)
    assert "args.yes" in source, "--yes가 confirm gate에 연결되지 않았다"


# MARK: - project fail closed


def test_allowed_project_resolves():
    assert resolve_project(None, {}) == PROJECT
    assert resolve_project(PROJECT, {"GCP_PROJECT_ID": PROJECT}) == PROJECT


def test_other_project_argument_fails_closed():
    with pytest.raises(AdminError):
        resolve_project("opicmobile-45cd5", {})


@pytest.mark.parametrize(
    "name",
    ["GCP_PROJECT_ID", "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT", "CLOUDSDK_CORE_PROJECT"],
)
def test_dailyopic_environment_fails_closed(name):
    """이 머신의 gcloud 기본 project가 DailyOPIc이다. env로 새어 들어오면 막는다."""
    with pytest.raises(AdminError) as error:
        resolve_project(None, {name: "opicmobile-45cd5"})
    assert "opicmobile-45cd5" in str(error.value)


def test_project_is_printed_in_plan():
    service, _, users = _world()
    _, output = _run(_adjustment(100), service, users, dry_run=True)
    assert f"Project        : {PROJECT}" in output


def test_plan_shows_balance_math():
    wallet_service, _, users = _world(balance=2)
    _, output = _run(_adjustment(100), wallet_service, users, dry_run=True)
    assert "Current        : 2" in output
    assert "Adjustment     : +100" in output
    assert "After          : 102" in output
    assert f"Reason         : {ShardReason.ADMIN_ADJUSTMENT.value}" in output


def test_plan_warns_before_negative_balance():
    service, _, users = _world(balance=2)
    _, output = _run(_adjustment(-50), service, users, dry_run=True)
    assert "음수" in output


# MARK: - 우회 금지 (구조 고정)


SOURCE = (Path(__file__).resolve().parent.parent / "scripts" / "admin_shards.py").read_text()


def _code_only(source: str) -> str:
    """주석과 문자열을 걷어낸 코드만. 설명문에 나온 단어를 위반으로 세지 않는다."""
    return "".join(
        token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type not in (tokenize.COMMENT, tokenize.STRING)
    )


CODE = _code_only(SOURCE)


def test_cli_never_touches_wallet_or_ledger_documents_directly():
    """wallet projection · ledger 문서를 직접 쓰는 경로를 만들지 않는다."""
    for banned in (
        "ggumirror_shard_wallets",
        "ggumirror_shard_ledger",
        "_wallet_document",
        "_entry_document",
        "ShardLedgerEntry(",
        ".set(",
        ".update(",
        "transaction",
    ):
        assert banned not in CODE, f"CLI가 원장을 우회한다: {banned}"


def test_cli_goes_through_the_ledger_service_only():
    """조각이 움직이는 통로는 `credit` / `debit` 둘뿐이다."""
    assert "service.credit(" in SOURCE
    assert "service.debit(" in SOURCE
    assert "ShardLedgerService" in SOURCE


def test_mutation_goes_through_store_apply_exactly_once():
    service, store, users = _world()
    calls: list[tuple] = []
    original = store.apply

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    store.apply = spy  # type: ignore[method-assign]
    _run(_adjustment(100), service, users)

    assert len(calls) == 1
    assert calls[0][0][2] is ShardReason.ADMIN_ADJUSTMENT


def test_cli_does_not_add_a_new_shard_reason():
    """새 reason을 만들지 않았다 — 기존 admin_adjustment를 쓴다."""
    assert ShardReason.ADMIN_ADJUSTMENT.value == "admin_adjustment"
    assert "ShardReason.ADMIN_ADJUSTMENT" in SOURCE


def test_format_plan_is_pure():
    service, store, users = _world(balance=5)
    adjustment = _adjustment(10, "admin:pure")
    before = list(store.entries)

    format_plan(adjustment, service.wallet(USER_ID), False)

    assert store.entries == before
    assert service.wallet(USER_ID).balance == 5
