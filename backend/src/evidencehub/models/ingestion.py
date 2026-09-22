"""入库任务与 outbox 事件模型。

``ingest_job`` 在第二切片新增可空的 ``generation_id``；租约由
owner/token/until 三列共同表达，三者必须同时为空或同时非空。
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
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from evidencehub.models.base import Base, CreatedAtMixin, UpdatedAtMixin

LEASE_CONSISTENCY_SQL = (
    "(lease_owner IS NULL AND lease_token IS NULL AND lease_until IS NULL) "
    "OR (lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_until IS NOT NULL)"
)


class IngestJob(CreatedAtMixin, UpdatedAtMixin, Base):
    """PostgreSQL 中的入库任务事实；Celery 只承载投递，不承载状态。"""

    __tablename__ = "ingest_job"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED', 'PARSING', 'CHUNKING', 'EMBEDDING', "
            "'INDEXING', 'READY', 'FAILED', 'CANCELLED')",
            name="status",
        ),
        CheckConstraint("attempt >= 0", name="attempt_non_negative"),
        CheckConstraint(LEASE_CONSISTENCY_SQL, name="lease_consistent"),
        UniqueConstraint("dedupe_key", name="uq_ingest_job_dedupe_key"),
        Index("ix_ingest_job_status_next_run_at", "status", "next_run_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
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
    generation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("index_generation.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    lease_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)


class OutboxEvent(CreatedAtMixin, UpdatedAtMixin, Base):
    """与 ``ingest_job`` 同事务写入的投递事件；``job_id`` 不唯一。"""

    __tablename__ = "outbox_event"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'SENT', 'FAILED')", name="status"
        ),
        CheckConstraint(
            "dispatch_attempt >= 0", name="dispatch_attempt_non_negative"
        ),
        CheckConstraint(LEASE_CONSISTENCY_SQL, name="lease_consistent"),
        Index("ix_outbox_event_status_next_send_at", "status", "next_send_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("ingest_job.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    dispatch_attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    next_send_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
