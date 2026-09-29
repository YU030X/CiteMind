"""知识库、文档、文档版本与文档读取允许名单模型。

第一切片不包含 ``document_acl``；文档 ACL 切片后 ``document`` 增加 ``acl_mode``，
并新增 ``document_acl``。``acl_mode`` 只收紧读取：``INHERIT`` 沿用 KB 成员读权限，
``RESTRICTED`` 只允许 ``document_acl`` 中显式登记的用户读取。
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
    """文档；``active_version_id`` 指向当前服务版本，删除版本时置空。

    ``acl_mode`` 新上传默认 ``INHERIT``；``RESTRICTED`` 时读取只允许 ``document_acl``
    显式登记且仍是有效 KB 成员的用户，空名单连 OWNER 也不能读。
    """

    __tablename__ = "document"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('markdown', 'pdf', 'docx', 'web')", name="source_type"
        ),
        CheckConstraint(
            "lifecycle_status IN ('CREATED', 'INDEXING', 'READY', 'FAILED', 'DELETED')",
            name="lifecycle_status",
        ),
        CheckConstraint(
            "acl_mode IN ('INHERIT', 'RESTRICTED')", name="acl_mode"
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
    acl_mode: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'INHERIT'")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class DocumentAcl(CreatedAtMixin, Base):
    """文档读取允许名单；首片只有 ``USER``/``READ``，只收紧读取、不授予管理权。

    行不可变（运行角色没有 UPDATE），全量替换按 ``document_id`` 删除后重新插入。
    ``principal_id`` 外键指向 ``user_account``；它是否仍是同组织活跃 KB 成员由写入
    事务在服务端核对，数据库不强制组织一致。
    """

    __tablename__ = "document_acl"
    __table_args__ = (
        CheckConstraint("principal_type IN ('USER')", name="principal_type"),
        CheckConstraint("permission IN ('READ')", name="permission"),
        UniqueConstraint(
            "document_id",
            "principal_type",
            "principal_id",
            "permission",
            name="uq_document_acl_document_principal_permission",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("document.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    principal_type: Mapped[str] = mapped_column(Text, nullable=False)
    principal_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("user_account.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    permission: Mapped[str] = mapped_column(Text, nullable=False)


class DocumentVersion(CreatedAtMixin, UpdatedAtMixin, Base):
    """不可变原文件的版本记录；``(document_id, version_no)`` 唯一。

    网页来源额外记录抓取事实：``source_url`` 是服务端规范化后的请求 URL，``final_url`` 是
    跟随重定向后的最终 URL，``fetched_at`` 是抓取时刻；非网页来源这三列保持 NULL。
    """

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
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
