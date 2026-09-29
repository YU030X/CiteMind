"""文档上传与新版本受理、逻辑删除用例。

本模块同时承载三件事，语义边界（与用户确认一致）：

- **首次上传**：同一 KB、同一 Idempotency-Key、同内容、同标题复用已有 ``ingest_job``，返回
  同一组 ``documentId``/``versionId``/``jobId``，不新建文档。
- **文档新版本**：``document`` 行锁下分配 ``version_no = max + 1``，去重键含 ``document_id``，
  命中时校验 ``documentId``/内容摘要/标题/``expected active version``；同键同 expected 的有效回放
  返回同一新版本，其余冲突返回 409。发布前旧版本继续 READY 可检索，失败只把新版本置
  ``FAILED``，绝不下线旧版本。

幂等身份的两个字段来源固定：内容摘要取不可变 ``document_version.file_hash``，请求标题取受理
时写入的不可变 ``ingest_job.request_title``（旧任务该列为 NULL 时才回退到可变 ``document.title``，
见 :func:`_existing_from_row`）。因此新版本切换展示标题不会让旧 key 的原样重放变成 409。
- **逻辑删除**：对 ``document`` 行加锁后置 ``deleted_at``/``DELETED``、递增KB ``kb_revision``；
  把该文档所有非终态 ``ingest_job`` 置 ``CANCELLED`` 并清租约；只做 tombstone，保留共享 blob
  与版本/索引历史。

并发：同一 KB 去重键冲突走回滚后重读，再按上述复用/冲突规则处理；文档新版本在 ``document``
行锁内二次校验去重与 expected，因此同键并发只会留下一个新版本。

新上传/新版本在写事务前先用一次只读 SELECT 预检同 ``config_hash`` 的既有 profile 行：字段被
篡改时在 publish blob 前 fail closed；再由写事务内幂等登记/复用全局默认 ``index_profile``，并把
``ingest_job.profile_id`` 显式绑定到该行；该行只表示编码契约已登记，不代表任何文档可检索，也不
回填 KB 的 ``active_index_profile_id``。

本模块不实现 dispatcher、worker 业务任务、解析、embedding 与发布；发布事务在
``rag_backend.ingestion.indexing_worker``。已发布的最终 blob 在事务回滚或后续数据库异常时不删除
（并发事务可能已引用），因此可能留下孤儿文件窗口；本模块不实现 GC，也绝不删除潜在共享的最终
blob。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from rag_backend.dispatch.protocol import INGEST_REQUESTED_EVENT_TYPE
from rag_backend.ingestion.docx_parsing import DOCX_PARSER_VERSION
from rag_backend.ingestion.errors import (
    DocumentDeleted,
    DocumentNotFound,
    ExpectedVersionConflict,
    IdempotencyConflict,
    UnsupportedDocumentType,
)
from rag_backend.ingestion.parsing import MARKDOWN_PARSER_VERSION
from rag_backend.ingestion.pdf_parsing import PDF_PARSER_VERSION
from rag_backend.ingestion.profile_repository import (
    ensure_default_index_profile,
    precheck_default_index_profile,
)
from rag_backend.ingestion.storage import DocumentBlobStore, content_hash
from rag_backend.ingestion.validation import (
    DOCX_MEDIA_TYPE,
    MARKDOWN_MEDIA_TYPE,
    PDF_MEDIA_TYPE,
    build_dedupe_key,
    build_version_dedupe_key,
    build_version_dedupe_key_prefix,
    decode_markdown_content,
    normalize_idempotency_key,
    normalize_title,
    parse_version_dedupe_key,
    validate_docx_content,
    validate_pdf_content,
)
from rag_backend.models.ingestion import IngestJob, OutboxEvent
from rag_backend.models.knowledge import Document, DocumentVersion, KnowledgeBase
from rag_backend.models.profile_contract import current_keyword_analyzer_version

SOURCE_TYPE_MARKDOWN = "markdown"
SOURCE_TYPE_PDF = "pdf"
SOURCE_TYPE_DOCX = "docx"
DOCUMENT_LIFECYCLE_CREATED = "CREATED"
DOCUMENT_LIFECYCLE_DELETED = "DELETED"
VERSION_STATUS_PENDING = "PENDING"
JOB_STATUS_QUEUED = "QUEUED"
OUTBOX_STATUS_PENDING = "PENDING"
# 删除时写入非终态 job 的静态诊断码；状态本身为 ``CANCELLED``，接收壳/补偿都先看状态。
JOB_ERROR_DOCUMENT_DELETED = "DOCUMENT_DELETED"
# outbox 只携带 job 引用与协议事件类型；不含正文、凭据或可执行路径。
# 事件类型由 dispatch 协议统一定义，避免投递端与写入端各写一份字面量。
OUTBOX_EVENT_TYPE = INGEST_REQUESTED_EVENT_TYPE
# 并发去重冲突识别：只有该具名唯一约束的 23505 冲突才允许回读复用。
DEDUPE_KEY_CONSTRAINT_NAME = "uq_ingest_job_dedupe_key"
UNIQUE_VIOLATION_SQLSTATE = "23505"

# 逻辑删除：对目标文档全部非终态 job 清租约、置 CANCELLED，并把非 NULL 诊断码留给可观测性。
# 谓词限定 status 与 error_code IS NULL，不会覆盖已终结或已有诊断的行。
CANCEL_DOCUMENT_JOBS_SQL = text(
    """
    UPDATE ingest_job
    SET status = 'CANCELLED',
        lease_owner = NULL,
        lease_token = NULL,
        lease_until = NULL,
        heartbeat_at = NULL,
        error_code = :error_code,
        updated_at = clock_timestamp()
    WHERE document_id = :document_id
      AND status IN ('QUEUED', 'PARSING', 'CHUNKING', 'EMBEDDING', 'INDEXING')
      AND error_code IS NULL
    """
)


@dataclass(frozen=True)
class UploadOutcome:
    """一次上传/新版本受理的结果；``reused`` 只用于测试与日志判定，不进入外部响应体。"""

    document_id: uuid.UUID
    version_id: uuid.UUID
    job_id: uuid.UUID
    reused: bool


@dataclass(frozen=True)
class DeleteOutcome:
    """一次逻辑删除的结果；``already_deleted`` 表示二次删除的幂等分支。"""

    document_id: uuid.UUID
    already_deleted: bool


@dataclass(frozen=True)
class _ExistingJob:
    job_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    title: str
    file_hash: str
    document_deleted: bool
    expected_active_version_id: uuid.UUID | None


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
    """校验 Markdown 输入、保存原文件并在单个事务中写入四张表。

    文件名后缀已在路由层校验，本函数只接收正文；原始文件名与 Idempotency-Key 原值都不
    落库，存储路径只由 KB ID 与内容摘要派生。
    """

    decode_markdown_content(content)
    return await _create_document(
        session,
        store,
        kb_id=kb_id,
        organization_id=organization_id,
        title=title,
        content=content,
        idempotency_key=idempotency_key,
        source_type=SOURCE_TYPE_MARKDOWN,
        media_type=MARKDOWN_MEDIA_TYPE,
        parser_version=MARKDOWN_PARSER_VERSION,
    )


async def create_pdf_document(
    session: AsyncSession,
    store: DocumentBlobStore,
    *,
    kb_id: uuid.UUID,
    organization_id: uuid.UUID,
    title: str,
    content: bytes,
    idempotency_key: str,
) -> UploadOutcome:
    """受理文本 PDF：只做二进制形状校验，保存原文件并登记 PDF 真实解析器版本。

    页数、加密与结构损坏属于解析期判定，由 worker 子进程在写 blob 之后静态落库；本函数
    与 Markdown 路径共享幂等/去重/默认 profile 登记逻辑。
    """

    validate_pdf_content(content)
    return await _create_document(
        session,
        store,
        kb_id=kb_id,
        organization_id=organization_id,
        title=title,
        content=content,
        idempotency_key=idempotency_key,
        source_type=SOURCE_TYPE_PDF,
        media_type=PDF_MEDIA_TYPE,
        parser_version=PDF_PARSER_VERSION,
    )


async def create_docx_document(
    session: AsyncSession,
    store: DocumentBlobStore,
    *,
    kb_id: uuid.UUID,
    organization_id: uuid.UUID,
    title: str,
    content: bytes,
    idempotency_key: str,
) -> UploadOutcome:
    """受理 DOCX：标准库 ZIP 元数据校验后保存原文件并登记 DOCX 真实解析器版本。

    嵌套表、实体声明、CRC 与实际解压总量属于解析期判定，由 worker 子进程在写 blob 之后
    静态落库；本函数与 Markdown/PDF 路径共享幂等/去重/默认 profile 登记逻辑。
    """

    validate_docx_content(content)
    return await _create_document(
        session,
        store,
        kb_id=kb_id,
        organization_id=organization_id,
        title=title,
        content=content,
        idempotency_key=idempotency_key,
        source_type=SOURCE_TYPE_DOCX,
        media_type=DOCX_MEDIA_TYPE,
        parser_version=DOCX_PARSER_VERSION,
    )


async def _create_document(
    session: AsyncSession,
    store: DocumentBlobStore,
    *,
    kb_id: uuid.UUID,
    organization_id: uuid.UUID,
    title: str,
    content: bytes,
    idempotency_key: str,
    source_type: str,
    media_type: str,
    parser_version: str,
) -> UploadOutcome:
    """Markdown/PDF/DOCX 共用的入库受理：幂等判定、profile 登记、blob 发布与四表事务。"""

    normalized_title = normalize_title(title)
    normalized_key = normalize_idempotency_key(idempotency_key)
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
        source_type=source_type,
        media_type=media_type,
        parser_version=parser_version,
    )


async def _insert_upload(
    session: AsyncSession,
    *,
    kb_id: uuid.UUID,
    title: str,
    file_ref: str,
    file_hash: str,
    dedupe_key: str,
    source_type: str,
    media_type: str,
    parser_version: str,
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
                source_type=source_type,
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
                mime=media_type,
                parser_version=parser_version,
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
                request_title=title,
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
    """按首次上传的去重键读取已有任务；join 文档限定在同一 KB，避免跨 KB 读取。"""

    statement = (
        select(
            IngestJob.id,
            IngestJob.document_id,
            IngestJob.version_id,
            IngestJob.dedupe_key,
            IngestJob.request_title,
            Document.title,
            Document.deleted_at,
            DocumentVersion.file_hash,
        )
        .join(DocumentVersion, DocumentVersion.id == IngestJob.version_id)
        .join(Document, Document.id == IngestJob.document_id)
        .where(IngestJob.dedupe_key == dedupe_key, Document.kb_id == kb_id)
    )
    row = (await session.execute(statement)).first()
    if row is None:
        return None
    return _existing_from_row(row)


async def _load_existing_version_job(
    session: AsyncSession,
    *,
    kb_id: uuid.UUID,
    document_id: uuid.UUID,
    key_prefix: str,
) -> _ExistingJob | None:
    """按「文档新版本」去重键前缀读取已有任务；限定在同一 KB 与同一文档。

    前缀匹配让「同 key、不同 expected」也能命中已有行，进而在应用层判定为 409 冲突，
    而不是静默新建第二个版本。去重键是普通 text，无需迁移。
    """

    statement = (
        select(
            IngestJob.id,
            IngestJob.document_id,
            IngestJob.version_id,
            IngestJob.dedupe_key,
            IngestJob.request_title,
            Document.title,
            Document.deleted_at,
            DocumentVersion.file_hash,
        )
        .join(DocumentVersion, DocumentVersion.id == IngestJob.version_id)
        .join(Document, Document.id == IngestJob.document_id)
        .where(
            IngestJob.dedupe_key.like(f"{key_prefix}:%"),
            Document.kb_id == kb_id,
            Document.id == document_id,
        )
        .order_by(IngestJob.created_at, IngestJob.id)
    )
    row = (await session.execute(statement)).first()
    if row is None:
        return None
    return _existing_from_row(row)


def _existing_from_row(row: Any) -> _ExistingJob:
    """把查询行映射为 _ExistingJob；解析新版本键里的 expected active version。

    ``title`` 取受理时写入的不可变 ``ingest_job.request_title``；仅当该列为 NULL（旧数据
    边界）时回退到当前 ``document.title``，保持既有历史行为、不静默改变旧 key 语义。
    """

    (
        job_id,
        document_id,
        version_id,
        dedupe_key,
        request_title,
        document_title,
        deleted_at,
        file_hash,
    ) = row
    parsed = parse_version_dedupe_key(str(dedupe_key))
    expected = parsed[1] if parsed is not None else None
    return _ExistingJob(
        job_id=job_id,
        document_id=document_id,
        version_id=version_id,
        title=request_title if request_title is not None else document_title,
        file_hash=file_hash,
        document_deleted=deleted_at is not None,
        expected_active_version_id=expected,
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
    """首次上传回放：已删除文档不得当有效资源；内容与标题都一致时复用，否则冲突。

    标题比对基准是不可变 ``_ExistingJob.title``（新行来自 ``ingest_job.request_title``，NULL
    旧行回退到当时的 ``document.title``），不再直接依赖后续会被新版本改写的文档标题。
    回滚由调用方负责。
    """

    if existing.document_deleted:
        raise DocumentDeleted("该 Idempotency-Key 对应的文档已删除")
    if existing.title != title or existing.file_hash != file_hash:
        raise IdempotencyConflict("同一 Idempotency-Key 已用于不同的内容或标题")
    return UploadOutcome(
        document_id=existing.document_id,
        version_id=existing.version_id,
        job_id=existing.job_id,
        reused=True,
    )


def _reuse_version_or_conflict(
    existing: _ExistingJob,
    *,
    title: str,
    file_hash: str,
    expected_active_version_id: uuid.UUID,
) -> UploadOutcome:
    """新版本回放：校验文档未删除、expected active、内容与标题都一致时才复用。

    同一 key 用于不同 expected/内容/标题一律 409；已删除文档不得当有效新资源。
    标题与内容摘要分别取受理时写入的不可变快照（``request_title`` 与 ``file_hash``）。
    回滚由调用方负责。
    """

    if existing.document_deleted:
        raise DocumentDeleted("该 Idempotency-Key 对应的文档已删除")
    if existing.expected_active_version_id != expected_active_version_id:
        raise IdempotencyConflict(
            "同一 Idempotency-Key 已用于不同的 expectedVersionId"
        )
    if existing.title != title or existing.file_hash != file_hash:
        raise IdempotencyConflict("同一 Idempotency-Key 已用于不同的内容或标题")
    return UploadOutcome(
        document_id=existing.document_id,
        version_id=existing.version_id,
        job_id=existing.job_id,
        reused=True,
    )


async def create_markdown_version(
    session: AsyncSession,
    store: DocumentBlobStore,
    *,
    kb_id: uuid.UUID,
    organization_id: uuid.UUID,
    document_id: uuid.UUID,
    title: str,
    content: bytes,
    idempotency_key: str,
    expected_active_version_id: uuid.UUID,
) -> UploadOutcome:
    """受理 Markdown 文档的新版本；``version_no`` 在文档行锁下分配。"""

    decode_markdown_content(content)
    return await _create_version(
        session,
        store,
        kb_id=kb_id,
        organization_id=organization_id,
        document_id=document_id,
        title=title,
        content=content,
        idempotency_key=idempotency_key,
        expected_active_version_id=expected_active_version_id,
        source_type=SOURCE_TYPE_MARKDOWN,
        media_type=MARKDOWN_MEDIA_TYPE,
        parser_version=MARKDOWN_PARSER_VERSION,
    )


async def create_pdf_version(
    session: AsyncSession,
    store: DocumentBlobStore,
    *,
    kb_id: uuid.UUID,
    organization_id: uuid.UUID,
    document_id: uuid.UUID,
    title: str,
    content: bytes,
    idempotency_key: str,
    expected_active_version_id: uuid.UUID,
) -> UploadOutcome:
    """受理文本 PDF 文档的新版本；只做受理期二进制形状校验。"""

    validate_pdf_content(content)
    return await _create_version(
        session,
        store,
        kb_id=kb_id,
        organization_id=organization_id,
        document_id=document_id,
        title=title,
        content=content,
        idempotency_key=idempotency_key,
        expected_active_version_id=expected_active_version_id,
        source_type=SOURCE_TYPE_PDF,
        media_type=PDF_MEDIA_TYPE,
        parser_version=PDF_PARSER_VERSION,
    )


async def create_docx_version(
    session: AsyncSession,
    store: DocumentBlobStore,
    *,
    kb_id: uuid.UUID,
    organization_id: uuid.UUID,
    document_id: uuid.UUID,
    title: str,
    content: bytes,
    idempotency_key: str,
    expected_active_version_id: uuid.UUID,
) -> UploadOutcome:
    """受理 DOCX 文档的新版本；只做受理期标准库 ZIP 元数据校验。"""

    validate_docx_content(content)
    return await _create_version(
        session,
        store,
        kb_id=kb_id,
        organization_id=organization_id,
        document_id=document_id,
        title=title,
        content=content,
        idempotency_key=idempotency_key,
        expected_active_version_id=expected_active_version_id,
        source_type=SOURCE_TYPE_DOCX,
        media_type=DOCX_MEDIA_TYPE,
        parser_version=DOCX_PARSER_VERSION,
    )


async def _create_version(
    session: AsyncSession,
    store: DocumentBlobStore,
    *,
    kb_id: uuid.UUID,
    organization_id: uuid.UUID,
    document_id: uuid.UUID,
    title: str,
    content: bytes,
    idempotency_key: str,
    expected_active_version_id: uuid.UUID,
    source_type: str,
    media_type: str,
    parser_version: str,
) -> UploadOutcome:
    """文档新版本受理：幂等判定、只读预检、blob 发布与行锁内的单事务写入。

    写事务前先做一次只读预检（文档存在、未删除、expected 匹配、来源格式一致），避免
    常见的老旧 expected 在 publish 后失败而留下孤儿 blob；权威校验仍在行锁内重做。
    """

    normalized_title = normalize_title(title)
    normalized_key = normalize_idempotency_key(idempotency_key)
    file_hash = content_hash(content)
    key_prefix = build_version_dedupe_key_prefix(
        organization_id, kb_id, document_id, normalized_key
    )
    dedupe_key = build_version_dedupe_key(key_prefix, expected_active_version_id)

    existing = await _load_existing_version_job(
        session, kb_id=kb_id, document_id=document_id, key_prefix=key_prefix
    )
    if existing is not None:
        try:
            return _reuse_version_or_conflict(
                existing,
                title=normalized_title,
                file_hash=file_hash,
                expected_active_version_id=expected_active_version_id,
            )
        finally:
            await session.rollback()

    document = (
        await session.execute(
            select(
                Document.source_type,
                Document.active_version_id,
                Document.deleted_at,
            ).where(Document.id == document_id, Document.kb_id == kb_id)
        )
    ).first()
    await session.rollback()
    if document is None:
        raise DocumentNotFound("文档不存在")
    existing_source_type, active_version_id, deleted_at = document
    if deleted_at is not None:
        raise DocumentDeleted("文档已删除")
    if active_version_id != expected_active_version_id:
        raise ExpectedVersionConflict("expectedVersionId 与文档当前有效版本不一致")
    if str(existing_source_type) != source_type:
        raise UnsupportedDocumentType("文档更新不能改变来源格式")

    await run_in_threadpool(current_keyword_analyzer_version)
    try:
        await precheck_default_index_profile(session)
    finally:
        await session.rollback()

    file_ref = await run_in_threadpool(store.publish, kb_id, file_hash, content)
    return await _insert_version(
        session,
        kb_id=kb_id,
        document_id=document_id,
        title=normalized_title,
        file_ref=file_ref,
        file_hash=file_hash,
        dedupe_key=dedupe_key,
        key_prefix=key_prefix,
        expected_active_version_id=expected_active_version_id,
        source_type=source_type,
        media_type=media_type,
        parser_version=parser_version,
    )


async def _insert_version(
    session: AsyncSession,
    *,
    kb_id: uuid.UUID,
    document_id: uuid.UUID,
    title: str,
    file_ref: str,
    file_hash: str,
    dedupe_key: str,
    key_prefix: str,
    expected_active_version_id: uuid.UUID,
    source_type: str,
    media_type: str,
    parser_version: str,
) -> UploadOutcome:
    """文档行锁内二次校验并写入 ``document_version``/``ingest_job``/``outbox_event``。

    行锁把同一文档的并发新版本串行化，因此去重与 ``version_no`` 分配都无竞态；只有
    ``uq_ingest_job_dedupe_key`` 的 23505 才回滚重读复用，其它完整性错误原样重抛。
    """

    version_id = uuid.uuid4()
    job_id = uuid.uuid4()
    event_id = uuid.uuid4()
    try:
        document = (
            await session.execute(
                select(Document).where(Document.id == document_id).with_for_update()
            )
        ).scalar_one_or_none()
        if document is None or document.kb_id != kb_id:
            raise DocumentNotFound("文档不存在")
        if document.deleted_at is not None:
            raise DocumentDeleted("文档已删除")
        if document.active_version_id != expected_active_version_id:
            raise ExpectedVersionConflict("expectedVersionId 与文档当前有效版本不一致")
        if document.source_type != source_type:
            raise UnsupportedDocumentType("文档更新不能改变来源格式")

        existing = await _load_existing_version_job(
            session, kb_id=kb_id, document_id=document_id, key_prefix=key_prefix
        )
        if existing is not None:
            outcome = _reuse_version_or_conflict(
                existing,
                title=title,
                file_hash=file_hash,
                expected_active_version_id=expected_active_version_id,
            )
            await session.rollback()
            return outcome

        # 新版本把展示标题带到文档上；幂等标题比对不再依赖它（见 request_title）。
        document.title = title
        max_version_no = (
            await session.execute(
                select(func.max(DocumentVersion.version_no)).where(
                    DocumentVersion.document_id == document_id
                )
            )
        ).scalar_one()
        version_no = int(max_version_no or 0) + 1
        profile_id = await ensure_default_index_profile(session)
        session.add(
            DocumentVersion(
                id=version_id,
                document_id=document_id,
                version_no=version_no,
                file_ref=file_ref,
                file_hash=file_hash,
                mime=media_type,
                parser_version=parser_version,
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
                request_title=title,
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
        await session.rollback()
        if not _is_dedupe_key_conflict(error):
            raise
        existing = await _load_existing_version_job(
            session, kb_id=kb_id, document_id=document_id, key_prefix=key_prefix
        )
        if existing is None:
            raise
        return _reuse_version_or_conflict(
            existing,
            title=title,
            file_hash=file_hash,
            expected_active_version_id=expected_active_version_id,
        )
    except BaseException:
        await session.rollback()
        raise
    return UploadOutcome(
        document_id=document_id,
        version_id=version_id,
        job_id=job_id,
        reused=False,
    )


async def delete_document(
    session: AsyncSession,
    *,
    kb_id: uuid.UUID,
    document_id: uuid.UUID,
) -> DeleteOutcome:
    """逻辑删除文档：行锁内 tombstone、递增 ``kb_revision``、终止非终态 job。

    - 只写 ``deleted_at`` 与 ``lifecycle_status='DELETED'``；保留共享 blob、版本与索引历史。
    - 把该文档所有非终态 ``ingest_job`` 置 ``CANCELLED`` 并清租约，同时写入静态诊断码；
      正在发布的 worker 因失租约或发布事务看到 ``deleted_at`` 而不会复活文档。
    - 二次删除不重复递增 revision，显式返回幂等结果。
    """

    try:
        document = (
            await session.execute(
                select(Document).where(Document.id == document_id).with_for_update()
            )
        ).scalar_one_or_none()
        if document is None or document.kb_id != kb_id:
            raise DocumentNotFound("文档不存在")
        if document.deleted_at is not None:
            await session.rollback()
            return DeleteOutcome(document_id=document_id, already_deleted=True)

        document.deleted_at = datetime.now(UTC)
        document.lifecycle_status = DOCUMENT_LIFECYCLE_DELETED
        await session.execute(
            update(KnowledgeBase)
            .where(KnowledgeBase.id == kb_id)
            .values(kb_revision=KnowledgeBase.kb_revision + 1)
        )
        await session.execute(
            CANCEL_DOCUMENT_JOBS_SQL,
            {"document_id": document_id, "error_code": JOB_ERROR_DOCUMENT_DELETED},
        )
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    return DeleteOutcome(document_id=document_id, already_deleted=False)
