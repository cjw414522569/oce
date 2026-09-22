"""多用户接入的应用层：端口、读模型与编排服务。

用户身份来自 LinuxDo OAuth2（connect.linux.do），数据面鉴权用每用户独立
sk-oce-* key（hash 索引校验 + 明文持久化供门户常显）。与 credential_admin
同款分层：本文件只定义 Protocol 端口与编排，SQL 实现在 infrastructure。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from oce.application.messages import Command, Query
from oce.shared.errors import LoginDeniedError


@dataclass(frozen=True)
class LinuxDoProfile:
    """userinfo 归一化结果；linuxdo_id 是提供方不可变标识。"""

    linuxdo_id: int
    username: str
    name: str | None = None
    avatar_template: str | None = None
    active: bool = True
    silenced: bool = False
    trust_level: int = 0


@dataclass(frozen=True)
class UserRecord:
    id: int
    linuxdo_id: int
    username: str
    name: str | None
    avatar_template: str | None
    trust_level: int
    status: str
    created_at: datetime
    last_login_at: datetime | None


@dataclass(frozen=True)
class UserApiKeyView:
    """key 视图。api_key 为明文（运维选择门户常显）；存量行可能为 None——
    此时仅展示末 4 位，轮换一次后即有明文。"""

    key_last4: str
    status: str
    created_at: datetime
    last_used_at: datetime | None
    api_key: str | None = None


@dataclass(frozen=True)
class IssuedApiKey:
    """签发/轮换的返回；key 同时持久化，门户可随时查看。"""

    api_key: str
    key_last4: str


@dataclass(frozen=True)
class ApiKeyIdentity:
    key_id: int
    user_id: int


@dataclass(frozen=True)
class UserUsageSummary:
    window_hours: int
    api_calls: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class PortalView:
    user: UserRecord
    api_key: UserApiKeyView | None
    usage_24h: UserUsageSummary = field(default_factory=lambda: UserUsageSummary(24))
    usage_7d: UserUsageSummary = field(default_factory=lambda: UserUsageSummary(168))


@dataclass(frozen=True)
class AdminUserOverview:
    user: UserRecord
    api_key_last4: str | None
    api_calls_24h: int = 0
    total_tokens_24h: int = 0


@dataclass(frozen=True)
class LeaderboardEntry:
    """今日用量榜条目；排序由 store 保证（tokens 降序，calls 次级）。"""

    user_id: int
    username: str
    name: str | None
    api_calls: int = 0
    total_tokens: int = 0


class OAuthProvider(Protocol):
    """LinuxDo OAuth2 客户端端口（infrastructure 实现）。"""

    @property
    def redirect_uri(self) -> str: ...

    def authorize_url(self, state: str) -> str: ...

    async def exchange_code(self, code: str) -> LinuxDoProfile: ...


class UserAccessStore(Protocol):
    """用户与 key 的持久化端口；独立于领域 UoW（同 credential store 定位）。"""

    async def upsert_user(self, profile: LinuxDoProfile) -> UserRecord: ...

    async def get_user(self, user_id: int) -> UserRecord | None: ...

    async def set_user_status(self, user_id: int, status: str) -> UserRecord | None: ...

    async def get_user_by_linuxdo(self, linuxdo_id: int) -> UserRecord | None: ...

    async def count_users(self) -> int: ...

    async def delete_users_by_ids(self, user_ids: list[int]) -> tuple[int, ...]: ...

    async def user_ids_registered_between(self, date_from: str, date_to: str) -> list[int]: ...

    async def get_int_setting(self, key: str) -> int | None: ...

    async def set_int_setting(self, key: str, value: int) -> None: ...

    async def get_active_api_key(self, user_id: int) -> UserApiKeyView | None: ...

    async def rotate_api_key(self, user_id: int) -> IssuedApiKey: ...

    async def resolve_api_key(self, key_hash: str) -> ApiKeyIdentity | None: ...

    async def touch_api_key(self, key_id: int) -> None: ...

    async def usage_summary(self, user_id: int, window_hours: int) -> UserUsageSummary: ...

    async def usage_leaderboard(self, day_start: datetime) -> list[LeaderboardEntry]: ...

    async def list_users_with_usage(
        self, window_hours: int = 24, *, page: int = 1, page_size: int = 0, search: str = ""
    ) -> AdminUserPage: ...


class UserAccessService:
    """登录 / 门户 / key 生命周期编排。provider 为 None 表示功能整体关闭。"""

    _MAX_USERS_KEY = "auth.max_users"

    def __init__(
        self,
        *,
        store: UserAccessStore,
        provider: OAuthProvider | None,
        min_trust_level: int | None = None,
        env_max_users: int = 0,
    ) -> None:
        self._store = store
        self._provider = provider
        self._min_trust_level = min_trust_level
        self._env_max_users = max(0, env_max_users)

    @property
    def provider(self) -> OAuthProvider | None:
        return self._provider

    @property
    def store(self) -> UserAccessStore:
        return self._store

    async def authenticate(self, code: str) -> UserRecord:
        """code → 用户记录；首次登录懒签发 key。拒绝条件见各分支。"""
        if self._provider is None:
            raise LoginDeniedError("多用户接入未启用")
        profile = await self._provider.exchange_code(code)
        if not profile.active or profile.silenced:
            raise LoginDeniedError("提供方账号状态不允许（inactive 或被禁言）")
        if self._min_trust_level is not None and profile.trust_level < self._min_trust_level:
            raise LoginDeniedError(f"信任等级低于门槛 {self._min_trust_level}")
        await self._enforce_registration_quota(profile)
        user = await self._store.upsert_user(profile)
        if user.status != "active":
            raise LoginDeniedError("账号已被本服务禁用")
        if await self._store.get_active_api_key(user.id) is None:
            await self._store.rotate_api_key(user.id)
        return user

    async def usage_leaderboard(
        self, day_start: datetime, limit: int = 0
    ) -> list[LeaderboardEntry]:
        """今日用量榜；limit>0 时截断前 N 名（调用方需要自己的完整排名时不截断）。"""
        entries = await self._store.usage_leaderboard(day_start)
        return entries[:limit] if limit > 0 else entries

    async def get_portal(self, user_id: int) -> PortalView:
        user = await self._store.get_user(user_id)
        if user is None or user.status != "active":
            # 会话仍有效但用户已删/禁：按未登录处理，门户切回登录页
            raise LoginDeniedError("账号不可用")
        return PortalView(
            user=user,
            api_key=await self._store.get_active_api_key(user_id),
            usage_24h=await self._store.usage_summary(user_id, 24),
            usage_7d=await self._store.usage_summary(user_id, 24 * 7),
        )

    async def rotate_key(self, user_id: int) -> IssuedApiKey:
        user = await self._store.get_user(user_id)
        if user is None or user.status != "active":
            raise LoginDeniedError("账号不可用")
        return await self._store.rotate_api_key(user_id)

    async def effective_max_users(self) -> int:
        override = await self._store.get_int_setting(self._MAX_USERS_KEY)
        return self._env_max_users if override is None else max(0, override)

    async def registration_info(self) -> RegistrationInfo:
        override = await self._store.get_int_setting(self._MAX_USERS_KEY)
        return RegistrationInfo(
            env_max_users=self._env_max_users,
            override=override,
            effective_max_users=(
                self._env_max_users if override is None else max(0, override)
            ),
            active_count=await self._store.count_users(),
        )

    async def set_max_users(self, max_users: int) -> None:
        await self._store.set_int_setting(self._MAX_USERS_KEY, max(0, max_users))

    async def _enforce_registration_quota(self, profile: LinuxDoProfile) -> None:
        """名额只挡新注册：老用户在满员后仍可登录。"""
        limit = await self.effective_max_users()
        if limit <= 0:
            return
        if await self._store.get_user_by_linuxdo(profile.linuxdo_id) is not None:
            return
        if await self._store.count_users() >= limit:
            raise LoginDeniedError(f"注册名额已满（{limit} 人）")


@dataclass(frozen=True)
class RegistrationInfo:
    """注册开关状态：覆盖值优先于 env 默认，0 = 不限。"""

    env_max_users: int
    override: int | None
    effective_max_users: int
    active_count: int

    @property
    def open(self) -> bool:
        return self.effective_max_users <= 0 or self.active_count < self.effective_max_users


@dataclass(frozen=True)
class ListUsersQuery(Query):
    window_hours: int = 24
    page: int = 1        # 1-based；page_size=0 时忽略（全量，向后兼容）
    page_size: int = 0   # 0 = 不分页
    search: str = ""     # username / name 模糊，纯数字时附带 ID 精确匹配


@dataclass(frozen=True)
class AdminUserPage:
    """分页结果：items 为当前页，total 为过滤后的总数。"""

    items: tuple[AdminUserOverview, ...]
    total: int


class ListUsersQueryHandler:
    """admin 用户总览（/admin/users）。"""

    def __init__(self, service: UserAccessService) -> None:
        self._service = service

    async def handle(self, query: ListUsersQuery) -> AdminUserPage:
        return await self._service.store.list_users_with_usage(
            query.window_hours,
            page=query.page,
            page_size=query.page_size,
            search=query.search,
        )


@dataclass(frozen=True)
class SetUserStatusCommand(Command):
    user_id: int
    status: str  # active | disabled


@dataclass(frozen=True)
class DeleteUsersResult:
    deleted_ids: tuple[int, ...] = ()

    @property
    def deleted_count(self) -> int:
        return len(self.deleted_ids)


class SetUserStatusCommandHandler:
    """本地封禁/解封（/admin/users/{id} PATCH）。封禁即时切断数据面（resolve JOIN）。"""

    def __init__(self, store: UserAccessStore) -> None:
        self._store = store

    async def handle(self, command: SetUserStatusCommand) -> UserRecord:
        if command.status not in ("active", "disabled"):
            raise ValueError("status 只允许 active | disabled")
        record = await self._store.set_user_status(command.user_id, command.status)
        if record is None:
            raise LookupError("user not found")
        return record


@dataclass(frozen=True)
class DeleteUserCommand(Command):
    user_id: int


class DeleteUserCommandHandler:
    def __init__(self, store: UserAccessStore) -> None:
        self._store = store

    async def handle(self, command: DeleteUserCommand) -> DeleteUsersResult:
        deleted = await self._store.delete_users_by_ids([command.user_id])
        if not deleted:
            raise LookupError("user not found")
        return DeleteUsersResult(deleted_ids=deleted)


@dataclass(frozen=True)
class DeleteUsersByIdsCommand(Command):
    user_ids: tuple[int, ...]


class DeleteUsersByIdsCommandHandler:
    def __init__(self, store: UserAccessStore) -> None:
        self._store = store

    async def handle(self, command: DeleteUsersByIdsCommand) -> DeleteUsersResult:
        deleted = await self._store.delete_users_by_ids(list(command.user_ids))
        return DeleteUsersResult(deleted_ids=deleted)


@dataclass(frozen=True)
class DeleteUsersRegisteredCommand(Command):
    date_from: str  # YYYY-MM-DD（含）
    date_to: str  # YYYY-MM-DD（含）
    dry_run: bool = True


class DeleteUsersRegisteredCommandHandler:
    """按注册日期区间删除（某一天=from==to；某一周=起止同周）。dry_run 只计数。"""

    def __init__(self, store: UserAccessStore) -> None:
        self._store = store

    async def handle(self, command: DeleteUsersRegisteredCommand) -> DeleteUsersResult:
        ids = await self._store.user_ids_registered_between(
            command.date_from, command.date_to
        )
        if command.dry_run:
            return DeleteUsersResult(deleted_ids=tuple(ids))
        deleted = await self._store.delete_users_by_ids(ids)
        return DeleteUsersResult(deleted_ids=deleted)


@dataclass(frozen=True)
class RegistrationInfoQuery(Query):
    pass


class RegistrationInfoQueryHandler:
    def __init__(self, service: UserAccessService) -> None:
        self._service = service

    async def handle(self, _query: RegistrationInfoQuery) -> RegistrationInfo:
        return await self._service.registration_info()


@dataclass(frozen=True)
class SetMaxUsersCommand(Command):
    max_users: int  # 0 = 不限；>=0


class SetMaxUsersCommandHandler:
    def __init__(self, service: UserAccessService) -> None:
        self._service = service

    async def handle(self, command: SetMaxUsersCommand) -> RegistrationInfo:
        if command.max_users < 0:
            raise ValueError("max_users 必须 >= 0（0 = 不限）")
        await self._service.set_max_users(command.max_users)
        return await self._service.registration_info()
