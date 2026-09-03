"""사용자에게 보이는 이름. **경제도 신원도 아니다** — 표시용 값 하나다.

신원의 authority는 검증된 Apple identity token의 `sub`이고 그것으로 만든 내부
user id다. Apple이 주는 `fullName`은 **서명된 claim이 아니라서** 신원·권한·소유권
판단에 쓰지 않는다. 여기서는 처음 이름을 채워 주는 용도로만 쓴다.

이름을 자주 바꾸면 상점의 판매자 표시가 계속 흔들린다. 그래서 30일에 한 번이고,
그 판단은 **서버 시계**가 한다 — 기기 시계를 믿으면 바꿔 가며 우회할 수 있다.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta

#: 사람이 보는 글자 수 기준. **byte 길이를 쓰지 않는다** — 한글 이름이 부당하게 막힌다.
MAX_DISPLAY_NAME_LENGTH = 20
MIN_DISPLAY_NAME_LENGTH = 1
#: 이름 변경 간격.
RENAME_COOLDOWN = timedelta(days=30)


class InvalidDisplayName(ValueError):
    """비었거나, 줄바꿈/제어문자가 있거나, 너무 길다."""


class DisplayNameTaken(Exception):
    """다른 사람이 이미 쓰고 있는 이름이다.

    판단은 **서버 transaction**이 한다 — client가 "검색해 보니 없더라"로 정하면
    동시에 같은 이름을 적은 두 사람이 둘 다 통과한다.
    """


class DisplayNameCooldown(Exception):
    """아직 바꿀 수 없다. 언제부터 되는지 함께 들고 있다."""

    def __init__(self, available_at: datetime) -> None:
        super().__init__("display name change is on cooldown")
        self.available_at = available_at


def normalize_display_name(raw: str) -> str:
    """앞뒤 공백을 다듬고 한 줄로 만든다. 이상하면 거절한다.

    특수문자를 넓게 막지 않는다 — 이름은 사람마다 다르고, 막는 쪽이 틀릴 때가 많다.
    막는 것은 **보이지 않는 것**뿐이다: 제어문자와 줄바꿈.
    """
    if not isinstance(raw, str):
        raise InvalidDisplayName("display name must be a string")

    # 자모가 분리된 입력(macOS/iOS 한글)도 같은 이름으로 센다.
    text = unicodedata.normalize("NFC", raw).strip()
    if not text:
        raise InvalidDisplayName("display name is empty")
    if any(unicodedata.category(ch) in {"Cc", "Cf", "Zl", "Zp"} for ch in text):
        raise InvalidDisplayName("display name contains control characters")

    # grapheme 근사치로 len()을 쓴다. 정확한 grapheme clustering까지는 필요 없고,
    # byte 길이보다 사람이 보는 길이에 훨씬 가깝다.
    if len(text) < MIN_DISPLAY_NAME_LENGTH or len(text) > MAX_DISPLAY_NAME_LENGTH:
        raise InvalidDisplayName("display name length is out of range")
    return text


def display_name_key(raw: str) -> str:
    """**같은 이름인지 판단하는 열쇠.** 표시용 이름을 바꾸지 않는다.

    `찬찡` · ` 찬찡 `은 같은 이름이고, `Mark` · `mark` · `MARK`도 같은 이름이다.
    한글은 자모가 분리돼 들어와도(NFD) 같은 이름으로 센다 —
    눈에 같아 보이는 두 이름이 서로 다른 사람의 것이 되면 안 된다.

    `casefold()`를 쓴다. `lower()`보다 넓게 접어서 독일어 `ß`/`ss` 같은 경우까지 같다.
    """
    text = unicodedata.normalize("NFC", raw).strip()
    return text.casefold()


def next_change_at(changed_at: datetime | None) -> datetime | None:
    """다음에 바꿀 수 있는 시각. 한 번도 바꾼 적 없으면 `None`(= 지금 가능)."""
    return None if changed_at is None else changed_at + RENAME_COOLDOWN


def can_change(changed_at: datetime | None, now: datetime) -> bool:
    """지금 바꿀 수 있는가.

    **아직 이름이 없는 사용자는 언제든 처음 이름을 정할 수 있다** —
    Apple이 넣어 준 최초 값은 `changed_at`을 남기지 않기 때문이다(§10).
    """
    available = next_change_at(changed_at)
    return available is None or now >= available


@dataclass(frozen=True)
class ProfileView:
    """화면에 필요한 값만. **공개 표면에는 `display_name`만 나간다.**"""

    display_name: str | None
    display_name_changed_at: datetime | None
    now: datetime

    @property
    def can_change_display_name(self) -> bool:
        return can_change(self.display_name_changed_at, self.now)

    @property
    def next_display_name_change_at(self) -> datetime | None:
        return next_change_at(self.display_name_changed_at)
