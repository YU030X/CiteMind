"""给 worker 增加发布 READY 索引所需的最小 ``knowledge_base`` 列级 UPDATE 权限。

首次 READY 发布事务需要在锁定/更新 KB 行时把 ``active_index_profile_id`` 从 NULL 置为
已发布 generation 的 profile，并按 [入库与版本](../../docs/ingestion.md) 的规则原子递增
``kb_revision``。worker 目前对 ``knowledge_base`` 只有表级 SELECT，因此本迁移补充**仅两列**
的列级 UPDATE：

- ``active_index_profile_id``：首次 READY 发布置位，后续同 profile 发布保持原值；
- ``kb_revision``：发布事务原子递增，用于让依赖 KB 可见性快照的读取失效。

它刻意不做以下事情：

- 不授予全表 UPDATE，也不授予 INSERT/DELETE/TRUNCATE/REFERENCES/TRIGGER；
- 不改 ``knowledge_base`` 结构、不新增索引、不做回填或 seed；
- 不改 api 角色或其它表的授权，也不触及其它业务表。

发布事务仍然必须在同一事务中先校验目标 document 的 ``active_version_id`` 为空、目标
generation 的 profile 与 KB 既有 ``active_index_profile_id`` 兼容，再用带谓词的条件
UPDATE 拒绝非 NULL 且不同的 profile；本迁移只提供列级权限，不代替这些应用层校验。

Revision ID: 20260925_0007
Revises: 20260925_0006
Create Date: 2026-09-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260925_0007"
down_revision: str | None = "20260925_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WORKER_ROLE = "citemind_worker"

# 发布事务真正写入的两列；列级授权不扩大到整表。
PUBLISH_COLUMNS = ("active_index_profile_id", "kb_revision")


def upgrade() -> None:
    # 只补 worker 缺少的两列 UPDATE；表级 SELECT 与既有授权保持不变。
    op.execute(
        "GRANT UPDATE (active_index_profile_id, kb_revision) "
        f"ON TABLE knowledge_base TO {WORKER_ROLE}"
    )


def downgrade() -> None:
    # 只撤回本迁移授予的两列 UPDATE；不删除 KB 行，也不动其它授权。
    op.execute(
        "REVOKE UPDATE (active_index_profile_id, kb_revision) "
        f"ON TABLE knowledge_base FROM {WORKER_ROLE}"
    )
