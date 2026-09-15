"""HMAC 签名的无状态令牌（会话 cookie / OAuth state 共用）。

格式 ``payload.exp.sig``，sig = base64url(HMAC-SHA256(secret, "payload.exp"))。
不引入 JWT 库：负载只有用户 id / 随机 state 这类非敏感短字符串，stdlib 足够。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

SESSION_COOKIE = "oce_session"
STATE_COOKIE = "oce_oauth_state"


def sign_payload(payload: str, ttl_seconds: int, secret: str) -> str:
    exp = int(time.time()) + ttl_seconds
    body = f"{payload}.{exp}"
    sig = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    return f"{body}.{sig}"


def verify_payload(token: str, secret: str) -> str | None:
    """校验签名与有效期；任何不合法都返回 None（调用方按未登录处理）。"""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload, exp_text, sig = parts
    body = f"{payload}.{exp_text}"
    expected = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        exp = int(exp_text)
    except ValueError:
        return None
    if exp < time.time():
        return None
    return payload


def new_state() -> str:
    """OAuth state 原始值；发送前用 sign_payload 包上有效期。"""
    return secrets.token_urlsafe(16)
