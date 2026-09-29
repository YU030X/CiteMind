"""扩展 ``document.source_type`` 到 ``web``，并给 ``document_version`` 增加网页抓取元数据。

本迁移紧接 ``20260929_0014``，是受限静态网页入库最小闭环的存储步骤：

- 放宽既有具名 CHECK ``ck_document_source_type``，新增 ``web``；不新增表、索引或授权。
- 给 ``document_version`` 增加可空 ``source_url``/``final_url``/``fetched_at``：非网页来源保持
  NULL，网页来源记录服务端规范化请求 URL、跟随重定向后的最终 URL 与抓取时刻。

降级不会删除数据：若库中已存在 ``source_type='web'`` 的文档，降级直接失败并保留原行；只有在
没有 web 行时才删除三列并恢复旧约束 ``markdown``/``pdf``/``docx``。

Revision ID: 20260929_0015
Revises: 20260929_0014
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0015"
down_revision: str | None = "20260929_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCE_TYPE_CONSTRAINT = "ck_document_source_type"
WEB_SOURCE_TYPE = "web"


def upgrade() -> None:
    op.drop_constraint(op.f(SOURCE_TYPE_CONSTRAINT), "document", type_="check")
    op.create_check_constraint(
        "source_type", "document", "source_type IN ('markdown', 'pdf', 'docx', 'web')"
    )
    op.add_column(
        "document_version", sa.Column("source_url", sa.Text(), nullable=True)
    )
    op.add_column(
        "document_version", sa.Column("final_url", sa.Text(), nullable=True)
    )
    op.add_column(
        "document_version",
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind is not None:
        # 离线 SQL 生成模式下 execute 返回 None；真实迁移仍会先拒绝 web 行。
        result = bind.execute(
            sa.text("SELECT count(*) FROM document WHERE source_type = :source_type"),
            {"source_type": WEB_SOURCE_TYPE},
        )
        web_rows = None if result is None else result.scalar_one()
        if web_rows:
            raise RuntimeError(
                "存在 source_type='web' 的文档，拒绝降级；请先显式处理这些文档，不自动删除数据"
            )
    op.drop_column("document_version", "fetched_at")
    op.drop_column("document_version", "final_url")
    op.drop_column("document_version", "source_url")
    op.drop_constraint(op.f(SOURCE_TYPE_CONSTRAINT), "document", type_="check")
    op.create_check_constraint(
        "source_type", "document", "source_type IN ('markdown', 'pdf', 'docx')"
    )
