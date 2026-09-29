"""给 append-only 的 ``llm_usage`` 增加可空 ``query_run_id`` 关联键与普通 btree 索引。

本迁移紧接 ``20260929_0015``，是 Phase 3 成本归因片的第一步：让一次提问产生的
``qa_rewrite``/``qa_answer`` 账本行都能归属到同一个调用前生成的关联键，而不是只能靠
provider+model+stage+created_at 猜测。

- ``query_run_id`` 是 **nullable UUID 关联键，不建外键**：账本行按 attempt 分次提交，
  且 answer 失败时可能永远没有对应的 ``query_run`` 行，普通或 deferred FK 都会在不经重排
  事务的情况下失败。它只在调用前生成，用来把同一轮的全部 attempt 归到一起，不代表一定存在
  对应 ``query_run``。
- 历史行保持 NULL，不回填、不伪造。
- 建普通 btree 索引 ``ix_llm_usage_query_run_id`` 供按关联键聚合；该表其余列与授权不变，
  表级 ``SELECT``/``INSERT`` GRANT 已覆盖新列，worker 仍无任何权限。
- 降级先删索引再删列，数据可空且无回填，安全。

Revision ID: 20260929_0016
Revises: 20260929_0015
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0016"
down_revision: str | None = "20260929_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "ix_llm_usage_query_run_id"


def upgrade() -> None:
    op.add_column(
        "llm_usage", sa.Column("query_run_id", sa.Uuid(), nullable=True)
    )
    op.create_index(INDEX_NAME, "llm_usage", ["query_run_id"])


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="llm_usage")
    op.drop_column("llm_usage", "query_run_id")
