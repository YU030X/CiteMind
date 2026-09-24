"""索引 profile 与 generation 模型。

``index_profile`` 描述 embedding 模型、分词与切分的完整编码契约；
``index_generation`` 记录某个文档版本在某个 profile 下的一次构建。``chunk`` 与
``chunk_embedding`` 在同一模型的相邻模块中定义。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from rag_backend.models.base import Base, CreatedAtMixin, UpdatedAtMixin


class IndexProfile(CreatedAtMixin, Base):
    """不可变的编码契约；只允许插入，不允许更新或删除。"""

    __tablename__ = "index_profile"
    __table_args__ = (
        CheckConstraint("dimension = 512", name="dimension_is_512"),
        UniqueConstraint("config_hash", name="uq_index_profile_config_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    embedding_model: Mapped[str] = mapped_column(Text, nullable=False)
    model_revision: Mapped[str] = mapped_column(Text, nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    normalize: Mapped[bool] = mapped_column(Boolean, nullable=False)
    tokenizer_revision: Mapped[str] = mapped_column(Text, nullable=False)
    chunker_version: Mapped[str] = mapped_column(Text, nullable=False)
    keyword_analyzer_version: Mapped[str] = mapped_column(Text, nullable=False)
    config_hash: Mapped[str] = mapped_column(Text, nullable=False)


class IndexGeneration(CreatedAtMixin, UpdatedAtMixin, Base):
    """文档版本在某个 profile 下的一次构建；READY 表示可发布。

    同一 ``(version_id, profile_id)`` 最多一条 READY generation，由部分唯一索引保证；
    BUILDING、FAILED 与 RETIRED 可以并存。
    """

    __tablename__ = "index_generation"
    __table_args__ = (
        CheckConstraint(
            "status IN ('BUILDING', 'READY', 'RETIRED', 'FAILED')", name="status"
        ),
        CheckConstraint(
            "expected_chunks >= 0", name="expected_chunks_non_negative"
        ),
        CheckConstraint("actual_chunks >= 0", name="actual_chunks_non_negative"),
        CheckConstraint(
            "actual_chunks <= expected_chunks", name="actual_chunks_within_expected"
        ),
        Index(
            "ix_index_generation_version_id_profile_id_status",
            "version_id",
            "profile_id",
            "status",
        ),
        Index(
            "uq_index_generation_version_id_profile_id_ready",
            "version_id",
            "profile_id",
            unique=True,
            postgresql_where=text("status = 'READY'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("document_version.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("index_profile.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    expected_chunks: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    actual_chunks: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    ready_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
