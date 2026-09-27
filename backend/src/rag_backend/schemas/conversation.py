"""问答会话接口的请求/响应 schema（外部字段 camelCase）。

请求只提交问题与请求关联 ID 以及创建会话时的 ``kbIds``；组织、所有者、会话范围与引用
映射都由服务端确定。响应中的引用只含服务端从已保存 chunk 映射出的 locator 与短引文，
不含任何模型自造的 URL、页码或数据库 ID。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator

from rag_backend.schemas.base import CamelModel

# 问题本身的字符上限；输入预算由本地 tokenizer 在装配阶段单独强制。
MAX_QUESTION_CHARS = 8000
MAX_REQUEST_ID_CHARS = 128


class CreateConversationRequest(CamelModel):
    """``POST /conversations`` 的输入；KB 集合必须全部在当前会话可访问范围内。"""

    kb_ids: list[uuid.UUID] = Field(min_length=1)


class CreateConversationResponse(CamelModel):
    """新建会话结果；``kbIds`` 是固化后的会话范围。"""

    conversation_id: uuid.UUID
    kb_ids: list[uuid.UUID]
    created_at: datetime


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


class AskQuestionRequest(CamelModel):
    """``POST /conversations/{id}/messages`` 的输入。

    ``requestId`` 只作为调用方关联标识记录在 ``query_run``；本切片不实现按它去重。
    """

    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    request_id: str | None = Field(default=None, max_length=MAX_REQUEST_ID_CHARS)

    @field_validator("question")
    @classmethod
    def _reject_blank_question(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("问题不能为空或纯空白")
        return value


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
    "AnswerResponse",
    "AnswerUsageResponse",
    "AskQuestionRequest",
    "CitationResponse",
    "ConversationMessageResponse",
    "ConversationMessagesResponse",
    "CreateConversationRequest",
    "CreateConversationResponse",
]
