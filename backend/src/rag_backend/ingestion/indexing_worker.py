"""Markdown 首次入库的真实 worker 管线：领取、解析切分、编码、暂存与发布。

本模块把已经独立实现并验收的前置能力串成一条最小一致的入库链：

``read_verified_markdown`` → 独立子进程 ``parse_markdown`` → ``chunk_markdown`` →
``build_model_input`` → ``InternalEmbeddingClient`` → 暂存 ``index_generation``/``chunk``/
``chunk_embedding`` → 发布事务。

设计边界（与 [文档入库](../../../docs/ingestion.md) 一致）：

- 只处理**新上传首次版本**的合格任务：``ingest_job.status='QUEUED'``、无接收 marker、无
  既有诊断码、``profile_id`` 已绑定、``parser_version`` 为当前实现版本、``version_no=1``、
  document 尚无 ``active_version_id`` 且该 version 尚无 READY generation。旧任务、已有
  marker/诊断的任务、超范围更新都在领取阶段静态拒绝，不猜退役逻辑、不自动补绑或重投。
- 领取使用行锁 + 带全部守卫的原子 CAS，写入 ``lease_owner``/``lease_token``/``lease_until``
  与 ``heartbeat_at``，并把 ``status`` 置为 ``PARSING``；各阶段用短事务。长时间解析/编码期间
  由 :class:`LeaseHeartbeat` 用独立连接续租；失租约时绝不发布、也不覆盖他人结果。
- 编码前按 ``model_input_hash`` 批量查增量缓存（复用既有 ``chunk_embedding`` 的向量，
  不新增表/Redis/LRU）；命中不调用编码器，miss 只编码唯一的 ``model_input_hash`` 输入并
  fan-out 到重复 chunk，输出顺序仍与 chunks 一致。缓存查询是独立短只读事务的优化：失败
  只记静态 warning 并回退全量 miss，不污染主事务。
- 暂存 generation 为 ``BUILDING``，并在同一暂存事务内把 ``ingest_job.generation_id`` 绑定
  到该 generation；chunk 与向量全部落库并核对数量后才进入发布事务。
- 发布事务原子置 generation ``READY``、``document.active_version_id``、版本状态 ``READY``、
  文档 ``READY`` 与 ``ingest_job.status=READY`` 并清租约；KB 的
  ``active_index_profile_id`` 只在为 NULL 时首次置位，非 NULL 且与目标 profile 不同则拒绝。
- 失败路径只把目标 generation 置 ``FAILED``、job 置 ``FAILED`` 并清租约，再按守卫把版本与
  文档置 ``FAILED``（仅当 document 没有 active version，绝不使既有有效文档下线）。不 DELETE、
  不碰共享 blob。
- 解析在独立子进程 :mod:`rag_backend.ingestion.parse_subprocess` 中带硬时限运行，超时真实
  ``kill``+``wait`` 回收并静态失败；子进程只接收原始字节、环境经白名单剥离凭据且返回体有界。
- 领取后若发生 ``SQLAlchemyError``，在连接回滚释放后用独立短事务重读 job：已知 ``READY``
  （commit 结果未知）不误改，仍持未过期租约才 CAS 落静态 ``FAILED``，数据库持续不可用则返回
  ``persist_unconfirmed`` 保留人工恢复。
- 无模型依赖位于模块导入期；只有显式注入的 identity provider 才会按需加载 tokenizer 资产。
  没有常驻 reaper：处理中崩溃会留下带租约的 ``PARSING`` 任务，本模块不实现自动恢复。

本模块不导入 ``rag_backend.ingestion.worker_index_identity``（那会拉起 ``tokenizers``），
也不在导入期初始化 jieba；调用方负责注入 identity provider、embedder 工厂与存储。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Final, Protocol

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from rag_backend.database import SyncSessionFactory
from rag_backend.ingestion import chunking
from rag_backend.ingestion.chunking import (
    Chunk,
    ChunkingError,
    NoChunkableContent,
    build_model_input,
    chunk_markdown,
)
from rag_backend.ingestion.docx_parsing import DOCX_PARSER_VERSION
from rag_backend.ingestion.embedding_cache import (
    as_cache_vectors,
    load_cached_embeddings,
)
from rag_backend.ingestion.embedding_client import (
    EmbeddingBusyError,
    EmbeddingClientError,
    EmbeddingNotReadyError,
    EmbeddingPermanentError,
    EmbeddingResponseError,
    EmbeddingTransportError,
    EmbeddingUnavailableError,
    EmbeddingUpstreamError,
)
from rag_backend.ingestion.errors import BlobReadError
from rag_backend.ingestion.identity_preflight import (
    ProfileIdentityDecision,
    StoredIndexProfile,
    decide_profile_identity,
)
from rag_backend.ingestion.parse_subprocess import (
    DocxInvalidSubprocessError,
    DocxUnsupportedSubprocessError,
    ParseSubprocessError,
    ParseSubprocessTimeout,
    PdfEncryptedSubprocessError,
    PdfInvalidSubprocessError,
    PdfTooManyPagesSubprocessError,
    parse_docx_in_subprocess,
    parse_markdown_in_subprocess,
    parse_pdf_in_subprocess,
)
from rag_backend.ingestion.parsing import MARKDOWN_PARSER_VERSION, ParsedDocument
from rag_backend.ingestion.pdf_parsing import PDF_PARSER_VERSION
from rag_backend.ingestion.storage import DocumentBlobStore, InvalidBlobReference
from rag_backend.ingestion.validation import (
    SOURCE_TYPE_DOCX,
    SOURCE_TYPE_MARKDOWN,
    SOURCE_TYPE_PDF,
    parse_version_dedupe_key,
)
from rag_backend.models.profile_contract import IndexProfileContract

logger = logging.getLogger(__name__)

# 领取任务的阶段状态；只有 CHECK 允许的取值，不凭空造阶段。
JOB_STATUS_QUEUED: Final = "QUEUED"
JOB_STATUS_PARSING: Final = "PARSING"
JOB_STATUS_CHUNKING: Final = "CHUNKING"
JOB_STATUS_EMBEDDING: Final = "EMBEDDING"
JOB_STATUS_INDEXING: Final = "INDEXING"
JOB_STATUS_READY: Final = "READY"
JOB_STATUS_FAILED: Final = "FAILED"

ACTIVE_JOB_STATUSES: Final = (
    JOB_STATUS_PARSING,
    JOB_STATUS_CHUNKING,
    JOB_STATUS_EMBEDDING,
    JOB_STATUS_INDEXING,
)

# 管线返回状态；与接收壳状态共享可观察命名，但语义是“本次消息在真实处理路径的结果”。
PROCESS_STATUS_READY: Final = "ready"
PROCESS_STATUS_ALREADY_RECEIVED: Final = "already_received"
PROCESS_STATUS_NOT_QUEUED: Final = "not_queued"
PROCESS_STATUS_DELETED: Final = "deleted"
PROCESS_STATUS_VERSION_MISMATCH: Final = "version_mismatch"
PROCESS_STATUS_LEGACY_UNSUPPORTED: Final = "legacy_unsupported"
PROCESS_STATUS_EXISTING_DIAGNOSTIC: Final = "existing_diagnostic"
PROCESS_STATUS_UNSUPPORTED_UPDATE: Final = "unsupported_update"
# 陈旧 expected：可解析的 expected 已不等于文档当前 active，本任务永远无法发布，领取期静态拒绝。
PROCESS_STATUS_STALE_EXPECTED: Final = "stale_expected"
# 恢复后的退避尚未到期：不领取，但也不改状态；旧 Redis 重投不能绕过该门槛。
PROCESS_STATUS_NOT_DUE: Final = "not_due"
PROCESS_STATUS_FAILED: Final = "failed"
PROCESS_STATUS_LEASE_LOST: Final = "lease_lost"
# 数据库持续不可用、无法落任何终态：诚实上报，不当作成功，也不谎报 FAILED。
PROCESS_STATUS_PERSIST_UNCONFIRMED: Final = "persist_unconfirmed"

# 静态失败终态错误码；消息/日志只出现这些固定值，不回显正文、路径、DSN 或 UUID。
ERROR_PIPELINE_IDENTITY_UNAVAILABLE: Final = "PIPELINE_IDENTITY_UNAVAILABLE"
ERROR_PIPELINE_PROFILE_MISMATCH: Final = "PIPELINE_PROFILE_MISMATCH"
ERROR_PIPELINE_SOURCE_UNSUPPORTED: Final = "PIPELINE_SOURCE_UNSUPPORTED"
ERROR_PIPELINE_BLOB_INVALID: Final = "PIPELINE_BLOB_INVALID"
ERROR_PIPELINE_SOURCE_HASH_MISMATCH: Final = "PIPELINE_SOURCE_HASH_MISMATCH"
ERROR_PIPELINE_CONTENT_EMPTY: Final = "PIPELINE_CONTENT_EMPTY"
ERROR_PIPELINE_CHUNK_FAILED: Final = "PIPELINE_CHUNK_FAILED"
ERROR_PIPELINE_EMBEDDING_FAILED: Final = "PIPELINE_EMBEDDING_FAILED"
ERROR_PIPELINE_EMBEDDING_REJECTED: Final = "PIPELINE_EMBEDDING_REJECTED"
ERROR_PIPELINE_STAGING_INVALID: Final = "PIPELINE_STAGING_INVALID"
ERROR_PIPELINE_PUBLISH_CONFLICT: Final = "PIPELINE_PUBLISH_CONFLICT"
ERROR_PIPELINE_UNSUPPORTED_UPDATE: Final = "PIPELINE_UNSUPPORTED_UPDATE"
# 领取期即可确认任务携带的 expected active 已被更早的发布超越；静态冲突码，不重试。
ERROR_PIPELINE_STALE_EXPECTED: Final = "PIPELINE_STALE_EXPECTED"
ERROR_PIPELINE_PARSE_TIMEOUT: Final = "PIPELINE_PARSE_TIMEOUT"
ERROR_PIPELINE_PARSE_FAILED: Final = "PIPELINE_PARSE_FAILED"
# PDF 具名静态失败：加密/超页/结构损坏分别可区分；零可提取文本单独走 NEEDS_OCR。
ERROR_PIPELINE_PDF_ENCRYPTED: Final = "PIPELINE_PDF_ENCRYPTED"
ERROR_PIPELINE_PDF_TOO_MANY_PAGES: Final = "PIPELINE_PDF_TOO_MANY_PAGES"
ERROR_PIPELINE_PDF_INVALID: Final = "PIPELINE_PDF_INVALID"
# DOCX 具名静态失败：嵌套表/宏/实体声明等收窄外结构可区分地静态失败。
ERROR_PIPELINE_DOCX_UNSUPPORTED: Final = "PIPELINE_DOCX_UNSUPPORTED"
ERROR_PIPELINE_DOCX_INVALID: Final = "PIPELINE_DOCX_INVALID"
ERROR_PIPELINE_NEEDS_OCR: Final = "PIPELINE_NEEDS_OCR"
# 已领取任务后发生数据库错误，未能完成：尝试落这个静态诊断码。
ERROR_PIPELINE_DB_ERROR: Final = "PIPELINE_DB_ERROR"

# 租约与心跳参数；解析/编码硬时限内保持心跳间隔远小于租约期限。
DEFAULT_LEASE_SECONDS: Final = 120
DEFAULT_HEARTBEAT_INTERVAL_SECONDS: Final = 20.0
DEFAULT_MAX_EMBEDDING_ATTEMPTS: Final = 3
MAX_RETRY_BACKOFF_SECONDS: Final = 8.0

LEASE_OWNER_PREFIX: Final = "pipeline:"
EMBEDDING_DIMENSION: Final = 512


class PipelineDependencyUnavailable(RuntimeError):
    """worker 运行管线所需的本地依赖（模型资产/身份/编码客户端）不可用。

    调用方应在领取任务后把它映射为显式静态失败终态，而不是 ACK 后把 job 永久留在 QUEUED。
    """


class LeaseLost(RuntimeError):
    """行锁 CAS 发现租约已不属于本执行者；此时不得再写业务状态。"""


class StagingValidationError(RuntimeError):
    """暂存后的 chunk/向量数量或内容不满足发布前置条件。"""


class ResolvedIdentity(Protocol):
    """一次运行期索引身份；``WorkerIndexIdentity`` 在结构上满足本协议。"""

    @property
    def profile(self) -> IndexProfileContract: ...

    @property
    def parser_version(self) -> str: ...

    @property
    def pdf_parser_version(self) -> str: ...

    @property
    def docx_parser_version(self) -> str: ...

    @property
    def token_counter(self) -> chunking.TokenCounter: ...

    @property
    def keyword_analyzer(self) -> KeywordAnalyzerLike: ...


class KeywordAnalyzerLike(Protocol):
    """文档侧关键词分析器接口；只产出可交给 ``to_tsvector('simple', :param)`` 的词流。"""

    def analyze(self, text: str) -> str: ...


class DocumentEmbedder(Protocol):
    """受限文档编码客户端接口；``InternalEmbeddingClient`` 满足本协议。"""

    def embed_document_texts(self, texts: Sequence[str]) -> list[list[float]]: ...

    def close(self) -> None: ...


class CacheLookup(Protocol):
    """增量 embedding 缓存查询接口；``load_cached_embeddings`` 满足本协议。

    调用方只把它当优化：返回值按 ``model_input_hash`` 给出可复用向量，缺项即 miss。
    """

    def __call__(
        self,
        session_factory: SyncSessionFactory,
        *,
        organization_id: uuid.UUID,
        profile_id: uuid.UUID,
        model_input_hashes: Sequence[str],
    ) -> Mapping[str, Sequence[float]]: ...


IdentityProvider = Callable[[], ResolvedIdentity]
EmbedderFactory = Callable[[chunking.TokenCounter], DocumentEmbedder]
# 解析入口；默认在独立子进程中带硬时限运行，注入仅用于单元测试。
ParseDocument = Callable[[bytes], ParsedDocument]


def classify_embedding_error(error: EmbeddingClientError) -> str:
    """把编码失败收敛为静态错误码；不拼接服务端正文或输入。"""

    if isinstance(error, EmbeddingPermanentError):
        return ERROR_PIPELINE_EMBEDDING_REJECTED
    if isinstance(
        error,
        (
            EmbeddingBusyError,
            EmbeddingTransportError,
            EmbeddingUpstreamError,
            EmbeddingNotReadyError,
            EmbeddingUnavailableError,
            EmbeddingResponseError,
        ),
    ):
        return ERROR_PIPELINE_EMBEDDING_FAILED
    return ERROR_PIPELINE_EMBEDDING_FAILED


def _retryable(error: EmbeddingClientError) -> bool:
    return bool(getattr(error, "retryable", False))


# --- 领取 ---------------------------------------------------------------------

SELECT_JOB_FOR_CLAIM_SQL: Final = text(
    """
    SELECT
        j.status,
        j.error_code,
        j.lease_owner,
        j.heartbeat_at,
        j.profile_id,
        j.dedupe_key,
        j.document_id,
        j.version_id,
        dv.parser_version,
        dv.version_no,
        dv.document_id AS version_document_id,
        dv.status AS version_status,
        dv.file_ref,
        dv.file_hash,
        d.deleted_at,
        d.active_version_id,
        d.lifecycle_status,
        d.source_type,
        d.kb_id,
        j.next_run_at <= clock_timestamp() AS next_run_due,
        kb.organization_id,
        kb.active_index_profile_id,
        EXISTS (
            SELECT 1 FROM index_generation AS g
            WHERE g.version_id = j.version_id AND g.status = 'READY'
        ) AS ready_generation_present
    FROM ingest_job AS j
    JOIN document AS d ON d.id = j.document_id
    JOIN document_version AS dv ON dv.id = j.version_id
    JOIN knowledge_base AS kb ON kb.id = d.kb_id
    WHERE j.id = :job_id
    FOR UPDATE OF j
    """
)

CLAIM_JOB_SQL: Final = text(
    """
    UPDATE ingest_job
    SET status = 'PARSING',
        attempt = attempt + 1,
        lease_owner = :lease_owner,
        lease_token = :lease_token,
        lease_until = clock_timestamp() + (:lease_seconds * interval '1 second'),
        heartbeat_at = clock_timestamp(),
        updated_at = clock_timestamp()
    WHERE id = :job_id
      AND status = 'QUEUED'
      AND lease_owner IS NULL
      AND heartbeat_at IS NULL
      AND error_code IS NULL
      AND profile_id IS NOT NULL
      AND next_run_at <= clock_timestamp()
    RETURNING id
    """
)

REJECT_JOB_SQL: Final = text(
    """
    UPDATE ingest_job
    SET status = 'FAILED',
        error_code = :error_code,
        updated_at = clock_timestamp()
    WHERE id = :job_id
      AND status = 'QUEUED'
      AND lease_owner IS NULL
      AND heartbeat_at IS NULL
      AND error_code IS NULL
    RETURNING id
    """
)


class ClaimAction(Enum):
    """行锁读取事实后应采取的领取动作；互斥且可独立单测。"""

    CLAIM = "CLAIM"
    NOT_QUEUED = "NOT_QUEUED"
    DELETED = "DELETED"
    VERSION_MISMATCH = "VERSION_MISMATCH"
    ALREADY_RECEIVED = "ALREADY_RECEIVED"
    EXISTING_DIAGNOSTIC = "EXISTING_DIAGNOSTIC"
    LEGACY_UNSUPPORTED = "LEGACY_UNSUPPORTED"
    UNSUPPORTED_UPDATE = "UNSUPPORTED_UPDATE"
    STALE_EXPECTED = "STALE_EXPECTED"
    NOT_DUE = "NOT_DUE"


_CLAIM_ACTION_STATUS: Final[dict[ClaimAction, str]] = {
    ClaimAction.NOT_QUEUED: PROCESS_STATUS_NOT_QUEUED,
    ClaimAction.DELETED: PROCESS_STATUS_DELETED,
    ClaimAction.VERSION_MISMATCH: PROCESS_STATUS_VERSION_MISMATCH,
    ClaimAction.ALREADY_RECEIVED: PROCESS_STATUS_ALREADY_RECEIVED,
    ClaimAction.EXISTING_DIAGNOSTIC: PROCESS_STATUS_EXISTING_DIAGNOSTIC,
    ClaimAction.LEGACY_UNSUPPORTED: PROCESS_STATUS_LEGACY_UNSUPPORTED,
    ClaimAction.UNSUPPORTED_UPDATE: PROCESS_STATUS_UNSUPPORTED_UPDATE,
    ClaimAction.STALE_EXPECTED: PROCESS_STATUS_STALE_EXPECTED,
    ClaimAction.NOT_DUE: PROCESS_STATUS_NOT_DUE,
}

# 领取期静态拒绝动作到具名 error_code 的固定映射；只有这些动作才写入 FAILED 终态。
_REJECT_ERROR_CODE: Final[dict[ClaimAction, str]] = {
    ClaimAction.LEGACY_UNSUPPORTED: "LEGACY_JOB_UNSUPPORTED",
    ClaimAction.UNSUPPORTED_UPDATE: ERROR_PIPELINE_UNSUPPORTED_UPDATE,
    ClaimAction.STALE_EXPECTED: ERROR_PIPELINE_STALE_EXPECTED,
}


@dataclass(frozen=True)
class ClaimFacts:
    """行锁下读到的 job/document/version/KB 事实，供纯函数判定。"""

    status: str
    document_deleted: bool
    version_matches_document: bool
    receive_marker_present: bool
    existing_error_code: str | None
    profile_bound: bool
    parser_version: str
    expected_parser_version: str | None
    version_no: int
    document_active_version_id: uuid.UUID | None
    ready_generation_present: bool
    expected_active_version_id: uuid.UUID | None
    next_run_due: bool


def decide_claim_action(facts: ClaimFacts) -> ClaimAction:
    """按已验收的固定优先级判定领取动作；不修改任何状态、不做 IO。

    优先级：非 QUEUED → 已删除 → 版本归属不符 → 已有接收 marker → 已有非 NULL 诊断 →
    旧 job（profile 未绑定或 parser 非当前实现版本）→ 超出本实现范围的版本/重建 →
    陈旧 expected → 领取。

    首次版本（``version_no == 1``）要求文档尚无 active version 且本版本尚无 READY generation；
    新版本更新（``version_no > 1``）要求去重键携带可解析的 ``expected_active_version``、文档
    已有 active version；无法解析 expected 的更新任务按超范围静态拒绝，不猜发布语义。当
    expected 可解析且文档 active 已存在，但二者不等时，任务已永远无法发布（active 只前进不
    回退），在领取期以静态冲突码早拒；发布事务仍保留同一 CAS，作为并发变化的最后防线。
    """

    if facts.status != JOB_STATUS_QUEUED:
        return ClaimAction.NOT_QUEUED
    if facts.document_deleted:
        return ClaimAction.DELETED
    if not facts.version_matches_document:
        return ClaimAction.VERSION_MISMATCH
    if facts.receive_marker_present:
        return ClaimAction.ALREADY_RECEIVED
    if facts.existing_error_code is not None:
        return ClaimAction.EXISTING_DIAGNOSTIC
    if (
        not facts.profile_bound
        or facts.expected_parser_version is None
        or facts.parser_version != facts.expected_parser_version
    ):
        return ClaimAction.LEGACY_UNSUPPORTED
    if facts.version_no == 1:
        if facts.document_active_version_id is not None or facts.ready_generation_present:
            return ClaimAction.UNSUPPORTED_UPDATE
    else:
        # 文档新版本：必须有可解析的 expected active，且文档已有 active version。
        if (
            facts.expected_active_version_id is None
            or facts.document_active_version_id is None
            or facts.ready_generation_present
        ):
            return ClaimAction.UNSUPPORTED_UPDATE
        if facts.expected_active_version_id != facts.document_active_version_id:
            # expected 已被更早的发布超越；active 只前进不回退，本任务不可能成功。
            return ClaimAction.STALE_EXPECTED
    if not facts.next_run_due:
        # 恢复后的退避未到期：不领取，也不改状态；旧 Redis 重投无法绕过。
        return ClaimAction.NOT_DUE
    return ClaimAction.CLAIM


@dataclass(frozen=True)
class ClaimedJob:
    """领取成功后继续处理所需的只读事实。"""

    job_id: uuid.UUID
    lease_token: str
    document_id: uuid.UUID
    version_id: uuid.UUID
    kb_id: uuid.UUID
    organization_id: uuid.UUID
    profile_id: uuid.UUID
    parser_version: str
    source_type: str
    file_ref: str
    file_hash: str


@dataclass(frozen=True)
class ClaimResult:
    """领取结果：``claimed`` 非空当且仅当 ``status == PROCESS_STATUS_CLAIMED``。"""

    status: str
    claimed: ClaimedJob | None = None


PROCESS_STATUS_CLAIMED: Final = "claimed"


def claim_ingest_job(
    session_factory: SyncSessionFactory,
    *,
    job_id: uuid.UUID,
    event_id: str,
    expected_parser_versions: Mapping[str, str],
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> ClaimResult:
    """行锁读取 job，按固定优先级永久拒绝旧任务/超范围更新，或原子领取 lease。

    ``expected_parser_versions`` 按 ``document.source_type`` 给出当前实现的期望解析器版本；
    来源不在映射中或版本不符都判为 ``LEGACY_UNSUPPORTED``，因此 PDF 新 job 不会被 Markdown
    期望版本误杀，而旧占位版本仍被静态拒绝。
    """

    lease_token = uuid.uuid4().hex
    with session_factory() as session, session.begin():
        row = session.execute(
            SELECT_JOB_FOR_CLAIM_SQL, {"job_id": job_id}
        ).mappings().first()
        if row is None:
            return ClaimResult(PROCESS_STATUS_NOT_QUEUED)
        source_type = str(row["source_type"])
        parsed_dedupe = parse_version_dedupe_key(str(row["dedupe_key"]))
        facts = ClaimFacts(
            status=str(row["status"]),
            document_deleted=row["deleted_at"] is not None,
            version_matches_document=row["version_document_id"] == row["document_id"],
            receive_marker_present=(
                row["lease_owner"] is not None or row["heartbeat_at"] is not None
            ),
            existing_error_code=row["error_code"],
            profile_bound=row["profile_id"] is not None,
            parser_version=str(row["parser_version"]),
            expected_parser_version=expected_parser_versions.get(source_type),
            version_no=int(row["version_no"]),
            document_active_version_id=row["active_version_id"],
            ready_generation_present=bool(row["ready_generation_present"]),
            expected_active_version_id=(
                parsed_dedupe[1] if parsed_dedupe is not None else None
            ),
            next_run_due=bool(row["next_run_due"]),
        )
        action = decide_claim_action(facts)
        if action in _REJECT_ERROR_CODE:
            rejected = session.execute(
                REJECT_JOB_SQL,
                {"job_id": job_id, "error_code": _REJECT_ERROR_CODE[action]},
            ).first()
            if rejected is None:
                return ClaimResult(PROCESS_STATUS_NOT_QUEUED)
            return ClaimResult(_CLAIM_ACTION_STATUS[action])
        if action is not ClaimAction.CLAIM:
            return ClaimResult(_CLAIM_ACTION_STATUS[action])

        claimed = session.execute(
            CLAIM_JOB_SQL,
            {
                "job_id": job_id,
                "lease_owner": f"{LEASE_OWNER_PREFIX}{event_id}",
                "lease_token": lease_token,
                "lease_seconds": lease_seconds,
            },
        ).first()
        if claimed is None:
            return ClaimResult(PROCESS_STATUS_NOT_QUEUED)
        return ClaimResult(
            PROCESS_STATUS_CLAIMED,
            ClaimedJob(
                job_id=job_id,
                lease_token=lease_token,
                document_id=row["document_id"],
                version_id=row["version_id"],
                kb_id=row["kb_id"],
                organization_id=row["organization_id"],
                profile_id=row["profile_id"],
                parser_version=str(row["parser_version"]),
                source_type=str(row["source_type"]),
                file_ref=str(row["file_ref"]),
                file_hash=str(row["file_hash"]),
            ),
        )


# --- 阶段推进、失败与心跳 -------------------------------------------------------

ADVANCE_STAGE_SQL: Final = text(
    """
    UPDATE ingest_job
    SET status = :to_status,
        updated_at = clock_timestamp()
    WHERE id = :job_id
      AND lease_token = :lease_token
      AND status = :from_status
      AND lease_until > clock_timestamp()
    RETURNING id
    """
)

HEARTBEAT_SQL: Final = text(
    """
    UPDATE ingest_job
    SET lease_until = clock_timestamp() + (:lease_seconds * interval '1 second'),
        heartbeat_at = clock_timestamp()
    WHERE id = :job_id
      AND lease_token = :lease_token
      AND status IN ('PARSING', 'CHUNKING', 'EMBEDDING', 'INDEXING')
      AND lease_until > clock_timestamp()
    RETURNING id
    """
)

FAIL_JOB_SQL: Final = text(
    """
    UPDATE ingest_job
    SET status = 'FAILED',
        error_code = :error_code,
        lease_owner = NULL,
        lease_token = NULL,
        lease_until = NULL,
        heartbeat_at = NULL,
        updated_at = clock_timestamp()
    WHERE id = :job_id
      AND lease_token = :lease_token
      AND status IN ('PARSING', 'CHUNKING', 'EMBEDDING', 'INDEXING')
    RETURNING document_id, version_id, generation_id
    """
)

FAIL_GENERATION_SQL: Final = text(
    """
    UPDATE index_generation
    SET status = 'FAILED', updated_at = clock_timestamp()
    WHERE id = :generation_id AND status = 'BUILDING'
    RETURNING id
    """
)

FAIL_VERSION_SQL: Final = text(
    """
    UPDATE document_version
    SET status = 'FAILED', updated_at = clock_timestamp()
    WHERE id = :version_id AND status = 'PENDING'
    RETURNING id
    """
)

# 零可提取文本 PDF 的专用版本终态：document_version 置 NEEDS_OCR，绝不伪造正文或行号。
FAIL_VERSION_NEEDS_OCR_SQL: Final = text(
    """
    UPDATE document_version
    SET status = 'NEEDS_OCR', updated_at = clock_timestamp()
    WHERE id = :version_id AND status = 'PENDING'
    RETURNING id
    """
)

FAIL_DOCUMENT_SQL: Final = text(
    """
    UPDATE document
    SET lifecycle_status = 'FAILED', updated_at = clock_timestamp()
    WHERE id = :document_id
      AND active_version_id IS NULL
      AND lifecycle_status IN ('CREATED', 'INDEXING')
    RETURNING id
    """
)


def advance_ingest_stage(
    session_factory: SyncSessionFactory,
    *,
    job_id: uuid.UUID,
    lease_token: str,
    from_status: str,
    to_status: str,
) -> bool:
    """带租约 CAS 的阶段推进；仍持有未过期租约才命中。"""

    with session_factory() as session, session.begin():
        row = session.execute(
            ADVANCE_STAGE_SQL,
            {
                "job_id": job_id,
                "lease_token": lease_token,
                "from_status": from_status,
                "to_status": to_status,
            },
        ).first()
        return row is not None


def fail_ingest_job(
    session_factory: SyncSessionFactory,
    *,
    job_id: uuid.UUID,
    lease_token: str,
    error_code: str,
    generation_id: uuid.UUID | None = None,
) -> bool:
    """静态失败终态：generation/job 置 FAILED 并清租约，再守卫式标记版本与文档。

    只有仍持有租约时才写入；失租约时返回 False，绝不覆盖他人结果，也不 DELETE 或碰 blob。
    文档与版本只在没有 active version 时改为 FAILED，避免使既有有效文档下线。
    """

    with session_factory() as session, session.begin():
        row = session.execute(
            FAIL_JOB_SQL, {"job_id": job_id, "lease_token": lease_token, "error_code": error_code}
        ).mappings().first()
        if row is None:
            return False
        effective_generation = generation_id or row["generation_id"]
        if effective_generation is not None:
            session.execute(
                FAIL_GENERATION_SQL, {"generation_id": effective_generation}
            )
        session.execute(FAIL_VERSION_SQL, {"version_id": row["version_id"]})
        session.execute(FAIL_DOCUMENT_SQL, {"document_id": row["document_id"]})
        return True


def mark_ingest_needs_ocr(
    session_factory: SyncSessionFactory,
    *,
    job_id: uuid.UUID,
    lease_token: str,
) -> bool:
    """零可提取文本 PDF 的终态：job FAILED/PIPELINE_NEEDS_OCR、version NEEDS_OCR。

    与 :func:`fail_ingest_job` 同样只在仍持租约时写入，且只在 version 仍为 PENDING 时置
    NEEDS_OCR，不建 generation、不置 ``document.active_version_id``；文档仍按
    ``active_version_id IS NULL`` 守卫标记 FAILED，不会使既有有效文档下线。
    """

    with session_factory() as session, session.begin():
        row = session.execute(
            FAIL_JOB_SQL,
            {
                "job_id": job_id,
                "lease_token": lease_token,
                "error_code": ERROR_PIPELINE_NEEDS_OCR,
            },
        ).mappings().first()
        if row is None:
            return False
        session.execute(FAIL_VERSION_NEEDS_OCR_SQL, {"version_id": row["version_id"]})
        session.execute(FAIL_DOCUMENT_SQL, {"document_id": row["document_id"]})
        return True


SELECT_JOB_STATE_SQL: Final = text(
    """
    SELECT status, lease_token, lease_until > clock_timestamp() AS lease_valid
    FROM ingest_job
    WHERE id = :job_id
    """
)


class ResolveOutcome(Enum):
    """数据库错误后对目标 job 的重新判定；不包含任何正文或凭据。"""

    READY = "READY"
    FAILED_MARKED = "FAILED_MARKED"
    LEASE_LOST = "LEASE_LOST"
    UNKNOWN = "UNKNOWN"


def resolve_after_db_error(
    session_factory: SyncSessionFactory,
    *,
    job_id: uuid.UUID,
    lease_token: str,
    error_code: str = ERROR_PIPELINE_DB_ERROR,
) -> ResolveOutcome:
    """数据库错误后用独立短事务判定：已知 READY 不动，仍持有租约才落静态 FAILED。

    - job 已是 ``READY``：说明之前的结果（可能是“commit 结果未知”）已生效，绝不再改 FAILED。
    - 租约已不属于本执行者或已过期：不得覆盖其他 worker，返回 ``LEASE_LOST``。
    - 仍持有未过期租约且处于活动阶段：调用带 CAS 的 :func:`fail_ingest_job` 落终态。
    - 读不到行等异常情况：返回 ``UNKNOWN``，由调用方诚实上报。
    """

    with session_factory() as session:
        row = session.execute(SELECT_JOB_STATE_SQL, {"job_id": job_id}).mappings().first()
        session.rollback()
    if row is None:
        return ResolveOutcome.UNKNOWN
    status = str(row["status"])
    if status == JOB_STATUS_READY:
        return ResolveOutcome.READY
    if (
        status not in ACTIVE_JOB_STATUSES
        or row["lease_token"] != lease_token
        or not bool(row["lease_valid"])
    ):
        return ResolveOutcome.LEASE_LOST
    if fail_ingest_job(
        session_factory, job_id=job_id, lease_token=lease_token, error_code=error_code
    ):
        return ResolveOutcome.FAILED_MARKED
    return ResolveOutcome.LEASE_LOST


def recover_from_db_error(
    session_factory: SyncSessionFactory,
    heartbeat: LeaseHeartbeat,
    *,
    job_id: uuid.UUID,
    lease_token: str,
) -> str:
    """已知数据库错误后的收敛：尝试落终态；数据库持续不可用则诚实保留。"""

    if heartbeat.lost:
        return PROCESS_STATUS_LEASE_LOST
    try:
        outcome = resolve_after_db_error(
            session_factory, job_id=job_id, lease_token=lease_token
        )
    except SQLAlchemyError:
        # 数据库仍不可用，无法确认任何终态；不谎报成功也不谎报 FAILED。
        logger.warning("ingest 数据库错误后仍无法落终态，保留人工恢复 job_id=%s", job_id)
        return PROCESS_STATUS_PERSIST_UNCONFIRMED
    if outcome is ResolveOutcome.READY:
        return PROCESS_STATUS_READY
    if outcome is ResolveOutcome.FAILED_MARKED:
        logger.info(
            "ingest 数据库错误后落静态失败 job_id=%s error_code=%s",
            job_id,
            ERROR_PIPELINE_DB_ERROR,
        )
        return PROCESS_STATUS_FAILED
    if outcome is ResolveOutcome.LEASE_LOST:
        return PROCESS_STATUS_LEASE_LOST
    return PROCESS_STATUS_PERSIST_UNCONFIRMED


class LeaseHeartbeat:
    """用独立数据库连接在长时间处理中续租；失租约或心跳异常都 fail closed。

    线程持有自己的 Session（来自同一工厂的不同连接），只在 ``_run`` 中写入心跳列。``stop``
    幂等，显式 ``join`` 确保线程结束；不吞业务错误，只把心跳失败收敛为 ``lost``，让调用方
    在发布前放弃。
    """

    def __init__(
        self,
        session_factory: SyncSessionFactory,
        *,
        job_id: uuid.UUID,
        lease_token: str,
        lease_seconds: int,
        interval_seconds: float,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("心跳间隔必须为正数")
        self._session_factory = session_factory
        self._job_id = job_id
        self._lease_token = lease_token
        self._lease_seconds = lease_seconds
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("心跳线程已启动")
        self._thread = threading.Thread(
            target=self._run, name="ingest-lease-heartbeat", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            if self._stop.is_set():
                return
            try:
                renewed = self._beat()
            except Exception:  # noqa: BLE001 - 心跳失败一律 fail closed
                logger.warning("ingest 心跳续租失败，按失租约处理")
                self._lost.set()
                return
            if not renewed:
                self._lost.set()
                return

    def _beat(self) -> bool:
        with self._session_factory() as session, session.begin():
            row = session.execute(
                HEARTBEAT_SQL,
                {
                    "job_id": self._job_id,
                    "lease_token": self._lease_token,
                    "lease_seconds": self._lease_seconds,
                },
            ).first()
            return row is not None

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(5.0, self._interval_seconds * 3))

    def __enter__(self) -> LeaseHeartbeat:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


# --- 身份预检 -----------------------------------------------------------------

SELECT_PROFILE_SQL: Final = text(
    """
    SELECT id, config_hash, embedding_model, model_revision, dimension, normalize,
           tokenizer_revision, chunker_version, keyword_analyzer_version
    FROM index_profile
    WHERE id = :profile_id
    """
)


def load_stored_profile(
    session_factory: SyncSessionFactory, profile_id: uuid.UUID
) -> StoredIndexProfile | None:
    """按 id 读取 ``index_profile`` 行并映射为只读 DTO；读不到返回 None。"""

    with session_factory() as session:
        row = session.execute(
            SELECT_PROFILE_SQL, {"profile_id": profile_id}
        ).mappings().first()
    if row is None:
        return None
    return StoredIndexProfile(
        profile_id=row["id"],
        config_hash=str(row["config_hash"]),
        embedding_model=str(row["embedding_model"]),
        model_revision=str(row["model_revision"]),
        dimension=int(row["dimension"]),
        normalize=bool(row["normalize"]),
        tokenizer_revision=str(row["tokenizer_revision"]),
        chunker_version=str(row["chunker_version"]),
        keyword_analyzer_version=str(row["keyword_analyzer_version"]),
    )


def classify_identity_decision(decision: ProfileIdentityDecision) -> str | None:
    """把身份预检判定映射为静态错误码；ALLOWED 返回 None。"""

    if decision is ProfileIdentityDecision.ALLOWED:
        return None
    if decision in (
        ProfileIdentityDecision.PROFILE_UNBOUND,
        ProfileIdentityDecision.PARSER_UNSUPPORTED,
    ):
        return "LEGACY_JOB_UNSUPPORTED"
    if decision is ProfileIdentityDecision.SOURCE_UNSUPPORTED:
        return ERROR_PIPELINE_SOURCE_UNSUPPORTED
    return ERROR_PIPELINE_PROFILE_MISMATCH


# --- 暂存 ---------------------------------------------------------------------

INSERT_GENERATION_SQL: Final = text(
    """
    INSERT INTO index_generation
        (id, version_id, profile_id, status, expected_chunks, actual_chunks)
    VALUES (:id, :version_id, :profile_id, 'BUILDING', :expected_chunks, 0)
    """
)

BIND_GENERATION_SQL: Final = text(
    """
    UPDATE ingest_job
    SET generation_id = :generation_id,
        updated_at = clock_timestamp()
    WHERE id = :job_id
      AND lease_token = :lease_token
      AND status IN ('PARSING', 'CHUNKING', 'EMBEDDING', 'INDEXING')
      AND lease_until > clock_timestamp()
    RETURNING id
    """
)

INSERT_CHUNK_SQL: Final = text(
    """
    INSERT INTO chunk
        (id, generation_id, organization_id, kb_id, document_id, version_id,
         chunk_index, text, text_hash, model_input_hash, parser_version, chunker_version,
         token_count, heading_path, source_locator, fts)
    VALUES
        (:id, :generation_id, :organization_id, :kb_id, :document_id, :version_id,
         :chunk_index, :text, :text_hash, :model_input_hash, :parser_version, :chunker_version,
         :token_count, CAST(:heading_path AS jsonb), CAST(:source_locator AS jsonb),
         to_tsvector('simple', :fts_source))
    """
)

INSERT_CHUNK_EMBEDDING_SQL: Final = text(
    """
    INSERT INTO chunk_embedding (chunk_id, profile_id, embedding)
    VALUES (:chunk_id, :profile_id, CAST(:embedding AS vector))
    """
)

COUNT_CHUNKS_SQL: Final = text(
    "SELECT count(*) FROM chunk WHERE generation_id = :generation_id"
)

COUNT_EMBEDDINGS_SQL: Final = text(
    """
    SELECT count(*) FROM chunk_embedding AS ce
    JOIN chunk AS c ON c.id = ce.chunk_id
    WHERE c.generation_id = :generation_id AND ce.profile_id = :profile_id
    """
)


def _vector_literal(vector: Sequence[float]) -> str:
    """把向量渲染成 pgvector 可解析的 ``[v0,v1,...]`` 字面量；仅用于参数绑定。"""

    return json.dumps([float(value) for value in vector], allow_nan=False)


def create_staging_generation(
    session_factory: SyncSessionFactory,
    *,
    job_id: uuid.UUID,
    lease_token: str,
    claimed: ClaimedJob,
    chunks: Sequence[Chunk],
    vectors: Sequence[Sequence[float]],
    keyword_analyzer: KeywordAnalyzerLike,
) -> uuid.UUID:
    """在同一短事务内登记 BUILDING generation、绑定 job 并写入 chunk/向量/词流。

    数量与向量维度在提交前核对；任一不符即回滚并抛 :class:`StagingValidationError`，绝不发布。
    租约 CAS 失败抛 :class:`LeaseLost`，不写任何业务状态。
    """

    if len(chunks) != len(vectors) or not chunks:
        raise StagingValidationError("chunk 与向量数量不一致")
    generation_id = uuid.uuid4()
    with session_factory() as session, session.begin():
        session.execute(
            INSERT_GENERATION_SQL,
            {
                "id": generation_id,
                "version_id": claimed.version_id,
                "profile_id": claimed.profile_id,
                "expected_chunks": len(chunks),
            },
        )
        bound = session.execute(
            BIND_GENERATION_SQL,
            {
                "job_id": job_id,
                "lease_token": lease_token,
                "generation_id": generation_id,
            },
        ).first()
        if bound is None:
            raise LeaseLost("暂存阶段租约已失效")
        for chunk, vector in zip(chunks, vectors):
            if len(vector) != EMBEDDING_DIMENSION:
                raise StagingValidationError("向量维度不是 512")
            chunk_id = uuid.uuid4()
            model_input = build_model_input(chunk.heading_path, chunk.text)
            session.execute(
                INSERT_CHUNK_SQL,
                {
                    "id": chunk_id,
                    "generation_id": generation_id,
                    "organization_id": claimed.organization_id,
                    "kb_id": claimed.kb_id,
                    "document_id": claimed.document_id,
                    "version_id": claimed.version_id,
                    "chunk_index": chunk.chunk_index,
                    "text": chunk.text,
                    "text_hash": chunk.text_hash,
                    "model_input_hash": chunk.model_input_hash,
                    "parser_version": chunk.parser_version,
                    "chunker_version": chunk.chunker_version,
                    "token_count": chunk.token_count,
                    "heading_path": json.dumps(
                        list(chunk.heading_path), ensure_ascii=False
                    ),
                    "source_locator": json.dumps(
                        chunk.source_locator,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "fts_source": keyword_analyzer.analyze(model_input),
                },
            )
            session.execute(
                INSERT_CHUNK_EMBEDDING_SQL,
                {
                    "chunk_id": chunk_id,
                    "profile_id": claimed.profile_id,
                    "embedding": _vector_literal(vector),
                },
            )
        written_chunks = session.execute(
            COUNT_CHUNKS_SQL, {"generation_id": generation_id}
        ).scalar_one()
        written_embeddings = session.execute(
            COUNT_EMBEDDINGS_SQL,
            {"generation_id": generation_id, "profile_id": claimed.profile_id},
        ).scalar_one()
        if written_chunks != len(chunks) or written_embeddings != len(chunks):
            raise StagingValidationError("暂存后的 chunk/向量数量不完整")
    return generation_id


# --- 发布 ---------------------------------------------------------------------

SELECT_JOB_FOR_PUBLISH_SQL: Final = text(
    """
    SELECT
        j.status,
        j.lease_token,
        j.lease_until > clock_timestamp() AS lease_valid,
        j.generation_id,
        j.profile_id,
        j.document_id,
        j.version_id,
        j.dedupe_key,
        dv.version_no,
        d.kb_id,
        d.active_version_id,
        d.deleted_at,
        d.lifecycle_status
    FROM ingest_job AS j
    JOIN document AS d ON d.id = j.document_id
    JOIN document_version AS dv ON dv.id = j.version_id
    WHERE j.id = :job_id
    FOR UPDATE OF j, d
    """
)

PUBLISH_KB_SQL: Final = text(
    """
    UPDATE knowledge_base
    SET active_index_profile_id = :profile_id,
        kb_revision = kb_revision + 1
    WHERE id = :kb_id
      AND (active_index_profile_id IS NULL OR active_index_profile_id = :profile_id)
    RETURNING active_index_profile_id
    """
)

PUBLISH_GENERATION_SQL: Final = text(
    """
    UPDATE index_generation
    SET status = 'READY',
        actual_chunks = expected_chunks,
        ready_at = clock_timestamp(),
        updated_at = clock_timestamp()
    WHERE id = :generation_id
      AND status = 'BUILDING'
      AND version_id = :version_id
      AND profile_id = :profile_id
    RETURNING id
    """
)

PUBLISH_DOCUMENT_SQL: Final = text(
    """
    UPDATE document
    SET active_version_id = :version_id,
        lifecycle_status = 'READY',
        updated_at = clock_timestamp()
    WHERE id = :document_id
      AND active_version_id IS NULL
    RETURNING id
    """
)

# 文档新版本发布：行锁内原子 compare expected active version 且未删除，命中才切换指针。
# 并发更新者或删除者先提交时该 UPDATE 不命中，整个事务回滚，旧版本继续服务。
PUBLISH_DOCUMENT_UPDATE_SQL: Final = text(
    """
    UPDATE document
    SET active_version_id = :version_id,
        lifecycle_status = 'READY',
        updated_at = clock_timestamp()
    WHERE id = :document_id
      AND active_version_id = :expected_active_version_id
      AND deleted_at IS NULL
    RETURNING id
    """
)

PUBLISH_VERSION_SQL: Final = text(
    """
    UPDATE document_version
    SET status = 'READY', updated_at = clock_timestamp()
    WHERE id = :version_id AND status = 'PENDING'
    RETURNING id
    """
)

PUBLISH_JOB_SQL: Final = text(
    """
    UPDATE ingest_job
    SET status = 'READY',
        generation_id = :generation_id,
        lease_owner = NULL,
        lease_token = NULL,
        lease_until = NULL,
        heartbeat_at = NULL,
        error_code = NULL,
        updated_at = clock_timestamp()
    WHERE id = :job_id
      AND lease_token = :lease_token
      AND status IN ('PARSING', 'CHUNKING', 'EMBEDDING', 'INDEXING')
    RETURNING id
    """
)


class PublishOutcome(Enum):
    """发布事务结果；只有 PUBLISHED 代表文档已可检索。"""

    PUBLISHED = "PUBLISHED"
    LEASE_LOST = "LEASE_LOST"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    CONFLICT = "CONFLICT"


def publish_ingest_generation(
    session_factory: SyncSessionFactory,
    *,
    job_id: uuid.UUID,
    lease_token: str,
    generation_id: uuid.UUID,
    expected_chunks: int,
) -> PublishOutcome:
    """同一事务内发布 READY generation 并切换文档/KB 指针；任一步失败整体回滚。

    只有 PUBLISHED 才 commit；其余结果都显式 rollback，避免返回前已写入的 KB 递增被提交。
    """

    with session_factory() as session:
        try:
            row = session.execute(
                SELECT_JOB_FOR_PUBLISH_SQL, {"job_id": job_id}
            ).mappings().first()
            if row is None:
                session.rollback()
                return PublishOutcome.LEASE_LOST
            if (
                row["lease_token"] != lease_token
                or not bool(row["lease_valid"])
                or str(row["status"]) not in ACTIVE_JOB_STATUSES
            ):
                session.rollback()
                return PublishOutcome.LEASE_LOST
            if row["generation_id"] != generation_id or row["profile_id"] is None:
                session.rollback()
                return PublishOutcome.CONFLICT
            if row["deleted_at"] is not None:
                session.rollback()
                return PublishOutcome.OUT_OF_SCOPE

            # 首次版本直接激活；文档新版本在行锁内原子 compare expected active 且未删除。
            version_no = int(row["version_no"])
            parsed_dedupe = parse_version_dedupe_key(str(row["dedupe_key"]))
            is_update = version_no > 1
            expected_active_version_id: uuid.UUID | None = None
            if is_update:
                if parsed_dedupe is None or parsed_dedupe[0] != row["document_id"]:
                    session.rollback()
                    return PublishOutcome.CONFLICT
                expected_active_version_id = parsed_dedupe[1]
                if row["active_version_id"] != expected_active_version_id:
                    session.rollback()
                    return PublishOutcome.CONFLICT
            elif (
                row["active_version_id"] is not None
                or str(row["lifecycle_status"]) not in ("CREATED", "INDEXING")
            ):
                session.rollback()
                return PublishOutcome.OUT_OF_SCOPE

            written_chunks = session.execute(
                COUNT_CHUNKS_SQL, {"generation_id": generation_id}
            ).scalar_one()
            written_embeddings = session.execute(
                COUNT_EMBEDDINGS_SQL,
                {"generation_id": generation_id, "profile_id": row["profile_id"]},
            ).scalar_one()
            if written_chunks != expected_chunks or written_embeddings != expected_chunks:
                session.rollback()
                return PublishOutcome.CONFLICT

            kb = session.execute(
                PUBLISH_KB_SQL,
                {"kb_id": row["kb_id"], "profile_id": row["profile_id"]},
            ).first()
            if kb is None:
                # KB 的 active_index_profile_id 已是非 NULL 且与目标 profile 不同。
                session.rollback()
                return PublishOutcome.CONFLICT
            generation = session.execute(
                PUBLISH_GENERATION_SQL,
                {
                    "generation_id": generation_id,
                    "version_id": row["version_id"],
                    "profile_id": row["profile_id"],
                },
            ).first()
            if generation is None:
                session.rollback()
                return PublishOutcome.CONFLICT
            if is_update:
                document = session.execute(
                    PUBLISH_DOCUMENT_UPDATE_SQL,
                    {
                        "document_id": row["document_id"],
                        "version_id": row["version_id"],
                        "expected_active_version_id": expected_active_version_id,
                    },
                ).first()
            else:
                document = session.execute(
                    PUBLISH_DOCUMENT_SQL,
                    {"document_id": row["document_id"], "version_id": row["version_id"]},
                ).first()
            if document is None:
                session.rollback()
                return PublishOutcome.CONFLICT if is_update else PublishOutcome.OUT_OF_SCOPE
            version = session.execute(
                PUBLISH_VERSION_SQL, {"version_id": row["version_id"]}
            ).first()
            if version is None:
                session.rollback()
                return PublishOutcome.CONFLICT
            published = session.execute(
                PUBLISH_JOB_SQL,
                {
                    "job_id": job_id,
                    "lease_token": lease_token,
                    "generation_id": generation_id,
                },
            ).first()
            if published is None:
                session.rollback()
                return PublishOutcome.LEASE_LOST
            session.commit()
            return PublishOutcome.PUBLISHED
        except BaseException:
            session.rollback()
            raise


# --- 编码（有限重试） -----------------------------------------------------------

def embed_chunk_inputs(
    embedder: DocumentEmbedder,
    inputs: Sequence[str],
    *,
    max_attempts: int = DEFAULT_MAX_EMBEDDING_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> list[list[float]]:
    """调用受限客户端编码；只有暂时性编码失败在总预算内退避重试。"""

    if max_attempts < 1:
        raise ValueError("max_attempts 必须为正整数")
    attempt = 0
    while True:
        attempt += 1
        try:
            return embedder.embed_document_texts(inputs)
        except EmbeddingClientError as error:
            if attempt >= max_attempts or not _retryable(error):
                raise
            sleep(min(2.0**attempt, MAX_RETRY_BACKOFF_SECONDS))


# --- 编排 ---------------------------------------------------------------------

def load_ingest_identity() -> ResolvedIdentity:
    """按需构造真实 worker 索引身份；失败统一收敛为可映射的本地依赖不可用。

    只有显式调用本函数才会导入 ``worker_index_identity`` 并校验 tokenizer 资产；模块导入期
    不加载 ``tokenizers``，也不注册启动信令。调用方（真实 worker 任务）把它作为 identity
    provider 注入管线，从而在领取任务之后才可能因资产缺失而失败并被映射为静态终态。
    """

    from rag_backend.ingestion.worker_index_identity import (
        WorkerIndexIdentityError,
        initialize_worker_index_identity,
    )

    try:
        return initialize_worker_index_identity()
    except WorkerIndexIdentityError:
        raise PipelineDependencyUnavailable() from None


@dataclass(frozen=True)
class PipelineDependencies:
    """一次管线执行所需的可注入依赖；生产入口负责构造真实实现。

    ``parse_document`` 是 Markdown 解析入口，``parse_pdf_document``/``parse_docx_document``
    分别是 PDF/DOCX 解析入口；管线按 ``document.source_type`` 分派，都默认走受控子进程。
    """

    session_factory: SyncSessionFactory
    storage: DocumentBlobStore
    identity_provider: IdentityProvider
    embedder_factory: EmbedderFactory
    parse_document: ParseDocument = parse_markdown_in_subprocess
    parse_pdf_document: ParseDocument = parse_pdf_in_subprocess
    parse_docx_document: ParseDocument = parse_docx_in_subprocess
    cache_lookup: CacheLookup = load_cached_embeddings


def process_ingest_event(
    dependencies: PipelineDependencies,
    *,
    job_id: uuid.UUID,
    event_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    max_embedding_attempts: int = DEFAULT_MAX_EMBEDDING_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """执行一次真实入库；返回可观察状态字符串。"""

    session_factory = dependencies.session_factory
    claim = claim_ingest_job(
        session_factory,
        job_id=job_id,
        event_id=event_id,
        expected_parser_versions={
            SOURCE_TYPE_MARKDOWN: MARKDOWN_PARSER_VERSION,
            SOURCE_TYPE_PDF: PDF_PARSER_VERSION,
            SOURCE_TYPE_DOCX: DOCX_PARSER_VERSION,
        },
        lease_seconds=lease_seconds,
    )
    if claim.status != PROCESS_STATUS_CLAIMED or claim.claimed is None:
        return claim.status
    claimed = claim.claimed
    lease_token = claimed.lease_token

    with LeaseHeartbeat(
        session_factory,
        job_id=job_id,
        lease_token=lease_token,
        lease_seconds=lease_seconds,
        interval_seconds=heartbeat_interval_seconds,
    ) as heartbeat:
        try:
            identity = dependencies.identity_provider()
        except PipelineDependencyUnavailable:
            return _fail(
                session_factory,
                heartbeat,
                job_id=job_id,
                lease_token=lease_token,
                error_code=ERROR_PIPELINE_IDENTITY_UNAVAILABLE,
            )

        expected_parser_version = (
            identity.pdf_parser_version
            if claimed.source_type == SOURCE_TYPE_PDF
            else identity.docx_parser_version
            if claimed.source_type == SOURCE_TYPE_DOCX
            else identity.parser_version
        )
        try:
            stored = load_stored_profile(session_factory, claimed.profile_id)
        except SQLAlchemyError:
            return recover_from_db_error(
                session_factory, heartbeat, job_id=job_id, lease_token=lease_token
            )
        decision = decide_profile_identity(
            job_profile_id=claimed.profile_id,
            source_type=claimed.source_type,
            parser_version=claimed.parser_version,
            stored_profile=stored,
            expected=identity.profile,
            expected_parser_version=expected_parser_version,
        )
        identity_error = classify_identity_decision(decision)
        if identity_error is not None:
            return _fail(
                session_factory,
                heartbeat,
                job_id=job_id,
                lease_token=lease_token,
                error_code=identity_error,
            )

        if heartbeat.lost:
            return PROCESS_STATUS_LEASE_LOST
        try:
            advanced_to_chunking = advance_ingest_stage(
                session_factory,
                job_id=job_id,
                lease_token=lease_token,
                from_status=JOB_STATUS_PARSING,
                to_status=JOB_STATUS_CHUNKING,
            )
        except SQLAlchemyError:
            return recover_from_db_error(
                session_factory, heartbeat, job_id=job_id, lease_token=lease_token
            )
        if not advanced_to_chunking:
            return PROCESS_STATUS_LEASE_LOST

        try:
            if claimed.source_type == SOURCE_TYPE_PDF:
                parsed = dependencies.parse_pdf_document(
                    dependencies.storage.read_verified_pdf(
                        claimed.kb_id, claimed.file_ref, claimed.file_hash
                    )
                )
            elif claimed.source_type == SOURCE_TYPE_DOCX:
                parsed = dependencies.parse_docx_document(
                    dependencies.storage.read_verified_docx(
                        claimed.kb_id, claimed.file_ref, claimed.file_hash
                    )
                )
            else:
                markdown_text = dependencies.storage.read_verified_markdown(
                    claimed.kb_id, claimed.file_ref, claimed.file_hash
                )
                parsed = dependencies.parse_document(markdown_text.encode("utf-8"))
        except (BlobReadError, InvalidBlobReference):
            return _fail(
                session_factory,
                heartbeat,
                job_id=job_id,
                lease_token=lease_token,
                error_code=ERROR_PIPELINE_BLOB_INVALID,
            )
        except ParseSubprocessTimeout:
            return _fail(
                session_factory,
                heartbeat,
                job_id=job_id,
                lease_token=lease_token,
                error_code=ERROR_PIPELINE_PARSE_TIMEOUT,
            )
        except PdfEncryptedSubprocessError:
            return _fail(
                session_factory,
                heartbeat,
                job_id=job_id,
                lease_token=lease_token,
                error_code=ERROR_PIPELINE_PDF_ENCRYPTED,
            )
        except PdfTooManyPagesSubprocessError:
            return _fail(
                session_factory,
                heartbeat,
                job_id=job_id,
                lease_token=lease_token,
                error_code=ERROR_PIPELINE_PDF_TOO_MANY_PAGES,
            )
        except PdfInvalidSubprocessError:
            return _fail(
                session_factory,
                heartbeat,
                job_id=job_id,
                lease_token=lease_token,
                error_code=ERROR_PIPELINE_PDF_INVALID,
            )
        except DocxUnsupportedSubprocessError:
            return _fail(
                session_factory,
                heartbeat,
                job_id=job_id,
                lease_token=lease_token,
                error_code=ERROR_PIPELINE_DOCX_UNSUPPORTED,
            )
        except DocxInvalidSubprocessError:
            return _fail(
                session_factory,
                heartbeat,
                job_id=job_id,
                lease_token=lease_token,
                error_code=ERROR_PIPELINE_DOCX_INVALID,
            )
        except ParseSubprocessError:
            return _fail(
                session_factory,
                heartbeat,
                job_id=job_id,
                lease_token=lease_token,
                error_code=ERROR_PIPELINE_PARSE_FAILED,
            )
        if parsed.source_sha256 != claimed.file_hash:
            return _fail(
                session_factory,
                heartbeat,
                job_id=job_id,
                lease_token=lease_token,
                error_code=ERROR_PIPELINE_SOURCE_HASH_MISMATCH,
            )
        if parsed.parser_version != expected_parser_version:
            # 解析结果声明的 parser 版本必须与本次索引身份一致；不符不 embed/stage/publish。
            return _fail(
                session_factory,
                heartbeat,
                job_id=job_id,
                lease_token=lease_token,
                error_code=ERROR_PIPELINE_PARSE_FAILED,
            )
        try:
            chunks = chunk_markdown(parsed, identity.token_counter)
        except NoChunkableContent:
            if claimed.source_type == SOURCE_TYPE_PDF:
                # 零可提取文本（扫描件）：不把空提取当成功，落 NEEDS_OCR 终态。
                return _fail_needs_ocr(
                    session_factory,
                    heartbeat,
                    job_id=job_id,
                    lease_token=lease_token,
                )
            return _fail(
                session_factory,
                heartbeat,
                job_id=job_id,
                lease_token=lease_token,
                error_code=ERROR_PIPELINE_CONTENT_EMPTY,
            )
        except ChunkingError:
            return _fail(
                session_factory,
                heartbeat,
                job_id=job_id,
                lease_token=lease_token,
                error_code=ERROR_PIPELINE_CHUNK_FAILED,
            )

        try:
            advanced_to_embedding = advance_ingest_stage(
                session_factory,
                job_id=job_id,
                lease_token=lease_token,
                from_status=JOB_STATUS_CHUNKING,
                to_status=JOB_STATUS_EMBEDDING,
            )
        except SQLAlchemyError:
            return recover_from_db_error(
                session_factory, heartbeat, job_id=job_id, lease_token=lease_token
            )
        if not advanced_to_embedding:
            return PROCESS_STATUS_LEASE_LOST

        # 按 model_input_hash 去重，命中缓存的 hash 不再编码；重复 chunk 只编码一次并 fan-out。
        inputs_by_hash: dict[str, str] = {}
        for chunk in chunks:
            inputs_by_hash.setdefault(
                chunk.model_input_hash, build_model_input(chunk.heading_path, chunk.text)
            )

        # 缓存查询是纯优化：独立短只读连接，失败只记静态 warning 并回退为全量 miss 编码。
        # 不吞 KeyboardInterrupt/SystemExit/MemoryError，只捕预期 SQLAlchemy/缓存解析类错误。
        try:
            raw_cached = dependencies.cache_lookup(
                session_factory,
                organization_id=claimed.organization_id,
                profile_id=claimed.profile_id,
                model_input_hashes=list(inputs_by_hash),
            )
        except SQLAlchemyError:
            logger.warning(
                "embedding cache lookup failed; encoding all chunks job_id=%s", job_id
            )
            cached_by_hash: dict[str, list[float]] = {}
        else:
            cached_by_hash = as_cache_vectors(raw_cached)

        missing_inputs: dict[str, str] = {
            model_input_hash: text
            for model_input_hash, text in inputs_by_hash.items()
            if model_input_hash not in cached_by_hash
        }
        vectors_by_hash = dict(cached_by_hash)
        if missing_inputs:
            embedder: DocumentEmbedder | None = None
            try:
                try:
                    embedder = dependencies.embedder_factory(identity.token_counter)
                except PipelineDependencyUnavailable:
                    return _fail(
                        session_factory,
                        heartbeat,
                        job_id=job_id,
                        lease_token=lease_token,
                        error_code=ERROR_PIPELINE_EMBEDDING_FAILED,
                    )
                try:
                    encoded = embed_chunk_inputs(
                        embedder,
                        list(missing_inputs.values()),
                        max_attempts=max_embedding_attempts,
                        sleep=sleep,
                    )
                except EmbeddingClientError as error:
                    return _fail(
                        session_factory,
                        heartbeat,
                        job_id=job_id,
                        lease_token=lease_token,
                        error_code=classify_embedding_error(error),
                    )
            finally:
                if embedder is not None:
                    embedder.close()

            if len(encoded) != len(missing_inputs):
                return _fail(
                    session_factory,
                    heartbeat,
                    job_id=job_id,
                    lease_token=lease_token,
                    error_code=ERROR_PIPELINE_STAGING_INVALID,
                )
            for model_input_hash, encoded_vector in zip(missing_inputs, encoded):
                vectors_by_hash[model_input_hash] = encoded_vector

        # 输出顺序必须与 chunks 一致：命中与 miss 都按 chunk 的 hash 取回，绝不重排。
        vectors: list[list[float]] = []
        for chunk in chunks:
            cached_vector = vectors_by_hash.get(chunk.model_input_hash)
            if cached_vector is None:
                return _fail(
                    session_factory,
                    heartbeat,
                    job_id=job_id,
                    lease_token=lease_token,
                    error_code=ERROR_PIPELINE_STAGING_INVALID,
                )
            vectors.append(cached_vector)

        try:
            advanced_to_indexing = advance_ingest_stage(
                session_factory,
                job_id=job_id,
                lease_token=lease_token,
                from_status=JOB_STATUS_EMBEDDING,
                to_status=JOB_STATUS_INDEXING,
            )
        except SQLAlchemyError:
            return recover_from_db_error(
                session_factory, heartbeat, job_id=job_id, lease_token=lease_token
            )
        if not advanced_to_indexing:
            return PROCESS_STATUS_LEASE_LOST

        generation_id: uuid.UUID | None = None
        try:
            generation_id = create_staging_generation(
                session_factory,
                job_id=job_id,
                lease_token=lease_token,
                claimed=claimed,
                chunks=chunks,
                vectors=vectors,
                keyword_analyzer=identity.keyword_analyzer,
            )
        except LeaseLost:
            return PROCESS_STATUS_LEASE_LOST
        except StagingValidationError:
            return _fail(
                session_factory,
                heartbeat,
                job_id=job_id,
                lease_token=lease_token,
                error_code=ERROR_PIPELINE_STAGING_INVALID,
            )
        except SQLAlchemyError:
            return recover_from_db_error(
                session_factory, heartbeat, job_id=job_id, lease_token=lease_token
            )

        if heartbeat.lost:
            return PROCESS_STATUS_LEASE_LOST

        try:
            outcome = publish_ingest_generation(
                session_factory,
                job_id=job_id,
                lease_token=lease_token,
                generation_id=generation_id,
                expected_chunks=len(chunks),
            )
        except SQLAlchemyError:
            return recover_from_db_error(
                session_factory, heartbeat, job_id=job_id, lease_token=lease_token
            )
        if outcome is PublishOutcome.PUBLISHED:
            return PROCESS_STATUS_READY
        if outcome is PublishOutcome.LEASE_LOST:
            return PROCESS_STATUS_LEASE_LOST
        error_code = (
            ERROR_PIPELINE_UNSUPPORTED_UPDATE
            if outcome is PublishOutcome.OUT_OF_SCOPE
            else ERROR_PIPELINE_PUBLISH_CONFLICT
        )
        return _fail(
            session_factory,
            heartbeat,
            job_id=job_id,
            lease_token=lease_token,
            error_code=error_code,
            generation_id=generation_id,
        )


def _fail(
    session_factory: SyncSessionFactory,
    heartbeat: LeaseHeartbeat,
    *,
    job_id: uuid.UUID,
    lease_token: str,
    error_code: str,
    generation_id: uuid.UUID | None = None,
) -> str:
    """在发布前失败时写静态终态；失租约则返回 ``lease_lost`` 且不覆盖他人。"""

    if heartbeat.lost:
        return PROCESS_STATUS_LEASE_LOST
    try:
        marked = fail_ingest_job(
            session_factory,
            job_id=job_id,
            lease_token=lease_token,
            error_code=error_code,
            generation_id=generation_id,
        )
    except SQLAlchemyError:
        return recover_from_db_error(
            session_factory, heartbeat, job_id=job_id, lease_token=lease_token
        )
    if not marked:
        return PROCESS_STATUS_LEASE_LOST
    logger.info("ingest pipeline failed job_id=%s error_code=%s", job_id, error_code)
    return PROCESS_STATUS_FAILED


def _fail_needs_ocr(
    session_factory: SyncSessionFactory,
    heartbeat: LeaseHeartbeat,
    *,
    job_id: uuid.UUID,
    lease_token: str,
) -> str:
    """零可提取文本 PDF 的终态写入；失租约则返回 ``lease_lost`` 且不覆盖他人。"""

    if heartbeat.lost:
        return PROCESS_STATUS_LEASE_LOST
    try:
        marked = mark_ingest_needs_ocr(
            session_factory, job_id=job_id, lease_token=lease_token
        )
    except SQLAlchemyError:
        return recover_from_db_error(
            session_factory, heartbeat, job_id=job_id, lease_token=lease_token
        )
    if not marked:
        return PROCESS_STATUS_LEASE_LOST
    logger.info(
        "ingest pipeline needs ocr job_id=%s error_code=%s", job_id, ERROR_PIPELINE_NEEDS_OCR
    )
    return PROCESS_STATUS_FAILED


__all__ = [
    "CacheLookup",
    "ClaimAction",
    "ClaimFacts",
    "ClaimResult",
    "ClaimedJob",
    "DEFAULT_HEARTBEAT_INTERVAL_SECONDS",
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_MAX_EMBEDDING_ATTEMPTS",
    "DocumentEmbedder",
    "ERROR_PIPELINE_BLOB_INVALID",
    "ERROR_PIPELINE_CONTENT_EMPTY",
    "ERROR_PIPELINE_DB_ERROR",
    "ERROR_PIPELINE_DOCX_INVALID",
    "ERROR_PIPELINE_DOCX_UNSUPPORTED",
    "ERROR_PIPELINE_EMBEDDING_FAILED",
    "ERROR_PIPELINE_IDENTITY_UNAVAILABLE",
    "ERROR_PIPELINE_NEEDS_OCR",
    "ERROR_PIPELINE_PARSE_FAILED",
    "ERROR_PIPELINE_PARSE_TIMEOUT",
    "ERROR_PIPELINE_PDF_ENCRYPTED",
    "ERROR_PIPELINE_PDF_INVALID",
    "ERROR_PIPELINE_PDF_TOO_MANY_PAGES",
    "ERROR_PIPELINE_PROFILE_MISMATCH",
    "ERROR_PIPELINE_PUBLISH_CONFLICT",
    "ERROR_PIPELINE_STALE_EXPECTED",
    "ERROR_PIPELINE_STAGING_INVALID",
    "ERROR_PIPELINE_UNSUPPORTED_UPDATE",
    "IdentityProvider",
    "KeywordAnalyzerLike",
    "LeaseHeartbeat",
    "LeaseLost",
    "ParseDocument",
    "PipelineDependencies",
    "PipelineDependencyUnavailable",
    "PROCESS_STATUS_PERSIST_UNCONFIRMED",
    "PROCESS_STATUS_STALE_EXPECTED",
    "PROCESS_STATUS_NOT_DUE",
    "PublishOutcome",
    "ResolveOutcome",
    "ResolvedIdentity",
    "StagingValidationError",
    "advance_ingest_stage",
    "claim_ingest_job",
    "classify_embedding_error",
    "classify_identity_decision",
    "create_staging_generation",
    "decide_claim_action",
    "embed_chunk_inputs",
    "fail_ingest_job",
    "load_stored_profile",
    "load_ingest_identity",
    "mark_ingest_needs_ocr",
    "process_ingest_event",
    "publish_ingest_generation",
    "recover_from_db_error",
    "resolve_after_db_error",
]
