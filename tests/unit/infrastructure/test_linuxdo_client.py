"""LinuxDoOAuthClient：授权 URL、token 表单、userinfo 映射与错误（MockTransport 注入）。"""

from __future__ import annotations

import httpx
import pytest
from urllib.parse import parse_qs

from oce.infrastructure.oauth.linuxdo import LinuxDoOAuthClient
from oce.shared.config.settings import AuthSettings
from oce.shared.errors import OAuthExchangeError


def _settings(**overrides) -> AuthSettings:
    values = dict(
        enabled=True,
        client_id="cid",
        client_secret="csecret",
        session_secret="ssecret",
        redirect_base="https://oce.example.com",
    )
    values.update(overrides)
    return AuthSettings(**values)


def _client(handler) -> LinuxDoOAuthClient:
    return LinuxDoOAuthClient(
        _settings(),
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=5.0
        ),
    )


def test_authorize_url_contains_encoded_params():
    client = LinuxDoOAuthClient(_settings())
    url = client.authorize_url("st ate+")
    assert url.startswith("https://connect.linux.do/oauth2/authorize?")
    assert "response_type=code" in url
    assert "client_id=cid" in url
    assert "redirect_uri=https%3A%2F%2Foce.example.com%2Fauth%2Fcallback" in url
    assert "scope=user" in url
    assert "state=st+ate%2B" in url


async def test_exchange_code_posts_form_and_maps_userinfo():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            seen["token_form"] = {
                k: v[0] for k, v in parse_qs(request.read().decode()).items()
            }
            return httpx.Response(200, json={"access_token": "at-123"})
        seen["userinfo_auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={
                "id": 4242,
                "username": "alice",
                "name": "Alice",
                "avatar_template": "https://cdn/{size}.png",
                "active": True,
                "silenced": False,
                "trust_level": 2,
            },
        )

    profile = await _client(handler).exchange_code("the-code")

    # token 表单字段精确匹配（redirect_uri 与 authorize 逐字节一致）
    assert seen["token_form"] == {
        "grant_type": "authorization_code",
        "client_id": "cid",
        "client_secret": "csecret",
        "code": "the-code",
        "redirect_uri": "https://oce.example.com/auth/callback",
    }
    assert seen["userinfo_auth"] == "Bearer at-123"
    assert profile.linuxdo_id == 4242
    assert profile.username == "alice" and profile.trust_level == 2
    assert profile.active and not profile.silenced


async def test_missing_optional_fields_tolerated():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "at"})
        return httpx.Response(200, json={"id": 7, "username": "bob"})

    profile = await _client(handler).exchange_code("c")
    assert profile.linuxdo_id == 7 and profile.name is None and profile.trust_level == 0


async def test_missing_id_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "at"})
        return httpx.Response(200, json={"username": "noid"})

    with pytest.raises(OAuthExchangeError):
        await _client(handler).exchange_code("c")


async def test_upstream_http_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502)

    with pytest.raises(OAuthExchangeError):
        await _client(handler).exchange_code("c")


async def test_non_json_token_body_raises_exchange_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>proxy error page</html>")

    with pytest.raises(OAuthExchangeError):
        await _client(handler).exchange_code("c")


async def test_non_coercible_trust_level_raises_exchange_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "at"})
        return httpx.Response(200, json={"id": 5, "username": "x", "trust_level": "高"})

    with pytest.raises(OAuthExchangeError):
        await _client(handler).exchange_code("c")
