"""创建第一切片业务表（不含向量）。

依次创建 ``index_profile``、``knowledge_base``、``document``、``document_version``、
``ingest_job`` 与 ``outbox_event``。``document.active_version_id`` 在
``document_version`` 建表后再补外键，避免建表顺序循环。所有表由迁移账号创建，
并逐表收回 PUBLIC 权限、只给运行角色显式 DML 授权；不授权 DELETE/TRUNCATE/
REFERENCES/TRIGGER，也不使用 PUBLIC、ALTER DEFAULT PRIVILEGES 或 schema 级授权。

Revision ID: 20260922_0002
Revises: 20260921_0001
Create Date: 2026-09-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260922_0002"
down_revision: str | None = "20260921_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

API_ROLE = "citemind_api"
WORKER_ROLE = "citemind_worker"

LEASE_CONSISTENCY_SQL = (
    "(lease_owner IS NULL AND lease_token IS NULL AND lease_until IS NULL) "
    "OR (lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_until IS NOT NULL)"
)

# 迁移通过 target_metadata 继承共享 naming convention，CHECK 只写短名，由 convention
# 补出 ck_<table>_<short>。主键/唯一/外键 convention 不含 %(constraint_name)s 令牌，
# 显式全名按原样使用，不能写成短名。


def upgrade() -> None:
    op.create_table(
        "index_profile",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("embedding_model", sa.Text(), nullable=False),
        sa.Column("model_revision", sa.Text(), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("normalize", sa.Boolean(), nullable=False),
        sa.Column("tokenizer_revision", sa.Text(), nullable=False),
        sa.Column("chunker_version", sa.Text(), nullable=False),
        sa.Column("keyword_analyzer_version", sa.Text(), nullable=False),
        sa.Column("config_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("dimension = 512", name="dimension_is_512"),
        sa.PrimaryKeyConstraint("id", name="pk_index_profile"),
        sa.UniqueConstraint("config_hash", name="uq_index_profile_config_hash"),
    )

    op.create_table(
        "knowledge_base",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("active_index_profile_id", sa.Uuid(), nullable=True),
        sa.Column("kb_revision", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("acl_revision", sa.Integer(), server_default=sa.text("0"), nullable=False),
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
        sa.CheckConstraint(
            "kb_revision >= 0", name="kb_revision_non_negative"
        ),
        sa.CheckConstraint(
            "acl_revision >= 0", name="acl_revision_non_negative"
        ),
        sa.ForeignKeyConstraint(
            ["active_index_profile_id"],
            ["index_profile.id"],
            name="fk_knowledge_base_active_index_profile_id_index_profile",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_base"),
    )

    op.create_table(
        "document",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kb_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("active_version_id", sa.Uuid(), nullable=True),
        sa.Column("lifecycle_status", sa.Text(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("source_type IN ('markdown', 'pdf')", name="source_type"),
        sa.CheckConstraint(
            "lifecycle_status IN ('CREATED', 'INDEXING', 'READY', 'FAILED', 'DELETED')",
            name="lifecycle_status",
        ),
        sa.ForeignKeyConstraint(
            ["kb_id"],
            ["knowledge_base.id"],
            name="fk_document_kb_id_knowledge_base",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document"),
    )
    op.create_index(
        "ix_document_kb_id_lifecycle_status", "document", ["kb_id", "lifecycle_status"]
    )

    op.create_table(
        "document_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("file_ref", sa.Text(), nullable=False),
        sa.Column("file_hash", sa.Text(), nullable=False),
        sa.Column("mime", sa.Text(), nullable=False),
        sa.Column("parser_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
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
        sa.CheckConstraint("version_no > 0", name="version_no_positive"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'READY', 'FAILED', 'NEEDS_OCR')",
            name="status",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["document.id"],
            name="fk_document_version_document_id_document",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_version"),
        sa.UniqueConstraint(
            "document_id",
            "version_no",
            name="uq_document_version_document_id_version_no",
        ),
    )

    op.create_foreign_key(
        "fk_document_active_version_id_document_version",
        "document",
        "document_version",
        ["active_version_id"],
        ["id"],
        ondelete="SET NULL",
        onupdate="RESTRICT",
    )

    op.create_table(
        "ingest_job",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_token", sa.Text(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "next_run_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
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
        sa.CheckConstraint(
            "status IN ('QUEUED', 'PARSING', 'CHUNKING', 'EMBEDDING', "
            "'INDEXING', 'READY', 'FAILED', 'CANCELLED')",
            name="status",
        ),
        sa.CheckConstraint("attempt >= 0", name="attempt_non_negative"),
        sa.CheckConstraint(LEASE_CONSISTENCY_SQL, name="lease_consistent"),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["document.id"],
            name="fk_ingest_job_document_id_document",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["document_version.id"],
            name="fk_ingest_job_version_id_document_version",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ingest_job"),
        sa.UniqueConstraint("dedupe_key", name="uq_ingest_job_dedupe_key"),
    )
    op.create_index(
        "ix_ingest_job_status_next_run_at", "ingest_job", ["status", "next_run_at"]
    )

    op.create_table(
        "outbox_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("dispatch_attempt", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "next_send_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_token", sa.Text(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "status IN ('PENDING', 'SENT', 'FAILED')", name="status"
        ),
        sa.CheckConstraint(
            "dispatch_attempt >= 0", name="dispatch_attempt_non_negative"
        ),
        sa.CheckConstraint(LEASE_CONSISTENCY_SQL, name="lease_consistent"),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["ingest_job.id"],
            name="fk_outbox_event_job_id_ingest_job",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_event"),
    )
    op.create_index(
        "ix_outbox_event_status_next_send_at", "outbox_event", ["status", "next_send_at"]
    )

    # 逐表收回 PUBLIC 权限并显式授权；不使用 ALTER DEFAULT PRIVILEGES 或 schema 级授权。
    op.execute("REVOKE ALL ON TABLE index_profile FROM PUBLIC")
    op.execute(f"GRANT SELECT, INSERT ON TABLE index_profile TO {API_ROLE}")
    op.execute(f"GRANT SELECT ON TABLE index_profile TO {WORKER_ROLE}")

    op.execute("REVOKE ALL ON TABLE knowledge_base FROM PUBLIC")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE knowledge_base TO {API_ROLE}")
    op.execute(f"GRANT SELECT ON TABLE knowledge_base TO {WORKER_ROLE}")

    op.execute("REVOKE ALL ON TABLE document FROM PUBLIC")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE document TO {API_ROLE}")
    op.execute(f"GRANT SELECT, UPDATE ON TABLE document TO {WORKER_ROLE}")

    op.execute("REVOKE ALL ON TABLE document_version FROM PUBLIC")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE document_version TO {API_ROLE}")
    op.execute(f"GRANT SELECT, UPDATE ON TABLE document_version TO {WORKER_ROLE}")

    op.execute("REVOKE ALL ON TABLE ingest_job FROM PUBLIC")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE ingest_job TO {API_ROLE}")
    op.execute(f"GRANT SELECT, UPDATE ON TABLE ingest_job TO {WORKER_ROLE}")

    op.execute("REVOKE ALL ON TABLE outbox_event FROM PUBLIC")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE outbox_event TO {API_ROLE}")


def downgrade() -> None:
    op.drop_index("ix_outbox_event_status_next_send_at", table_name="outbox_event")
    op.drop_table("outbox_event")

    op.drop_index("ix_ingest_job_status_next_run_at", table_name="ingest_job")
    op.drop_table("ingest_job")

    op.drop_constraint(
        "fk_document_active_version_id_document_version", "document", type_="foreignkey"
    )
    op.drop_index("ix_document_kb_id_lifecycle_status", table_name="document")
    op.drop_table("document_version")
    op.drop_table("document")
    op.drop_table("knowledge_base")
    op.drop_table("index_profile")
