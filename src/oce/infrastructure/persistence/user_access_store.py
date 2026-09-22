"""用户与 API key 的 SQL 实现（UserAccessStore 端口）。

用户 key 校验按 hash 索引查找；明文持久化（运维选择的门户常显能力，与
model_credentials 回放明文同等安全姿态）。用量查询走 api_call_metrics /
token_usage_metrics，与 stats_store 同样只用可移植 SQL。
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, false, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from oce.application.user_access import (
    AdminUserOverview,
    AdminUserPage,
    ApiKeyIdentity,
    IssuedApiKey,
    LinuxDoProfile,
    UserApiKeyView,
    UserRecord,
    UserUsageSummary,
)
from oce.infrastructure.persistence.models import (
    ApiCallMetricModel,
    AppSettingModel,
    TokenUsageMetricModel,
    UserApiKeyModel,
    UserModel,
)
from oce.shared.key_hash import hash_api_key
from oce.shared.metrics import POLLING_ENDPOINTS

_KEY_PREFIX = "sk-oce-"
# touch 节流窗口：数据面每次请求都 resolve key，last_used_at 每次都 UPDATE 会放大写压
_TOUCH_THROTTLE_SECONDS = 60.0
_touch_last_seen: dict[int, float] = {}


def _generate_key() -> str:
    # token_urlsafe(32) ≈ 256bit 熵，无盐 sha256 存储已足够（非密码场景）
    return _KEY_PREFIX + secrets.token_urlsafe(32)


def _user_record(model: UserModel) -> UserRecord:
    return UserRecord(
        id=model.id,
        linuxdo_id=model.linuxdo_id,
        username=model.username,
        name=model.name,
        avatar_template=model.avatar_template,
        trust_level=model.trust_level,
        status=model.status,
        created_at=model.created_at,
        last_login_at=model.last_login_at,
    )


def _key_view(model: UserApiKeyModel) -> UserApiKeyView:
    return UserApiKeyView(
        key_last4=model.key_last4,
        status=model.status,
        created_at=model.created_at,
        last_used_at=model.last_used_at,
        api_key=model.key_plaintext,
    )


class SqlUserAccessStore:
    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def upsert_user(self, profile: LinuxDoProfile) -> UserRecord:
        """按 linuxdo_id upsert：可变字段（用户名/昵称/头像/等级/状态）每次登录刷新。"""
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            model = (
                (
                    await session.execute(
                        select(UserModel).where(UserModel.linuxdo_id == profile.linuxdo_id)
                    )
                )
                .scalars()
                .first()
            )
            if model is None:
                model = UserModel(linuxdo_id=profile.linuxdo_id)
                session.add(model)
            model.username = profile.username
            model.name = profile.name
            model.avatar_template = profile.avatar_template
            model.trust_level = profile.trust_level
            model.active = profile.active
            model.silenced = profile.silenced
            model.last_login_at = now
            try:
                await session.commit()
            except IntegrityError:
                # 并发首登录：另一请求已插入同 linuxdo_id 行（回调重放/双击）。
                # 回滚后重取既有行走更新路径，登录仍正常完成。
                await session.rollback()
                model = (
                    (
                        await session.execute(
                            select(UserModel).where(
                                UserModel.linuxdo_id == profile.linuxdo_id
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if model is None:  # pragma: no cover - 理论不可达
                    raise
                model.username = profile.username
                model.name = profile.name
                model.avatar_template = profile.avatar_template
                model.trust_level = profile.trust_level
                model.active = profile.active
                model.silenced = profile.silenced
                model.last_login_at = now
                await session.commit()
            await session.refresh(model)
            return _user_record(model)

    async def get_user(self, user_id: int) -> UserRecord | None:
        async with self._session_factory() as session:
            model = await session.get(UserModel, user_id)
            return _user_record(model) if model is not None else None

    async def set_user_status(self, user_id: int, status: str) -> UserRecord | None:
        async with self._session_factory() as session:
            model = await session.get(UserModel, user_id)
            if model is None:
                return None
            model.status = status
            await session.commit()
            await session.refresh(model)
            return _user_record(model)

    async def get_user_by_linuxdo(self, linuxdo_id: int) -> UserRecord | None:
        async with self._session_factory() as session:
            model = (
                (
                    await session.execute(
                        select(UserModel).where(UserModel.linuxdo_id == linuxdo_id)
                    )
                )
                .scalars()
                .first()
            )
            return _user_record(model) if model is not None else None

    async def count_users(self) -> int:
        async with self._session_factory() as session:
            return int(
                (await session.execute(select(func.count()).select_from(UserModel))).scalar_one()
            )

    async def delete_users_by_ids(self, user_ids: list[int]) -> tuple[int, ...]:
        """级联删除 key（FK ondelete CASCADE）；返回实际删除的 id（不含不存在者）。"""
        if not user_ids:
            return ()
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(UserModel.id).where(UserModel.id.in_(user_ids))
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                return ()
            await session.execute(
                delete(UserModel).where(UserModel.id.in_(rows))
            )
            await session.commit()
            return tuple(rows)

    async def user_ids_registered_between(
        self, date_from: str, date_to: str
    ) -> list[int]:
        """注册日落在 [date_from, date_to]（闭区间，UTC）的用户 id。"""
        start = datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc)
        end_exclusive = datetime.fromisoformat(date_to).replace(
            tzinfo=timezone.utc
        ) + timedelta(days=1)
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(UserModel.id).where(
                            UserModel.created_at >= start,
                            UserModel.created_at < end_exclusive,
                        )
                    )
                )
                .scalars()
                .all()
            )
            return list(rows)

    # ---- 运行时 kv 设置（注册名额覆盖等） ----

    async def get_int_setting(self, key: str) -> int | None:
        async with self._session_factory() as session:
            model = await session.get(AppSettingModel, key)
            if model is None or model.value == "":
                return None
            try:
                return int(model.value)
            except ValueError:
                return None

    async def set_int_setting(self, key: str, value: int) -> None:
        async with self._session_factory() as session:
            model = await session.get(AppSettingModel, key)
            if model is None:
                session.add(AppSettingModel(key=key, value=str(value)))
            else:
                model.value = str(value)
            await session.commit()

    async def get_active_api_key(self, user_id: int) -> UserApiKeyView | None:
        async with self._session_factory() as session:
            model = (
                (
                    await session.execute(
                        select(UserApiKeyModel).where(
                            UserApiKeyModel.user_id == user_id,
                            UserApiKeyModel.status == "active",
                        )
                    )
                )
                .scalars()
                .first()
            )
            return _key_view(model) if model is not None else None

    async def rotate_api_key(self, user_id: int) -> IssuedApiKey:
        """吊销全部 active 再签发新 key；「单 active」由部分唯一索引兜底，
        并发轮换冲突时重试一次（后完成者胜出）。"""
        now = datetime.now(timezone.utc)
        api_key = _generate_key()
        async with self._session_factory() as session:
            await self._revoke_active(session, user_id, now)
            session.add(self._new_key_row(user_id, api_key))
            try:
                await session.commit()
            except IntegrityError:
                # 并发轮换（双击/双开标签页）：另一请求已先插入 active 行。
                # 重试：连对方刚插的一并吊销再插入，不变式恢复为单 active。
                await session.rollback()
                await self._revoke_active(session, user_id, now)
                session.add(self._new_key_row(user_id, api_key))
                await session.commit()
            return IssuedApiKey(api_key=api_key, key_last4=api_key[-4:])

    @staticmethod
    async def _revoke_active(session: AsyncSession, user_id: int, now: datetime) -> None:
        await session.execute(
            update(UserApiKeyModel)
            .where(
                UserApiKeyModel.user_id == user_id,
                UserApiKeyModel.status == "active",
            )
            .values(status="revoked", revoked_at=now)
        )

    @staticmethod
    def _new_key_row(user_id: int, api_key: str) -> UserApiKeyModel:
        return UserApiKeyModel(
            user_id=user_id,
            key_hash=hash_api_key(api_key),
            key_plaintext=api_key,
            key_last4=api_key[-4:],
            status="active",
        )

    async def resolve_api_key(self, key_hash: str) -> ApiKeyIdentity | None:
        # JOIN users.status：本地封禁（disabled）必须即时切断数据面访问，
        # 而不是只挡住下一次登录
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(UserApiKeyModel.id, UserApiKeyModel.user_id)
                        .join(UserModel, UserModel.id == UserApiKeyModel.user_id)
                        .where(
                            UserApiKeyModel.key_hash == key_hash,
                            UserApiKeyModel.status == "active",
                            UserModel.status == "active",
                        )
                    )
                )
                .first()
            )
            if row is None:
                return None
            return ApiKeyIdentity(key_id=row.id, user_id=row.user_id)

    async def touch_api_key(self, key_id: int) -> None:
        """尽力而为刷新 last_used_at；节流 + 吞错——记账失败绝不能影响数据面。"""
        now = time.monotonic()
        if now - _touch_last_seen.get(key_id, 0.0) < _TOUCH_THROTTLE_SECONDS:
            return
        _touch_last_seen[key_id] = now
        try:
            async with self._session_factory() as session:
                await session.execute(
                    update(UserApiKeyModel)
                    .where(UserApiKeyModel.id == key_id)
                    .values(last_used_at=datetime.now(timezone.utc))
                )
                await session.commit()
        except Exception:  # noqa: BLE001 — 监控旁路语义：记账失败只跳过
            _touch_last_seen.pop(key_id, None)

    async def usage_summary(self, user_id: int, window_hours: int) -> UserUsageSummary:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        async with self._session_factory() as session:
            calls = (
                await session.execute(
                    select(func.count())
                    .select_from(ApiCallMetricModel)
                    .where(
                        ApiCallMetricModel.ts >= cutoff,
                        ApiCallMetricModel.user_id == user_id,
                        ApiCallMetricModel.endpoint.notin_(POLLING_ENDPOINTS),
                    )
                )
            ).scalar_one()
            tokens = (
                (
                    await session.execute(
                        select(func.coalesce(func.sum(TokenUsageMetricModel.total_tokens), 0))
                        .where(
                            TokenUsageMetricModel.ts >= cutoff,
                            TokenUsageMetricModel.user_id == user_id,
                        )
                    )
                ).scalar_one()
                or 0
            )
        return UserUsageSummary(
            window_hours=window_hours, api_calls=int(calls), total_tokens=int(tokens)
        )

    async def list_users_with_usage(
        self,
        window_hours: int = 24,
        *,
        page: int = 1,
        page_size: int = 0,
        search: str = "",
    ) -> AdminUserPage:
        """三段查询 + Python 拼装，避免方言特定的聚合/子查询。

        page_size>0 时服务端分页：keys/统计三段都限定当前页用户，
        total 单独 count。search 匹配 username/name 模糊，纯数字附带 ID 精确。
        page_size=0 保持全量（向后兼容），此时 total=len(items)。
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        async with self._session_factory() as session:
            user_filter = True
            if search := search.strip():
                user_filter = or_(
                    UserModel.username.ilike(f"%{search}%"),
                    UserModel.name.ilike(f"%{search}%"),
                )
                if search.isdigit():
                    user_filter = or_(user_filter, UserModel.id == int(search))

            if page_size > 0:
                total = int(
                    (
                        await session.execute(
                            select(func.count())
                            .select_from(UserModel)
                            .where(user_filter)
                        )
                    ).scalar_one()
                )
                users = (
                    (
                        await session.execute(
                            select(UserModel)
                            .where(user_filter)
                            .order_by(UserModel.id)
                            .offset((max(1, page) - 1) * page_size)
                            .limit(page_size)
                        )
                    )
                    .scalars()
                    .all()
                )
                page_ids = [user.id for user in users]

                def _scope(col):
                    # 每张表各自构建 in_ 表达式：跨表复用同一列表达式会带入其
                    # FROM 元素，触发 SQLAlchemy 笛卡尔积告警
                    return col.in_(page_ids) if page_ids else false()

                id_scope = _scope(UserApiKeyModel.user_id)
                call_scope = _scope(ApiCallMetricModel.user_id)
                token_scope = _scope(TokenUsageMetricModel.user_id)
            else:
                users = (
                    (
                        await session.execute(
                            select(UserModel)
                            .where(user_filter)
                            .order_by(UserModel.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                total = len(users)
                id_scope = call_scope = token_scope = True

            keys = {
                row.user_id: row.key_last4
                for row in (
                    await session.execute(
                        select(
                            UserApiKeyModel.user_id, UserApiKeyModel.key_last4
                        ).where(
                            UserApiKeyModel.status == "active",
                            id_scope,
                        )
                    )
                ).all()
            }
            call_rows = dict(
                (
                    await session.execute(
                        select(ApiCallMetricModel.user_id, func.count())
                        .where(
                            ApiCallMetricModel.ts >= cutoff,
                            ApiCallMetricModel.user_id.is_not(None),
                            ApiCallMetricModel.endpoint.notin_(POLLING_ENDPOINTS),
                            call_scope,
                        )
                        .group_by(ApiCallMetricModel.user_id)
                    )
                ).all()
            )
            token_rows = dict(
                (
                    await session.execute(
                        select(
                            TokenUsageMetricModel.user_id,
                            func.coalesce(func.sum(TokenUsageMetricModel.total_tokens), 0),
                        )
                        .where(
                            TokenUsageMetricModel.ts >= cutoff,
                            TokenUsageMetricModel.user_id.is_not(None),
                            token_scope,
                        )
                        .group_by(TokenUsageMetricModel.user_id)
                    )
                ).all()
            )
        items = tuple(
            AdminUserOverview(
                user=_user_record(user),
                api_key_last4=keys.get(user.id),
                api_calls_24h=int(call_rows.get(user.id, 0)),
                total_tokens_24h=int(token_rows.get(user.id, 0)),
            )
            for user in users
        )
        return AdminUserPage(items=items, total=total)
