"""chunk 与 chunk_embedding 模型（第二切片）。

``chunk`` 是不可变的检索单元，来源字段（``organization_id``、``kb_id``、
``document_id``、``version_id``、``generation_id``）在写入时固定；默认关闭的真实入库
管线在暂存事务中写入 chunk 与向量，发布前核对两者数量一致并校验 512 维，但数据库本身
不强制这些来源字段与 chunk 所有权一致。``chunk_embedding`` 每个 chunk 一条
固定 512 维向量。两者都视为不可变：worker 只有 SELECT+INSERT，没有 UPDATE/DELETE。
"""

import uuid
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from rag_backend.models.base import Base, CreatedAtMixin


class Chunk(CreatedAtMixin, Base):
    """已切分的检索单元；``(generation_id, chunk_index)`` 唯一。"""

    __tablename__ = "chunk"
    __table_args__ = (
        CheckConstraint("chunk_index >= 0", name="chunk_index_non_negative"),
        CheckConstraint("btrim(text) <> ''", name="text_non_empty"),
        CheckConstraint("token_count >= 0", name="token_count_non_negative"),
        UniqueConstraint(
            "generation_id", "chunk_index", name="uq_chunk_generation_id_chunk_index"
        ),
        Index("ix_chunk_generation_id", "generation_id"),
        Index("ix_chunk_model_input_hash", "model_input_hash"),
        Index("ix_chunk_fts", "fts", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("index_generation.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    kb_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("knowledge_base.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("document.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("document_version.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_hash: Mapped[str] = mapped_column(Text, nullable=False)
    model_input_hash: Mapped[str] = mapped_column(Text, nullable=False)
    parser_version: Mapped[str] = mapped_column(Text, nullable=False)
    chunker_version: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    heading_path: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    source_locator: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    fts: Mapped[str] = mapped_column(TSVECTOR, nullable=False)


class ChunkEmbedding(Base):
    """每个 chunk 一条固定 512 维向量；MVP 以 ``chunk_id`` 作为主键。"""

    __tablename__ = "chunk_embedding"

    chunk_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("chunk.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        primary_key=True,
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("index_profile.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    embedding: Mapped[Any] = mapped_column(Vector(512), nullable=False)
