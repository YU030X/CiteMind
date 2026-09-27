"""身份与会话接口的请求/响应 schema。"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import Field

from rag_backend.auth.accounts import MAX_USERNAME_LENGTH
from rag_backend.generation.capabilities import SUPPORTED_MODELS, ReasoningEffort
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


class ThinkingCapability(CamelModel):
    """单个模型的思考能力事实：是否支持开关与可用强度。"""

    supported: bool
    efforts: list[ReasoningEffort]
    default_effort: ReasoningEffort | None


class ModelCapability(CamelModel):
    """服务端白名单内的一个模型及其可切换能力。"""

    id: str
    thinking: ThinkingCapability


class GenerationCapability(CamelModel):
    """只读的生成能力事实：受支持模型与思考选项，以及服务端默认组合。

    它只列举经证实与固定 tokenizer/渲染契约兼容的模型；不在列表里的模型（例如词表尚未验证的
    ``deepseek-v4-pro``）不接受、也不展示。``default_thinking`` 描述省略请求字段时的行为。
    """

    enabled: bool
    default_model: str
    default_thinking: Literal["enabled", "disabled"]
    models: list[ModelCapability]


def generation_capability(enabled: bool, default_model: str) -> GenerationCapability:
    """按服务端白名单构造只读能力事实；不含密钥、端点与任何客户端可提交的开关。"""

    return GenerationCapability(
        enabled=enabled,
        default_model=default_model,
        default_thinking="disabled",
        models=[
            ModelCapability(
                id=model.model_id,
                thinking=ThinkingCapability(
                    supported=model.thinking_supported,
                    efforts=list(model.efforts),
                    default_effort=model.default_effort if model.thinking_supported else None,
                ),
            )
            for model in SUPPORTED_MODELS
        ],
    )


class MeResponse(CamelModel):
    """GET /me 与登录成功共用的响应；CSRF 令牌供后续状态变更请求使用。"""

    user: UserSummary
    csrf_token: str
    generation: GenerationCapability


class KbRoleSummary(CamelModel):
    """GET /me 授权概览中的 KB 角色条目。"""

    id: uuid.UUID
    name: str
    role: KbRole


class MeOverviewResponse(MeResponse):
    """GET /me 的响应：在基础授权概览上附带当前可访问的 KB 角色。"""

    knowledge_bases: list[KbRoleSummary] = Field(default_factory=list)
