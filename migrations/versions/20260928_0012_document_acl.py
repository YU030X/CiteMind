"""新增文档读取收紧：``document.acl_mode`` 与 ``document_acl`` 允许名单。

本迁移紧接 ``20260927_0011``，是 Phase 2 文档 ACL 首片：

1. 给 ``document`` 增加非空 ``acl_mode``，取值 ``INHERIT``（默认，沿用 KB 成员读权限）
   或 ``RESTRICTED``（只允许 ``document_acl`` 中显式登记的用户读取）；``server_default``
   只服务既有行升级回填，应用写入显式提供。
2. 创建 ``document_acl``：``(document_id, principal_type, principal_id, permission)`` 唯一，
   首片只允许 ``principal_type='USER'`` 与 ``permission='READ'``；``principal_id`` 外键指向
   ``user_account``。允许名单只**收紧**读取，不授予管理权（KB ``EDITOR`` 更新、``OWNER``
   删除与管理 ACL 不受影响）。

授权：``document_acl`` 逐表 ``REVOKE ALL ... FROM PUBLIC`` 后只给 ``citemind_api``
``SELECT, INSERT, DELETE``：全量替换需要删除旧登记行，这是运行角色首次获得 DELETE；
它仍然没有 ``UPDATE``（名单行不可变，只整行换）。``citemind_worker`` 在读取收紧上没有
写路径，不授予任何权限。绝不授权 ``TRUNCATE``/``REFERENCES``/``TRIGGER``、sequence 或
PostgreSQL ENUM。给 ``document`` 加列不改变该表既有表级授权。

Revision ID: 20260928_0012
Revises: 20260927_0011
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260928_0012"
down_revision: str | None = "20260927_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

API_ROLE = "citemind_api"
DOCUMENT_ACL_TABLE = "document_acl"
ACL_MODE_CONSTRAINT = "acl_mode"
PRINCIPAL_TYPE_CONSTRAINT = "principal_type"
PERMISSION_CONSTRAINT = "permission"


def upgrade() -> None:
    op.add_column(
        "document",
        sa.Column(
            "acl_mode",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'INHERIT'"),
        ),
    )
    op.create_check_constraint(
        ACL_MODE_CONSTRAINT,
        "document",
        "acl_mode IN ('INHERIT', 'RESTRICTED')",
    )

    op.create_table(
        DOCUMENT_ACL_TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("principal_type", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("permission", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("principal_type IN ('USER')", name=PRINCIPAL_TYPE_CONSTRAINT),
        sa.CheckConstraint("permission IN ('READ')", name=PERMISSION_CONSTRAINT),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["document.id"],
            name="fk_document_acl_document_id_document",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["user_account.id"],
            name="fk_document_acl_principal_id_user_account",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_acl"),
        sa.UniqueConstraint(
            "document_id",
            "principal_type",
            "principal_id",
            "permission",
            name="uq_document_acl_document_principal_permission",
        ),
    )

    # 逐表收回 PUBLIC；api 只得到 SELECT+INSERT+DELETE，worker 无权限。
    op.execute(f"REVOKE ALL ON TABLE {DOCUMENT_ACL_TABLE} FROM PUBLIC")
    op.execute(
        f"GRANT SELECT, INSERT, DELETE ON TABLE {DOCUMENT_ACL_TABLE} TO {API_ROLE}"
    )


def downgrade() -> None:
    op.drop_table(DOCUMENT_ACL_TABLE)
    op.drop_constraint(ACL_MODE_CONSTRAINT, "document", type_="check")
    op.drop_column("document", "acl_mode")
