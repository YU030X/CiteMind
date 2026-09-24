"""知识库与 KB 成员接口的请求/响应 schema。"""

from __future__ import annotations

import uuid

from pydantic import Field

from rag_backend.knowledge.roles import KbRole
from rag_backend.schemas.base import CamelModel

MAX_KB_NAME_LENGTH = 200


class KnowledgeBaseCreateRequest(CamelModel):
    """创建 KB 的输入；组织由服务端会话确定，请求体不能提交 organization_id。"""

    name: str = Field(min_length=1, max_length=MAX_KB_NAME_LENGTH)


class KnowledgeBaseSummary(CamelModel):
    """列表与授权概览中的 KB 条目；含当前用户的角色。"""

    id: uuid.UUID
    name: str
    role: KbRole
    acl_revision: int


class KnowledgeBaseListResponse(CamelModel):
    """GET /knowledge-bases 的响应体。"""

    knowledge_bases: list[KnowledgeBaseSummary]


class KnowledgeBaseResponse(CamelModel):
    """创建成功后的 KB 描述。"""

    id: uuid.UUID
    name: str
    role: KbRole
    acl_revision: int
    kb_revision: int


class KbMemberSummary(CamelModel):
    """KB 的一名有效成员。"""

    user_id: uuid.UUID
    username: str
    role: KbRole


class KbMemberListResponse(CamelModel):
    """成员读取与替换后的统一响应。"""

    members: list[KbMemberSummary]
    acl_revision: int


class KbMemberInput(CamelModel):
    """成员替换条目；角色是期望值，服务端在事务内核对用户与组织。"""

    user_id: uuid.UUID
    role: KbRole


class KbMemberReplaceRequest(CamelModel):
    """全量替换成员；空列表由服务端以具名错误拒绝。"""

    members: list[KbMemberInput]
