"""Marketplace snapshot asset 저장 (B-7F).

**서버는 거울/스티커 포맷을 새로 만들지 않는다.** client에 이미 완전한 `Codable`이
있고(`MyMirror` · `StickerProject`), 모델에 파일 경로가 없다 — 이미지는 전부
`assetID`(UUID)로만 참조된다. 그래서 package는 그것을 **그대로** 담는다:

    manifest.json    client Codable JSON + 참조 assetID 목록
    preview.png      목록/상세에 보여줄 그림
    assets/<id>.png  manifest가 참조하는 이미지들

master geometry(1080 × 2340 · insets)는 client **상수**이고 좌표는 0…1 normalized라
package에 담지 않는다 — 담으면 두 곳이 어긋날 수 있다.

**signed URL을 만들지 않는다.** URL 자체가 credential이 되어 로그 한 줄로 새어 나간다
(A-1B AI 결과와 같은 규칙). 앱은 우리 endpoint로 받는다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

#: snapshot 하나의 namespace. `snapshotId`가 다르면 절대 겹치지 않는다.
PREFIX = "marketplace/snapshots"

MANIFEST_NAME = "manifest.json"
PREVIEW_NAME = "preview.png"

# 크기 상한. **DoS 방지**이고 실제 client export 규모를 기준으로 잡았다 —
# 거울 하나는 JSON 수십 KB + PNG 몇 장이다.
MAX_MANIFEST_BYTES = 256 * 1024
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 10 * 1024 * 1024
MAX_ASSETS = 32

#: PNG signature. **확장자를 믿지 않는다.**
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: assetID는 UUID뿐이다 — client가 그렇게 저장한다. 경로 문자가 들어올 자리가 없다.
ASSET_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# manifest에 있으면 거절하는 것들. Marketplace 템플릿은 **앱 안의 안전한 local
# 콘텐츠만** 표현한다 — 원격 자원을 끌어오거나 경로를 가리킬 이유가 없다.
FORBIDDEN_IN_MANIFEST = (
    "../", "..\\", "file://", "http://", "https://", "javascript:", "data:",
    "<script", "/etc/", "\\\\",
)


class AssetError(Exception):
    """package가 규칙에 맞지 않는다."""


class AssetTooLarge(AssetError):
    """상한을 넘었다. `Content-Length`가 아니라 **실제로 읽은 바이트**로 판단한다."""


class AssetNotFound(AssetError):
    """저장소에 없다. **거짓 미리보기를 만들지 않는다.**"""


class AssetStorageUnavailable(AssetError):
    """bucket이 설정되지 않았다. **`AssetNotFound`와 구분한다** —
    404로 뭉개면 운영자가 설정 누락을 "데이터 없음"으로 오진한다."""


class AssetAlreadyExists(AssetError):
    """같은 자리에 이미 있다. **덮어쓰지 않는다** — snapshot은 불변이다."""


@dataclass(frozen=True)
class StoredObject:
    data: bytes
    content_type: str


@dataclass(frozen=True)
class SnapshotPackage:
    """올라온 package 하나. **검증을 통과한 뒤**에만 만들어진다."""

    manifest: bytes
    preview: bytes
    assets: dict[str, bytes]

    @property
    def total_bytes(self) -> int:
        return len(self.manifest) + len(self.preview) + sum(len(x) for x in self.assets.values())

    @property
    def manifest_checksum(self) -> str:
        """**서버가 계산한다.** client가 보낸 checksum을 authority로 쓰지 않는다."""
        return hashlib.sha256(self.manifest).hexdigest()


class MarketplaceAssetStorage(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None:
        """**만들기만 한다.** 같은 자리에 있으면 `AssetAlreadyExists` —
        snapshot이 불변이라는 것이 저장소 수준에서 보장돼야 한다."""

    def get(self, key: str) -> StoredObject:
        """없으면 `AssetNotFound`."""

    def delete(self, key: str) -> None:
        """실패한 업로드 정리용. **best-effort**다."""


class InMemoryMarketplaceAssetStorage:
    """test / local용."""

    def __init__(self) -> None:
        self.objects: dict[str, StoredObject] = {}

    def put(self, key: str, data: bytes, content_type: str) -> None:
        if key in self.objects:
            raise AssetAlreadyExists(key)
        self.objects[key] = StoredObject(data=data, content_type=content_type)

    def get(self, key: str) -> StoredObject:
        found = self.objects.get(key)
        if found is None:
            raise AssetNotFound(key)
        return found

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)


class GCSMarketplaceAssetStorage:
    """꾸미러 전용 **private** bucket.

    - **AI 결과 bucket을 재사용하지 않는다.** 그쪽은 7일 lifecycle이고 이쪽은 영구다.
      lifecycle rule 하나를 잘못 걸면 판매 중인 템플릿이 사라진다
    - `if_generation_match=0` — **이미 있으면 실패한다.** snapshot 불변의 저장소 쪽 보증
    - signed URL을 만들지 않는다. 읽기는 우리 endpoint가 stream한다
    """

    def __init__(self, bucket_name: str, client=None) -> None:
        self._bucket_name = bucket_name
        self._client = client

    def _bucket(self):
        if self._client is None:
            from google.cloud import storage

            self._client = storage.Client()
        return self._client.bucket(self._bucket_name)

    def put(self, key: str, data: bytes, content_type: str) -> None:
        from google.api_core import exceptions as gcp_exceptions

        blob = self._bucket().blob(key)
        try:
            # **create-only.** 같은 object가 있으면 412가 나고 덮어쓰지 않는다.
            blob.upload_from_string(data, content_type=content_type, if_generation_match=0)
        except gcp_exceptions.PreconditionFailed as error:
            raise AssetAlreadyExists(key) from error
        except gcp_exceptions.GoogleAPIError as error:
            logger.warning("marketplace_asset_put_failed error=%s", type(error).__name__)
            raise AssetError(type(error).__name__) from error

    def get(self, key: str) -> StoredObject:
        from google.api_core import exceptions as gcp_exceptions

        blob = self._bucket().blob(key)
        try:
            data = blob.download_as_bytes()
        except gcp_exceptions.NotFound as error:
            raise AssetNotFound(key) from error
        except gcp_exceptions.GoogleAPIError as error:
            logger.warning("marketplace_asset_get_failed error=%s", type(error).__name__)
            raise AssetError(type(error).__name__) from error
        return StoredObject(data=data, content_type=blob.content_type or "application/octet-stream")

    def delete(self, key: str) -> None:
        from google.api_core import exceptions as gcp_exceptions

        try:
            self._bucket().blob(key).delete()
        except gcp_exceptions.GoogleAPIError:
            # best-effort다 — 정리에 실패해도 Firestore snapshot 문서가 없으므로
            # 아무 listing도 그 orphan을 참조할 수 없다.
            logger.warning("marketplace_asset_orphan key_prefix=%s", key.split("/")[-2:-1])


# MARK: - object key


def manifest_key(snapshot_id: str) -> str:
    return f"{PREFIX}/{snapshot_id}/{MANIFEST_NAME}"


def preview_key(snapshot_id: str) -> str:
    return f"{PREFIX}/{snapshot_id}/{PREVIEW_NAME}"


def asset_key(snapshot_id: str, asset_id: str) -> str:
    """`asset_id`는 **UUID만** 허용되므로 경로 조작이 성립하지 않는다."""
    if not ASSET_ID.match(asset_id):
        raise AssetError("assetId")
    return f"{PREFIX}/{snapshot_id}/assets/{asset_id}.png"


# MARK: - 검증


def checked_package(
    *, manifest: bytes, preview: bytes, assets: dict[str, bytes]
) -> SnapshotPackage:
    """**저장 전에** package를 검사한다. 통과하지 못하면 아무것도 올라가지 않는다.

    확장자·`Content-Type`을 믿지 않는다 — 실제 바이트를 본다.
    """
    if len(manifest) == 0 or len(manifest) > MAX_MANIFEST_BYTES:
        raise AssetTooLarge("manifest")
    if len(preview) == 0 or len(preview) > MAX_IMAGE_BYTES:
        raise AssetTooLarge("preview")
    if len(assets) > MAX_ASSETS:
        raise AssetTooLarge("assetCount")

    document = _checked_manifest(manifest)

    if not preview.startswith(PNG_MAGIC):
        raise AssetError("preview is not a PNG")
    for asset_id, data in assets.items():
        if not ASSET_ID.match(asset_id):
            raise AssetError("assetId")
        if len(data) == 0 or len(data) > MAX_IMAGE_BYTES:
            raise AssetTooLarge(f"asset:{asset_id}")
        if not data.startswith(PNG_MAGIC):
            raise AssetError("asset is not a PNG")

    # manifest가 말한 것과 실제 올라온 것이 **정확히 같아야 한다** —
    # 빠진 이미지가 있으면 다른 기기에서 복원할 때 조용히 깨진다.
    declared = {str(x) for x in document.get("assetIds", [])}
    if declared != set(assets):
        raise AssetError("assetIds do not match the uploaded assets")

    package = SnapshotPackage(manifest=manifest, preview=preview, assets=dict(assets))
    if package.total_bytes > MAX_SNAPSHOT_BYTES:
        raise AssetTooLarge("snapshot")
    return package


def _checked_manifest(manifest: bytes) -> dict:
    """valid UTF-8 JSON이어야 하고 **경로 · 원격 자원 · 스크립트를 담을 수 없다.**

    Marketplace 템플릿은 앱 안의 안전한 local 콘텐츠만 표현한다. client 모델에는
    애초에 경로가 없고 이미지는 assetID로만 참조되므로, 그런 문자열이 나오면
    우리가 아는 포맷이 아니다.
    """
    try:
        text = manifest.decode("utf-8")
    except UnicodeDecodeError as error:
        raise AssetError("manifest is not UTF-8") from error

    lowered = text.lower()
    for banned in FORBIDDEN_IN_MANIFEST:
        if banned in lowered:
            raise AssetError("manifest contains a forbidden reference")

    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise AssetError("manifest is not JSON") from error
    if not isinstance(document, dict):
        raise AssetError("manifest is not an object")
    if not isinstance(document.get("assetIds", []), list):
        raise AssetError("assetIds is not a list")
    return document
