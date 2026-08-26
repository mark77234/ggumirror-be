"""AI 스티커 생성 모델.

**프롬프트 원문을 저장하지 않는다.** provider에게 보내는 그 순간에만 존재하고,
응답을 만들고 나면 사라진다. 로그에도 길이만 남긴다 —
사용자가 무엇을 만들었는지는 서버가 알 필요가 없다.

생성물(이미지)은 **짧은 기간만** 서버에 남는다. 영구 보관소가 아니라
**응답 유실 복구창**이다 — 기기가 받아 가면 그만이고, 못 받았을 때 다시 받을 자리다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from app.shards.models import utcnow

# 한 장에 6 조각. **client가 정하지 않는다** — 값은 여기 한 곳에만 있고
# 응답으로 내려보내 client가 그대로 표시하게 한다.
DEFAULT_STICKER_PRICE = 6

#: AI 거울 한 장. **스티커와 따로 둔다** — 한 값을 공유하면 한쪽을 바꿀 때
#: 다른 쪽이 조용히 따라 움직인다. 거울은 더 크고 provider 비용도 더 든다.
DEFAULT_MIRROR_PRICE = 10

# 프롬프트 길이 상한. 길다고 좋은 그림이 나오지 않고, provider 요금만 늘어난다.
MAX_PROMPT_LENGTH = 200

# provider가 만들 정사각 이미지 한 변. client의 `StickerCanvas.size`(1024)와 맞춘다.
IMAGE_SIZE = 1024

# client가 만드는 requestId의 최대 길이. UUID면 36자다.
MAX_REQUEST_ID_LENGTH = 64

# 결과 이미지를 서버에 남겨 두는 기간. **영구 보관이 아니다.**
#
# 기기가 정상적으로 받아 `UserStickerAssets`에 저장했더라도 그동안은 남는다 —
# 앱을 지웠다 깔거나, 저장 직전에 죽었거나, 응답만 유실된 경우를 위해서다.
# 7일이면 "만들어 두고 며칠 뒤에 여는" 사용 패턴까지 덮으면서도
# 서버가 사용자 콘텐츠 보관소가 되지는 않는다.
RESULT_RETENTION = timedelta(days=7)

# lease 길이. **소유권 표시일 뿐, 죽음의 증거가 아니다.**
#
# ⚠️ 예전에 여기 "Cloud Run timeout이 지나면 process가 죽으므로 lease 만료 =
# worker 사망"이라고 적혀 있었다. **틀렸다.** Cloud Run의 request timeout은
# client 연결을 끊고 504를 돌려줄 뿐이고, **container instance를 종료하지 않는다.**
# 그 요청을 처리하던 application code는 계속 돌 수 있고, 한참 뒤에 provider 응답을
# 받아 upload까지 마칠 수도 있다.
#
# 그래서 "죽었을 것"이라는 추론에 경제를 걸지 않는다. 안전은 시간이 아니라
# **CAS(lease 일치 + terminal 상태 금지)**가 만든다. lease는 "지금 누가 들고 있다고
# 주장하는가"를 나타내는 version token이고, 그 이상을 뜻하지 않는다.
GENERATION_LEASE = timedelta(seconds=300)

# 시간만 보고 환불하기까지 기다리는 시간. **worker가 살아 있을 수 있기 때문에** 길게 잡는다.
#
# 정상 worker의 최대 수명: provider HTTP timeout(90초) + upload + 여유.
# 그보다 훨씬 길게 두면 "아직 도는 중인데 환불"이 사실상 일어나지 않고,
# 그래도 일어난다면 CAS가 막는다(늦은 성공이 refunded를 뒤집지 못한다).
#
# 이 값을 줄이면 조각이 빨리 풀리는 대신 헛된 환불과 orphan object가 늘어난다.
RECOVERY_GRACE = timedelta(minutes=15)


class AIStickerReason(StrEnum):
    """생성이 안 된 이유. **client에게는 분류만 준다** — provider 내부 사정을 옮기지 않는다."""

    NOT_CONFIGURED = "not_configured"
    EMPTY_PROMPT = "empty_prompt"
    PROMPT_TOO_LONG = "prompt_too_long"
    INVALID_REQUEST_ID = "invalid_request_id"
    PROVIDER_REJECTED = "provider_rejected"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    INSUFFICIENT_SHARDS = "insufficient_shards"
    # 결과를 durable하게 두지 못했다. 조각은 돌려준다 —
    # 저장하지 못한 그림은 없는 것과 같고, 응답만 주면 유실 시 복구할 방법이 없다.
    STORAGE_FAILED = "storage_failed"
    # 남의 생성이거나 없는 생성.
    NOT_FOUND = "not_found"
    # 성공했지만 보관 기간이 지나 이미지가 사라졌다.
    RESULT_EXPIRED = "result_expired"
    # 아직 만드는 중이다(오류가 아니다).
    STILL_PENDING = "still_pending"
    # process가 중간에 죽어서 되돌렸다.
    INTERRUPTED = "interrupted"


class GenerationStatus(StrEnum):
    """생성 작업의 상태. **서버가 authoritative하다.**

    전이는 이 넷 사이에서만 일어나고, 전부 transaction 안에서 조건부로 바뀐다:

        pending ─┬─▶ succeeded            결과가 durable storage에 있다
                 ├─▶ refunded             실패했고 조각을 돌려줬다
                 └─▶ failed ──▶ refunded  실패했는데 환불까지 못 했다(나중에 되돌린다)

    `pending`은 §4의 `created → debited → generating`을 모두 담는다 —
    셋은 **한 transaction 안에서 함께** 일어나므로(문서 생성 + 차감) 따로 볼 수 없고,
    나눠 두면 있을 수 없는 상태(`created`인데 차감됨)를 표현할 수 있게 된다.
    지금 누가 들고 있는지는 `lease_expires_at`이 말한다.
    """

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUNDED = "refunded"

    @property
    def is_terminal(self) -> bool:
        """**두 번 다시 바뀌지 않는 상태인가.**

        `failed`는 환불이 남아 있어 terminal이 아니다 — 아직 `refunded`로 갈 수 있다.
        """
        return self in (GenerationStatus.SUCCEEDED, GenerationStatus.REFUNDED)


def can_transition(current: GenerationStatus, target: GenerationStatus) -> bool:
    """상태 전이가 허용되는가. **terminal에서 나가는 길은 없다.**

    이 함수가 경제의 마지막 방어선이다. 늦게 돌아온 worker가
    `refunded → succeeded`(공짜 결과)나 `succeeded → refunded`(공짜 조각)를
    만들지 못하게 한다. lease가 어쩌다 맞아떨어져도 여기서 막힌다.
    """
    if current.is_terminal:
        return False
    if current is GenerationStatus.FAILED:
        # 환불만 남았다. 실패한 작업이 뒤늦게 성공이 되지 않는다.
        return target is GenerationStatus.REFUNDED
    return target is not GenerationStatus.PENDING


# 사용자에게 조각이 돌아간 상태.
REFUNDED_REASONS = frozenset({
    AIStickerReason.PROVIDER_REJECTED,
    AIStickerReason.PROVIDER_UNAVAILABLE,
    AIStickerReason.STORAGE_FAILED,
    AIStickerReason.INTERRUPTED,
})


class AIStickerError(Exception):
    """생성 실패. endpoint가 HTTP 응답으로 바꾼다."""

    def __init__(self, reason: AIStickerReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


def generation_id(user_id: str, request_id: str) -> str:
    """`(user, requestId)` → 생성 작업 ID. **결정적이다.**

    별도 index 문서를 두지 않는다 — 이 값이 곧 Firestore document ID이고,
    transaction 안의 `create()`가 "같은 requestId로 두 번 만들 수 없음"을 **구조적으로** 보장한다.
    조회해서 없으면 만드는 방식이면 그 사이에 두 요청이 들어와 둘 다 만든다.

    길이 접두사 canonical encoding은 원장(`idempotency_hash`)과 같은 이유다:
    값에 `:`나 `|`가 들어 있어도 다른 조합과 같은 문자열이 되지 않는다.

    raw user id도 requestId도 문서 ID에 남지 않는다.
    """
    canonical = "|".join(f"{len(part.encode())}:{part}" for part in (user_id, request_id))
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class Generation:
    """생성 작업 하나. **조각이 움직인 사실과 결과가 같은 문서에 묶여 있다.**

    프롬프트 원문은 없다. `debit_entry_id` / `refund_entry_id`로 원장을 가리키므로
    "이 조각이 어디에 쓰였고 돌아왔는지"를 감사할 수 있다.
    """

    id: str
    user_id: str
    status: GenerationStatus
    price: int
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    # 지금 이 작업을 들고 있는 요청의 임차 만료 시각. 지나면 그 process는 죽은 것이다.
    lease_expires_at: datetime | None = None
    # 원장 참조. 금액이 아니라 **그 줄을 가리키는 external event id**(원장 멱등 키)다.
    # 결정적이라 문서를 잃어도 다시 계산할 수 있지만, 감사할 때 문서만 보고
    # 원장 줄을 찾을 수 있어야 하므로 적어 둔다.
    debit_entry_id: str | None = None
    refund_entry_id: str | None = None
    # 성공했을 때만. bucket 안의 object 이름이고 URL이 아니다.
    result_object: str | None = None
    result_expires_at: datetime | None = None
    # 실패했을 때만. 안전한 분류값이고 provider 문구가 아니다.
    failure_reason: AIStickerReason | None = None

    @property
    def refund_event_id(self) -> str:
        """환불 원장 키. **결정적이다** — 몇 번 되돌려도 원장에는 한 줄뿐이다."""
        return f"ai_sticker_refund:{self.id}"

    @property
    def is_lease_expired(self) -> bool:
        """아무도 소유권을 주장하고 있지 않은가.

        **worker가 죽었다는 뜻이 아니다.** Cloud Run은 timeout에서 container를 죽이지
        않으므로, 예전 worker가 아직 provider 응답을 기다리는 중일 수 있다.
        이 값은 "임차권을 가져가도 되는가"만 답하고, 환불 여부는 `is_recoverable`이 정한다.
        """
        if self.status is not GenerationStatus.PENDING:
            return False
        return self.lease_expires_at is None or self.lease_expires_at <= utcnow()

    @property
    def is_recoverable(self) -> bool:
        """시간만 보고 정리해도 되는가. **lease보다 훨씬 보수적이다.**

        정상 worker의 최대 수명(provider timeout + upload)을 한참 넘겼을 때만 True다.
        살아 있는 worker를 죽었다고 보고 환불하는 일을 줄인다 —
        그래도 일어나면 CAS가 막지만, 애초에 덜 일어나는 편이 낫다.
        """
        if self.status is not GenerationStatus.PENDING:
            return False
        return self.created_at + RECOVERY_GRACE <= utcnow()

    @property
    def is_result_available(self) -> bool:
        """지금 이미지를 다시 받을 수 있는가."""
        if self.status is not GenerationStatus.SUCCEEDED or self.result_object is None:
            return False
        return self.result_expires_at is None or self.result_expires_at > utcnow()
