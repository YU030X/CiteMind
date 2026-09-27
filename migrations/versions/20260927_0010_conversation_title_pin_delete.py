"""给 ``conversation`` 补正式会话管理所需的三列与最小列级 UPDATE 授权。

本迁移紧接 ``20260927_0009``，只给 ``conversation`` 增加三个可空列：

- ``title``：首轮提问派生的展示标题（真实来源，不编造内容）；为空表示尚无标题。
- ``pinned_at``：非空表示置顶；未置顶为 NULL，因此不需要布尔列或默认值。
- ``deleted_at``：逻辑删除时间；非空后所有读取与追加追问路径都必须拒绝。

删除复用软删（不授予 DELETE），因此 api 角色只追加 ``conversation`` 的列级 UPDATE：

```sql
GRANT UPDATE (title, pinned_at, deleted_at, updated_at) ON TABLE conversation TO citemind_api;
```

它刻意不授予全表 UPDATE，也不授予 DELETE/TRUNCATE/REFERENCES/TRIGGER；不改结构之外的其它
对象、不新增索引、不 seed、不改 worker 授权（worker 在问答上没有写路径）。

Revision ID: 20260927_0010
Revises: 20260927_0009
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260927_0010"
down_revision: str | None = "20260927_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

API_ROLE = "citemind_api"

CONVERSATION_UPDATE_COLUMNS = ("title", "pinned_at", "deleted_at", "updated_at")
CONVERSATION_UPDATE_GRANT = (
    "GRANT UPDATE (title, pinned_at, deleted_at, updated_at) "
    "ON TABLE conversation TO citemind_api"
)


def upgrade() -> None:
    op.add_column(
        "conversation",
        sa.Column("title", sa.Text(), nullable=True),
    )
    op.add_column(
        "conversation",
        sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "conversation",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(CONVERSATION_UPDATE_GRANT)


def downgrade() -> None:
    op.execute(
        "REVOKE UPDATE (title, pinned_at, deleted_at, updated_at) "
        "ON TABLE conversation FROM citemind_api"
    )
    op.drop_column("conversation", "deleted_at")
    op.drop_column("conversation", "pinned_at")
    op.drop_column("conversation", "title")
