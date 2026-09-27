"""创建证据问答主流程的四张表 conversation、message、query_run、citation。

本迁移紧接 ``20260926_0008``，只建立会话、消息、一次问答运行与引用快照的持久化结构；
检索、上下文预算、生成客户端与路由由 ``rag_backend.conversation`` 与
``rag_backend.generation`` 实现。四张表由迁移账号创建，逐表 ``REVOKE ALL ... FROM PUBLIC``
后只给 ``citemind_api`` 角色 SELECT+INSERT；worker 在本切片没有问答写路径，不授予任何权限。
绝不授权 UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER 或 sequence，也不使用 PostgreSQL ENUM、
serial/identity 与 ``ALTER DEFAULT PRIVILEGES``。

设计要点：

- ``conversation`` 只由所有者访问，``kb_scope`` 固化创建时可访问的 KB 集合；独立问题改写
  不得扩大该范围，检索只在该集合内重新鉴权。
- ``message`` 在会话内按 ``sequence`` 单调编号，``role`` 只有 user/assistant 两种；
  ``query_run_id`` 指向产生该消息的一次问答运行。
- ``query_run`` 记录原问题与独立问题、本次 scope 快照、本地预算与 provider 实际 usage 的
  对照、状态与降级阶段；``llm_usage_id`` 指向真实 provider attempt 的账本行（不复制其事实）。
- ``citation`` 是服务端从已保存 chunk 映射出的引用快照（版本、locator、短引文与全文摘要），
  不接受任何模型自造的 URL、页码或数据库 ID。

Revision ID: 20260927_0009
Revises: 20260926_0008
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "20260927_0009"
down_revision: str | None = "20260926_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

API_ROLE = "citemind_api"

CONVERSATION_TABLES = ("conversation", "message", "query_run", "citation")


def upgrade() -> None:
    op.create_table(
        "conversation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        # 创建时固化的可访问 KB 集合；检索只在此集合内重新鉴权，改写成不了扩权手段。
        sa.Column("kb_scope", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["user_account.id"],
            name="fk_conversation_owner_id_user_account",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_conversation"),
    )

    op.create_table(
        "query_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        # 原始问题与用于检索的独立问题；两者分别保存，改写不覆盖原始输入。
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("standalone_question", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=True),
        # 本次实际参与检索的 KB 集合快照（已按会话范围重新鉴权后的子集）。
        sa.Column("scope_snapshot", JSONB(), nullable=False),
        sa.Column("input_token_budget", sa.Integer(), nullable=False),
        sa.Column("output_token_budget", sa.Integer(), nullable=False),
        # 本地 tokenizer 估算；与 provider 实际上报分开存储，绝不互相冒充。
        sa.Column("estimated_input_tokens", sa.Integer(), nullable=True),
        sa.Column("evidence_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("insufficient_evidence", sa.Boolean(), nullable=False),
        sa.Column("degraded_stages", JSONB(), nullable=False),
        sa.Column("llm_usage_id", sa.Uuid(), nullable=True),
        sa.Column("provider_prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("provider_completion_tokens", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('SUCCEEDED', 'REFUSED', 'FAILED')", name="status"),
        sa.CheckConstraint("input_token_budget > 0", name="input_token_budget_positive"),
        sa.CheckConstraint("output_token_budget > 0", name="output_token_budget_positive"),
        sa.CheckConstraint(
            "estimated_input_tokens >= 0", name="estimated_input_tokens_non_negative"
        ),
        sa.CheckConstraint("evidence_count >= 0", name="evidence_count_non_negative"),
        sa.CheckConstraint(
            "provider_prompt_tokens >= 0", name="provider_prompt_tokens_non_negative"
        ),
        sa.CheckConstraint(
            "provider_completion_tokens >= 0",
            name="provider_completion_tokens_non_negative",
        ),
        sa.CheckConstraint(
            "btrim(question) <> ''", name="question_non_empty"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.id"],
            name="fk_query_run_conversation_id_conversation",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["llm_usage_id"],
            ["llm_usage.id"],
            name="fk_query_run_llm_usage_id_llm_usage",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_query_run"),
    )

    op.create_table(
        "message",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("query_run_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="role"),
        sa.CheckConstraint("sequence > 0", name="sequence_positive"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.id"],
            name="fk_message_conversation_id_conversation",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["query_run_id"],
            ["query_run.id"],
            name="fk_message_query_run_id_query_run",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_message"),
        sa.UniqueConstraint(
            "conversation_id", "sequence", name="uq_message_conversation_id_sequence"
        ),
    )

    op.create_table(
        "citation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("query_run_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        # 模型只能返回临时 E 编号；展示标签由服务端按检索顺序分配。
        sa.Column("display_label", sa.Text(), nullable=False),
        sa.Column("locator_snapshot", JSONB(), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        # 全文摘要（不是短引文的摘要），用于稳定标识引用来源。
        sa.Column("quote_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("btrim(display_label) <> ''", name="display_label_non_empty"),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["message.id"],
            name="fk_citation_message_id_message",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["query_run_id"],
            ["query_run.id"],
            name="fk_citation_query_run_id_query_run",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["chunk.id"],
            name="fk_citation_chunk_id_chunk",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["document_version.id"],
            name="fk_citation_version_id_document_version",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_citation"),
        sa.UniqueConstraint(
            "message_id", "display_label", name="uq_citation_message_id_display_label"
        ),
    )

    for table in CONVERSATION_TABLES:
        op.execute(f"REVOKE ALL ON TABLE {table} FROM PUBLIC")
        op.execute(f"GRANT SELECT, INSERT ON TABLE {table} TO {API_ROLE}")


def downgrade() -> None:
    op.drop_table("citation")
    op.drop_table("message")
    op.drop_table("query_run")
    op.drop_table("conversation")
