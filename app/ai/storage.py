"""생성 결과 보관소.

**응답 유실 복구를 위한 자리이지 사용자 저장소가 아니다.** 스티커의 주인은 기기다
(`UserStickerAssets/<id>.png`). 여기 있는 것은 짧은 복구창 동안의 사본이다.

## 왜 signed URL이 아니라 우리 endpoint인가 (§6의 A안)

signed URL은 **그 자체가 credential**이다. client 로그 · crash report · proxy ·
Cloud Run request log 어디에 한 번 찍히면 그 URL을 가진 누구나 이미지를 받는다.
우리에게는 이미 Bearer session이 있고 소유자 검증도 해야 하므로,
`GET /ai/stickers/{id}/image`로 스트리밍하면 **새 credential을 만들지 않고** 끝난다.

bucket은 완전 private이고 public ACL을 쓰지 않는다. object 이름에 프롬프트 · 이메일 ·
사용자 식별자를 넣지 않는다 — `ai/stickers/<generationId>.png`뿐이고 그 id도 hash다.
"""

from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)

OBJECT_PREFIX = "ai/stickers"


def object_name(generation_id: str) -> str:
    """`ai/stickers/<generationId>.png`. **사용자 정보가 들어가지 않는다.**"""
    return f"{OBJECT_PREFIX}/{generation_id}.png"


class GenerationStorage(Protocol):
    def put(self, name: str, png: bytes) -> None:
        """올린다. 실패하면 예외 — **성공했다고 답하면 안 된다.**"""

    def get(self, name: str) -> bytes | None:
        """없으면 None(보관 기간이 지나 lifecycle이 지웠을 수 있다)."""

    def exists(self, name: str) -> bool:
        """복구가 "결과가 이미 있는가"를 물을 때 쓴다. 내려받지 않는다.

        **존재는 증거일 뿐 결론이 아니다.** 사용자에게 무엇을 내보낼지는
        Firestore의 terminal state가 정한다.
        """

    def delete(self, name: str) -> None:
        """orphan object를 치운다. **best effort** — 실패해도 lifecycle이 결국 지운다."""

    @property
    def is_configured(self) -> bool: ...


class UnconfiguredStorage:
    """bucket이 설정되지 않았을 때. **fail closed** — 조용히 메모리에 두지 않는다.

    저장하지 못하는데 성공으로 답하면 응답이 유실됐을 때 복구할 방법이 없다.
    그래서 provider를 부르기도 전에 기능을 꺼 둔다.
    """

    is_configured = False

    def put(self, name: str, png: bytes) -> None:
        raise RuntimeError("generation storage is not configured")

    def get(self, name: str) -> bytes | None:
        return None

    def exists(self, name: str) -> bool:
        return False

    def delete(self, name: str) -> None:
        return None


class InMemoryGenerationStorage:
    """test / local용."""

    is_configured = True

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_failure: Exception | None = None

    def put(self, name: str, png: bytes) -> None:
        if self.put_failure is not None:
            raise self.put_failure
        self.objects[name] = png

    def get(self, name: str) -> bytes | None:
        return self.objects.get(name)

    def exists(self, name: str) -> bool:
        return name in self.objects

    def delete(self, name: str) -> None:
        self.objects.pop(name, None)


class GCSGenerationStorage:
    """꾸미러 전용 private bucket. **public ACL을 쓰지 않는다.**

    보관 기간은 bucket lifecycle rule이 정리한다 — 앱이 지우러 다니지 않는다.
    """

    is_configured = True

    def __init__(self, bucket_name: str, client: object | None = None) -> None:
        self._bucket_name = bucket_name
        self._client = client

    def _bucket(self):
        if self._client is None:
            # import를 여기서 한다 — storage가 없는 환경에서도 app은 뜬다.
            from google.cloud import storage

            self._client = storage.Client()
        return self._client.bucket(self._bucket_name)  # type: ignore[union-attr]

    def put(self, name: str, png: bytes) -> None:
        self._bucket().blob(name).upload_from_string(png, content_type="image/png")

    def get(self, name: str) -> bytes | None:
        from google.cloud import exceptions as storage_exceptions

        try:
            return self._bucket().blob(name).download_as_bytes()
        except storage_exceptions.NotFound:
            return None

    def exists(self, name: str) -> bool:
        return self._bucket().blob(name).exists()

    def delete(self, name: str) -> None:
        """orphan 정리. 이미 없으면 조용히 넘어간다."""
        from google.cloud import exceptions as storage_exceptions

        try:
            self._bucket().blob(name).delete()
        except storage_exceptions.NotFound:
            return


def build_storage(bucket_name: str) -> GenerationStorage:
    """설정 → 보관소. **비어 있으면 fail closed다.**"""
    if not bucket_name:
        logger.info("ai_storage_not_configured")
        return UnconfiguredStorage()
    return GCSGenerationStorage(bucket_name)
