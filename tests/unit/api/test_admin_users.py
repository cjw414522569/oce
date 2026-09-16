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


async def test_patch_user_status_updates_row(client, monkeypatch):
    from oce.application.user_access import UserRecord

    record_holder = {}

    class _PatchApp(StubApplication):
        async def set_user_status(self, user_id: int, status: str) -> UserRecord:
            record_holder["called"] = (user_id, status)
            return _overview("alice").user.__class__(
                id=user_id,
                linuxdo_id=1001,
                username="alice",
                name="Alice",
                avatar_template=None,
                trust_level=2,
                status=status,
                created_at=_overview("alice").user.created_at,
                last_login_at=None,
            )

    app = client._transport.app  # noqa: SLF001
    app.dependency_overrides[get_application] = lambda: _PatchApp()
    resp = await client.patch(
        "/admin/users/1",
        json={"status": "disabled"},
        headers={"Authorization": "Bearer sk-admin"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"
    assert record_holder["called"] == (1, "disabled")


async def test_patch_user_status_invalid_rejected(client):
    resp = await client.patch(
        "/admin/users/1",
        json={"status": "banned"},
        headers={"Authorization": "Bearer sk-admin"},
    )
    assert resp.status_code == 422  # Literal 校验


@dataclass
class StubDeletionApp:
    async def delete_user(self, user_id: int):
        from oce.application.user_access import DeleteUsersResult
        return DeleteUsersResult(deleted_ids=(user_id,)) if user_id != 99 else DeleteUsersResult()

    async def delete_users(self, user_ids):
        from oce.application.user_access import DeleteUsersResult
        return DeleteUsersResult(deleted_ids=tuple(user_ids))

    async def delete_users_registered(self, date_from, date_to, dry_run):
        from oce.application.user_access import DeleteUsersResult
        return DeleteUsersResult(deleted_ids=(1, 2) if not dry_run else (1, 2, 3))

    async def registration_info(self):
        from oce.application.user_access import RegistrationInfo
        return RegistrationInfo(env_max_users=0, override=5, effective_max_users=5, active_count=3)

    async def set_max_users(self, max_users):
        from oce.application.user_access import RegistrationInfo
        return RegistrationInfo(env_max_users=0, override=max_users, effective_max_users=max_users, active_count=3)


async def test_delete_user(client):
    app = client._transport.app  # noqa: SLF001
    app.dependency_overrides[get_application] = lambda: StubDeletionApp()
    ok = await client.delete("/admin/users/1", headers={"Authorization": "Bearer sk-admin"})
    assert ok.status_code == 200 and ok.json()["deleted_count"] == 1
    missing = await client.delete("/admin/users/99", headers={"Authorization": "Bearer sk-admin"})
    assert missing.status_code == 404


async def test_batch_delete_users(client):
    app = client._transport.app  # noqa: SLF001
    app.dependency_overrides[get_application] = lambda: StubDeletionApp()
    resp = await client.post(
        "/admin/users/batch-delete",
        json={"user_ids": [1, 2, 3]},
        headers={"Authorization": "Bearer sk-admin"},
    )
    assert resp.status_code == 200
    assert resp.json()["deleted_count"] == 3


async def test_delete_registered_dry_run_and_execute(client):
    app = client._transport.app  # noqa: SLF001
    app.dependency_overrides[get_application] = lambda: StubDeletionApp()
    dry = await client.post(
        "/admin/users/delete-registered",
        json={"date_from": "2026-09-01", "date_to": "2026-09-07", "dry_run": True},
        headers={"Authorization": "Bearer sk-admin"},
    )
    assert dry.status_code == 200 and dry.json()["deleted_count"] == 3
    real = await client.post(
        "/admin/users/delete-registered",
        json={"date_from": "2026-09-01", "date_to": "2026-09-07", "dry_run": False},
        headers={"Authorization": "Bearer sk-admin"},
    )
    assert real.status_code == 200 and real.json()["deleted_count"] == 2
    bad = await client.post(
        "/admin/users/delete-registered",
        json={"date_from": "2026-09-08", "date_to": "2026-09-01", "dry_run": True},
        headers={"Authorization": "Bearer sk-admin"},
    )
    assert bad.status_code == 422


async def test_registration_quota_endpoints(client):
    app = client._transport.app  # noqa: SLF001
    app.dependency_overrides[get_application] = lambda: StubDeletionApp()
    info = await client.get("/admin/users/registration", headers={"Authorization": "Bearer sk-admin"})
    assert info.status_code == 200
    assert info.json()["effective_max_users"] == 5 and info.json()["open"] is True
    patched = await client.patch(
        "/admin/users/registration",
        json={"max_users": 10},
        headers={"Authorization": "Bearer sk-admin"},
    )
    assert patched.status_code == 200 and patched.json()["effective_max_users"] == 10
    invalid = await client.patch(
        "/admin/users/registration",
        json={"max_users": -1},
        headers={"Authorization": "Bearer sk-admin"},
    )
    assert invalid.status_code == 422


async def test_queue_throughput_endpoint(client):
    from oce.application.queries.queue import QueueThroughputResult

    class _ThroughputApp(StubApplication):
        async def queue_throughput(self):
            return QueueThroughputResult(
                counts={"last_1m": 3, "last_1h": 42, "last_24h": 900, "last_7d": 7000, "last_30d": 14000},
                failed={"last_1m": 0, "last_1h": 1, "last_24h": 5, "last_7d": 5, "last_30d": 5},
                error_total=5,
            )

    app = client._transport.app  # noqa: SLF001
    app.dependency_overrides[get_application] = lambda: _ThroughputApp()
    resp = await client.get("/admin/queue/throughput", headers={"Authorization": "Bearer sk-admin"})
    assert resp.status_code == 200
    assert resp.json()["counts"]["last_1h"] == 42
    assert resp.json()["error_total"] == 5


async def test_queue_throughput_includes_failures(client):
    class _ThroughputApp2(StubApplication):
        async def queue_throughput(self):
            from oce.application.queries.queue import QueueThroughputResult

            return QueueThroughputResult(
                counts={"last_1h": 5},
                failed={"last_1h": 2},
                error_total=3434,
            )

        async def clear_failed_blobs(self, limit: int) -> int:
            return limit

    app = client._transport.app  # noqa: SLF001
    app.dependency_overrides[get_application] = lambda: _ThroughputApp2()
    resp = await client.get("/admin/queue/throughput", headers={"Authorization": "Bearer sk-admin"})
    body = resp.json()
    assert body["failed"]["last_1h"] == 2 and body["error_total"] == 3434
    cleared = await client.post(
        "/admin/queue/clear-failed",
        json={"limit": 500},
        headers={"Authorization": "Bearer sk-admin"},
    )
    assert cleared.status_code == 200 and cleared.json()["cleared"] == 500
