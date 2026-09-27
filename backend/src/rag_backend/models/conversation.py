"""问答主流程模型：``conversation``、``message``、``query_run`` 与 ``citation``。

四张表由迁移 ``20260927_0009`` 创建，api 角色只有 SELECT+INSERT，worker 无权限。
``citation`` 是服务端从已保存 chunk 映射出的引用快照；模型只能返回临时 ``E`` 编号，
永远不能提交 URL、页码或数据库 ID。
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from rag_backend.models.base import Base, CreatedAtMixin, UpdatedAtMixin


class Conversation(CreatedAtMixin, UpdatedAtMixin, Base):
    """一次问答会话；只由所有者访问，``kb_scope`` 固化可访问 KB 集合。

    ``title`` 在首轮提问时由该问题派生（真实来源，非编造），``pinned_at`` 非空表示置顶，
    ``deleted_at`` 非空表示逻辑删除；删除后所有读取与追加路径都必须拒绝。
    """

    __tablename__ = "conversation"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("user_account.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    # 创建时固化的 KB 集合（UUID 字符串列表）；改写成不了扩大范围的手段。
    kb_scope: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    # 首轮提问派生的展示标题；为空表示尚无标题（如空会话或尚未追问）。
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 非空表示已置顶；未置顶为 NULL，不引入布尔列与默认值。
    pinned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 逻辑删除时间；非空后列表、历史、引用与追加追问全部拒绝。
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class QueryRun(CreatedAtMixin, Base):
    """一次问答运行：原问题/独立问题、scope 快照、预算与 provider usage 对照。"""

    __tablename__ = "query_run"
    __table_args__ = (
        CheckConstraint("status IN ('SUCCEEDED', 'REFUSED', 'FAILED')", name="status"),
        CheckConstraint("input_token_budget > 0", name="input_token_budget_positive"),
        CheckConstraint("output_token_budget > 0", name="output_token_budget_positive"),
        CheckConstraint(
            "estimated_input_tokens >= 0", name="estimated_input_tokens_non_negative"
        ),
        CheckConstraint("evidence_count >= 0", name="evidence_count_non_negative"),
        CheckConstraint(
            "provider_prompt_tokens >= 0", name="provider_prompt_tokens_non_negative"
        ),
        CheckConstraint(
            "provider_completion_tokens >= 0",
            name="provider_completion_tokens_non_negative",
        ),
        CheckConstraint("btrim(question) <> ''", name="question_non_empty"),
        CheckConstraint(
            "jsonb_typeof(generation_options) = 'object'", name="generation_options_object"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("conversation.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    standalone_question: Mapped[str] = mapped_column(Text, nullable=False)
    request_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope_snapshot: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    input_token_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    output_token_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    insufficient_evidence: Mapped[bool] = mapped_column(Boolean, nullable=False)
    degraded_stages: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    llm_usage_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("llm_usage.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=True,
    )
    provider_prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 本轮实际使用的生成选项快照（模型/思考开关/强度）；只写一次，后续选择不改写历史。
    generation_options: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class Message(CreatedAtMixin, Base):
    """会话内按 ``sequence`` 单调编号的一条消息；只允许 user/assistant。"""

    __tablename__ = "message"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant')", name="role"),
        CheckConstraint("sequence > 0", name="sequence_positive"),
        UniqueConstraint(
            "conversation_id", "sequence", name="uq_message_conversation_id_sequence"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("conversation.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    query_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("query_run.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=True,
    )


class Citation(CreatedAtMixin, Base):
    """服务端映射出的引用快照；模型不能提交 URL、页码或数据库 ID。"""

    __tablename__ = "citation"
    __table_args__ = (
        CheckConstraint("btrim(display_label) <> ''", name="display_label_non_empty"),
        UniqueConstraint(
            "message_id", "display_label", name="uq_citation_message_id_display_label"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("message.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    query_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("query_run.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("chunk.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("document_version.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    display_label: Mapped[str] = mapped_column(Text, nullable=False)
    locator_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    quote_hash: Mapped[str] = mapped_column(Text, nullable=False)
