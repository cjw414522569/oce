"""/auth 路由：LinuxDo OAuth2 登录、会话与用户 key 管理。

不挂 verify_api_key（登录前无凭据）；会话用 HMAC 签名 cookie
（shared/signed_tokens），HttpOnly + SameSite=Lax。cookie_secure 由配置决定
（nginx TLS 后置 true；直连 http 调试时保持 false 否则浏览器丢弃 cookie）。

state 防伪：/login 生成随机 state 并签名，签名串同时写入 cookie 与发往提供方；
/callback 要求两者逐字节相等（常量时间比较）且 cookie 未过期，再进入 code 交换。
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from oce.api.schemas import (
    AuthApiKeyResponse,
    AuthMeResponse,
    AuthUsageWindowResponse,
    AuthUserResponse,
    RotateKeyResponse,
)
from oce.application.container import get_container
from oce.application.user_access import PortalView, UserAccessService
from oce.shared.config import get_settings
from oce.shared.errors import LoginDeniedError, OAuthExchangeError
from oce.shared.signed_tokens import (
    SESSION_COOKIE,
    STATE_COOKIE,
    new_state,
    sign_payload,
    verify_payload,
)

auth_router = APIRouter(prefix="/auth", tags=["Auth"])


def get_user_access() -> UserAccessService:
    return get_container().user_access


async def get_session_user_id(request: Request) -> int:
    """会话依赖：签名 cookie 校验失败一律 401（门户 JS 据此切回登录页）。"""
    token = request.cookies.get(SESSION_COOKIE)
    payload = (
        verify_payload(token, get_settings().auth.session_secret.get_secret_value())
        if token
        else None
    )
    if payload is None or not payload.startswith("u:"):
        raise HTTPException(status_code=401, detail="not authenticated")
    try:
        return int(payload[2:])
    except ValueError:
        raise HTTPException(status_code=401, detail="not authenticated") from None


def _set_cookie(response: RedirectResponse, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path="/",
        httponly=True,
        samesite="lax",
        secure=get_settings().auth.cookie_secure,
    )


@auth_router.get("/login")
async def login(service: UserAccessService = Depends(get_user_access)) -> RedirectResponse:
    if service.provider is None:
        raise HTTPException(status_code=503, detail="多用户接入未启用")
    settings = get_settings().auth
    secret = settings.session_secret.get_secret_value()
    signed_state = sign_payload(new_state(), settings.state_ttl_seconds, secret)
    response = RedirectResponse(
        service.provider.authorize_url(signed_state), status_code=302
    )
    _set_cookie(response, STATE_COOKIE, signed_state, settings.state_ttl_seconds)
    return response


@auth_router.get("/callback")
async def callback(
    request: Request,
    code: str = "",
    state: str = "",
    service: UserAccessService = Depends(get_user_access),
) -> RedirectResponse:
    settings = get_settings().auth
    secret = settings.session_secret.get_secret_value()
    cookie_state = request.cookies.get(STATE_COOKIE, "")
    if not code or not cookie_state or not hmac.compare_digest(state, cookie_state):
        raise HTTPException(status_code=401, detail="invalid oauth state")
    if verify_payload(cookie_state, secret) is None:
        raise HTTPException(status_code=401, detail="oauth state expired")

    try:
        user = await service.authenticate(code)
    except LoginDeniedError:
        return RedirectResponse("/?login_error=denied", status_code=302)
    except OAuthExchangeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    token = sign_payload(f"u:{user.id}", settings.session_ttl_seconds, secret)
    response = RedirectResponse("/", status_code=302)
    _set_cookie(response, SESSION_COOKIE, token, settings.session_ttl_seconds)
    response.delete_cookie(STATE_COOKIE, path="/")
    return response


@auth_router.post("/logout")
async def logout(response: Response) -> dict[str, str]:
    # 删 cookie 需写响应头，因此注入 Response 而非返回裸 dict
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(STATE_COOKIE, path="/")
    return {"status": "ok"}


@auth_router.get("/me", response_model=AuthMeResponse)
async def me(
    user_id: int = Depends(get_session_user_id),
    service: UserAccessService = Depends(get_user_access),
) -> AuthMeResponse:
    try:
        view: PortalView = await service.get_portal(user_id)
    except LoginDeniedError:
        # 会话仍有效但用户已被删/禁：等价未登录，门户据此切回登录页
        raise HTTPException(status_code=401, detail="user unavailable") from None
    return _me_response(view)


@auth_router.post("/key/rotate", response_model=RotateKeyResponse, status_code=201)
async def rotate_key(
    user_id: int = Depends(get_session_user_id),
    service: UserAccessService = Depends(get_user_access),
) -> RotateKeyResponse:
    """轮换并一次性返回新 key 明文；旧 key 立即失效。"""
    try:
        issued = await service.rotate_key(user_id)
    except LoginDeniedError:
        # 用户已被删/禁：会话等价失效
        raise HTTPException(status_code=401, detail="user unavailable") from None
    return RotateKeyResponse(api_key=issued.api_key, key_last4=issued.key_last4)


def _me_response(view: PortalView) -> AuthMeResponse:
    api_key = view.api_key
    return AuthMeResponse(
        user=AuthUserResponse(
            id=view.user.id,
            username=view.user.username,
            name=view.user.name,
            avatar_template=view.user.avatar_template,
            trust_level=view.user.trust_level,
        ),
        api_key=AuthApiKeyResponse(
            key_last4=api_key.key_last4,
            status=api_key.status,
            created_at=api_key.created_at,
            last_used_at=api_key.last_used_at,
        )
        if api_key is not None
        else None,
        usage_24h=AuthUsageWindowResponse(
            window_hours=view.usage_24h.window_hours,
            api_calls=view.usage_24h.api_calls,
            total_tokens=view.usage_24h.total_tokens,
        ),
        usage_7d=AuthUsageWindowResponse(
            window_hours=view.usage_7d.window_hours,
            api_calls=view.usage_7d.api_calls,
            total_tokens=view.usage_7d.total_tokens,
        ),
    )
