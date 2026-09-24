"""Markdown 上传受理用例：幂等判定与单事务写入入库事实。

语义边界（与用户确认一致）：

- 同一 KB、同一 Idempotency-Key、同内容、同标题复用已有 ``ingest_job``，返回同一组
  ``documentId``/``versionId``/``jobId``，不新建文档。
- 同一 KB、同一 key 但内容或标题任一不同返回冲突（409）；跨 KB 因去重键含 KB 作用域而
  互不影响，也不泄露其他 KB 的 key 使用情况。
- 不同 key、相同内容创建新的 ``document``，但复用 KB 范围内的私有 blob；这不是
  “同文档重复上传”，而是两份独立文档共享同一份内容寻址文件。
- 并发下唯一去重键冲突走回滚后重读，再按上述复用/冲突规则处理。

本切片不实现 dispatcher、worker 业务任务、解析、embedding、发布与检索。KB 配额没有对应
的存储字段或实体，因此这里不实现配额判定，也不新增迁移或权限。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from rag_backend.ingestion.errors import DocumentTooLarge, IdempotencyConflict
from rag_backend.ingestion.storage import DocumentBlobStore, content_hash
from rag_backend.ingestion.validation import (
    MARKDOWN_MEDIA_TYPE,
    MARKDOWN_PARSER_VERSION,
    MAX_MARKDOWN_BYTES,
    build_dedupe_key,
    decode_markdown_content,
    normalize_idempotency_key,
    normalize_title,
)
from rag_backend.models.ingestion import IngestJob, OutboxEvent
from rag_backend.models.knowledge import Document, DocumentVersion

SOURCE_TYPE_MARKDOWN = "markdown"
DOCUMENT_LIFECYCLE_CREATED = "CREATED"
VERSION_STATUS_PENDING = "PENDING"
JOB_STATUS_QUEUED = "QUEUED"
OUTBOX_STATUS_PENDING = "PENDING"
# outbox 只携带 job 引用与协议事件类型；不含正文、凭据或可执行路径。
OUTBOX_EVENT_TYPE = "ingest.requested"


@dataclass(frozen=True)
class UploadOutcome:
    """一次上传受理的结果；``reused`` 只用于测试与日志判定，不进入外部响应体。"""

    document_id: uuid.UUID
    version_id: uuid.UUID
    job_id: uuid.UUID
    reused: bool


@dataclass(frozen=True)
class _ExistingJob:
    job_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    title: str
    file_hash: str


async def create_markdown_document(
    session: AsyncSession,
    store: DocumentBlobStore,
    *,
    kb_id: uuid.UUID,
    organization_id: uuid.UUID,
    title: str,
    content: bytes,
    idempotency_key: str,
) -> UploadOutcome:
    """校验输入、保存原文件并在单个事务中写入四张表。

    文件名后缀已在路由层校验，本函数只接收正文；原始文件名与 Idempotency-Key 原值都不
    落库，存储路径只由 KB ID 与内容摘要派生。
    """

    normalized_title = normalize_title(title)
    normalized_key = normalize_idempotency_key(idempotency_key)
    if len(content) > MAX_MARKDOWN_BYTES:
        raise DocumentTooLarge("上传内容超过单文件字节上限")
    decode_markdown_content(content)

    file_hash = content_hash(content)
    dedupe_key = build_dedupe_key(organization_id, kb_id, normalized_key)

    existing = await _load_existing_job(session, dedupe_key=dedupe_key, kb_id=kb_id)
    if existing is not None:
        return _reuse_or_conflict(existing, title=normalized_title, file_hash=file_hash)

    # publish 内部有 fsync 与 os.replace，是阻塞文件 I/O；放进线程池避免阻塞事件循环。
    # 仍在写事务之前完成（失败不落库），publish 自身按目标文件存在与否幂等。
    file_ref = await run_in_threadpool(store.publish, kb_id, file_hash, content)
    return await _insert_upload(
        session,
        kb_id=kb_id,
        title=normalized_title,
        file_ref=file_ref,
        file_hash=file_hash,
        dedupe_key=dedupe_key,
    )


async def _insert_upload(
    session: AsyncSession,
    *,
    kb_id: uuid.UUID,
    title: str,
    file_ref: str,
    file_hash: str,
    dedupe_key: str,
) -> UploadOutcome:
    """写入四张表并提交；唯一去重键并发冲突时回滚后重读。"""

    now = datetime.now(UTC)
    document_id = uuid.uuid4()
    version_id = uuid.uuid4()
    job_id = uuid.uuid4()
    event_id = uuid.uuid4()
    try:
        # 无 ORM 关系时插入顺序不保证，按外键依赖逐条 flush。
        session.add(
            Document(
                id=document_id,
                kb_id=kb_id,
                title=title,
                source_type=SOURCE_TYPE_MARKDOWN,
                active_version_id=None,
                lifecycle_status=DOCUMENT_LIFECYCLE_CREATED,
                deleted_at=None,
            )
        )
        await session.flush()
        session.add(
            DocumentVersion(
                id=version_id,
                document_id=document_id,
                version_no=1,
                file_ref=file_ref,
                file_hash=file_hash,
                mime=MARKDOWN_MEDIA_TYPE,
                parser_version=MARKDOWN_PARSER_VERSION,
                status=VERSION_STATUS_PENDING,
            )
        )
        await session.flush()
        session.add(
            IngestJob(
                id=job_id,
                document_id=document_id,
                version_id=version_id,
                generation_id=None,
                status=JOB_STATUS_QUEUED,
                attempt=0,
                lease_owner=None,
                lease_token=None,
                lease_until=None,
                heartbeat_at=None,
                next_run_at=now,
                dedupe_key=dedupe_key,
                error_code=None,
            )
        )
        await session.flush()
        session.add(
            OutboxEvent(
                id=event_id,
                job_id=job_id,
                event_type=OUTBOX_EVENT_TYPE,
                status=OUTBOX_STATUS_PENDING,
                dispatch_attempt=0,
                next_send_at=now,
                lease_owner=None,
                lease_token=None,
                lease_until=None,
                sent_at=None,
            )
        )
        await session.commit()
    except IntegrityError:
        # 同一去重键的并发插入由唯一约束拒绝；回滚后重读并按复用/冲突规则处理。
        # 已发布的最终 blob 不删除：并发其他事务可能已引用它，孤儿窗口留待 GC。
        await session.rollback()
        existing = await _load_existing_job(session, dedupe_key=dedupe_key, kb_id=kb_id)
        if existing is None:
            raise
        return _reuse_or_conflict(existing, title=title, file_hash=file_hash)
    return UploadOutcome(
        document_id=document_id,
        version_id=version_id,
        job_id=job_id,
        reused=False,
    )


async def _load_existing_job(
    session: AsyncSession, *, dedupe_key: str, kb_id: uuid.UUID
) -> _ExistingJob | None:
    """按去重键读取已有任务；join 文档限定在同一 KB，避免跨 KB 读取。"""

    statement = (
        select(
            IngestJob.id,
            IngestJob.document_id,
            IngestJob.version_id,
            Document.title,
            DocumentVersion.file_hash,
        )
        .join(DocumentVersion, DocumentVersion.id == IngestJob.version_id)
        .join(Document, Document.id == IngestJob.document_id)
        .where(IngestJob.dedupe_key == dedupe_key, Document.kb_id == kb_id)
    )
    row = (await session.execute(statement)).first()
    if row is None:
        return None
    job_id, document_id, version_id, title, file_hash = row
    return _ExistingJob(
        job_id=job_id,
        document_id=document_id,
        version_id=version_id,
        title=title,
        file_hash=file_hash,
    )


def _reuse_or_conflict(
    existing: _ExistingJob, *, title: str, file_hash: str
) -> UploadOutcome:
    """内容与标题都一致时复用；任一不同即冲突，回滚由调用方负责。"""

    if existing.title != title or existing.file_hash != file_hash:
        raise IdempotencyConflict("同一 Idempotency-Key 已用于不同的内容或标题")
    return UploadOutcome(
        document_id=existing.document_id,
        version_id=existing.version_id,
        job_id=existing.job_id,
        reused=True,
    )
