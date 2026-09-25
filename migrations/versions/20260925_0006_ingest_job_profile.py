"""给 ingest_job 增加可空 profile_id 外键。

本迁移只给 ``ingest_job`` 增加一个可空 UUID 列 ``profile_id``，并建立指向
``index_profile(id)`` 的具名外键 ``ON DELETE/UPDATE RESTRICT``。它刻意不做以下事情：

- 不改变任何既有行的值：已有 ``ingest_job`` 行的 ``profile_id`` 保持 NULL，不设
  server default、不回填、不 seed，也不回填 ``knowledge_base.active_index_profile_id``；
- 不“修复”或改写既有 ``QUEUED``/``HANDLER_NOT_READY`` 接收标记任务，旧任务既不被删除
  也不被宣称已按 profile 处理；
- 不新建任何业务索引（当前没有按 ``profile_id`` 过滤的读写路径，新建索引不是必需），
  不改动其它表的结构或 ACL。

``ingest_job`` 的 api/worker 表级 UPDATE 授权已覆盖新列，因此本迁移不新增
GRANT/REVOKE，也不给 PUBLIC 任何权限。数据库层面该列仍可被 UPDATE：``profile_id``
只在后续 worker 接线后才具有业务含义，worker 必须核对任务携带的 profile 与目标
generation/profile 一致并限制对 ``profile_id`` 的更改；当前没有任何应用路径写入它，
所以不能宣称数据库在结构上不可变，也不能宣称既有旧任务已可安全处理。

Revision ID: 20260925_0006
Revises: 20260923_0005
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260925_0006"
down_revision: str | None = "20260923_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 旧行保持 NULL：不加 server default，不做回填或 seed。
    op.add_column("ingest_job", sa.Column("profile_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_ingest_job_profile_id_index_profile",
        "ingest_job",
        "index_profile",
        ["profile_id"],
        ["id"],
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    )


def downgrade() -> None:
    # 数据可逆：只删外键与列，不删除旧任务行，也不触碰其它表或 ACL。
    op.drop_constraint(
        "fk_ingest_job_profile_id_index_profile", "ingest_job", type_="foreignkey"
    )
    op.drop_column("ingest_job", "profile_id")
