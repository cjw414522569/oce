"""/auth 路由：登录跳转、state 防伪、会话签发、me/rotate/logout。

standalone FastAPI + dependency_overrides[get_user_access]，不触碰真实容器。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI

from oce.api import auth_router
from oce.api.auth_router import get_user_access
from oce.application.user_access import (
    IssuedApiKey,
    LinuxDoProfile,
    PortalView,
    UserApiKeyView,
    UserRecord,
    UserUsageSummary,
)
from oce.shared.errors import LoginDeniedError
from oce.shared.signed_tokens import SESSION_COOKIE, STATE_COOKIE, sign_payload

SECRET = "test-session-secret"


@dataclass
class StubProvider:
    redirect: str = "https://connect.linux.do/oauth2/authorize"
    profile: LinuxDoProfile = field(
        default_factory=lambda: LinuxDoProfile(linuxdo_id=1, username="alice")
    )

    @property
    def redirect_uri(self) -> str:
        return "https://oce.example.com/auth/callback"

    def authorize_url(self, state: str) -> str:
        return f"{self.redirect}?state={state}"

    async def exchange_code(self, code: str) -> LinuxDoProfile:
        if code == "deny":
            raise LoginDeniedError("denied")
        return self.profile


@dataclass
class StubService:
    provider: StubProvider | None = field(default_factory=StubProvider)
    portal: PortalView | None = None

    async def authenticate(self, code: str) -> UserRecord:
        if code == "deny":
            raise LoginDeniedError("denied")
        return _user()

    async def get_portal(self, user_id: int) -> PortalView:
        if self.portal is None:
            self.portal = PortalView(
                user=_user(),
                api_key=UserApiKeyView(
                    key_last4="abcd",
                    status="active",
                    created_at=datetime.now(timezone.utc),
                    last_used_at=None,
                    api_key="sk-oce-full-key-abcd",
                ),
                usage_24h=UserUsageSummary(24, api_calls=3, total_tokens=150),
                usage_7d=UserUsageSummary(168, api_calls=9, total_tokens=450),
            )
        return self.portal

    async def rotate_key(self, user_id: int) -> IssuedApiKey:
        return IssuedApiKey(api_key="sk-oce-new-key-xyz", key_last4="-xyz")


def _user() -> UserRecord:
    return UserRecord(
        id=7,
        linuxdo_id=1,
        username="alice",
        name="Alice",
        avatar_template=None,
        trust_level=2,
        status="active",
        created_at=datetime.now(timezone.utc),
        last_login_at=None,
    )


@pytest.fixture
def app(monkeypatch) -> FastAPI:
    from pydantic import SecretStr

    from oce.shared.config import settings as settings_module

    class _AuthFlags:
        enabled = True
        session_secret = SecretStr(SECRET)
        session_ttl_seconds = 3600
        state_ttl_seconds = 600
        cookie_secure = False

    class _Settings:
        auth = _AuthFlags()

    monkeypatch.setattr(settings_module, "get_settings", lambda: _Settings())
    monkeypatch.setattr(auth_router, "get_settings", lambda: _Settings())

    application = FastAPI()
    application.include_router(auth_router.auth_router)
    application.dependency_overrides[get_user_access] = lambda: StubService()
    return application


@pytest.fixture
def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


async def test_login_redirects_with_signed_state_cookie(client):
    resp = await client.get("/auth/login", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://connect.linux.do")
    assert STATE_COOKIE in resp.cookies
    # state cookie 是签名串（三段）
    assert len(resp.cookies[STATE_COOKIE].split(".")) == 3


async def test_callback_requires_matching_state(client):
    resp = await client.get("/auth/callback", params={"code": "c", "state": "forged"})
    assert resp.status_code == 401


async def test_callback_happy_path_issues_session(client):
    login = await client.get("/auth/login", follow_redirects=False)
    state = login.cookies[STATE_COOKIE]
    resp = await client.get(
        "/auth/callback", params={"code": "c", "state": state}, follow_redirects=False
    )
    assert resp.status_code == 302 and resp.headers["location"] == "/"
    assert SESSION_COOKIE in resp.cookies
    # 会话可用：直接带 cookie 请求 /auth/me
    me = await client.get("/auth/me")
    assert me.status_code == 200
    body = me.json()
    assert body["user"]["username"] == "alice"
    assert body["api_key"]["key_last4"] == "abcd"
    assert body["api_key"]["api_key"] == "sk-oce-full-key-abcd"
    assert body["usage_24h"]["api_calls"] == 3


async def test_callback_denied_redirects_with_error_flag(client):
    login = await client.get("/auth/login", follow_redirects=False)
    state = login.cookies[STATE_COOKIE]
    resp = await client.get(
        "/auth/callback", params={"code": "deny", "state": state}, follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/?login_error=denied"
    assert SESSION_COOKIE not in resp.cookies


async def test_me_requires_session(client):
    resp = await client.get("/auth/me")
    assert resp.status_code == 401


async def test_expired_session_rejected(client):
    # 已过期签名（exp 在过去）→ 401
    expired = sign_payload("u:7", -10, SECRET)
    resp = await client.get(
        "/auth/me", cookies={SESSION_COOKIE: expired}
    )
    assert resp.status_code == 401


async def test_rotate_returns_plaintext_once(client):
    login = await client.get("/auth/login", follow_redirects=False)
    state = login.cookies[STATE_COOKIE]
    await client.get(
        "/auth/callback", params={"code": "c", "state": state}, follow_redirects=False
    )
    resp = await client.post("/auth/key/rotate")
    assert resp.status_code == 201
    body = resp.json()
    assert body["api_key"].startswith("sk-oce-")
    assert body["key_last4"] == body["api_key"][-4:]


async def test_logout_clears_cookies(client):
    login = await client.get("/auth/login", follow_redirects=False)
    state = login.cookies[STATE_COOKIE]
    await client.get(
        "/auth/callback", params={"code": "c", "state": state}, follow_redirects=False
    )
    resp = await client.post("/auth/logout")
    assert resp.status_code == 200
    me = await client.get("/auth/me")
    assert me.status_code == 401


async def test_rotate_unavailable_user_maps_to_401(client, monkeypatch):
    from oce.shared.errors import LoginDeniedError

    class _DeniedService(StubService):
        async def rotate_key(self, user_id: int) -> IssuedApiKey:
            raise LoginDeniedError("账号不可用")

    # 直接替换 override 后重新登录拿会话
    import oce.api.auth_router as ar

    app = client._transport.app  # noqa: SLF001 - 测试需要换回原 app
    login = await client.get("/auth/login", follow_redirects=False)
    state = login.cookies[STATE_COOKIE]
    await client.get("/auth/callback", params={"code": "c", "state": state}, follow_redirects=False)
    app.dependency_overrides[ar.get_user_access] = lambda: _DeniedService()
    resp = await client.post("/auth/key/rotate")
    assert resp.status_code == 401
