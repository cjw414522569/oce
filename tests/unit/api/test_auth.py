"""verify_api_key / verify_admin_key 鉴权语义测试。"""

from __future__ import annotations

from hashlib import sha256
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from oce import auth
from oce.auth import verify_admin_key, verify_api_key


def _settings(*, api_key: str, admin_api_key: str = "", auth_enabled: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        api_key=api_key,
        admin_api_key=admin_api_key,
        auth=SimpleNamespace(enabled=auth_enabled),
    )


class _FakeRequest:
    def __init__(self) -> None:
        self.scope: dict = {}


async def test_api_key_accepts_matching_and_rejects_wrong(monkeypatch):
    monkeypatch.setattr(auth, "get_settings", lambda: _settings(api_key="sk-main"))
    assert await verify_api_key("Bearer sk-main") == "sk-main"
    with pytest.raises(HTTPException) as exc:
        await verify_api_key("Bearer sk-wrong")
    assert exc.value.status_code == 401


async def test_superuser_key_sets_null_user_scope(monkeypatch):
    # 全局 key = 超管：身份归属为空（metrics user_id NULL）
    monkeypatch.setattr(auth, "get_settings", lambda: _settings(api_key="sk-main"))
    request = _FakeRequest()
    assert await verify_api_key("Bearer sk-main", request) == "sk-main"
    assert request.scope["oce_user_id"] is None


async def test_wrong_key_with_auth_disabled_never_touches_container(monkeypatch):
    monkeypatch.setattr(auth, "get_settings", lambda: _settings(api_key="sk-main"))

    def _boom():
        raise AssertionError("auth disabled 时不得构建容器")

    import oce.application.container as container_module

    monkeypatch.setattr(container_module, "get_container", _boom)
    with pytest.raises(HTTPException) as exc:
        await verify_api_key("Bearer sk-user", _FakeRequest())
    assert exc.value.status_code == 401


async def test_user_key_resolves_identity_into_scope_and_context(monkeypatch):
    from oce.application.user_access import ApiKeyIdentity
    from oce.shared.user_context import get_current_user_id, set_current_user_id

    monkeypatch.setattr(
        auth, "get_settings", lambda: _settings(api_key="sk-main", auth_enabled=True)
    )
    identity = ApiKeyIdentity(key_id=3, user_id=42)

    class _Store:
        async def resolve_api_key(self, key_hash: str):
            assert key_hash == sha256(b"sk-user-1").hexdigest()
            return identity

        async def touch_api_key(self, key_id: int) -> None:
            assert key_id == 3

    class _UserAccess:
        store = _Store()

    import oce.application.container as container_module

    monkeypatch.setattr(
        container_module, "get_container", lambda: SimpleNamespace(user_access=_UserAccess())
    )

    token = set_current_user_id(None)
    request = _FakeRequest()
    assert await verify_api_key("Bearer sk-user-1", request) == "sk-user-1"
    assert request.scope["oce_user_id"] == 42
    assert get_current_user_id() == 42
    set_current_user_id(token)  # 复位，避免污染其他用例


async def test_user_key_unknown_rejected_with_openai_envelope(monkeypatch):
    monkeypatch.setattr(
        auth, "get_settings", lambda: _settings(api_key="sk-main", auth_enabled=True)
    )

    class _Store:
        async def resolve_api_key(self, key_hash: str):
            return None

        async def touch_api_key(self, key_id: int) -> None:  # pragma: no cover
            raise AssertionError("未命中不应 touch")

    import oce.application.container as container_module

    monkeypatch.setattr(
        container_module,
        "get_container",
        lambda: SimpleNamespace(user_access=SimpleNamespace(store=_Store())),
    )
    with pytest.raises(HTTPException) as exc:
        await verify_api_key("Bearer sk-unknown", _FakeRequest())
    assert exc.value.status_code == 401
    assert exc.value.detail["error"]["code"] == "invalid_api_key"


async def test_admin_key_falls_back_to_api_key_when_unset(monkeypatch):
    # 未配置 ADMIN_API_KEY：admin 接口回落 API_KEY
    monkeypatch.setattr(
        auth, "get_settings", lambda: _settings(api_key="sk-main", admin_api_key="")
    )
    assert await verify_admin_key("Bearer sk-main") == "sk-main"


async def test_admin_key_is_exclusive_once_configured(monkeypatch):
    # 配置 ADMIN_API_KEY 后：只认 admin key，普通 API_KEY 不再放行
    monkeypatch.setattr(
        auth,
        "get_settings",
        lambda: _settings(api_key="sk-main", admin_api_key="sk-admin"),
    )
    assert await verify_admin_key("Bearer sk-admin") == "sk-admin"
    with pytest.raises(HTTPException) as exc:
        await verify_admin_key("Bearer sk-main")
    assert exc.value.status_code == 401


async def test_admin_key_missing_header_rejected(monkeypatch):
    monkeypatch.setattr(
        auth, "get_settings", lambda: _settings(api_key="sk-main", admin_api_key="sk-admin")
    )
    with pytest.raises(HTTPException) as exc:
        await verify_admin_key(None)
    assert exc.value.status_code == 401
