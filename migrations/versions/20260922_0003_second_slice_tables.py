"""创建第二切片业务表并给 ingest_job 补 generation_id。

依次创建 ``index_generation``、``chunk`` 与 ``chunk_embedding``，并给 ``ingest_job``
增加可空的 ``generation_id`` 外键。所有表由迁移账号创建，并逐表收回 PUBLIC 权限、
只给运行角色显式 DML 授权；不授权 DELETE/TRUNCATE/REFERENCES/TRIGGER，也不使用
PUBLIC、ALTER DEFAULT PRIVILEGES 或 schema 级授权。``chunk_embedding.embedding``
固定为 VECTOR(512)，本迁移不建 ANN 索引，也不引入触发器。

Revision ID: 20260922_0003
Revises: 20260922_0002
Create Date: 2026-09-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

revision: str = "20260922_0003"
down_revision: str | None = "20260922_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

API_ROLE = "citemind_api"
WORKER_ROLE = "citemind_worker"


def upgrade() -> None:
    op.create_table(
        "index_generation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "expected_chunks", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "actual_chunks", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('BUILDING', 'READY', 'RETIRED', 'FAILED')", name="status"
        ),
        sa.CheckConstraint(
            "expected_chunks >= 0", name="expected_chunks_non_negative"
        ),
        sa.CheckConstraint("actual_chunks >= 0", name="actual_chunks_non_negative"),
        sa.CheckConstraint(
            "actual_chunks <= expected_chunks", name="actual_chunks_within_expected"
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["document_version.id"],
            name="fk_index_generation_version_id_document_version",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["index_profile.id"],
            name="fk_index_generation_profile_id_index_profile",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_index_generation"),
    )
    op.create_index(
        "ix_index_generation_version_id_profile_id_status",
        "index_generation",
        ["version_id", "profile_id", "status"],
    )
    # 同 version/profile 最多一个有效 READY generation 的并发发布约束。
    op.create_index(
        "uq_index_generation_version_id_profile_id_ready",
        "index_generation",
        ["version_id", "profile_id"],
        unique=True,
        postgresql_where=sa.text("status = 'READY'"),
    )

    op.add_column("ingest_job", sa.Column("generation_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_ingest_job_generation_id_index_generation",
        "ingest_job",
        "index_generation",
        ["generation_id"],
        ["id"],
        ondelete="RESTRICT",
        onupdate="RESTRICT",
    )

    op.create_table(
        "chunk",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("kb_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.Text(), nullable=False),
        sa.Column("model_input_hash", sa.Text(), nullable=False),
        sa.Column("parser_version", sa.Text(), nullable=False),
        sa.Column("chunker_version", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("heading_path", JSONB(), nullable=False),
        sa.Column("source_locator", JSONB(), nullable=False),
        sa.Column("fts", TSVECTOR(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("chunk_index >= 0", name="chunk_index_non_negative"),
        sa.CheckConstraint("btrim(text) <> ''", name="text_non_empty"),
        sa.CheckConstraint("token_count >= 0", name="token_count_non_negative"),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["index_generation.id"],
            name="fk_chunk_generation_id_index_generation",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["kb_id"],
            ["knowledge_base.id"],
            name="fk_chunk_kb_id_knowledge_base",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["document.id"],
            name="fk_chunk_document_id_document",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["document_version.id"],
            name="fk_chunk_version_id_document_version",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chunk"),
        sa.UniqueConstraint(
            "generation_id", "chunk_index", name="uq_chunk_generation_id_chunk_index"
        ),
    )
    op.create_index("ix_chunk_generation_id", "chunk", ["generation_id"])
    op.create_index("ix_chunk_fts", "chunk", ["fts"], postgresql_using="gin")

    op.create_table(
        "chunk_embedding",
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("embedding", Vector(512), nullable=False),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["chunk.id"],
            name="fk_chunk_embedding_chunk_id_chunk",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["index_profile.id"],
            name="fk_chunk_embedding_profile_id_index_profile",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("chunk_id", name="pk_chunk_embedding"),
    )

    # 逐表收回 PUBLIC 权限并显式授权；chunk 与 chunk_embedding 保持不可变（无 UPDATE）。
    op.execute("REVOKE ALL ON TABLE index_generation FROM PUBLIC")
    op.execute(f"GRANT SELECT ON TABLE index_generation TO {API_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE index_generation TO {WORKER_ROLE}")

    op.execute("REVOKE ALL ON TABLE chunk FROM PUBLIC")
    op.execute(f"GRANT SELECT ON TABLE chunk TO {API_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE chunk TO {WORKER_ROLE}")

    op.execute("REVOKE ALL ON TABLE chunk_embedding FROM PUBLIC")
    op.execute(f"GRANT SELECT ON TABLE chunk_embedding TO {API_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE chunk_embedding TO {WORKER_ROLE}")


def downgrade() -> None:
    op.drop_constraint(
        "fk_ingest_job_generation_id_index_generation", "ingest_job", type_="foreignkey"
    )
    op.drop_column("ingest_job", "generation_id")

    op.drop_index("ix_chunk_fts", table_name="chunk")
    op.drop_index("ix_chunk_generation_id", table_name="chunk")
    op.drop_table("chunk_embedding")
    op.drop_table("chunk")

    op.drop_index(
        "uq_index_generation_version_id_profile_id_ready", table_name="index_generation"
    )
    op.drop_index(
        "ix_index_generation_version_id_profile_id_status", table_name="index_generation"
    )
    op.drop_table("index_generation")
