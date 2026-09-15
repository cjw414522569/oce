"""AuthSettings：默认关闭、启用校验、redirect_uri 单一真源。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from oce.shared.config.settings import AuthSettings

# _env_file=None：仓库根 .env 可能带有部署值（AUTH_COOKIE_SECURE 等），
# 设置组用例必须与 env 文件隔离，只测代码默认值与显式入参
_HERMETIC = {"_env_file": None}


def _enabled_overrides() -> dict:
    return {
        **_HERMETIC,
        "enabled": True,
        "client_id": "cid",
        "client_secret": "csecret",
        "session_secret": "ssecret",
        "redirect_base": "https://oce.example.com",
    }


_AUTH_KEYS = (
    "AUTH_ENABLED",
    "AUTH_CLIENT_ID",
    "AUTH_CLIENT_SECRET",
    "AUTH_REDIRECT_BASE",
    "AUTH_REDIRECT_URI",
    "AUTH_SESSION_SECRET",
    "AUTH_MIN_TRUST_LEVEL",
    "AUTH_PORTAL_DIST_DIR",
    "AUTH_COOKIE_SECURE",
)


def _isolate_env(monkeypatch):
    """设置组测试必须完全隔离：清掉进程内可能残留的 AUTH_*（其他用例或
    cli 路径的 load_dotenv 会写 os.environ），并用 _env_file=None 屏蔽仓库 .env。"""
    for key in _AUTH_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_disabled_by_default_and_no_validation(monkeypatch):
    _isolate_env(monkeypatch)
    settings = AuthSettings(_env_file=None)
    assert settings.enabled is False
    assert settings.min_trust_level is None
    assert settings.cookie_secure is False


def test_enabled_requires_all_credentials(monkeypatch):
    _isolate_env(monkeypatch)
    for missing in ("client_id", "client_secret", "session_secret", "redirect_base"):
        overrides = _enabled_overrides()
        overrides[missing] = ""
        with pytest.raises(ValidationError, match="AUTH_ENABLED"):
            AuthSettings(**overrides)


def test_redirect_uri_single_source_of_truth(monkeypatch):
    _isolate_env(monkeypatch)
    overrides = _enabled_overrides()
    assert AuthSettings(**overrides).redirect_uri == "https://oce.example.com/auth/callback"
    # 尾斜杠归一
    overrides["redirect_base"] = "https://oce.example.com/"
    assert AuthSettings(**overrides).redirect_uri == "https://oce.example.com/auth/callback"
    # 显式覆盖优先
    overrides["redirect_uri_override"] = "https://other.example.com/cb"
    assert AuthSettings(**overrides).redirect_uri == "https://other.example.com/cb"


def test_env_prefix_mapping(monkeypatch):
    _isolate_env(monkeypatch)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("AUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("AUTH_SESSION_SECRET", "ssecret")
    monkeypatch.setenv("AUTH_REDIRECT_BASE", "https://oce.example.com")
    monkeypatch.setenv("AUTH_MIN_TRUST_LEVEL", "2")
    settings = AuthSettings(_env_file=None)
    assert settings.enabled is True
    assert settings.min_trust_level == 2
