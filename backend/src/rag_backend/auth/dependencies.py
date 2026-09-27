"""FastAPI 依赖：登录限流器、当前授权上下文、Origin 与 CSRF 校验。

依赖每次请求都重新读取数据库，不做跨请求缓存；因此禁用、过期、撤销与角色变化立即生效。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import cast
from urllib.parse import urlsplit

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from rag_backend.api.errors import (
    CODE_AUTH_DEPENDENCY_UNAVAILABLE,
    CODE_AUTH_REQUIRED,
    CODE_CSRF_INVALID,
    CODE_DOCUMENT_NOT_FOUND,
    CODE_KNOWLEDGE_BASE_NOT_FOUND,
    CODE_ORIGIN_NOT_ALLOWED,
    ApiError,
)
from rag_backend.auth.context import AuthContext
from rag_backend.auth.ratelimit import LoginRateLimiter
from rag_backend.auth.service import build_auth_context
from rag_backend.auth.tokens import CSRF_HEADER_NAME, csrf_tokens_match
from rag_backend.config import Settings, normalise_origin
from rag_backend.database import get_database_session
from rag_backend.knowledge.roles import KbRole, kb_role_rank
from rag_backend.knowledge.service import (
    DocumentAccess,
    KbAccess,
    resolve_document_access,
    resolve_knowledge_base_access,
)


def get_login_rate_limiter(request: Request) -> LoginRateLimiter:
    """返回进程级 Redis 限流器；未配置 Redis 时明确不可用，而不是静默放行。"""

    limiter = cast(
        "LoginRateLimiter | None", getattr(request.app.state, "login_rate_limiter", None)
    )
    if limiter is None:
        raise ApiError(
            503,
            CODE_AUTH_DEPENDENCY_UNAVAILABLE,
            "登录服务暂时不可用",
        )
    return limiter


def request_origin(request: Request) -> str | None:
    """取 Origin；缺失时回退到 Referer 的 scheme+host。"""

    origin = request.headers.get("origin")
    if origin:
        return origin
    referer = request.headers.get("referer")
    if referer:
        # 恶意/畸形的 Referer（如非法 IPv6 主机）会让 urlsplit 抛 ValueError；
        # 这是不可信的请求输入，统一当作“无法确定来源”，交由调用方返回 403，
        # 绝不让它升级成未捕获异常（500）。
        try:
            parsed = urlsplit(referer)
        except ValueError:
            return None
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    return None


def enforce_allowed_origin(request: Request, settings: Settings) -> None:
    """校验状态变更请求来源；缺失或不匹配都返回 403。"""

    origin = request_origin(request)
    if origin is None:
        raise ApiError(403, CODE_ORIGIN_NOT_ALLOWED, "缺少请求来源")
    try:
        normalised = normalise_origin(origin)
    except ValueError as error:
        raise ApiError(403, CODE_ORIGIN_NOT_ALLOWED, "请求来源不被允许") from error
    if normalised not in settings.allowed_origin_set:
        raise ApiError(403, CODE_ORIGIN_NOT_ALLOWED, "请求来源不被允许")


async def get_auth_context(
    request: Request,
    session: AsyncSession = Depends(get_database_session),
) -> AuthContext:
    """从 Cookie 会话令牌构造授权上下文；无有效会话返回 401。"""

    settings: Settings = request.app.state.settings
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise ApiError(401, CODE_AUTH_REQUIRED, "需要登录")
    context = await build_auth_context(session, settings, token)
    if context is None:
        raise ApiError(401, CODE_AUTH_REQUIRED, "会话无效或已过期")
    return context


async def require_csrf(
    request: Request,
    context: AuthContext = Depends(get_auth_context),
) -> AuthContext:
    """状态变更端点使用：在有效会话之上再校验 CSRF 请求头。"""

    provided = request.headers.get(CSRF_HEADER_NAME)
    if not csrf_tokens_match(context.csrf_token, provided):
        raise ApiError(403, CODE_CSRF_INVALID, "CSRF 校验失败")
    return context


def require_kb_role(
    minimum_role: KbRole,
) -> Callable[..., Awaitable[KbAccess]]:
    """构造 KB 最小角色依赖；角色由本次请求的数据库查询判定，不跨请求缓存。

    不存在、跨组织、成员已撤销以及角色不足都统一返回不暴露存在性的 404。
    """

    async def dependency(
        kb_id: uuid.UUID,
        context: AuthContext = Depends(get_auth_context),
        session: AsyncSession = Depends(get_database_session),
    ) -> KbAccess:
        access = await resolve_knowledge_base_access(
            session,
            kb_id=kb_id,
            user_id=context.user_id,
            organization_id=context.organization_id,
        )
        if access is None or kb_role_rank(access.role) < kb_role_rank(minimum_role):
            raise ApiError(
                404,
                CODE_KNOWLEDGE_BASE_NOT_FOUND,
                "知识库不存在或无权访问",
            )
        return access

    return dependency


def require_document_role(
    minimum_role: KbRole,
) -> Callable[..., Awaitable[DocumentAccess]]:
    """构造「文档 → KB」最小角色依赖；角色由本次请求的数据库查询判定。

    ``document_id`` 是路径参数；解析到文档所属 KB 后按会话组织与未撤销成员判定角色。
    文档不存在、属于其他组织、成员已撤销或角色不足都统一返回不暴露存在性的 404。
    """

    async def dependency(
        document_id: uuid.UUID,
        context: AuthContext = Depends(get_auth_context),
        session: AsyncSession = Depends(get_database_session),
    ) -> DocumentAccess:
        access = await resolve_document_access(
            session,
            document_id=document_id,
            user_id=context.user_id,
            organization_id=context.organization_id,
        )
        if access is None or kb_role_rank(access.role) < kb_role_rank(minimum_role):
            raise ApiError(
                404,
                CODE_DOCUMENT_NOT_FOUND,
                "文档不存在或无权访问",
            )
        return access

    return dependency
