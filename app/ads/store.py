"""광고 보상 context 저장소.

## 왜 context가 필요한가

Google SSV callback은 **우리 세션을 모른다.** "누구에게 줄 조각인가"를 알려면
client가 광고 요청에 무언가를 실어 보내야 하고, Google이 그것을 서명된 callback에
그대로 담아 돌려준다.

거기에 **넣으면 안 되는 것**: 서버 session token · Apple identity token · 내부 user UUID.
callback URL은 로그 · 중계 서버 · 재시도 기록에 남는다. 세션이 새면 계정이 털리고,
내부 UUID가 노출되면 남의 id를 넣어 남에게 조각을 주거나 관계를 추적할 수 있다.

## 그래서 opaque context

로그인한 사용자가 광고를 보기 직전에 서버가 **짧게 사는 무의미한 문자열**을 하나 발급하고,
그것만 Google에 간다. callback이 오면 서버가 그 문자열로 사용자를 되찾는다.

session과 **똑같은 규칙**을 쓴다 — `secrets.token_urlsafe`, 저장은 `sha256`만,
document ID도 hash, 만료는 서버 시계.

### 왜 signed stateless token이 아닌가

서명 token이면 저장소가 없어도 되지만 **새 server secret**이 필요하다.
지금 이 서비스에는 secret이 하나도 없고(Secret Manager도 만들지 않았다),
보상 하나를 위해 secret 관리 · rotation · 유출 대응을 새로 들이는 것보다
이미 있는 opaque-token + hash 저장 패턴을 재사용하는 쪽이 작고 안전하다.
게다가 저장형은 **서버가 취소할 수 있다** — 서명 token은 만료 전까지 못 막는다.

영구 collection이 되지 않도록 수명을 짧게 두고, 만료된 문서는 읽는 쪽에서 거절한다.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from app.auth.models import sha256_hex
from app.shards.models import utcnow

# 광고를 띄우고 보고 SSV가 도착하기까지. Google 재시도까지 감안해 넉넉하되 하루를 넘기지 않는다.
CONTEXT_LIFETIME = timedelta(hours=6)


@dataclass(frozen=True)
class RewardContext:
    """`sha256(token) → user`. **raw token을 저장하지 않는다.**"""

    token_hash: str
    user_id: str
    expires_at: datetime
    created_at: datetime

    def is_valid(self, now: datetime | None = None) -> bool:
        return (now or utcnow()) < self.expires_at


def new_context(user_id: str, token: str, now: datetime | None = None) -> RewardContext:
    moment = now or utcnow()
    return RewardContext(
        token_hash=sha256_hex(token),
        user_id=user_id,
        expires_at=moment + CONTEXT_LIFETIME,
        created_at=moment,
    )


class RewardContextStore(Protocol):
    def save(self, context: RewardContext) -> None:
        """발급. 같은 hash가 다시 올 일은 사실상 없다(무작위 token)."""

    def context(self, token_hash: str) -> RewardContext | None:
        """저장된 context. 없으면 `None`.

        **만료 여부로 거르지 않고 그대로 돌려준다.** "우리가 발급한 적 없다"와
        "발급했는데 시간이 지났다"는 다른 사건이고, 로그에서 구분돼야
        실제 광고 흐름이 느려진 것인지 엉뚱한 값이 온 것인지 알 수 있다.
        만료 판단은 부르는 쪽이 한다.

        **소비(삭제)하지 않는다.** Google이 같은 callback을 재전송할 수 있고,
        그때도 같은 사용자로 풀려야 한다. 중복 지급은 원장 idempotency가 막는다.
        """


class InMemoryRewardContextStore:
    """test / local용."""

    def __init__(self) -> None:
        self.contexts: dict[str, RewardContext] = {}
        self._lock = threading.Lock()

    def save(self, context: RewardContext) -> None:
        with self._lock:
            self.contexts[context.token_hash] = context

    def context(self, token_hash: str) -> RewardContext | None:
        with self._lock:
            return self.contexts.get(token_hash)
