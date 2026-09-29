"""给 ``chunk.model_input_hash`` 增加具名 btree 索引，服务增量 embedding 缓存的批量查找。

本迁移紧接 ``20260929_0013``，是 Phase 2 增量 embedding 缓存的第一步：缓存直接复用既有
``chunk_embedding``，按 ``model_input_hash`` 在同组织、同 profile、READY generation 内批量
查找，因此需要 ``chunk(model_input_hash)`` 的普通 btree 索引。只新增一个具名索引，不建表、
不加列、不改授权、不 seed、不回填。SQLAlchemy 模型 ``Chunk`` 同步声明同名索引。

降级只删除该索引，不动任何数据或其它对象。

Revision ID: 20260929_0014
Revises: 20260929_0013
Create Date: 2026-09-29
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260929_0014"
down_revision: str | None = "20260929_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "ix_chunk_model_input_hash"
TABLE_NAME = "chunk"
COLUMN_NAME = "model_input_hash"


def upgrade() -> None:
    op.create_index(INDEX_NAME, TABLE_NAME, [COLUMN_NAME])


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
