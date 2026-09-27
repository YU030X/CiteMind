"""给 ``query_run`` 补生成本轮选项快照列，使历史选择不被后续选择改写。

本迁移紧接 ``20260927_0010``，只给 ``query_run`` 增加一个非空 JSONB 列
``generation_options``，记录该轮实际使用的模型、思考开关与生效强度：

```sql
ALTER TABLE query_run ADD COLUMN generation_options JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE query_run ADD CONSTRAINT ck_query_run_generation_options_object
    CHECK (jsonb_typeof(generation_options) = 'object');
```

``server_default`` 只服务既有行的升级回填（不写额外 ``UPDATE``）；每轮新插入都由应用显式提供
对象。该列不新增任何授权：``query_run`` 已由 ``20260927_0009`` 授予 api 角色表级
SELECT+INSERT，列级权限不参与 INSERT。worker 在问答上没有写路径，不授予任何权限。

Revision ID: 20260927_0011
Revises: 20260927_0010
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "20260927_0011"
down_revision: str | None = "20260927_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GENERATION_OPTIONS_CONSTRAINT = "generation_options_object"


def upgrade() -> None:
    op.add_column(
        "query_run",
        sa.Column(
            "generation_options",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_check_constraint(
        GENERATION_OPTIONS_CONSTRAINT,
        "query_run",
        "jsonb_typeof(generation_options) = 'object'",
    )


def downgrade() -> None:
    op.drop_constraint(
        GENERATION_OPTIONS_CONSTRAINT, "query_run", type_="check"
    )
    op.drop_column("query_run", "generation_options")
