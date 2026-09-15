"""SqlUserAccessStore：upsert、rotate、resolve（含封禁）、touch 节流、用量聚合。"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from oce.application.user_access import LinuxDoProfile
from oce.infrastructure.persistence.models import (
    ApiCallMetricModel,
    TokenUsageMetricModel,
    UserApiKeyModel,
    UserModel,
)
from oce.infrastructure.persistence.user_access_store import SqlUserAccessStore
from oce.shared.database.session import Base
from oce.shared.key_hash import hash_api_key


@asynccontextmanager
async def _store():
    """独立内存库 + store；用例结束 dispose，避免连接 GC 告警升级为错误。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield SqlUserAccessStore(session_factory), session_factory
    finally:
        await engine.dispose()


def _profile(linuxdo_id: int = 1001, **overrides) -> LinuxDoProfile:
    values = dict(
        linuxdo_id=linuxdo_id,
        username="alice",
        name="Alice",
        avatar_template="https://cdn/a/{size}.png",
        trust_level=2,
    )
    values.update(overrides)
    return LinuxDoProfile(**values)


async def test_upsert_inserts_then_updates_same_row():
    async with _store() as (store, _):
        first = await store.upsert_user(_profile(username="alice"))
        second = await store.upsert_user(_profile(username="alice2", trust_level=3))
        assert second.id == first.id  # linuxdo_id 是 upsert 键，不产生新行
        assert second.username == "alice2" and second.trust_level == 3
        assert second.last_login_at is not None


async def test_rotate_revokes_old_and_issues_new():
    async with _store() as (store, _):
        user = await store.upsert_user(_profile())
        issued = await store.rotate_api_key(user.id)
        assert issued.api_key.startswith("sk-oce-")
        assert issued.key_last4 == issued.api_key[-4:]

        view = await store.get_active_api_key(user.id)
        assert view is not None and view.key_last4 == issued.key_last4
        assert view.api_key == issued.api_key  # 明文持久化（门户常显）

        # 轮换后旧 key 不再可解析（单 active 不变式）
        assert await store.resolve_api_key(hash_api_key(issued.api_key)) is not None
        issued2 = await store.rotate_api_key(user.id)
        assert await store.resolve_api_key(hash_api_key(issued.api_key)) is None
        assert await store.resolve_api_key(hash_api_key(issued2.api_key)) is not None


async def test_single_active_enforced_at_db_level():
    # 部分唯一索引：绕过 store 直接插第二行 active 应被拒
    async with _store() as (store, session_factory):
        user = await store.upsert_user(_profile())
        await store.rotate_api_key(user.id)
        async with session_factory() as session:
            session.add(
                UserApiKeyModel(
                    user_id=user.id,
                    key_hash=hash_api_key("sk-oce-colliding-key"),
                    key_last4="key-",
                    status="active",
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()


async def test_resolve_unknown_hash_returns_none():
    async with _store() as (store, _):
        assert await store.resolve_api_key("0" * 64) is None


async def test_resolve_rejects_key_of_locally_disabled_user():
    # 本地封禁必须即时切断数据面：resolve JOIN users.status
    async with _store() as (store, session_factory):
        user = await store.upsert_user(_profile())
        issued = await store.rotate_api_key(user.id)
        assert await store.resolve_api_key(hash_api_key(issued.api_key)) is not None

        async with session_factory() as session:
            await session.execute(
                update(UserModel)
                .where(UserModel.id == user.id)
                .values(status="disabled")
            )
            await session.commit()

        assert await store.resolve_api_key(hash_api_key(issued.api_key)) is None


async def test_usage_summary_and_admin_listing():
    async with _store() as (store, session_factory):
        user = await store.upsert_user(_profile())
        issued = await store.rotate_api_key(user.id)

        async with session_factory() as session:
            session.add(
                ApiCallMetricModel(
                    endpoint="/agents/codebase-retrieval",
                    method="POST",
                    status_code=200,
                    latency_ms=12,
                    user_id=user.id,
                )
            )
            session.add(
                ApiCallMetricModel(
                    endpoint="/agents/codebase-retrieval",
                    method="POST",
                    status_code=200,
                    latency_ms=8,
                    user_id=None,  # 超管/旧数据：不计入任何用户
                )
            )
            session.add(
                TokenUsageMetricModel(
                    kind="embed",
                    model="qwen3-embedding-8b",
                    total_tokens=100,
                    user_id=user.id,
                )
            )
            await session.commit()

        summary = await store.usage_summary(user.id, 24)
        assert summary.api_calls == 1 and summary.total_tokens == 100

        overviews = await store.list_users_with_usage(24)
        assert len(overviews) == 1
        assert overviews[0].user.id == user.id
        assert overviews[0].api_key_last4 == issued.key_last4
        assert overviews[0].api_calls_24h == 1
        assert overviews[0].total_tokens_24h == 100


async def test_touch_throttled_within_window():
    async with _store() as (store, _):
        user = await store.upsert_user(_profile())
        issued = await store.rotate_api_key(user.id)
        identity = await store.resolve_api_key(hash_api_key(issued.api_key))
        assert identity is not None

        from oce.infrastructure.persistence import user_access_store as mod

        mod._touch_last_seen.clear()
        await store.touch_api_key(identity.key_id)
        first_ts = mod._touch_last_seen[identity.key_id]
        await store.touch_api_key(identity.key_id)
        assert mod._touch_last_seen[identity.key_id] == first_ts  # 未刷新 → 被节流
