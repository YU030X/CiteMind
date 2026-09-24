"""身份与会话接口的请求/响应 schema。"""

from __future__ import annotations

import uuid

from pydantic import Field

from rag_backend.auth.accounts import MAX_USERNAME_LENGTH
from rag_backend.knowledge.roles import KbRole
from rag_backend.schemas.base import CamelModel


class LoginRequest(CamelModel):
    """登录输入；密码只在内存中用于 Argon2 校验，不回显、不落库。

    ``username`` 长度与账号创建入口 ``validate_username`` 共用同一常量，避免建出
    登录校验永远匹配不到的账号。"""

    username: str = Field(min_length=1, max_length=MAX_USERNAME_LENGTH)
    password: str = Field(min_length=1, max_length=1024)


class UserSummary(CamelModel):
    """当前用户的授权概览；不含任何凭据或会话令牌。"""

    id: uuid.UUID
    username: str
    is_admin: bool
    organization_id: uuid.UUID


class MeResponse(CamelModel):
    """GET /me 与登录成功共用的响应；CSRF 令牌供后续状态变更请求使用。"""

    user: UserSummary
    csrf_token: str


class KbRoleSummary(CamelModel):
    """GET /me 授权概览中的 KB 角色条目。"""

    id: uuid.UUID
    name: str
    role: KbRole


class MeOverviewResponse(MeResponse):
    """GET /me 的响应：在基础授权概览上附带当前可访问的 KB 角色。"""

    knowledge_bases: list[KbRoleSummary] = Field(default_factory=list)
