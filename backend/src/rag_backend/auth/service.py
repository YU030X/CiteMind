"""会话用例：认证、会话签发、会话校验与撤销。

所有函数只接受调用方的 ``AsyncSession``；不缓存结果，也不跨请求复用授权上下文。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rag_backend.auth.context import AuthContext
from rag_backend.auth.passwords import verify_password, verify_password_or_dummy
from rag_backend.auth.tokens import (
    csrf_token_hash_matches,
    derive_csrf_from_secret,
    generate_session_token,
    hash_token,
)
from rag_backend.config import Settings
from rag_backend.models.identity import AuthSession, UserAccount


def _now() -> datetime:
    return datetime.now(UTC)


async def find_user_by_username(
    session: AsyncSession, *, organization_id: uuid.UUID, username: str
) -> UserAccount | None:
    """按服务端组织与用户名查账号；组织来自配置，不来自请求。"""

    statement = select(UserAccount).where(
        UserAccount.organization_id == organization_id,
        UserAccount.username == username,
    )
    result = await session.execute(statement)
    return result.scalar_one_or_none()


async def authenticate(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    username: str,
    password: str,
) -> UserAccount | None:
    """校验用户名与密码；未知、禁用或密码错误都返回 None 并做等价 Argon2 计算。"""

    user = await find_user_by_username(
        session, organization_id=organization_id, username=username
    )
    if user is None or not user.enabled:
        verify_password_or_dummy(None, password)
        return None
    if not verify_password(user.password_hash, password):
        return None
    return user


async def create_session(
    session: AsyncSession, settings: Settings, user: UserAccount
) -> tuple[AuthSession, str, str]:
    """签发会话；返回 ORM 行、原令牌与派生 CSRF 令牌（后两者都不落库）。"""

    token = generate_session_token()
    csrf_token = derive_csrf_from_secret(settings.csrf_secret, token)
    auth_session = AuthSession(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash=hash_token(token),
        csrf_token_hash=hash_token(csrf_token),
        expires_at=_now() + timedelta(seconds=settings.session_ttl_seconds),
    )
    session.add(auth_session)
    await session.commit()
    return auth_session, token, csrf_token


async def load_session_user(
    session: AsyncSession, *, token: str
) -> tuple[AuthSession, UserAccount] | None:
    """按原令牌查会话与账号；撤销、过期或禁用都返回 None。"""

    statement = (
        select(AuthSession, UserAccount)
        .join(UserAccount, AuthSession.user_id == UserAccount.id)
        .where(AuthSession.token_hash == hash_token(token))
    )
    result = await session.execute(statement)
    row = result.first()
    if row is None:
        return None
    auth_session, user = row
    if auth_session.revoked_at is not None:
        return None
    if auth_session.expires_at <= _now():
        return None
    if not user.enabled:
        return None
    return auth_session, user


async def build_auth_context(
    session: AsyncSession, settings: Settings, token: str
) -> AuthContext | None:
    """构造请求级授权上下文；CSRF hash 与会话令牌不一致时视为会话无效。"""

    loaded = await load_session_user(session, token=token)
    if loaded is None:
        return None
    auth_session, user = loaded
    if user.organization_id != settings.organization_id:
        # 单组织部署：服务端配置改成另一个组织后，原组织的旧会话必须失效，
        # 否则同一数据库上的会话能越界访问原组织 KB。fail closed。
        return None
    csrf_secret = settings.csrf_secret.get_secret_value()
    if not csrf_token_hash_matches(csrf_secret, token, auth_session.csrf_token_hash):
        return None
    return AuthContext(
        user_id=user.id,
        organization_id=user.organization_id,
        username=user.username,
        is_admin=user.is_admin,
        session_id=auth_session.id,
        csrf_token=derive_csrf_from_secret(settings.csrf_secret, token),
    )


async def revoke_session(session: AsyncSession, *, session_id: uuid.UUID) -> None:
    """软撤销会话；已撤销则幂等返回。"""

    auth_session = await session.get(AuthSession, session_id)
    if auth_session is None or auth_session.revoked_at is not None:
        return
    auth_session.revoked_at = _now()
    await session.commit()
