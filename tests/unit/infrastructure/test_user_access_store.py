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
            # 客户端轮询：不计入个人用量
            for _ in range(5):
                session.add(
                    ApiCallMetricModel(
                        endpoint="/agents/blob-status",
                        method="POST",
                        status_code=200,
                        latency_ms=3,
                        user_id=user.id,
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
        # 轮询 5 次被排除：只数 1 次真实调用
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


async def test_delete_users_and_registered_range():
    from datetime import datetime, timezone

    async with _store() as (store, session_factory):
        u1 = await store.upsert_user(_profile(linuxdo_id=1, username="a"))
        u2 = await store.upsert_user(_profile(linuxdo_id=2, username="b"))

        # 批量删除（含不存在 id）
        deleted = await store.delete_users_by_ids([u1.id, u2.id, 999])
        assert deleted == (u1.id, u2.id)
        assert await store.get_user(u1.id) is None
        assert await store.count_users() == 0

        # 注册日期区间：手工回填 created_at 以脱离「现在」
        u3 = await store.upsert_user(_profile(linuxdo_id=3, username="c"))
        async with session_factory() as session:
            model = await session.get(UserModel, u3.id)
            model.created_at = datetime(2026, 9, 3, tzinfo=timezone.utc)
            await session.commit()
        ids = await store.user_ids_registered_between("2026-09-01", "2026-09-07")
        assert ids == [u3.id]
        ids_empty = await store.user_ids_registered_between("2026-08-01", "2026-08-31")
        assert ids_empty == []


async def test_registration_quota_setting_roundtrip():
    async with _store() as (store, _):
        assert await store.get_int_setting("auth.max_users") is None
        await store.set_int_setting("auth.max_users", 5)
        assert await store.get_int_setting("auth.max_users") == 5
        await store.set_int_setting("auth.max_users", 0)
        assert await store.get_int_setting("auth.max_users") == 0


async def test_registration_quota_blocks_new_but_not_existing():
    from oce.application.user_access import UserAccessService
    from oce.shared.errors import LoginDeniedError

    class _Provider:
        @property
        def redirect_uri(self):
            return "https://x/cb"

        def authorize_url(self, state):
            return "https://x"

        async def exchange_code(self, code):
            return _profile(linuxdo_id=77, username="newbie")

    async with _store() as (store, _):
        await store.upsert_user(_profile(linuxdo_id=76, username="old"))
        await store.set_int_setting("auth.max_users", 1)  # 已满

        service = UserAccessService(store=store, provider=_Provider())
        with pytest.raises(LoginDeniedError, match="名额"):
            await service.authenticate("code")
        # 既有用户不受影响
        assert (await store.get_user_by_linuxdo(76)) is not None


async def test_count_completed_windows():
    from datetime import datetime, timezone

    from oce.infrastructure.persistence.sql_blob_repo import SqlBlobRepository

    async with _store() as (_, session_factory):
        async with session_factory() as session:
            repo = SqlBlobRepository(session)
            now = datetime.now(timezone.utc)
            from oce.domain.blob.blob import Blob

            for minutes_ago, name in ((0.5, "a"), (30, "b"), (3 * 60, "c"), (10 * 24 * 60, "d")):
                blob = Blob(blob_name=hash_api_key(name), path=f"{name}.py")
                blob.status = blob.status.READY
                blob.completed_at = datetime.fromtimestamp(
                    now.timestamp() - minutes_ago * 60, tz=timezone.utc
                )
                from oce.infrastructure.persistence.models import BlobModel

                session.add(
                    BlobModel(
                        blob_name=blob.blob_name,
                        path=blob.path,
                        content_size=1,
                        file_type="text",
                        status="ready",
                        completed_at=blob.completed_at,
                    )
                )
            await session.commit()

            counts = await repo.count_completed_windows(
                {"last_1m": 60, "last_1h": 3600, "last_24h": 86400, "last_7d": 604800, "last_30d": 2592000}
            )
        assert counts["last_1m"] == 1
        assert counts["last_1h"] == 2
        assert counts["last_24h"] == 3
        assert counts["last_7d"] == 3
        assert counts["last_30d"] == 4


async def test_count_failed_windows_and_error_total():
    from datetime import datetime, timezone

    from oce.infrastructure.persistence.models import BlobModel
    from oce.infrastructure.persistence.sql_blob_repo import SqlBlobRepository

    async with _store() as (_, session_factory):
        async with session_factory() as session:
            now = datetime.now(timezone.utc)
            # 2 个近期失败 + 1 个老失败 + 1 个 ready
            # 30s/30min 避开窗口边界竞态；40 天前超出 30d 窗口
            for minutes_ago, name, status in ((0.5, "f1", "error"), (30, "f2", "error"), (40 * 24 * 60, "f3", "error")):
                session.add(
                    BlobModel(
                        blob_name=hash_api_key(name),
                        path=f"{name}.py",
                        content_size=1,
                        file_type="text",
                        status=status,
                        failed_at=datetime.fromtimestamp(
                            now.timestamp() - minutes_ago * 60, tz=timezone.utc
                        ),
                    )
                )
            session.add(
                BlobModel(
                    blob_name=hash_api_key("ok1"),
                    path="ok1.py",
                    content_size=1,
                    file_type="text",
                    status="ready",
                )
            )
            await session.commit()

            repo = SqlBlobRepository(session)
            windows = {"last_1m": 60, "last_1h": 3600, "last_30d": 2592000}
            failed, error_total = await repo.count_failed_windows(windows)
            assert failed["last_1m"] == 1
            assert failed["last_1h"] == 2
            assert failed["last_30d"] == 2  # 40 天前的失败超出 30d 窗口
        assert error_total == 3


async def test_find_error_names_and_reset_semantics():
    from oce.infrastructure.persistence.models import BlobModel

    async with _store() as (_, session_factory):
        async with session_factory() as session:
            session.add(
                BlobModel(
                    blob_name=hash_api_key("e1"),
                    path="e1.py",
                    content_size=1,
                    file_type="text",
                    status="error",
                )
            )
            session.add(
                BlobModel(
                    blob_name=hash_api_key("r1"),
                    path="r1.py",
                    content_size=1,
                    file_type="text",
                    status="ready",
                )
            )
            await session.commit()
            repo_names = None
        from oce.infrastructure.persistence.sql_blob_repo import SqlBlobRepository

        async with session_factory() as session:
            repo = SqlBlobRepository(session)
            repo_names = await repo.find_error_names(10)
        assert repo_names == [hash_api_key("e1")]
