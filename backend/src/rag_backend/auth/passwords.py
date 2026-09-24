"""Argon2 密码哈希与校验。

只在这里处理明文密码：hash 结果与校验都在进程内完成，明文绝不落库、不进日志。
未知用户也执行一次真实 Argon2 校验，避免用响应时间区分用户名是否存在。
"""

from __future__ import annotations

import secrets
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """生成 Argon2 哈希；空密码直接拒绝，避免写入可被滥用的空凭据。"""

    if not password:
        raise ValueError("密码不能为空")
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """校验密码；哈希非法或密码不匹配都返回 False，不向调用方泄露原因。"""

    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


@lru_cache(maxsize=1)
def dummy_password_hash() -> str:
    """为未知/禁用用户准备的占位哈希，保证仍执行一次真实 Argon2 计算。"""

    return hash_password(secrets.token_urlsafe(16))


def verify_password_or_dummy(password_hash: str | None, password: str) -> bool:
    """用户缺失或禁用时也做一次等价的 Argon2 校验，再统一返回 False。"""

    if password_hash is None:
        verify_password(dummy_password_hash(), password)
        return False
    return verify_password(password_hash, password)


def warm_password_hashing() -> None:
    """启动时预计算占位哈希，消除进程重启后首个未知用户登录的额外 Argon2 时延。"""

    dummy_password_hash()
