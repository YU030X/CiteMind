"""会话令牌与 CSRF 令牌的生成、哈希与派生。

安全属性：

- 会话令牌是 256-bit 随机值，只放进 HttpOnly Cookie；数据库只保存 SHA-256 hash，
  因此数据库泄露不能直接冒充登录，也不存在“Cookie 之外长期保存原令牌”。
- CSRF 令牌由服务端密钥与会话令牌 HMAC 派生，数据库只保存其 hash。派生是确定性的，
  进程重启后仍可由 Cookie 中的会话令牌恢复同一 CSRF 令牌，供 GET /me 返回。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Any

# 256-bit 会话令牌；token_urlsafe 的字节数固定为 32。
SESSION_TOKEN_BYTES = 32
CSRF_TOKEN_BYTES = 32

SAME_SITE = "lax"
COOKIE_PATH = "/"
# 状态变更请求携带 CSRF 令牌的请求头名。
CSRF_HEADER_NAME = "X-CSRF-Token"


def generate_session_token() -> str:
    """生成高熵会话令牌；返回值不落库、不进日志。"""

    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def hash_token(token: str) -> str:
    """令牌的单向 SHA-256 十六进制摘要，用作数据库查表键。"""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def derive_csrf_token(csrf_secret: str, session_token: str) -> str:
    """由服务端密钥与会话令牌派生 CSRF 令牌；输入相同则输出稳定。"""

    digest = hmac.new(
        csrf_secret.encode("utf-8"),
        session_token.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def derive_csrf_from_secret(csrf_secret: Any, session_token: str) -> str:
    """兼容 ``SecretStr`` 与普通字符串的派生入口。"""

    secret_value = getattr(csrf_secret, "get_secret_value", None)
    resolved = secret_value() if callable(secret_value) else str(csrf_secret)
    return derive_csrf_token(resolved, session_token)


def csrf_token_hash_matches(csrf_secret: str, session_token: str, stored_hash: str) -> bool:
    """确认数据库中的 CSRF hash 确实对应本次会话令牌派生的 CSRF 令牌。"""

    expected = derive_csrf_token(csrf_secret, session_token)
    return hmac.compare_digest(hash_token(expected), stored_hash)


def csrf_tokens_match(expected: str, provided: str | None) -> bool:
    """恒时比较请求头携带的 CSRF 令牌。"""

    if provided is None:
        return False
    return hmac.compare_digest(expected, provided)


def session_cookie_settings(*, secure: bool, max_age: int) -> dict[str, Any]:
    """返回 ``Response.set_cookie`` 的会话 Cookie 属性。

    只允许 HttpOnly + SameSite + 显式 Path；Secure 由配置决定，不做静默降级。
    """

    return {
        "httponly": True,
        "secure": secure,
        "samesite": SAME_SITE,
        "path": COOKIE_PATH,
        "max_age": max_age,
    }
