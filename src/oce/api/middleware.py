"""HTTP 调用监控中间件。

每次请求上报 endpoint（路由模板）/ method / status_code / latency_ms 到 MetricsSink，
落 api_call_metrics。exempt_paths（默认 /health）不记账；exempt_prefixes 与 Mount
匹配（门户静态资源）同样跳过，防止前端流量污染指标。用户归属读
request.scope["oce_user_id"]（verify_api_key 写入；scope dict 引用共享，BaseHTTPMiddleware
子任务里的写入对 dispatch 可见——此处 ContextVar 跨任务不可靠，见 user_context）。

监控是旁路：采集失败只记日志、绝不影响请求本身；``sink_provider`` 返回 None 时
（如应用尚未完成装配）直接跳过，避免误触发容器构建。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from time import perf_counter

from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

from oce.shared.metrics import ApiCallRecord, MetricsSink

_ENDPOINT_MAX = 128

SinkProvider = Callable[[], MetricsSink | None]


class TaggedStaticFiles(StaticFiles):
    """门户静态挂载用：在 scope 打标记，让监控中间件跳过。

    Starlette 的 Mount.matches 不设置 scope["route"]，无法靠路由类型识别静态
    请求；显式打标是唯一可靠的信号（否则每个带哈希的资产路径都会按原样落
    api_call_metrics，维度爆炸）。
    """

    async def __call__(self, scope, receive, send) -> None:
        scope["oce_static"] = True
        await super().__call__(scope, receive, send)


class ApiCallMetricsMiddleware(BaseHTTPMiddleware):
    """采集每次 HTTP 请求的耗时与状态码，旁路上报，不改变请求语义。"""

    def __init__(
        self,
        app,
        *,
        sink_provider: SinkProvider,
        exempt_paths: frozenset[str] = frozenset({"/health"}),
        exempt_prefixes: tuple[str, ...] = ("/auth",),
    ) -> None:
        super().__init__(app)
        self._sink_provider = sink_provider
        self._exempt = exempt_paths
        self._exempt_prefixes = exempt_prefixes

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.url.path in self._exempt or request.url.path.startswith(
            self._exempt_prefixes
        ):
            return await call_next(request)

        started = perf_counter()
        status_code = 500  # 未捕获异常时的兜底状态
        error_type: str | None = None
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception as exc:  # 记录后照常上抛，异常处理仍交给上层
            error_type = type(exc).__name__
            raise
        finally:
            self._record(request, status_code, error_type, perf_counter() - started)

    def _record(
        self,
        request: Request,
        status_code: int,
        error_type: str | None,
        elapsed_s: float,
    ) -> None:
        try:
            sink = self._sink_provider()
            if sink is None:
                return
            if request.scope.get("oce_static"):
                # 门户静态资源：不属于 API 调用面；未匹配路由（404）仍按原样
                # 记账，探测行为保留
                return
            route = request.scope.get("route")
            endpoint = getattr(route, "path", None) or request.url.path
            sink.record_api_call(
                ApiCallRecord(
                    endpoint=endpoint[:_ENDPOINT_MAX],
                    method=request.method,
                    status_code=status_code,
                    latency_ms=int(elapsed_s * 1000),
                    error_type=error_type,
                    user_id=request.scope.get("oce_user_id"),
                )
            )
        except Exception as exc:  # 旁路容错：监控绝不影响请求
            logger.warning("record api call failed: {}", exc)
