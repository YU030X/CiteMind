"""给 ingest_job 增加可空的原始请求标题 ``request_title``。

背景：首次上传与文档新版本的幂等身份目前用 ``document.title`` 与原始请求比对，而
``document.title`` 会随新版本切换（可编辑展示标题）而改变，导致同一 Idempotency-Key
在标题变化后原样重放被误判为 409。本迁移给 ``ingest_job`` 增加一个可空的 ``request_title``
列，作为受理那一刻的不可变请求身份快照；幂等判定优先读取它，不再依赖可变文档标题。

本迁移只给 ``ingest_job`` 增加一个可空 Text 列，并刻意不做以下事情：

- 不加 server default、不回填、不 seed；既有行的 ``request_title`` 保持 NULL，应用对
  NULL 行回退到既有 ``document.title`` 比较（旧数据边界，不静默改变历史行为）；
- 不新建索引（没有按 ``request_title`` 过滤的读写路径）、不改动其它表的结构或 ACL；
- 不新增 ``GRANT``/``REVOKE``：api/worker 对 ``ingest_job`` 的既有表级 SELECT/INSERT/UPDATE
  授权已覆盖新列。

数据库层仍不阻止 UPDATE ``request_title``；它是写路径一次性写入的业务快照，不是结构化
不可变约束，一致性由应用写路径维护。

Revision ID: 20260926_0008
Revises: 20260925_0007
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260926_0008"
down_revision: str | None = "20260925_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 旧行保持 NULL：不加 server default，不做回填或 seed。
    op.add_column("ingest_job", sa.Column("request_title", sa.Text(), nullable=True))


def downgrade() -> None:
    # 数据可逆：只删本列，不删除旧任务行，也不触碰其它表或 ACL。
    op.drop_column("ingest_job", "request_title")
