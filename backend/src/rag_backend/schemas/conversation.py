"""问答会话接口的请求/响应 schema（外部字段 camelCase）。

请求只提交问题与请求关联 ID 以及可选的模型/思考选项；组织、所有者、会话范围与引用映射都由
服务端确定。请求字段使用严格枚举并禁止未知字段，客户端不能提交任意模型名、端点或强度别名。
响应中的引用只含服务端从已保存 chunk 映射出的 locator 与短引文，不含任何模型自造的 URL、
页码或数据库 ID，也不包含思考模式返回的 chain of thought。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from rag_backend.generation.capabilities import ReasoningEffort
from rag_backend.schemas.base import CamelModel, StrictCamelModel

# 问题本身的字符上限；输入预算由本地 tokenizer 在装配阶段单独强制。
MAX_QUESTION_CHARS = 8000
MAX_REQUEST_ID_CHARS = 128
# 会话标题上限；与 ``conversation.service.derive_conversation_title`` 的截断长度一致。
MAX_TITLE_CHARS = 200


class CreateConversationRequest(CamelModel):
    """``POST /conversations`` 的输入；KB 集合必须全部在当前会话可访问范围内。"""

    kb_ids: list[uuid.UUID] = Field(min_length=1)


class CreateConversationResponse(CamelModel):
    """新建会话结果；``kbIds`` 是固化后的会话范围。"""

    conversation_id: uuid.UUID
    kb_ids: list[uuid.UUID]
    created_at: datetime


class ConversationSummary(CamelModel):
    """会话列表的一行；只暴露标题、置顶状态与范围/时间，不含任何消息正文。"""

    id: uuid.UUID
    title: str | None
    pinned: bool
    kb_ids: list[uuid.UUID]
    created_at: datetime
    last_message_at: datetime | None


class UpdateConversationRequest(CamelModel):
    """``PATCH /conversations/{id}`` 的输入：title 与 pinned 至少提供一个。

    ``title`` 为提供时的最终展示标题（去首尾空白、非空），``pinned`` 为显式布尔；
    两者都省略的请求是无效请求，不做空操作。
    """

    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE_CHARS)
    pinned: bool | None = None

    @field_validator("title")
    @classmethod
    def _normalise_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("标题不能为空或纯空白")
        return stripped

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> UpdateConversationRequest:
        if self.title is None and self.pinned is None:
            raise ValueError("title 与 pinned 至少提供一个")
        return self


class ConversationListResponse(CamelModel):
    """当前用户的会话列表；本片不分页。"""

    conversations: list[ConversationSummary]


class CitationResponse(CamelModel):
    """一条引用：locator 与 quote 全部由服务端映射。"""

    citation_id: uuid.UUID
    display_label: str
    document_title: str
    version: int
    locator: dict[str, Any]
    quote: str


class ConversationMessageResponse(CamelModel):
    """历史中的一条消息；助手消息的引用同样逐条复核过。"""

    message_id: uuid.UUID
    role: Literal["user", "assistant"]
    content: str
    query_run_id: uuid.UUID | None
    created_at: datetime
    citations: list[CitationResponse]


class ConversationMessagesResponse(CamelModel):
    """按 ``sequence`` 升序的当前合法历史。"""

    conversation_id: uuid.UUID
    messages: list[ConversationMessageResponse]


class ThinkingRequest(StrictCamelModel):
    """``thinking`` 开关；取值与官方文档一致，不接受强度别名、额外字段或未知取值。"""

    type: Literal["enabled", "disabled"]


class AskQuestionRequest(StrictCamelModel):
    """``POST /conversations/{id}/messages`` 的输入。

    ``requestId`` 只作为调用方关联标识记录在 ``query_run``；本切片不实现按它去重。
    ``model``/``thinking``/``reasoningEffort`` 都可省略：省略时沿用服务端默认模型并关闭思考，
    与旧请求体逐字兼容。``model`` 必须是服务端白名单内的已验证模型，``reasoningEffort`` 只在
    ``thinking.type=enabled`` 时才有意义。
    """

    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    request_id: str | None = Field(default=None, max_length=MAX_REQUEST_ID_CHARS)
    model: str | None = None
    thinking: ThinkingRequest | None = None
    reasoning_effort: ReasoningEffort | None = None

    @field_validator("question")
    @classmethod
    def _reject_blank_question(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("问题不能为空或纯空白")
        return value

    @model_validator(mode="after")
    def _require_thinking_for_effort(self) -> AskQuestionRequest:
        if self.reasoning_effort is not None and (
            self.thinking is None or self.thinking.type != "enabled"
        ):
            raise ValueError("reasoningEffort 需要 thinking.type=enabled")
        return self


class AnswerUsageResponse(CamelModel):
    """本地预算与 provider 实际用量的对照；本地值是估算，不是 provider 事实。"""

    local_input_tokens: int | None
    input_token_budget: int
    output_token_budget: int
    provider_prompt_tokens: int | None
    provider_completion_tokens: int | None


class AnswerResponse(CamelModel):
    """一次追问的完整结果。"""

    conversation_id: uuid.UUID
    message_id: uuid.UUID
    query_run_id: uuid.UUID
    answer: str
    citations: list[CitationResponse]
    insufficient_evidence: bool
    degraded_stages: list[str]
    follow_up: str | None
    usage: AnswerUsageResponse


__all__ = [
    "MAX_QUESTION_CHARS",
    "MAX_REQUEST_ID_CHARS",
    "MAX_TITLE_CHARS",
    "AnswerResponse",
    "AnswerUsageResponse",
    "AskQuestionRequest",
    "CitationResponse",
    "ConversationListResponse",
    "ConversationMessageResponse",
    "ConversationMessagesResponse",
    "ConversationSummary",
    "CreateConversationRequest",
    "CreateConversationResponse",
    "ThinkingRequest",
    "UpdateConversationRequest",
]
