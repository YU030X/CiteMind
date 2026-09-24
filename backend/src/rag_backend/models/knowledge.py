"""知识库、文档与文档版本模型（第一切片）。

第一切片不包含 ``document_acl``；``document`` 暂不建 ``acl_mode``。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
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


class KnowledgeBase(CreatedAtMixin, UpdatedAtMixin, Base):
    """知识库；``organization_id`` 暂不建立组织外键。"""

    __tablename__ = "knowledge_base"
    __table_args__ = (
        CheckConstraint("kb_revision >= 0", name="kb_revision_non_negative"),
        CheckConstraint("acl_revision >= 0", name="acl_revision_non_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    active_index_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("index_profile.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=True,
    )
    kb_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    acl_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )


class Document(CreatedAtMixin, UpdatedAtMixin, Base):
    """文档；``active_version_id`` 指向当前服务版本，删除版本时置空。"""

    __tablename__ = "document"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('markdown', 'pdf')", name="source_type"
        ),
        CheckConstraint(
            "lifecycle_status IN ('CREATED', 'INDEXING', 'READY', 'FAILED', 'DELETED')",
            name="lifecycle_status",
        ),
        Index("ix_document_kb_id_lifecycle_status", "kb_id", "lifecycle_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    kb_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("knowledge_base.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "document_version.id",
            name="fk_document_active_version_id_document_version",
            ondelete="SET NULL",
            onupdate="RESTRICT",
            use_alter=True,
        ),
        nullable=True,
    )
    lifecycle_status: Mapped[str] = mapped_column(Text, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class DocumentVersion(CreatedAtMixin, UpdatedAtMixin, Base):
    """不可变原文件的版本记录；``(document_id, version_no)`` 唯一。"""

    __tablename__ = "document_version"
    __table_args__ = (
        CheckConstraint("version_no > 0", name="version_no_positive"),
        CheckConstraint(
            "status IN ('PENDING', 'READY', 'FAILED', 'NEEDS_OCR')", name="status"
        ),
        UniqueConstraint(
            "document_id",
            "version_no",
            name="uq_document_version_document_id_version_no",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("document.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    file_ref: Mapped[str] = mapped_column(Text, nullable=False)
    file_hash: Mapped[str] = mapped_column(Text, nullable=False)
    mime: Mapped[str] = mapped_column(Text, nullable=False)
    parser_version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
