"""创建身份与会话基础表 user_account、auth_session、kb_member。

本迁移只建立登录主体、服务端会话与 KB 成员授权的持久化结构；登录、会话签发/撤销与
权限判定现由 ``rag_backend.auth`` 与 ``rag_backend.knowledge`` 实现。三张表由迁移账号创建，逐表
``REVOKE ALL ... FROM PUBLIC`` 后只给 ``citemind_api`` 角色 SELECT+INSERT+UPDATE；
worker 在本切片没有身份写路径，不授予任何权限。绝不授权 DELETE、TRUNCATE、
REFERENCES、TRIGGER 或 sequence，也不使用 PostgreSQL ENUM、serial/identity 与
``ALTER DEFAULT PRIVILEGES``。会话令牌与 CSRF 令牌只存 hash；成员用 ``revoked_at``
软撤销，登录会话同样以 ``revoked_at`` 失效，因此运行角色不需要 DELETE 权限。

Revision ID: 20260923_0005
Revises: 20260923_0004
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260923_0005"
down_revision: str | None = "20260923_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

API_ROLE = "citemind_api"


def upgrade() -> None:
    op.create_table(
        "user_account",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # 组织来自服务端会话；MVP 单组织下账号在组织内唯一。
        sa.UniqueConstraint(
            "organization_id",
            "username",
            name="uq_user_account_organization_id_username",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_account"),
    )

    op.create_table(
        "auth_session",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("csrf_token_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_account.id"],
            name="fk_auth_session_user_id_user_account",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        # Cookie 只携带随机原令牌；库中只存 hash，查会话命中该唯一约束。
        sa.UniqueConstraint("token_hash", name="uq_auth_session_token_hash"),
        sa.PrimaryKeyConstraint("id", name="pk_auth_session"),
    )

    op.create_table(
        "kb_member",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kb_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("role IN ('OWNER', 'EDITOR', 'READER')", name="role"),
        sa.ForeignKeyConstraint(
            ["kb_id"],
            ["knowledge_base.id"],
            name="fk_kb_member_kb_id_knowledge_base",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user_account.id"],
            name="fk_kb_member_user_id_user_account",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_kb_member"),
        sa.UniqueConstraint("kb_id", "user_id", name="uq_kb_member_kb_id_user_id"),
    )

    # 逐表收回 PUBLIC 权限并只给 api 角色 SELECT+INSERT+UPDATE；撤销走软撤销。
    op.execute("REVOKE ALL ON TABLE user_account FROM PUBLIC")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE user_account TO {API_ROLE}")

    op.execute("REVOKE ALL ON TABLE auth_session FROM PUBLIC")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE auth_session TO {API_ROLE}")

    op.execute("REVOKE ALL ON TABLE kb_member FROM PUBLIC")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE kb_member TO {API_ROLE}")


def downgrade() -> None:
    op.drop_table("kb_member")
    op.drop_table("auth_session")
    op.drop_table("user_account")
