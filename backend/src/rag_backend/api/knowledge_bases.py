"""知识库路由：列表、创建与成员读取/全量替换。

访问控制全部在服务端完成：组织来自会话，角色来自数据库；对不存在、跨组织、已撤销
与角色不足统一返回不暴露存在性的 404。创建与替换要求管理员/OWNER，并校验 Origin 与
CSRF。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from rag_backend.api.errors import (
    CODE_FORBIDDEN,
    CODE_KB_MEMBER_OWNER_DISABLED,
    CODE_KB_MEMBER_USER_INVALID,
    CODE_KB_MEMBERS_INVALID,
    CODE_KNOWLEDGE_BASE_NOT_FOUND,
    CODE_LAST_OWNER_REQUIRED,
    ApiError,
)
from rag_backend.auth.context import AuthContext
from rag_backend.auth.dependencies import (
    enforce_allowed_origin,
    get_auth_context,
    require_csrf,
    require_kb_role,
)
from rag_backend.config import Settings
from rag_backend.database import get_database_session
from rag_backend.knowledge.roles import (
    KbMemberOwnerDisabled,
    KbMembersInvalid,
    KbMemberUserInvalid,
    KbRole,
    KnowledgeBaseError,
    KnowledgeBaseNotFound,
    LastOwnerRequired,
    MemberReplacement,
)
from rag_backend.knowledge.service import (
    KbAccess,
    ValidMember,
    create_knowledge_base,
    list_accessible_knowledge_bases,
    list_valid_members,
    replace_knowledge_base_members,
)
from rag_backend.schemas.knowledge import (
    KbMemberListResponse,
    KbMemberReplaceRequest,
    KbMemberSummary,
    KnowledgeBaseCreateRequest,
    KnowledgeBaseListResponse,
    KnowledgeBaseResponse,
    KnowledgeBaseSummary,
)

router = APIRouter(prefix="/api/v1", tags=["knowledge-bases"])

require_reader = require_kb_role(KbRole.READER)
require_owner = require_kb_role(KbRole.OWNER)


def _knowledge_error(error: KnowledgeBaseError) -> ApiError:
    """把 KB 领域错误映射为具名错误体；不泄露资源是否存在。"""

    if isinstance(error, KnowledgeBaseNotFound):
        return ApiError(404, CODE_KNOWLEDGE_BASE_NOT_FOUND, "知识库不存在或无权访问")
    if isinstance(error, KbMembersInvalid):
        return ApiError(422, CODE_KB_MEMBERS_INVALID, str(error))
    if isinstance(error, KbMemberUserInvalid):
        return ApiError(422, CODE_KB_MEMBER_USER_INVALID, str(error))
    if isinstance(error, KbMemberOwnerDisabled):
        return ApiError(422, CODE_KB_MEMBER_OWNER_DISABLED, str(error))
    if isinstance(error, LastOwnerRequired):
        return ApiError(409, CODE_LAST_OWNER_REQUIRED, str(error))
    return ApiError(400, "KNOWLEDGE_BASE_ERROR", "知识库请求失败")


def _kb_summary(access: KbAccess) -> KnowledgeBaseSummary:
    return KnowledgeBaseSummary(
        id=access.kb_id,
        name=access.name,
        role=access.role,
        acl_revision=access.acl_revision,
    )


def _member_response(
    members: list[ValidMember], acl_revision: int
) -> KbMemberListResponse:
    return KbMemberListResponse(
        members=[
            KbMemberSummary(user_id=member.user_id, username=member.username, role=member.role)
            for member in members
        ],
        acl_revision=acl_revision,
    )


@router.get("/knowledge-bases", response_model=KnowledgeBaseListResponse)
async def list_knowledge_bases(
    context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_database_session),
) -> KnowledgeBaseListResponse:
    """只列出当前组织内当前用户仍是有效成员的 KB。"""

    accesses = await list_accessible_knowledge_bases(
        session,
        user_id=context.user_id,
        organization_id=context.organization_id,
    )
    return KnowledgeBaseListResponse(knowledge_bases=[_kb_summary(access) for access in accesses])


@router.post("/knowledge-bases", response_model=KnowledgeBaseResponse, status_code=201)
async def create_knowledge_base_route(
    request: Request,
    payload: KnowledgeBaseCreateRequest,
    context: AuthContext = Depends(require_csrf),
    session: AsyncSession = Depends(get_database_session),
) -> KnowledgeBaseResponse:
    """管理员创建 KB；组织来自会话，并在同一事务写入创建者的 OWNER 成员行。"""

    settings: Settings = request.app.state.settings
    enforce_allowed_origin(request, settings)
    if not context.is_admin:
        raise ApiError(403, CODE_FORBIDDEN, "需要管理员权限")

    try:
        access = await create_knowledge_base(
            session,
            organization_id=context.organization_id,
            name=payload.name,
            owner_user_id=context.user_id,
        )
    except KnowledgeBaseError as error:
        raise _knowledge_error(error) from error
    return KnowledgeBaseResponse(
        id=access.kb_id,
        name=access.name,
        role=access.role,
        acl_revision=access.acl_revision,
        kb_revision=access.kb_revision,
    )


@router.get("/knowledge-bases/{kb_id}/members", response_model=KbMemberListResponse)
async def get_knowledge_base_members(
    access: KbAccess = Depends(require_reader),
    session: AsyncSession = Depends(get_database_session),
) -> KbMemberListResponse:
    """任意有效成员可读取同组织 KB 的有效成员列表。"""

    members = await list_valid_members(
        session, kb_id=access.kb_id, organization_id=access.organization_id
    )
    return _member_response(members, access.acl_revision)


@router.put("/knowledge-bases/{kb_id}/members", response_model=KbMemberListResponse)
async def replace_knowledge_base_members_route(
    request: Request,
    payload: KbMemberReplaceRequest,
    access: KbAccess = Depends(require_owner),
    context: AuthContext = Depends(require_csrf),
    session: AsyncSession = Depends(get_database_session),
) -> KbMemberListResponse:
    """OWNER 全量替换成员；缺省者软撤销、重加入者恢复，实际变化递增 ``acl_revision``。"""

    settings: Settings = request.app.state.settings
    enforce_allowed_origin(request, settings)

    replacements = [
        MemberReplacement(user_id=member.user_id, role=member.role)
        for member in payload.members
    ]
    try:
        members, acl_revision = await replace_knowledge_base_members(
            session,
            kb_id=access.kb_id,
            organization_id=access.organization_id,
            actor_user_id=context.user_id,
            members=replacements,
        )
    except KnowledgeBaseError as error:
        raise _knowledge_error(error) from error
    return _member_response(members, acl_revision)


__all__ = ["router"]
