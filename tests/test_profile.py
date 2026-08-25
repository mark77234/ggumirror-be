"""사용자 이름(1.1.0).

**신원이 아니라 표시용 값이다.** Apple `fullName`은 서명된 claim이 아니므로
첫 이름을 채우는 데만 쓰고, 신원은 계속 identity token의 `sub`가 정한다.

1.0.7 client가 production에 남아 있으므로 **전부 additive여야** 한다 —
이름 없이 로그인하던 요청이 그대로 통해야 하고, 응답이 넓어져도 깨지면 안 된다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.auth.profile import (
    DisplayNameCooldown,
    InvalidDisplayName,
    ProfileView,
    RENAME_COOLDOWN,
    can_change,
    next_change_at,
    normalize_display_name,
)
from app.auth.store import InMemoryAuthStore
# `client` · `store` fixture는 auth test 모듈에 있다. 같은 구성을 그대로 쓴다.
from tests.test_auth_api import (  # noqa: F401
    RAW_NONCE,
    bearer,
    client,
    sign_in,
    store,
    token_for,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


# MARK: - 이름 규칙


@pytest.mark.parametrize(
    "raw,expected",
    [("병찬", "병찬"), ("  병찬  ", "병찬"), ("a", "a"), ("가" * 20, "가" * 20),
     ("Ana María", "Ana María"), ("mirror maker", "mirror maker")],
)
def test_valid_names_are_accepted(raw, expected):
    # 특수문자를 넓게 막지 않는다 — 이름은 사람마다 다르다.
    assert normalize_display_name(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "\n", "\t", "가" * 21, "이름\n둘째줄"])
def test_invalid_names_are_rejected(raw):
    with pytest.raises(InvalidDisplayName):
        normalize_display_name(raw)


def test_length_is_counted_in_characters_not_bytes():
    # 한글 20자는 UTF-8로 60 byte다. byte로 세면 정당한 이름이 막힌다.
    name = "가" * 20
    assert len(name.encode()) > 20
    assert normalize_display_name(name) == name


# MARK: - 30일 규칙


def test_user_without_a_name_can_always_set_one():
    assert can_change(None, NOW) is True
    assert next_change_at(None) is None


def test_cooldown_blocks_then_allows():
    assert can_change(NOW, NOW + timedelta(days=29)) is False
    assert can_change(NOW, NOW + RENAME_COOLDOWN) is True
    assert next_change_at(NOW) == NOW + RENAME_COOLDOWN


def test_profile_view_exposes_only_display_fields():
    view = ProfileView(display_name="병찬", display_name_changed_at=NOW, now=NOW)
    assert view.display_name == "병찬"
    assert view.can_change_display_name is False
    assert view.next_display_name_change_at == NOW + RENAME_COOLDOWN


# MARK: - 저장소 계약


def _user(store: InMemoryAuthStore):
    user, _ = store.user_for_identity("apple", "sub-1")
    return user


def test_seed_fills_only_when_empty():
    store = InMemoryAuthStore()
    user = _user(store)
    assert store.seed_display_name(user.id, "병찬").display_name == "병찬"
    # 로그인할 때마다 Apple 이름으로 되돌아가면 안 된다.
    assert store.seed_display_name(user.id, "다른이름").display_name == "병찬"


def test_seed_does_not_consume_the_cooldown():
    store = InMemoryAuthStore()
    user = _user(store)
    seeded = store.seed_display_name(user.id, "병찬")
    # Apple이 넣어 준 값은 사용자가 고른 것이 아니다 — 바로 한 번 고칠 수 있어야 한다.
    assert seeded.display_name_changed_at is None
    assert store.set_display_name(user.id, "내가고른이름", NOW).display_name == "내가고른이름"


def test_first_manual_name_starts_the_cooldown():
    store = InMemoryAuthStore()
    user = _user(store)
    result = store.set_display_name(user.id, "병찬", NOW)
    assert result.display_name_changed_at == NOW
    with pytest.raises(DisplayNameCooldown):
        store.set_display_name(user.id, "또바꾸기", NOW + timedelta(days=29))


def test_rename_allowed_after_thirty_days():
    store = InMemoryAuthStore()
    user = _user(store)
    store.set_display_name(user.id, "첫이름", NOW)
    later = NOW + RENAME_COOLDOWN
    assert store.set_display_name(user.id, "새이름", later).display_name == "새이름"


def test_cooldown_error_carries_the_next_available_date():
    store = InMemoryAuthStore()
    user = _user(store)
    store.set_display_name(user.id, "첫이름", NOW)
    with pytest.raises(DisplayNameCooldown) as caught:
        store.set_display_name(user.id, "x", NOW + timedelta(days=1))
    assert caught.value.available_at == NOW + RENAME_COOLDOWN


def test_legacy_user_without_profile_fields_reads_as_none():
    store = InMemoryAuthStore()
    user = _user(store)
    # 예전 문서에는 이 값들이 없다. migration 없이 그대로 읽힌다.
    assert user.display_name is None
    assert user.display_name_changed_at is None


# MARK: - API (1.0.7 호환)


def test_sign_in_without_display_name_still_works(client, apple_key):
    """**1.0.7 client의 요청이다.** 이름 없이 보내도 그대로 통해야 한다."""
    assert sign_in(client, apple_key).status_code == 200


def test_sign_in_with_display_name_seeds_it(client, apple_key):
    response = client.post(
        "/auth/apple",
        json={"identityToken": token_for(apple_key), "nonce": RAW_NONCE, "displayName": "병찬"},
    )
    assert response.status_code == 200
    me = client.get("/users/me", headers=bearer(response.json()["accessToken"]))
    assert me.json()["displayName"] == "병찬"
    # seed는 30일 규칙을 소비하지 않는다.
    assert me.json()["canChangeDisplayName"] is True


def test_apple_name_never_overwrites_a_chosen_name(client, apple_key):
    first = client.post(
        "/auth/apple",
        json={"identityToken": token_for(apple_key), "nonce": RAW_NONCE, "displayName": "애플이름"},
    ).json()
    token = first["accessToken"]
    client.patch("/users/me/profile", json={"displayName": "내가고른이름"}, headers=bearer(token))

    again = client.post(
        "/auth/apple",
        json={"identityToken": token_for(apple_key), "nonce": RAW_NONCE, "displayName": "애플이름"},
    ).json()
    me = client.get("/users/me", headers=bearer(again["accessToken"]))
    # 다시 로그인했다고 이름이 Apple 값으로 되돌아가면 안 된다.
    assert me.json()["displayName"] == "내가고른이름"


def test_invalid_apple_name_does_not_break_sign_in(client, apple_key):
    response = client.post(
        "/auth/apple",
        json={"identityToken": token_for(apple_key), "nonce": RAW_NONCE, "displayName": "   "},
    )
    # 이름 하나 때문에 로그인이 실패하면 안 된다.
    assert response.status_code == 200
    me = client.get("/users/me", headers=bearer(response.json()["accessToken"]))
    assert me.json()["displayName"] is None


def test_patch_sets_then_blocks(client, apple_key):
    token = sign_in(client, apple_key).json()["accessToken"]
    first = client.patch("/users/me/profile", json={"displayName": "병찬"}, headers=bearer(token))
    assert first.status_code == 200
    assert first.json()["displayName"] == "병찬"
    assert first.json()["canChangeDisplayName"] is False
    assert first.json()["nextDisplayNameChangeAt"] is not None

    second = client.patch("/users/me/profile", json={"displayName": "또바꾸기"}, headers=bearer(token))
    assert second.status_code == 409


def test_patch_rejects_invalid_name(client, apple_key):
    token = sign_in(client, apple_key).json()["accessToken"]
    assert client.patch(
        "/users/me/profile", json={"displayName": "   "}, headers=bearer(token)
    ).status_code == 422


def test_patch_requires_authentication(client):
    assert client.patch("/users/me/profile", json={"displayName": "누구세요"}).status_code == 401


def test_profile_response_has_no_private_identifiers(client, apple_key):
    token = sign_in(client, apple_key).json()["accessToken"]
    client.patch("/users/me/profile", json={"displayName": "병찬"}, headers=bearer(token))
    payload = client.get("/users/me", headers=bearer(token)).json()
    for forbidden in ("email", "sub", "subject", "appleSubject", "provider", "tokenHash"):
        assert forbidden not in payload


# MARK: - 판매자 이름 (공개 표면)


def test_seller_names_resolves_only_named_users():
    """이름을 정한 판매자만 담는다. 나머지는 아예 빠진다(= 화면에서 `null`)."""
    from app.api.marketplace import _seller_names

    store = InMemoryAuthStore()
    named, _ = store.user_for_identity("apple", "named")
    store.set_display_name(named.id, "병찬", NOW)
    unnamed, _ = store.user_for_identity("apple", "unnamed")

    names = _seller_names({named.id, unnamed.id}, store)
    assert names == {named.id: "병찬"}


def test_seller_names_reads_each_user_once():
    """같은 판매자의 상품이 여러 개여도 문서를 한 번만 읽는다.

    상품마다 조회하면 목록 하나에 N번 읽게 된다 — 이름을 listing에 복사해 두지
    않고도 비용이 늘지 않는 이유가 이것이다.
    """
    from app.api.marketplace import _seller_names

    store = InMemoryAuthStore()
    user, _ = store.user_for_identity("apple", "seller")
    store.set_display_name(user.id, "병찬", NOW)

    reads = []
    original = store.user

    def counting(user_id):
        reads.append(user_id)
        return original(user_id)

    store.user = counting  # type: ignore[method-assign]
    # 상품 다섯 개가 모두 같은 판매자여도 id 집합은 하나다.
    _seller_names({user.id}, store)
    assert reads == [user.id]


def test_seller_names_survives_a_store_failure():
    """이름을 못 읽어도 목록이 깨지지 않는다 — 이름만 비는 것이 맞다."""
    from app.api.marketplace import _seller_names
    from app.auth.store import StoreUnavailable

    class Failing(InMemoryAuthStore):
        def user(self, user_id):
            raise StoreUnavailable("boom")

    assert _seller_names({"someone"}, Failing()) == {}


def test_public_dto_defaults_seller_name_to_none():
    """1.0.7 시절 상품 · 이름 없는 판매자 — `null`이고 그것이 정상이다."""
    from app.api.marketplace import PublicListingResponse

    field = PublicListingResponse.model_fields["seller_display_name"]
    assert field.default is None
    assert field.serialization_alias == "sellerDisplayName"
