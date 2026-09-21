"""启用 pgvector 扩展；假设目标数据库由 CiteMind 独占。

Revision ID: 20260921_0001
Revises:
Create Date: 2026-09-21
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260921_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS vector")
