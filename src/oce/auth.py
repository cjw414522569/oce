"""HTTP Bearer 鉴权。

数据面两级：全局 API_KEY（超管，行为与单 key 模式完全一致）→ 每用户
sk-oce-* key（AUTH_ENABLED 时按 sha256 查 user_api_keys）。运维面独立
verify_admin_key。用户身份双通道传播：scope dict 给监控中间件（BaseHTTPMiddleware
跨任务），ContextVar 给 token 用量桥（同任务向下），见 shared/user_context.py。
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Request

from oce.shared.config import get_settings
from oce.shared.key_hash import hash_api_key


def _unauthorized(message: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "param": None,
                "code": "invalid_api_key",
            }
        },
    )


def _extract_bearer(authorization: str | None) -> str:
    """从 ``Authorization: Bearer <key>`` 取出 key，缺失或格式错误抛 401。"""
    if authorization is None:
        raise _unauthorized("You didn't provide an API key.")
    if not authorization.startswith("Bearer "):
        raise _unauthorized("Invalid API key format. Expected 'Bearer <key>'")
    return authorization.removeprefix("Bearer ")


async def verify_api_key(
    authorization: str | None = Header(default=None),
    request: Request = None,  # type: ignore[assignment]  # FastAPI 注入；None 兼容直接调用
) -> str:
    """校验客户端 ``Authorization: Bearer <key>``。

    全局 key 命中即超管（user_id 归属为空）；否则仅在多用户接入开启时查用户 key。
    个人模式（未开启）与历史行为逐字节一致：不触容器、不查库。
    """
    api_key = _extract_bearer(authorization)
    settings = get_settings()
    if hmac.compare_digest(api_key, settings.api_key):
        if request is not None:
            request.scope["oce_user_id"] = None
        return api_key

    if not settings.auth.enabled:
        raise _unauthorized("Invalid API key provided")

    # 懒导入避免 import 环（container 反向依赖 auth 链路）与超管快路径的容器构建
    from oce.application.container import get_container
    from oce.shared.user_context import set_current_user_id

    identity = await get_container().user_access.store.resolve_api_key(
        hash_api_key(api_key)
    )
    if identity is None:
        raise _unauthorized("Invalid API key provided")

    set_current_user_id(identity.user_id)
    if request is not None:
        request.scope["oce_user_id"] = identity.user_id
    try:
        await get_container().user_access.store.touch_api_key(identity.key_id)
    except Exception:  # noqa: BLE001 — 记账属旁路，失败不影响鉴权结果
        pass
    return api_key


async def verify_admin_key(authorization: str | None = Header(default=None)) -> str:
    """校验运维面 ``Authorization: Bearer <key>``；ADMIN_API_KEY 空则回落 API_KEY。"""
    api_key = _extract_bearer(authorization)
    settings = get_settings()
    expected = settings.admin_api_key or settings.api_key
    if not hmac.compare_digest(api_key, expected):
        raise _unauthorized("Invalid admin key provided")
    return api_key
