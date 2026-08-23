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

#: 사용자가 자유롭게 쓰는 글. **여기는 위 검사에서 뺀다.**
#:
#: `TextObject.text`는 100자 장식 문구이고 `name`은 사용자가 붙인 이름이다.
#: 거울에 "https://insta.gr/me"라고 써 넣는 것은 정상적인 사용인데, 전체 텍스트를
#: 훑는 검사는 그 package를 통째로 거절했다(B-7F.1에서 재현). 그 문자열은 asset을
#: 가리키지 않는다 — client는 `Text`로 그릴 뿐이고 우리는 JSON으로만 돌려준다.
#:
#: 진짜 방어는 **참조 위치를 구조에서 뽑아 UUID만 허용하는 것**이다(아래).
#: 경로·URL은 UUID 형식을 통과할 수 없으므로 asset 자리에서는 애초에 불가능하다.
PROSE_KEYS = frozenset({"name", "text"})

#: JSON 중첩 상한. 이보다 깊으면 우리 포맷이 아니고, `json.loads`가
#: `RecursionError`로 죽는다(재현했다) — 500이 아니라 400으로 끊는다.
MAX_MANIFEST_DEPTH = 32

#: contentType별 manifest 모양. **완전한 Swift decoder를 다시 만들지 않는다** —
#: 두 종류를 구분하기에 충분한 최소 구조만 본다. client가 나중에 optional field를
#: 더해도 깨지지 않도록 **key 동일성(exact equality)을 요구하지 않는다.**
MIRROR = "mirror"
STICKER = "sticker"


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
    *, content_type: str, manifest: bytes, preview: bytes, assets: dict[str, bytes]
) -> SnapshotPackage:
    """**저장 전에** package를 검사한다. 통과하지 못하면 아무것도 올라가지 않는다.

    확장자·`Content-Type`을 믿지 않는다 — 실제 바이트를 본다.

    `content_type`이 manifest의 실제 모양과 맞아야 한다. label만 바꿔
    스티커를 거울로 등록할 수 없다.
    """
    if len(manifest) == 0 or len(manifest) > MAX_MANIFEST_BYTES:
        raise AssetTooLarge("manifest")
    if len(preview) == 0 or len(preview) > MAX_IMAGE_BYTES:
        raise AssetTooLarge("preview")
    if len(assets) > MAX_ASSETS:
        raise AssetTooLarge("assetCount")

    referenced = referenced_asset_ids(content_type, _checked_manifest(manifest))

    if not preview.startswith(PNG_MAGIC):
        raise AssetError("preview is not a PNG")
    for asset_id, data in assets.items():
        if not ASSET_ID.match(asset_id):
            raise AssetError("assetId")
        if len(data) == 0 or len(data) > MAX_IMAGE_BYTES:
            raise AssetTooLarge(f"asset:{asset_id}")
        if not data.startswith(PNG_MAGIC):
            raise AssetError("asset is not a PNG")

    # **manifest가 authority다.** client가 따로 보낸 목록을 믿지 않는다.
    #
    # 정확히 같아야 한다. 빠지면 구매자 기기에서 이미지가 비어 보이고(조용히 깨진다),
    # 남으면 manifest 어디서도 쓰지 않는 이미지를 package에 몰래 넣은 것이다.
    if referenced != set(assets):
        missing = sorted(referenced - set(assets))
        extra = sorted(set(assets) - referenced)
        raise AssetError(f"asset set mismatch missing={len(missing)} extra={len(extra)}")

    package = SnapshotPackage(manifest=manifest, preview=preview, assets=dict(assets))
    if package.total_bytes > MAX_SNAPSHOT_BYTES:
        raise AssetTooLarge("snapshot")
    return package


def referenced_asset_ids(content_type: str, document: dict) -> set[str]:
    """manifest가 **실제로 참조하는** local asset id.

    client 코드에서 확인한 위치만 본다:

    거울(`MyMirror`)
      - `stickers[].source.assetID` — `kind == "photo"`일 때만(`PhotoStickerAssets`)
      - `importedArtworks[].assetID` (`ImportedArtworkAssets`)

    스티커(`StickerProject`)
      - `finalAssetID` — 완성 PNG(`UserStickerAssets`). optional이라 없으면 없는 것이다
      - `design.stickers[].source.assetID` · `design.importedArtworks[].assetID`

    **`stickers[].id` · `importedArtworks[].id` · `strokes[].id` · `texts[].id`는
    asset이 아니다** — 오브젝트 자기 식별자다. `generationIDs`도 아니다(AI 생성 기록
    id이고 파일이 아니다). client GC가 정확히 위 목록만 살려두는 것을 확인했다.
    """
    if content_type == MIRROR:
        _mirror_shape(document)
        return _design_asset_ids(document)
    if content_type == STICKER:
        design = _sticker_shape(document)
        ids = _design_asset_ids(design)
        # optional이다. 없는 finalAssetID를 만들어내지 않는다.
        final = document.get("finalAssetID")
        if final is not None:
            ids.add(_checked_asset_id(final))
        return ids
    raise AssetError("contentType")


