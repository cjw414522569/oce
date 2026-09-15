"""多用户接入的应用层：端口、读模型与编排服务。

用户身份来自 LinuxDo OAuth2（connect.linux.do），数据面鉴权用每用户独立
sk-oce-* key（hash 索引校验 + 明文持久化供门户常显）。与 credential_admin
同款分层：本文件只定义 Protocol 端口与编排，SQL 实现在 infrastructure。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from oce.application.messages import Query
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

    async def get_active_api_key(self, user_id: int) -> UserApiKeyView | None: ...

    async def rotate_api_key(self, user_id: int) -> IssuedApiKey: ...

    async def resolve_api_key(self, key_hash: str) -> ApiKeyIdentity | None: ...

    async def touch_api_key(self, key_id: int) -> None: ...

    async def usage_summary(self, user_id: int, window_hours: int) -> UserUsageSummary: ...

    async def list_users_with_usage(self, window_hours: int = 24) -> tuple[AdminUserOverview, ...]: ...


class UserAccessService:
    """登录 / 门户 / key 生命周期编排。provider 为 None 表示功能整体关闭。"""

    def __init__(
        self,
        *,
        store: UserAccessStore,
        provider: OAuthProvider | None,
        min_trust_level: int | None = None,
    ) -> None:
        self._store = store
        self._provider = provider
        self._min_trust_level = min_trust_level

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
        user = await self._store.upsert_user(profile)
        if user.status != "active":
            raise LoginDeniedError("账号已被本服务禁用")
        if await self._store.get_active_api_key(user.id) is None:
            await self._store.rotate_api_key(user.id)
        return user

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


@dataclass(frozen=True)
class ListUsersQuery(Query):
    window_hours: int = 24


class ListUsersQueryHandler:
    """admin 用户总览（/admin/users）。"""

    def __init__(self, service: UserAccessService) -> None:
        self._service = service

    async def handle(self, query: ListUsersQuery) -> tuple[AdminUserOverview, ...]:
        return await self._service.store.list_users_with_usage(query.window_hours)
