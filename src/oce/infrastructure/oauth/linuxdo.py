"""LinuxDo Connect OAuth2 客户端（OAuthProvider 端口实现）。

授权码流程：authorize 跳转 → callback 换 token（表单 POST）→ userinfo（Bearer）。
登录流量稀疏，每次调用自建 httpx client，不引入生命周期管理负担。
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
from urllib.parse import urlencode

from oce.application.user_access import LinuxDoProfile
from oce.shared.config.settings import AuthSettings
from oce.shared.errors import OAuthExchangeError


def _default_client_factory() -> httpx.AsyncClient:
    # 登录流量稀疏，每次调用自建 client；timeout 覆盖换 token + userinfo 两跳
    return httpx.AsyncClient(timeout=15.0)


class LinuxDoOAuthClient:
    def __init__(
        self,
        settings: AuthSettings,
        client_factory: Callable[[], httpx.AsyncClient] = _default_client_factory,
    ) -> None:
        self._settings = settings
        self._client_factory = client_factory

    @property
    def redirect_uri(self) -> str:
        return self._settings.redirect_uri

    def authorize_url(self, state: str) -> str:
        # redirect_uri 必须与 connect.linux.do 注册值逐字节一致（单一真源在 settings）
        params = urlencode(
            {
                "response_type": "code",
                "client_id": self._settings.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": "user",
                "state": state,
            }
        )
        return f"{self._settings.authorize_url}?{params}"

    async def exchange_code(self, code: str) -> LinuxDoProfile:
        secret = self._settings.client_secret.get_secret_value()
        try:
            async with self._client_factory() as client:
                token_resp = await client.post(
                    self._settings.token_url,
                    data={
                        "grant_type": "authorization_code",
                        "client_id": self._settings.client_id,
                        "client_secret": secret,
                        "code": code,
                        "redirect_uri": self.redirect_uri,
                    },
                )
                token_resp.raise_for_status()
                access_token = token_resp.json().get("access_token")
                if not access_token:
                    raise OAuthExchangeError("token 响应缺少 access_token")
                user_resp = await client.get(
                    self._settings.userinfo_url,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                user_resp.raise_for_status()
                payload = user_resp.json()
        except OAuthExchangeError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            # ValueError/TypeError 覆盖：非 JSON 响应体（代理错误页）、字段类型漂移
            raise OAuthExchangeError(f"连接 LinuxDo 失败: {exc}") from exc

        user_id = payload.get("id")
        if user_id is None:
            raise OAuthExchangeError("userinfo 响应缺少 id")
        return LinuxDoProfile(
            linuxdo_id=int(user_id),
            username=str(payload.get("username") or f"ld-{user_id}"),
            name=payload.get("name"),
            avatar_template=payload.get("avatar_template"),
            active=bool(payload.get("active", True)),
            silenced=bool(payload.get("silenced", False)),
            trust_level=int(payload.get("trust_level", 0)),
        )
