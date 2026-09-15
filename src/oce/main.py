"""OpenContextEngine ASGI 入口。"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from oce import __version__
from oce.api.admin_router import admin_router
from oce.api.middleware import ApiCallMetricsMiddleware
from oce.api.router import router
from oce.application.container import get_container
from oce.shared.config.settings import get_settings
from oce.shared.database.session import engine
from oce.shared.logging import DATA_DIR_ENV, LOG_LEVEL_ENV, configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 配置日志：`oce serve` 由 CLI 通过环境变量传递上下文（级别 + data dir）；
    # 直接 `uvicorn oce.main:app` 时无上下文，使用配置默认值。
    settings = get_settings()
    data_dir = os.environ.get(DATA_DIR_ENV)
    configure_logging(
        settings.log,
        level=os.environ.get(LOG_LEVEL_ENV),
        data_dir=Path(data_dir) if data_dir else None,
    )

    # 启动 worker（如果启用）
    container = get_container()
    if container.worker is not None:
        await container.worker.start()
    await container.metrics.start()
    if container.resource_sampler is not None:
        await container.resource_sampler.start()
    if container.monitoring_cleaner is not None:
        await container.monitoring_cleaner.start()
    yield
    # 关闭时停止 worker + 清理资源
    if get_container.cache_info().currsize:
        await get_container().close()
    await engine.dispose()


app = FastAPI(
    title="OpenContextEngine",
    version=__version__,
    description="Self-hosted codebase context engine with ACE-compatible APIs.",
    lifespan=lifespan,
)


def _parse_cors_origins(value: str) -> list[str]:
    """Normalize the comma-separated allowlist used by the static admin client."""
    return [origin.strip().rstrip("/") for origin in value.split(",") if origin.strip()]


cors_origins = _parse_cors_origins(get_settings().cors_origins)
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=600,
    )

app.include_router(router)
app.include_router(admin_router)

# 多用户接入：仅 AUTH_ENABLED 时挂载 /auth 路由
auth_settings = get_settings().auth
if auth_settings.enabled:
    from oce.api.auth_router import auth_router

    app.include_router(auth_router)


def _metrics_sink_provider():
    """仅在容器已装配后返回 sink；未装配（如未跑 lifespan）时返回 None 跳过，避免误构建容器。"""
    if get_container.cache_info().currsize == 0:
        return None
    return get_container().metrics


if get_settings().monitoring.enabled:
    app.add_middleware(ApiCallMetricsMiddleware, sink_provider=_metrics_sink_provider)


@app.get("/health", tags=["Meta"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/version", tags=["Meta"])
async def version() -> dict[str, str]:
    """服务端版本号，公开无需鉴权，供客户端做兼容性检查与升级提醒。"""
    return {"name": "oce", "version": __version__}


# 门户静态挂载必须放在全部路由之后注册（Starlette 按注册顺序匹配，API 路径
# 优先于 "/" 兜底）；dist 未构建时跳过挂载而不是启动失败。TaggedStaticFiles
# 给 scope 打标记，监控中间件据此跳过静态资产。
# GET /admin 精确返回门户页（管理控制台入口）；/admin/* API 路由先注册，不受影响。
if auth_settings.enabled and auth_settings.portal_dist_dir:
    portal_dir = Path(auth_settings.portal_dist_dir)
    if portal_dir.is_dir():
        from fastapi.responses import FileResponse

        from oce.api.middleware import TaggedStaticFiles

        @app.get("/admin", include_in_schema=False)
        async def admin_portal():
            return FileResponse(portal_dir / "index.html")

        app.mount(
            "/", TaggedStaticFiles(directory=str(portal_dir), html=True), name="portal"
        )
