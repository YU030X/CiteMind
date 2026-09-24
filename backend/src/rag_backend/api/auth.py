"""身份与会话路由：登录、注销与当前授权概览。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from rag_backend.api.errors import (
    CODE_AUTH_INVALID_CREDENTIALS,
    CODE_CSRF_INVALID,
    ApiError,
)
from rag_backend.auth.client_ip import resolve_client_ip
from rag_backend.auth.context import AuthContext
from rag_backend.auth.dependencies import (
    enforce_allowed_origin,
    get_login_rate_limiter,
)
from rag_backend.auth.dependencies import (
    get_auth_context as current_auth_context,
)
from rag_backend.auth.ratelimit import LoginRateLimiter
from rag_backend.auth.service import (
    authenticate,
    build_auth_context,
    create_session,
    revoke_session,
)
from rag_backend.auth.tokens import CSRF_HEADER_NAME, csrf_tokens_match, session_cookie_settings
from rag_backend.config import Settings
from rag_backend.database import get_database_session
from rag_backend.knowledge.service import list_accessible_knowledge_bases
from rag_backend.models.identity import UserAccount
from rag_backend.schemas.auth import (
    KbRoleSummary,
    LoginRequest,
    MeOverviewResponse,
    MeResponse,
    UserSummary,
)

router = APIRouter(prefix="/api/v1", tags=["auth"])


def _user_summary(user: UserAccount) -> UserSummary:
    return UserSummary(
        id=user.id,
        username=user.username,
        is_admin=user.is_admin,
        organization_id=user.organization_id,
    )


@router.post("/auth/login", response_model=MeResponse)
async def login(
    request: Request,
    payload: LoginRequest,
    response: Response,
    session: AsyncSession = Depends(get_database_session),
    limiter: LoginRateLimiter = Depends(get_login_rate_limiter),
) -> MeResponse:
    """校验来源与限流后认证；成功时签发会话并返回授权概览与 CSRF 令牌。"""

    settings: Settings = request.app.state.settings
    # 登录没有现成会话，用 Origin 白名单阻断 login CSRF；先于限流与用户查询。
    enforce_allowed_origin(request, settings)
    # 限流按真实客户端 IP；仅当直连对端是显式可信网关时才接受网关覆盖的单值头。
    client_ip = resolve_client_ip(request, settings)
    await limiter.check(client_ip=client_ip, username=payload.username)

    user = await authenticate(
        session,
        organization_id=settings.organization_id,
        username=payload.username,
        password=payload.password,
    )
    if user is None:
        # 未知、禁用与密码错误共用同一响应，不泄露用户名是否存在。
        raise ApiError(401, CODE_AUTH_INVALID_CREDENTIALS, "用户名或密码不正确")

    _, token, csrf_token = await create_session(session, settings, user)
    response.set_cookie(
        settings.session_cookie_name,
        token,
        **session_cookie_settings(
            secure=settings.session_cookie_secure, max_age=settings.session_ttl_seconds
        ),
    )
    return MeResponse(user=_user_summary(user), csrf_token=csrf_token)


@router.post("/auth/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_database_session),
) -> None:
    """撤销当前会话并清除 Cookie；无有效会话时返回 204 且不下发 Set-Cookie。"""

    settings: Settings = request.app.state.settings
    token = request.cookies.get(settings.session_cookie_name)
    if token:
        context = await build_auth_context(session, settings, token)
        if context is not None:
            enforce_allowed_origin(request, settings)
            if not csrf_tokens_match(
                context.csrf_token, request.headers.get(CSRF_HEADER_NAME)
            ):
                raise ApiError(403, CODE_CSRF_INVALID, "CSRF 校验失败")
            await revoke_session(session, session_id=context.session_id)
            # 只有确实撤销了服务端会话才下发清除 Cookie；否则跨站 POST 不能仅凭
            # 一个无会话请求就清掉访客 Cookie（防 logout CSRF）。
            response.delete_cookie(
                settings.session_cookie_name,
                path="/",
                secure=settings.session_cookie_secure,
                httponly=True,
                samesite="lax",
            )


@router.get("/me", response_model=MeOverviewResponse)
async def me(
    context: AuthContext = Depends(current_auth_context),
    session: AsyncSession = Depends(get_database_session),
) -> MeOverviewResponse:
    """返回当前授权概览；会话有效时始终可再次取得同一 CSRF 令牌与可访问 KB 角色。"""

    accesses = await list_accessible_knowledge_bases(
        session,
        user_id=context.user_id,
        organization_id=context.organization_id,
    )
    return MeOverviewResponse(
        user=UserSummary(
            id=context.user_id,
            username=context.username,
            is_admin=context.is_admin,
            organization_id=context.organization_id,
        ),
        csrf_token=context.csrf_token,
        knowledge_bases=[
            KbRoleSummary(id=access.kb_id, name=access.name, role=access.role)
            for access in accesses
        ],
    )