def source_content_id(content_type: str, document: dict) -> str:
    """manifest가 어느 **local 콘텐츠**에서 나왔는지. top-level `id`다.

    `MyMirror.id` / `StickerProject.id`이고 두 Codable 모두 이 자리에 문자열로 적는다
    (`_mirror_shape` · `_sticker_shape`가 이미 문자열임을 확인한다).
    **새 식별자를 만들지 않는다** — 이미 있는 것을 쓴다.

    거울 id는 UUID가 아닐 수 있다. 내장 템플릿에서 받은 거울은 `art-mint-flower`처럼
    사람이 읽는 값이고, 사용자가 만든 것은 UUID 문자열이다. 그래서 UUID를 요구하지
    않는다 — 대신 **경로가 될 수 없는 값**인지 본다(이 값은 저장 경로로 쓰지 않지만,
    나중에 누가 그렇게 쓰더라도 안전해야 한다).
    """
    shape = _mirror_shape if content_type == MIRROR else _sticker_shape
    if content_type not in (MIRROR, STICKER):
        raise AssetError("contentType")
    shape(document)
    found = _string(document, "id")
    if not found or len(found) > 128:
        raise AssetError("id")
    for banned in ("/", "\\", "..", "\x00"):
        if banned in found:
            raise AssetError("id")
    return found


def _mirror_shape(document: dict) -> None:
    """거울을 구분하기에 충분한 최소 구조. **모르는 key는 허용한다.**"""
    _string(document, "id")
    _string(document, "name")
    _object(document, "style")
    # `StickerProject`에는 `design`이 있고 `MyMirror`에는 없다.
    # 이 한 줄이 "스티커를 거울이라고 label만 바꾸는 것"을 막는다.
    if "design" in document:
        raise AssetError("mirror manifest must not contain design")


def _sticker_shape(document: dict) -> dict:
    """스티커를 구분하기에 충분한 최소 구조. 안쪽 `design`을 돌려준다."""
    _string(document, "id")
    _string(document, "name")
    design = _object(document, "design")
    # `MirrorDesign`은 `style`을 반드시 적는다 — 거울 JSON을 스티커로 우기면 여기서 걸린다.
    _object(design, "style")
    return design


def _design_asset_ids(design: dict) -> set[str]:
    """`MyMirror` / `MirrorDesign` 공통. 두 곳이 같은 부품을 쓴다."""
    ids: set[str] = set()
    for sticker in _array(design, "stickers"):
        if not isinstance(sticker, dict):
            raise AssetError("stickers")
        source = sticker.get("source")
        if not isinstance(source, dict):
            raise AssetError("sticker source")
        # `kind`가 없으면 client는 builtIn으로 읽는다 — asset이 아니다.
        if source.get("kind") != "photo":
            continue
        # photo인데 assetID가 없으면 client decode가 실패한다(required).
        if "assetID" not in source:
            raise AssetError("photo sticker without assetID")
        ids.add(_checked_asset_id(source["assetID"]))
    for artwork in _array(design, "importedArtworks"):
        if not isinstance(artwork, dict):
            raise AssetError("importedArtworks")
        if "assetID" not in artwork:
            raise AssetError("importedArtwork without assetID")
        ids.add(_checked_asset_id(artwork["assetID"]))
    return ids


def _checked_asset_id(value: object) -> str:
    """asset 참조는 **UUID 문자열만**이다.

    경로 구분자 · `../` · 절대 경로 · `file://` · 원격 URL은 이 형식을 통과할 수
    없다. 그래서 참조 위치에서의 경로 조작이 문자 수준에서 불가능하다.
    """
    if not isinstance(value, str) or not ASSET_ID.match(value):
        raise AssetError("asset reference is not a UUID")
    return value


def _string(document: dict, key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str):
        raise AssetError(f"{key} is not a string")
    return value


def _object(document: dict, key: str) -> dict:
    value = document.get(key)
    if not isinstance(value, dict):
        raise AssetError(f"{key} is not an object")
    return value


def _array(document: dict, key: str) -> list:
    """없으면 빈 배열이다 — client가 `decodeIfPresent`로 그렇게 읽는다.
    단 **있는데 배열이 아니면** 우리가 아는 포맷이 아니다."""
    value = document.get(key, [])
    if not isinstance(value, list):
        raise AssetError(f"{key} is not a list")
    return value


def _checked_manifest(manifest: bytes) -> dict:
    """valid UTF-8 JSON object여야 하고 **경로 · 원격 자원 · 스크립트를 담을 수 없다.**

    검사는 **사용자 산문(`PROSE_KEYS`)을 뺀 나머지 문자열**에 적용한다. 거울에 적은
    장식 문구는 asset을 가리키지 않으므로, 거기 URL을 썼다고 판매를 막지 않는다.
    """
    try:
        text = manifest.decode("utf-8")
    except UnicodeDecodeError as error:
        raise AssetError("manifest is not UTF-8") from error

    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise AssetError("manifest is not JSON") from error
    except RecursionError as error:
        # 깊은 중첩으로 parser를 죽이려는 것. 500이 아니라 400이다.
        raise AssetError("manifest is too deeply nested") from error

    if not isinstance(document, dict):
        raise AssetError("manifest is not an object")
    _reject_forbidden(document)
    return document


def _reject_forbidden(value: object, key: str | None = None, depth: int = 0) -> None:
    """산문이 아닌 문자열에서 경로 · 원격 자원 · 스크립트를 거절한다."""
    if depth > MAX_MANIFEST_DEPTH:
        raise AssetError("manifest is too deeply nested")
    if isinstance(value, str):
        if key in PROSE_KEYS:
            return
        lowered = value.lower()
        for banned in FORBIDDEN_IN_MANIFEST:
            if banned in lowered:
                raise AssetError("manifest contains a forbidden reference")
    elif isinstance(value, dict):
        for name, item in value.items():
            # key 이름도 본다 — 거기에 숨길 수 없다.
            _reject_forbidden(name, None, depth + 1)
            _reject_forbidden(item, name, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _reject_forbidden(item, key, depth + 1)
