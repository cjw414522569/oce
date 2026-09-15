"""main.py 装配开关：AUTH_ENABLED 挂 /auth 路由与门户静态，默认全关。

get_settings 是 lru_cache 且 main 在导入期读取，故用 importlib.reload 切换环境
后重建 app 模块；每例结束恢复默认导入状态，避免污染其他用例。
"""

from __future__ import annotations

import importlib

import httpx
import pytest


@pytest.fixture
def reload_main(monkeypatch, tmp_path):
    def _reload(*, enabled: bool, portal_dir: str = ""):
        import oce.shared.config.settings as settings_module

        monkeypatch.setenv("AUTH_ENABLED", "true" if enabled else "false")
        if enabled:
            monkeypatch.setenv("AUTH_CLIENT_ID", "cid")
            monkeypatch.setenv("AUTH_CLIENT_SECRET", "csecret")
            monkeypatch.setenv("AUTH_SESSION_SECRET", "ssecret")
            monkeypatch.setenv("AUTH_REDIRECT_BASE", "https://oce.example.com")
        if portal_dir:
            monkeypatch.setenv("AUTH_PORTAL_DIST_DIR", portal_dir)
        settings_module.get_settings.cache_clear()
        import oce.main as main_module

        return importlib.reload(main_module)

    yield _reload
    import os

    for key in (
        "AUTH_ENABLED",
        "AUTH_CLIENT_ID",
        "AUTH_CLIENT_SECRET",
        "AUTH_SESSION_SECRET",
        "AUTH_REDIRECT_BASE",
        "AUTH_PORTAL_DIST_DIR",
    ):
        os.environ.pop(key, None)
    import oce.shared.config.settings as settings_module

    settings_module.get_settings.cache_clear()
    # 容器可能在用例中被懒构建（auth 已启用状态），清缓存避免污染后续用例
    from oce.application.container import get_container

    get_container.cache_clear()
    import oce.main as main_module

    importlib.reload(main_module)


async def test_default_keeps_auth_routes_absent(reload_main):
    main = reload_main(enabled=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://t"
    ) as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/auth/me")).status_code == 404
        assert (await client.get("/")).status_code == 404  # 无门户挂载


async def test_enabled_mounts_auth_router_and_portal(reload_main, tmp_path):
    (tmp_path / "index.html").write_text("<html>portal</html>")
    main = reload_main(enabled=True, portal_dir=str(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://t"
    ) as client:
        # /auth 路由存在且 provider 已装配：302 跳转 connect.linux.do
        login = await client.get("/auth/login", follow_redirects=False)
        assert login.status_code == 302
        assert login.headers["location"].startswith("https://connect.linux.do/oauth2/authorize")
        # 门户静态挂载：/ 命中 index.html
        root = await client.get("/")
        assert root.status_code == 200 and "portal" in root.text
        # /admin 精确路由返回同一门户页（管理控制台入口）
        admin_page = await client.get("/admin")
        assert admin_page.status_code == 200 and "portal" in admin_page.text
        # /admin/* API 不受影响：无 key 401
        assert (await client.get("/admin/users")).status_code == 401
        # API 路径优先于 "/" 兜底挂载
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/version")).status_code == 200


async def test_enabled_without_dist_skips_mount_gracefully(reload_main, tmp_path):
    missing = tmp_path / "does-not-exist"
    main = reload_main(enabled=True, portal_dir=str(missing))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://t"
    ) as client:
        assert (await client.get("/health")).status_code == 200  # 启动不崩溃
        assert (await client.get("/")).status_code == 404
