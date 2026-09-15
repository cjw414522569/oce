"""/admin/users：admin key 保护 + 返回结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Header
from oce.api.admin_router import admin_router, get_application
from oce.auth import _unauthorized, verify_admin_key


@dataclass
class StubApplication:
    users: tuple = field(default_factory=tuple)

    async def list_users(self) -> Any:
        return self.users


def _overview(username: str):
    from oce.application.user_access import AdminUserOverview, UserRecord

    user = UserRecord(
        id=1,
        linuxdo_id=1001,
        username=username,
        name="Alice",
        avatar_template=None,
        trust_level=2,
        status="active",
        created_at=datetime.now(timezone.utc),
        last_login_at=None,
    )
    return AdminUserOverview(
        user=user, api_key_last4="abcd", api_calls_24h=5, total_tokens_24h=250
    )


@pytest.fixture
def app() -> FastAPI:
    application = FastAPI()
    application.include_router(admin_router)
    application.dependency_overrides[get_application] = lambda: StubApplication(
        users=(_overview("alice"),)
    )
    return application


@pytest.fixture
def client(app, monkeypatch) -> httpx.AsyncClient:
    async def _mock_admin_auth(
        authorization: str | None = Header(default=None),
    ) -> str:
        if authorization != "Bearer sk-admin":
            raise _unauthorized("Invalid admin key provided")
        return "sk-admin"

    app.dependency_overrides[verify_admin_key] = _mock_admin_auth
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_lists_users_with_usage(client):
    resp = await client.get("/admin/users", headers={"Authorization": "Bearer sk-admin"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["users"]) == 1
    entry = body["users"][0]
    assert entry["username"] == "alice"
    assert entry["api_key_last4"] == "abcd"
    assert entry["api_calls_24h"] == 5
    assert entry["total_tokens_24h"] == 250


async def test_admin_key_required(client):
    resp = await client.get("/admin/users", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401
