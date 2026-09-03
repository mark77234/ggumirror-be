"""운영/테스트용 조각 지급·회수 CLI.

**새 경제 로직을 만들지 않는다.** B-3 원장(`ShardLedgerService`)을 그대로 부른다 —
wallet 문서를 직접 고치지 않고, ledger 문서를 손으로 쓰지도 않는다.
`reason=admin_adjustment` 줄이 append-only로 쌓이고 잔액은 기존 transaction이 갱신한다.

    python3 ggumirror-be/scripts/admin_shards.py \\
      --user-id "<uid>" --delta 100 --note "AI sticker E2E"

"원복"도 과거 줄을 지우지 않는다. 반대 부호 줄을 새로 쌓는다.

이 CLI는 **`ggumirror-prod` 전용**이다. 이 머신의 gcloud 기본 project는 DailyOPIc
production(`opicmobile-45cd5`)이라, 기본값을 신뢰하면 남의 production을 만진다.
그래서 project를 상수로 박고 `firestore.Client(project=...)`에 **명시적으로** 넘긴다.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol, TextIO

# workspace root에서 `python3 ggumirror-be/scripts/admin_shards.py`로 부를 수 있어야 한다.
# 그러면 cwd가 backend repo가 아니므로 `app` package를 찾지 못한다.
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.shards.models import (  # noqa: E402  (sys.path를 먼저 세워야 한다)
    MAX_DELTA,
    InsufficientShards,
    InvalidShardAmount,
    ShardReason,
    ShardWallet,
    idempotency_hash,
)
from app.shards.service import ShardLedgerService  # noqa: E402

# 이 CLI가 붙을 수 있는 **유일한** project. 인자로도 env로도 바꿀 수 없다.
PROJECT = "ggumirror-prod"
# production과 같은 값(`app/core/config.py`의 기본값과 동일).
DATABASE_DEFAULT = "(default)"
REASON = ShardReason.ADMIN_ADJUSTMENT

# project를 가리키는 env는 여러 개다. 하나라도 다른 곳을 가리키면 열지 않는다 —
# 조용히 무시하면 "왜 저쪽에 들어갔지"를 사후에 추적해야 한다.
PROJECT_ENV_VARS = (
    "GCP_PROJECT_ID",
    "GOOGLE_CLOUD_PROJECT",
    "GCLOUD_PROJECT",
    "CLOUDSDK_CORE_PROJECT",
)


class AdminError(Exception):
    """운영자에게 그대로 보여줄 실패. traceback을 띄우지 않는다."""


class UserLookup(Protocol):
    def user(self, user_id: str) -> object | None: ...


class RecordedDelta(Protocol):
    """이 event id로 **이미 적힌** delta. 없으면 None.

    **read-only UX guard 전용이다.** exactly-once의 authority는 여전히
    `ShardStore.apply`의 원자적 `create()`다 — 여기서 무엇을 읽든 그 판정을 대신하지 않는다.
    """

    def __call__(self, user_id: str, reason: ShardReason, event_id: str) -> int | None: ...


@dataclass(frozen=True)
class Adjustment:
    user_id: str
    delta: int
    note: str
    event_id: str
    project: str
    database: str


# MARK: - 검증


def resolve_project(requested: str | None, env: Mapping[str, str]) -> str:
    """project는 상수다. 다른 값이 **전달되거나 감지되면** 열지 않는다."""
    if requested is not None and requested != PROJECT:
        raise AdminError(f"--project={requested!r} 은 허용되지 않는다. 이 CLI는 {PROJECT} 전용이다.")
    for name in PROJECT_ENV_VARS:
        value = (env.get(name) or "").strip()
        if value and value != PROJECT:
            raise AdminError(
                f"{name}={value!r} 이 다른 project를 가리킨다. "
                f"이 CLI는 {PROJECT} 전용이다 — 해당 환경변수를 지우고 다시 실행해라."
            )
    return PROJECT


def validated_user_id(raw: str) -> str:
    """내부 user id는 **canonical lowercase UUID v4**다(`app/auth/models.py`).

    문서 ID로 그대로 쓰이는 값이라 표기가 흔들리면 다른 문서를 가리킨다.
    """
    candidate = raw.strip()
    try:
        parsed = uuid.UUID(candidate)
    except (ValueError, AttributeError, TypeError) as error:
        raise AdminError(f"--user-id 가 UUID 형식이 아니다: {candidate!r}") from error
    if str(parsed) != candidate:
        raise AdminError(
            f"--user-id 를 canonical lowercase UUID로 넣어라 (받은 값: {candidate!r}, "
            f"기대 형식: {parsed})"
        )
    return candidate


def validated_delta(delta: int) -> int:
    """방향과 크기만 본다. **정책은 원장이 정한다** — 여기서 새로 만들지 않는다."""
    if delta == 0:
        raise AdminError("--delta 0 은 거절한다. 아무 일도 일어나지 않을 조정을 원장에 남기지 않는다.")
    if abs(delta) > MAX_DELTA:
        raise AdminError(f"--delta 가 너무 크다 (한도 {MAX_DELTA}).")
    return delta


def validated_note(note: str) -> str:
    note = note.strip()
    if not note:
        raise AdminError("--note 는 비워 둘 수 없다. 무엇 때문에 조정하는지 적어라.")
    return note


def new_event_id() -> str:
    return f"admin:{uuid.uuid4()}"


# MARK: - 출력


def format_plan(adjustment: Adjustment, wallet: ShardWallet, already_applied: bool) -> str:
    after = wallet.balance + adjustment.delta
    lines = [
        "----------------------------------------",
        "Ggumirror Admin Shard Adjustment",
        "----------------------------------------",
        f"Project        : {adjustment.project}",
        f"Database       : {adjustment.database}",
        f"User           : {adjustment.user_id}",
        f"Current        : {wallet.balance}",
        f"Adjustment     : {adjustment.delta:+d}",
        f"After          : {after}",
        f"Reason         : {REASON.value}",
        f"Event ID       : {adjustment.event_id}",
        f"Note           : {adjustment.note}",
        "----------------------------------------",
    ]
    if after < 0:
        # 정책은 원장이 집행한다(`balance < 0` → InsufficientShards). 미리 알려만 준다.
        lines.append("⚠ 잔액이 음수가 된다 — 원장이 거절한다(아무것도 기록되지 않는다).")
    if already_applied:
        # 표시 전용 조회다. "적었는가"의 authority는 여전히 원장의 원자적 쓰기다.
        lines.append("⚠ 이 event id는 이미 반영돼 있다 — 재실행해도 잔액이 움직이지 않는다.")
    return "\n".join(lines)


def ask_tty(prompt: str = "Apply adjustment? [y/N] ") -> bool:
    """기본값은 **N**이다. tty가 아니면 묻지 않고 거절한다 —
    파이프로 들어온 실행이 조용히 통과하면 안 된다(`--yes`를 쓰게 한다)."""
    if not sys.stdin.isatty():
        return False
    return input(prompt).strip().lower() in {"y", "yes"}


# MARK: - 실행


def run(
    adjustment: Adjustment,
    *,
    service: ShardLedgerService,
    users: UserLookup,
    recorded_delta: RecordedDelta,
    confirm: Callable[[], bool],
    dry_run: bool = False,
    out: TextIO = sys.stdout,
) -> int:
    """0 = 반영됨(중복 포함), 그 외 = 아무것도 쓰지 않고 끝났다."""
    if users.user(adjustment.user_id) is None:
        raise AdminError(f"user 를 찾을 수 없다: {adjustment.user_id}")

    recorded = recorded_delta(adjustment.user_id, REASON, adjustment.event_id)
    if recorded is not None and recorded != adjustment.delta:
        # 경제적으로는 원장이 이미 막는다(중복 지급 없음). 여기서 막는 것은 **혼동**이다 —
        # "Already applied"만 보고 요청한 +100이 반영됐다고 읽으면 안 된다.
        raise AdminError(
            f'event id "{adjustment.event_id}" was already used with delta {recorded:+d}. '
            f"requested delta is {adjustment.delta:+d}. "
            "Use a new event id for a different adjustment."
        )

    wallet = service.wallet(adjustment.user_id)
    print(format_plan(adjustment, wallet, recorded is not None), file=out)

    if dry_run:
        print("Dry run — nothing was written.", file=out)
        return 0

    if not confirm():
        print("Aborted — nothing was written.", file=out)
        return 1

    # 원장이 부호를 정한다. 음수를 그대로 넘겨 방향을 뒤집지 않는다.
    if adjustment.delta > 0:
        result = service.credit(
            adjustment.user_id, adjustment.delta, REASON, external_event_id=adjustment.event_id
        )
    else:
        result = service.debit(
            adjustment.user_id, -adjustment.delta, REASON, external_event_id=adjustment.event_id
        )

    if result.applied:
        print(f"Applied. balance={result.wallet.balance}", file=out)
    else:
        print(
            f"Already applied for this event id — nothing written. "
            f"balance={result.wallet.balance}",
            file=out,
        )
    # note는 여기까지다. ledger schema에 note 자리가 없어서 Firestore에 남지 않는다.
    print(f"audit: reason={REASON.value} event_id={adjustment.event_id} note={adjustment.note}", file=out)
    return 0


# MARK: - 조립


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="admin_shards.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "꾸미러 거울조각을 운영자가 지급/회수한다.\n"
            f"B-3 원장에 reason={REASON.value} 줄을 쌓는다 — wallet을 직접 고치지 않는다.\n"
            f"project는 {PROJECT} 로 고정이다."
        ),
        epilog=(
            "예시:\n"
            "  # 100 지급\n"
            "  python3 ggumirror-be/scripts/admin_shards.py \\\n"
            '      --user-id "<uid>" --delta 100 --note "AI sticker E2E"\n'
            "\n"
            "  # 20 회수 (지운 게 아니라 반대 부호 줄이 하나 더 쌓인다)\n"
            "  python3 ggumirror-be/scripts/admin_shards.py \\\n"
            '      --user-id "<uid>" --delta -20 --note "E2E 정리"\n'
            "\n"
            "  # 결과가 애매할 때: 같은 event id로 재실행하면 딱 한 번만 반영된다\n"
            "  python3 ggumirror-be/scripts/admin_shards.py \\\n"
            '      --user-id "<uid>" --delta 100 --note "AI sticker E2E" \\\n'
            '      --event-id "admin:..."\n'
            "\n"
            "Firestore Console에서 wallet balance를 직접 수정하지 마라 — 원장과 갈라진다.\n"
        ),
    )
    parser.add_argument("--user-id", required=True, help="내부 꾸미러 user id (canonical UUID)")
    parser.add_argument(
        "--delta", required=True, type=int, help="양수=지급, 음수=회수, 0=거절"
    )
    parser.add_argument("--note", required=True, help="왜 조정하는지 (사람이 읽을 목적)")
    parser.add_argument(
        "--event-id",
        default=None,
        help="idempotency 열쇠. 생략하면 admin:<uuid>를 만든다. 같은 값 재실행은 한 번만 반영된다",
    )
    parser.add_argument(
        "--yes", action="store_true", help="확인 질문을 건너뛴다 (없으면 항상 물어본다)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="계획만 보여주고 아무것도 쓰지 않는다"
    )
    parser.add_argument(
        "--project",
        default=None,
        help=f"확인용. {PROJECT} 외의 값은 거절한다",
    )
    return parser


def adjustment_from_args(args: argparse.Namespace, env: Mapping[str, str]) -> Adjustment:
    project = resolve_project(args.project, env)
    return Adjustment(
        user_id=validated_user_id(args.user_id),
        delta=validated_delta(args.delta),
        note=validated_note(args.note),
        event_id=(args.event_id or "").strip() or new_event_id(),
        project=project,
        database=(env.get("FIRESTORE_DATABASE") or "").strip() or DATABASE_DEFAULT,
    )


def _ensure_dependencies() -> None:
    """`python3`로 불려도 동작하게 한다 — repo venv로 한 번 다시 띄운다.

    system python3에는 google-cloud-firestore가 없다. 여기서 멈추지 않으면
    운영자가 import 오류만 보고 무엇을 해야 하는지 모른다.
    """
    try:
        import google.cloud.firestore  # noqa: F401
        return
    except ModuleNotFoundError:
        pass

    venv = BACKEND_ROOT / ".venv" / "bin" / "python"
    current = Path(sys.executable).resolve()
    if not venv.exists() or current == venv.resolve():
        raise AdminError(
            "google-cloud-firestore 가 없다. backend venv를 먼저 만들어라:\n"
            f"  cd {BACKEND_ROOT} && python3 -m venv .venv && "
            ".venv/bin/pip install -r requirements.txt"
        )
    script = Path(__file__).resolve()
    print(f"(re-exec with {venv})", file=sys.stderr)
    os.execv(str(venv), [str(venv), str(script), *sys.argv[1:]])


def _build_deps(
    adjustment: Adjustment,
) -> tuple[ShardLedgerService, UserLookup, RecordedDelta]:
    """production과 **같은 방식**으로 client를 만든다(`app/main.py`).

    project를 명시적으로 넘기는 것이 핵심이다 — ADC가 들고 있는 기본 project
    (이 머신에서는 DailyOPIc)를 절대 쓰지 않는다.
    """
    from google.cloud import firestore

    from app.auth.firestore_store import FirestoreAuthStore
    from app.shards.firestore_store import LEDGER, FirestoreShardStore

    client = firestore.Client(project=adjustment.project, database=adjustment.database)

    def recorded_delta(user_id: str, reason: ShardReason, event_id: str) -> int | None:
        """원장 문서 하나를 **읽기만** 한다. 문서 ID 규칙은 B-3의 것을 그대로 쓴다 —
        여기서 열쇠를 새로 만들면 언젠가 service와 어긋난다."""
        snapshot = (
            client.collection(LEDGER)
            .document(idempotency_hash(user_id, reason, event_id))
            .get()
        )
        if not snapshot.exists:
            return None
        return int((snapshot.to_dict() or {}).get("delta") or 0)

    return (
        ShardLedgerService(FirestoreShardStore(client)),
        FirestoreAuthStore(client),
        recorded_delta,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _ensure_dependencies()
        adjustment = adjustment_from_args(args, os.environ)
        service, users, recorded_delta = _build_deps(adjustment)
        return run(
            adjustment,
            service=service,
            users=users,
            recorded_delta=recorded_delta,
            confirm=(lambda: True) if args.yes else ask_tty,
            dry_run=args.dry_run,
        )
    except AdminError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except InvalidShardAmount as error:
        print(f"error: 원장이 금액을 거절했다 ({error}).", file=sys.stderr)
        return 2
    except InsufficientShards:
        print("error: 잔액이 모자라 회수할 수 없다 — 아무것도 기록되지 않았다.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
