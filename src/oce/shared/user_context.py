"""请求任务内的用户上下文。

verify_api_key 命中用户 key 后写入；Container._record_token_usage 在同一请求任务里
读取，把 token 用量归属到用户。worker 后台嵌入等无请求上下文的路径读恒为 None。

ContextVar 按任务隔离：每个请求/worker 任务拿到上下文副本，任务结束即丢弃，
不在热路径显式 reset 也不会跨请求泄漏。

注意与 request.scope["oce_user_id"] 的分工：监控中间件是 BaseHTTPMiddleware，
call_next 在子任务中执行，依赖里设置的 ContextVar 回到 dispatch 后不可见；
scope dict 是引用共享，跨任务可见。两处都写，各自服务一条传播路径。
"""

from __future__ import annotations

from contextvars import ContextVar, Token

_current_user_id: ContextVar[int | None] = ContextVar("oce_user_id", default=None)


def set_current_user_id(user_id: int | None) -> Token[int | None]:
    return _current_user_id.set(user_id)


def get_current_user_id() -> int | None:
    return _current_user_id.get()


def reset_current_user_id(token: Token[int | None]) -> None:
    _current_user_id.reset(token)
