"""기존 이름을 unique index에 등록한다. **이름을 바꾸지 않는다.**

새로 들어온 uniqueness 규칙은 claim collection **하나만** 본다
(`set_display_name` · `publish`가 사용자 문서나 listing을 다시 훑지 않는다).
그래서 규칙이 생기기 전에 만들어진 이름은 자리를 잡고 있지 않고,
**새 사용자가 그 이름을 그대로 가져갈 수 있다.** 이 script가 그 구멍을 메운다.

하는 일은 하나뿐이다: 이미 존재하는 이름을 그 값 그대로 claim 문서로 만든다.

절대 하지 않는 것 — 이름 변경 · 제목 변경 · listing 삭제 · 겹칠 때 승자 고르기.
겹치는 것이 하나라도 보이면 **아무것도 쓰지 않고 멈춘다**: 누구의 이름인지는
운영자가 사람에게 물어야 할 문제이고 script가 정할 일이 아니다.

두 번 돌려도 결과가 같다. 이미 있는 claim은 건드리지 않고, 만드는 것은
`create()`라 그 사이에 누가 먼저 잡으면 우리가 진다(그쪽이 실제 사용자다).

    python3 scripts/backfill_name_claims.py --dry-run
    python3 scripts/backfill_name_claims.py --apply --yes
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from dataclasses import dataclass, field
from datetime import datetime, timezone

#: `admin_shards.py`와 **같은 상수**다. 이 머신의 gcloud 기본 project는
#: DailyOPIc이라, 기본값을 믿으면 남의 production을 만진다.
PROJECT = "ggumirror-prod"
PROJECT_ENV_VARS = (
    "GCP_PROJECT_ID",
    "GOOGLE_CLOUD_PROJECT",
    "GCLOUD_PROJECT",
    "CLOUDSDK_CORE_PROJECT",
)

USERS = "ggumirror_users"
USERNAME_CLAIMS = "ggumirror_username_claims"
LISTINGS = "ggumirror_marketplace_listings"
LISTING_TITLE_CLAIMS = "ggumirror_listing_title_claims"

#: 이름 자리를 쥐고 있어야 하는 상태. `unpublish` · `delete`가 자리를 놓으므로
#: (`_release_title`) 그 둘은 대상이 아니다. **운영자가 내린 것(moderationStatus
#: = removed)은 여전히 `published`라 자리를 지킨다** — 되살리면 다시 공개된다.
CLAIMED_STATUS = "published"


class BackfillError(RuntimeError):
    pass


def guard_project(requested: str | None, environ) -> str:
    if requested is not None and requested != PROJECT:
        raise BackfillError(f"--project={requested!r} 은 허용되지 않는다. 이 script는 {PROJECT} 전용이다.")
    for name in PROJECT_ENV_VARS:
        value = environ.get(name)
        if value and value != PROJECT:
            raise BackfillError(
                f"{name}={value!r} 가 다른 project를 가리킨다. 이 script는 {PROJECT} 전용이다."
            )
    return PROJECT


@dataclass(frozen=True)
class Row:
    """claim 하나가 대표하는 기존 값."""

    key: str
    owner_id: str
    display: str
    #: claim 문서에 함께 적을 값. **실제 경로가 쓰는 모양을 그대로 따른다** —
    #: backfill한 자리만 field가 빠져 있으면 나중에 읽는 쪽이 둘을 구분해야 한다.
    extra: dict = field(default_factory=dict)


@dataclass
class Audit:
    rows: list[Row]
    existing: dict[str, str]      # key -> 이미 claim을 쥐고 있는 owner id
    missing: list[Row]
    conflicts: list[tuple[str, list[Row]]]        # 같은 열쇠를 두 record가 원한다
    mismatched: list[tuple[Row, str]]             # claim은 있는데 주인이 다르다

    @property
    def is_safe(self) -> bool:
        return not self.conflicts and not self.mismatched


def audit(rows: list[Row], claims: dict[str, str]) -> Audit:
    by_key: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        by_key[row.key].append(row)

    conflicts = [(key, found) for key, found in by_key.items() if len(found) > 1]
    missing: list[Row] = []
    mismatched: list[tuple[Row, str]] = []
    for key, found in by_key.items():
        if len(found) > 1:
            continue
        row = found[0]
        holder = claims.get(key)
        if holder is None:
            missing.append(row)
        elif holder != row.owner_id:
            # **조용히 덮어쓰지 않는다.** 이미 남이 잡은 자리다.
            mismatched.append((row, holder))
    return Audit(rows, claims, missing, conflicts, mismatched)


def username_rows(db) -> list[Row]:
    from app.auth.profile import display_name_key

    rows = []
    for doc in db.collection(USERS).stream():
        name = (doc.to_dict() or {}).get("displayName")
        if isinstance(name, str) and name.strip():
            rows.append(Row(display_name_key(name), doc.id, name))
    return rows


def listing_rows(db) -> list[Row]:
    from app.marketplace.models import listing_title_key

    rows = []
    for doc in db.collection(LISTINGS).stream():
        data = doc.to_dict() or {}
        if data.get("status") != CLAIMED_STATUS:
            continue
        title = data.get("title")
        if isinstance(title, str) and title.strip():
            rows.append(Row(
                listing_title_key(title), doc.id, title,
                {"sellerUserId": data.get("sellerUserId", "")},
            ))
    return rows


def read_claims(db, collection: str, owner_field: str) -> dict[str, str]:
    return {
        doc.id: (doc.to_dict() or {}).get(owner_field, "")
        for doc in db.collection(collection).stream()
    }


def create_claims(db, collection: str, rows: list[Row], payload) -> tuple[int, int]:
    """`create()`로 하나씩 쓴다. **이미 있으면 진다** — 그쪽이 실제 사용자다.

    batch를 쓰지 않는 이유: batch의 `create`는 하나가 충돌하면 묶음 전체가
    실패해서, 어느 것이 문제였는지 모른 채 나머지도 못 쓴다. 수가 작다.
    """
    from google.api_core import exceptions as gcp_exceptions

    created = skipped = 0
    for row in rows:
        try:
            db.collection(collection).document(row.key).create(payload(row))
            created += 1
        except gcp_exceptions.AlreadyExists:
            # 두 번째 실행이거나, 그 사이에 실제 사용자가 잡았다. 둘 다 정상이다.
            skipped += 1
    return created, skipped


def report(label: str, rows: list[Row], claims: dict[str, str], result: Audit) -> None:
    print(f"\n[{label}]")
    print(f"  expected claims : {len({r.key for r in rows})}  (records: {len(rows)})")
    print(f"  existing claims : {len(claims)}")
    print(f"  missing claims  : {len(result.missing)}")
    print(f"  conflicts       : {len(result.conflicts)}")
    print(f"  owner mismatch  : {len(result.mismatched)}")
    for key, found in result.conflicts:
        print(f"    ! 겹침 {key!r}: " + ", ".join(f"{r.owner_id}({r.display!r})" for r in found))
    for row, holder in result.mismatched:
        print(f"    ! 자리 주인 다름 {row.key!r}: 문서={row.owner_id} claim={holder}")
    for row in result.missing:
        print(f"    + {row.key!r} <- {row.owner_id} ({row.display!r})")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", help=f"확인용. {PROJECT} 외의 값은 거절한다")
    parser.add_argument("--apply", action="store_true", help="실제로 claim 문서를 만든다")
    parser.add_argument("--yes", action="store_true", help="확인 질문을 건너뛴다")
    args = parser.parse_args(argv)

    try:
        project = guard_project(args.project, os.environ)
    except BackfillError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    from google.cloud import firestore

    db = firestore.Client(project=project)
    print(f"project: {project}   mode: {'APPLY' if args.apply else 'DRY-RUN'}")

    users = username_rows(db)
    user_claims = read_claims(db, USERNAME_CLAIMS, "userId")
    user_audit = audit(users, user_claims)
    report("username", users, user_claims, user_audit)

    listings = listing_rows(db)
    title_claims = read_claims(db, LISTING_TITLE_CLAIMS, "listingId")
    title_audit = audit(listings, title_claims)
    report("listing title", listings, title_claims, title_audit)

    if not (user_audit.is_safe and title_audit.is_safe):
        # **겹치는 이름의 승자를 script가 고르지 않는다.**
        print("\nBLOCKED: 겹치거나 주인이 다른 이름이 있다. 아무것도 쓰지 않았다.", file=sys.stderr)
        return 1

    total = len(user_audit.missing) + len(title_audit.missing)
    if not args.apply:
        print(f"\ndry-run: 만들 claim {total}개. 쓰지 않았다.")
        return 0
    if total == 0:
        print("\n만들 것이 없다. 이미 전부 등록돼 있다.")
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            print("error: tty가 아니다. 자동 실행은 --yes 를 명시해라.", file=sys.stderr)
            return 2
        if input(f"claim {total}개를 만든다. 계속? [y/N] ").strip().lower() != "y":
            print("취소했다.")
            return 1

    now = datetime.now(timezone.utc)
    made_users, skipped_users = create_claims(
        db, USERNAME_CLAIMS, user_audit.missing,
        lambda row: {"userId": row.owner_id, "createdAt": now, "backfilled": True, **row.extra},
    )
    made_titles, skipped_titles = create_claims(
        db, LISTING_TITLE_CLAIMS, title_audit.missing,
        lambda row: {"listingId": row.owner_id, "createdAt": now, "backfilled": True, **row.extra},
    )
    print(f"\ncreated username claims: {made_users} (already there: {skipped_users})")
    print(f"created listing claims : {made_titles} (already there: {skipped_titles})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
