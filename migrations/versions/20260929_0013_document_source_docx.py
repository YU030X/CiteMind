"""把 ``document.source_type`` 的允许集合从 Markdown/PDF 扩展到 DOCX。

本迁移紧接 ``20260928_0012``，是 Phase 2 DOCX 最小闭环的第一步：只放宽既有具名 CHECK
``ck_document_source_type``，不新增列、表、索引或授权。SQLAlchemy 模型 ``Document`` 的同一
CHECK 同步更新，保持模型与迁移结构一致。

降级不会删除数据：若库中已存在 ``source_type='docx'`` 的文档，降级直接失败并保留原行，
绝不静默删除或改写这些文档；只有在没有 DOCX 行时才恢复旧约束 ``markdown``/``pdf``。

Revision ID: 20260929_0013
Revises: 20260928_0012
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0013"
down_revision: str | None = "20260928_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCE_TYPE_CONSTRAINT = "ck_document_source_type"
DOCX_SOURCE_TYPE = "docx"


def upgrade() -> None:
    op.drop_constraint(
        op.f(SOURCE_TYPE_CONSTRAINT), "document", type_="check"
    )
    op.create_check_constraint(
        "source_type", "document", "source_type IN ('markdown', 'pdf', 'docx')"
    )


def downgrade() -> None:
    bind = op.get_bind()
    docx_rows = bind.execute(
        sa.text("SELECT count(*) FROM document WHERE source_type = :source_type"),
        {"source_type": DOCX_SOURCE_TYPE},
    ).scalar_one()
    if docx_rows:
        raise RuntimeError(
            "存在 source_type='docx' 的文档，拒绝降级；请先显式处理这些文档，不自动删除数据"
        )
    op.drop_constraint(
        op.f(SOURCE_TYPE_CONSTRAINT), "document", type_="check"
    )
    op.create_check_constraint(
        "source_type", "document", "source_type IN ('markdown', 'pdf')"
    )
