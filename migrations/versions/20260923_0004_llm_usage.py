"""创建 append-only 的云 LLM 用量账本 llm_usage。

一次 provider attempt 恰好一行；失败与超时也必须追加事实。表由迁移账号创建，
逐表收回 PUBLIC 权限并只给 api 角色 SELECT+INSERT，worker 不获得任何权限；
不授权 UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER，也不使用 PostgreSQL ENUM、
serial/identity/sequence 或 ALTER DEFAULT PRIVILEGES。成功行必须由 provider
报告 prompt/completion tokens，否则只能是 FAILED/TIMEOUT 且 token 与费用为 NULL。
本迁移不写价目快照，也不计算费用。

Revision ID: 20260923_0004
Revises: 20260922_0003
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260923_0004"
down_revision: str | None = "20260922_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

API_ROLE = "citemind_api"


def upgrade() -> None:
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("usage_source", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("prompt_cache_hit_tokens", sa.Integer(), nullable=True),
        sa.Column("prompt_cache_miss_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("price_snapshot", sa.Text(), nullable=True),
        sa.Column("price_source", sa.Text(), nullable=True),
        sa.Column("price_currency", sa.Text(), nullable=True),
        sa.Column("cost_amount", sa.Numeric(18, 8), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('SUCCEEDED', 'FAILED', 'TIMEOUT')", name="status"
        ),
        sa.CheckConstraint(
            "usage_source IN ('PROVIDER_REPORTED', 'UNKNOWN')", name="usage_source"
        ),
        sa.CheckConstraint("attempt >= 1", name="attempt_positive"),
        sa.CheckConstraint("prompt_tokens >= 0", name="prompt_tokens_non_negative"),
        sa.CheckConstraint(
            "completion_tokens >= 0", name="completion_tokens_non_negative"
        ),
        sa.CheckConstraint(
            "prompt_cache_hit_tokens >= 0", name="cache_hit_tokens_non_negative"
        ),
        sa.CheckConstraint(
            "prompt_cache_miss_tokens >= 0", name="cache_miss_tokens_non_negative"
        ),
        sa.CheckConstraint("latency_ms >= 0", name="latency_ms_non_negative"),
        sa.CheckConstraint("cost_amount >= 0", name="cost_amount_non_negative"),
        sa.CheckConstraint(
            "status <> 'SUCCEEDED' OR (usage_source = 'PROVIDER_REPORTED' "
            "AND prompt_tokens IS NOT NULL AND completion_tokens IS NOT NULL)",
            name="succeeded_requires_provider_usage",
        ),
        sa.CheckConstraint(
            "status = 'SUCCEEDED' OR error_code IS NOT NULL",
            name="failure_has_error_code",
        ),
        sa.CheckConstraint(
            "(price_source IS NULL) = (price_currency IS NULL) "
            "AND (price_source IS NULL) = (cost_amount IS NULL)",
            name="price_consistent",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_llm_usage"),
    )

    # 只收回 PUBLIC 并给 api 角色追加事实；worker 在本切片没有 LLM 写路径，不授权。
    op.execute("REVOKE ALL ON TABLE llm_usage FROM PUBLIC")
    op.execute(f"GRANT SELECT, INSERT ON TABLE llm_usage TO {API_ROLE}")


def downgrade() -> None:
    op.drop_table("llm_usage")
