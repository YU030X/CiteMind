"""Markdown 上传受理用例：幂等判定与单事务写入入库事实。

语义边界（与用户确认一致）：

- 同一 KB、同一 Idempotency-Key、同内容、同标题复用已有 ``ingest_job``，返回同一组
  ``documentId``/``versionId``/``jobId``，不新建文档。
- 同一 KB、同一 key 但内容或标题任一不同返回冲突（409）；跨 KB 因去重键含 KB 作用域而
  互不影响，也不泄露其他 KB 的 key 使用情况。
- 不同 key、相同内容创建新的 ``document``，但复用 KB 范围内的私有 blob；这不是
  “同文档重复上传”，而是两份独立文档共享同一份内容寻址文件。
- 并发下唯一去重键冲突走回滚后重读，再按上述复用/冲突规则处理。
- 新上传在写事务前先用一次只读 SELECT 预检同 ``config_hash`` 的既有 profile 行：字段被篡改
  时在 publish blob 前 fail closed；没有行则继续，再由写事务内幂等登记/复用全局默认
  ``index_profile``，并把 ``ingest_job.profile_id`` 显式绑定到该行；该行只表示编码契约已
  登记，不代表任何文档可检索，也不回填 KB 的 ``active_index_profile_id``。既有旧任务的
  ``profile_id`` 保持 NULL，本切片不自动补绑、不重投、不处理。
- 只有 ``uq_ingest_job_dedupe_key`` 的 23505 唯一冲突才回滚重读；其它完整性错误原样重抛。

本切片不实现 dispatcher、worker 业务任务、解析、embedding、发布与检索。KB 配额没有对应
的存储字段或实体，因此这里不实现配额判定，也不新增迁移或权限。已发布的最终 blob 在事务
回滚或后续数据库异常时不删除（并发事务可能已引用），因此可能留下孤儿文件窗口；本切片不
实现 GC，也绝不删除潜在共享的最终 blob。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from rag_backend.dispatch.protocol import INGEST_REQUESTED_EVENT_TYPE
from rag_backend.ingestion.errors import DocumentTooLarge, IdempotencyConflict
from rag_backend.ingestion.parsing import MARKDOWN_PARSER_VERSION
from rag_backend.ingestion.profile_repository import (
    ensure_default_index_profile,
    precheck_default_index_profile,
)
from rag_backend.ingestion.storage import DocumentBlobStore, content_hash
from rag_backend.ingestion.validation import (
    MARKDOWN_MEDIA_TYPE,
    MAX_MARKDOWN_BYTES,
    build_dedupe_key,
    decode_markdown_content,
    normalize_idempotency_key,
    normalize_title,
)
from rag_backend.models.ingestion import IngestJob, OutboxEvent
from rag_backend.models.knowledge import Document, DocumentVersion
from rag_backend.models.profile_contract import current_keyword_analyzer_version

SOURCE_TYPE_MARKDOWN = "markdown"
DOCUMENT_LIFECYCLE_CREATED = "CREATED"
VERSION_STATUS_PENDING = "PENDING"
JOB_STATUS_QUEUED = "QUEUED"
OUTBOX_STATUS_PENDING = "PENDING"
# outbox 只携带 job 引用与协议事件类型；不含正文、凭据或可执行路径。
# 事件类型由 dispatch 协议统一定义，避免投递端与写入端各写一份字面量。
OUTBOX_EVENT_TYPE = INGEST_REQUESTED_EVENT_TYPE
# 并发去重冲突识别：只有该具名唯一约束的 23505 冲突才允许回读复用。
DEDUPE_KEY_CONSTRAINT_NAME = "uq_ingest_job_dedupe_key"
UNIQUE_VIOLATION_SQLSTATE = "23505"


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

    # 只有新路径才登记 profile；上面的幂等 SELECT 已开启一个只读事务并占用连接，这里先回滚
    # 释放连接，避免在 jieba 预热与 blob fsync 期间长时间空占连接（该事务不锁写，但连接
    # 仍被占用）。释放后并发同 key 仍由 _insert_upload 的唯一冲突回滚重读处理。
    await session.rollback()

    # 预热并校验默认 profile 的关键词分析器：default_index_profile() 会按需构造 jieba，同步
    # 读取约 5MB 基础词典并做私有临时目录 IO（约 0.6s）。放进线程池避免阻塞事件循环，且置于
    # publish 之前——失败时不会留下新 blob 或 DB 行。lru_cache 使后续
    # precheck_default_index_profile（只读短事务）与后续写事务里的
    # ensure_default_index_profile 都可快速返回，不再阻塞事件循环。
    await run_in_threadpool(current_keyword_analyzer_version)

    # publish 之前先用一次只读 SELECT 预检默认契约的既有行：同 config_hash 的旧行字段不一致
    # 时在发布 blob 前 fail closed，避免该内容反复成为唯一孤儿。预检 SELECT 会开启只读事务并
    # 占用连接，finally 里的 rollback 立即释放，避免随后 publish 的 fsync 期间空占连接；
    # 幂等登记/复用仍由写事务内的 ensure_default_index_profile 权威完成。
    try:
        await precheck_default_index_profile(session)
    finally:
        await session.rollback()

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
    """写入 profile 与四张表并提交；唯一去重键并发冲突时回滚后重读。

    事务归属是 ``session``：本函数在同一个事务里先登记/复用全局默认 ``index_profile``，再按
    外键依赖写入四张表，最后一次 ``commit``。``ensure_default_index_profile`` 不提交也不
    回滚，profile 行与入库事实同生共死；profile 行存在只代表契约已登记，**不代表任何 KB 可
    检索**，也不回填 ``knowledge_base.active_index_profile_id``。

    **异常范围**：``ensure_default_index_profile`` 单独放在唯一去重键重读的 ``try`` 之外（仍在
    同一事务内），因此它抛出的错误不会被误判为去重冲突。只有四表写入因
    ``uq_ingest_job_dedupe_key`` 的唯一冲突（SQLSTATE ``23505``）失败时才回滚重读；其它完整性
    错误原样重抛为静态 500，不复用现有 job。

    ``ingest_job.next_run_at`` 与 ``outbox_event.next_send_at`` 不传值，由数据库
    ``now()`` server_default 提供，避免应用时钟与 DB 时钟不一致导致投递后立即被补偿。
    """

    document_id = uuid.uuid4()
    version_id = uuid.uuid4()
    job_id = uuid.uuid4()
    event_id = uuid.uuid4()
    # 先登记/复用 profile：它使用 ``session.no_autoflush``，不会替调用方刷写待写对象；失败
    # （哈希不一致或 PG 错误）直接抛出，不产生部分写入。它不在下面的去重冲突 ``try`` 内，
    # 因此其异常（即使同为 IntegrityError）不会被误读为去重冲突。
    profile_id = await ensure_default_index_profile(session)
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
                profile_id=profile_id,
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
                lease_owner=None,
                lease_token=None,
                lease_until=None,
                sent_at=None,
            )
        )
        await session.commit()
    except IntegrityError as error:
        # 回滚必须先行：只把 ``uq_ingest_job_dedupe_key`` 的 23505 冲突当作并发去重信号，
        # 回读并按复用/冲突规则处理；其它完整性错误原样重抛为静态 500，不误读现有 job。
        # 已发布的最终 blob 不删除：并发其他事务可能已引用它，孤儿窗口留待 GC。
        await session.rollback()
        if not _is_dedupe_key_conflict(error):
            raise
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


def _is_dedupe_key_conflict(error: IntegrityError) -> bool:
    """只在 SQLSTATE ``23505`` 且约束名为 ``uq_ingest_job_dedupe_key`` 时判为去重冲突。

    其它唯一约束、外键或非 23505 的完整性错误都返回 False，由调用方原样重抛，不得回读
    现有 job。``orig`` 不是 psycopg 错误（缺 ``sqlstate`` / ``diag``）时同样返回 False。
    """

    original = error.orig
    if getattr(original, "sqlstate", None) != UNIQUE_VIOLATION_SQLSTATE:
        return False
    diag = getattr(original, "diag", None)
    return getattr(diag, "constraint_name", None) == DEDUPE_KEY_CONSTRAINT_NAME


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
