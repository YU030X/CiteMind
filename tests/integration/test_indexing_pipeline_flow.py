"""真实入库管线在真实 PostgreSQL 上的验收：假编码器 + 真 worker 角色。

覆盖成功 READY、重复投递、旧任务/诊断/marker 保护、坏 blob、空正文、编码失败、租约过期、
发布冲突不激活，以及同一 KB 两个文档发布不丢指针。编码器与 token 计数器是显式假实现，
**不**代表真实模型或 inference 连通性；真实模型/PG/broker 的完整验收由独立 tester 负责。

只使用破坏性测试库与三角色 DSN 守卫；缺少 DSN 时按既有契约跳过，绝不触碰开发库。
"""

from __future__ import annotations

import hashlib
import sys
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from database_guard import DestructiveTestDatabase
from database_roles_guard import (
    RoleTestDatabases,
    assert_destructive_matches_roles,
)
from rag_backend.database import SyncSessionFactory, create_sync_session_factory
from rag_backend.ingestion import indexing_worker as iw
from rag_backend.ingestion.docx_parsing import DOCX_PARSER_VERSION
from rag_backend.ingestion.embedding_client import (
    EmbeddingBusyError,
    EmbeddingPermanentError,
)
from rag_backend.ingestion.parsing import MARKDOWN_PARSER_VERSION
from rag_backend.ingestion.pdf_parsing import PDF_PARSER_VERSION
from rag_backend.ingestion.storage import DocumentBlobStore
from rag_backend.ingestion.web_parsing import WEB_PARSER_VERSION
from rag_backend.models.profile_contract import IndexProfileContract
from sqlalchemy import Engine, create_engine, text
from test_core_migration import alembic_config, alembic_revision, business_tables

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from docx_samples import nested_table_docx, positive_samples  # noqa: E402
from pdf_samples import positive_samples as pdf_positive_samples  # noqa: E402

pytestmark = pytest.mark.integration

SCHEMA_REVISION = "20260929_0016"

PROFILE = IndexProfileContract(
    embedding_model="test/model",
    model_revision="test-revision",
    dimension=512,
    normalize=True,
    tokenizer_revision="test-tokenizer",
    chunker_version="heading-pack-v1",
    keyword_analyzer_version="test-analyzer",
)
OTHER_PROFILE = IndexProfileContract(
    embedding_model="test/other-model",
    model_revision="test-other-revision",
    dimension=512,
    normalize=True,
    tokenizer_revision="test-other-tokenizer",
    chunker_version="heading-pack-v1",
    keyword_analyzer_version="test-other-analyzer",
)

MARKDOWN_BODY = "# 标题\n\n这是正文内容。\n"
VECTOR_512 = "[" + ",".join("0.0" for _ in range(512)) + "]"

TRUNCATE_SQL = (
    "TRUNCATE chunk_embedding, chunk, index_generation, outbox_event, ingest_job, "
    "document_version, document, knowledge_base, index_profile CASCADE"
)


class FakeCounter:
    """确定性假计数器：只为走通切分预算，不声称真实 tokenizer。"""

    def count_tokens(self, text: str) -> int:
        return max(1, len(text))


class FakeAnalyzer:
    """假关键词分析器：只产出可交给 ``to_tsvector('simple', ...)`` 的词流。"""

    def analyze(self, text: str) -> str:
        return " ".join(text.split())


@dataclass(frozen=True)
class FakeIdentity:
    profile: IndexProfileContract
    parser_version: str
    pdf_parser_version: str
    docx_parser_version: str
    web_parser_version: str
    token_counter: FakeCounter
    keyword_analyzer: FakeAnalyzer


class FakeEmbedder:
    """假编码器：按脚本返回向量或抛出编码错误；记录关闭。"""

    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.calls = 0
        self.closed = False

    def embed_document_texts(self, texts: Any) -> list[list[float]]:
        self.calls += 1
        if self.mode == "permanent":
            raise EmbeddingPermanentError("rejected")
        if self.mode == "busy":
            raise EmbeddingBusyError("busy")
        return [[0.01] * 512 for _ in texts]

    def close(self) -> None:
        self.closed = True


class RecordingEmbedder(FakeEmbedder):
    """记录每次收到的文本，用于量化增量缓存减少的编码输入。"""

    def __init__(self) -> None:
        super().__init__("ok")
        self.received: list[list[str]] = []

    def embed_document_texts(self, texts: Any) -> list[list[float]]:
        self.calls += 1
        self.received.append(list(texts))
        return [[0.01] * 512 for _ in texts]


@dataclass
class SeededJob:
    job_id: uuid.UUID
    kb_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    profile_id: uuid.UUID
    file_ref: str


@pytest.fixture(scope="module")
def pipeline_schema(
    destructive_test_database: DestructiveTestDatabase,
    role_test_databases: RoleTestDatabases,
) -> Iterator[Engine]:
    # 任何迁移/清理之前先确认破坏性 DSN 与三角色 DSN 指向同一 host/port/database。
    assert_destructive_matches_roles(destructive_test_database.url, role_test_databases)
    config = alembic_config(destructive_test_database.url)
    engine = create_engine(destructive_test_database.url, pool_pre_ping=True)
    owns_schema = False
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT current_database()"))
                == destructive_test_database.database_name
            )
            assert alembic_revision(connection) is None
            assert business_tables(connection) == set()
        owns_schema = True
        command.upgrade(config, SCHEMA_REVISION)
        yield engine
    finally:
        try:
            if owns_schema:
                command.downgrade(config, "base")
                with engine.connect() as connection:
                    assert alembic_revision(connection) is None
                    assert business_tables(connection) == set()
        finally:
            engine.dispose()


@pytest.fixture(autouse=True)
def clean_business_rows(pipeline_schema: Engine) -> Iterator[None]:
    with pipeline_schema.begin() as connection:
        connection.execute(text(TRUNCATE_SQL))
    yield
    # 测试后也清空：migration 0013 的 downgrade 会在存在 docx 行时拒绝，必须先清数据再降级。
    with pipeline_schema.begin() as connection:
        connection.execute(text(TRUNCATE_SQL))


@pytest.fixture(scope="module")
def worker_sessions(
    pipeline_schema: Engine, role_test_databases: RoleTestDatabases
) -> Iterator[SyncSessionFactory]:
    engine = create_engine(role_test_databases.worker_url, pool_pre_ping=True)
    try:
        yield create_sync_session_factory(engine)
    finally:
        engine.dispose()


@pytest.fixture
def storage(tmp_path: Path) -> DocumentBlobStore:
    return DocumentBlobStore(tmp_path)


def ensure_profile(engine: Engine, profile: IndexProfileContract) -> uuid.UUID:
    config_hash = profile.config_hash()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO index_profile (id, embedding_model, model_revision, dimension, "
                "normalize, tokenizer_revision, chunker_version, keyword_analyzer_version, "
                "config_hash) VALUES (:id, :embedding_model, :model_revision, 512, true, "
                ":tokenizer_revision, :chunker_version, :keyword_analyzer_version, :config_hash) "
                "ON CONFLICT (config_hash) DO NOTHING"
            ),
            {
                "id": uuid.uuid4(),
                "embedding_model": profile.embedding_model,
                "model_revision": profile.model_revision,
                "tokenizer_revision": profile.tokenizer_revision,
                "chunker_version": profile.chunker_version,
                "keyword_analyzer_version": profile.keyword_analyzer_version,
                "config_hash": config_hash,
            },
        )
        row = connection.execute(
            text("SELECT id FROM index_profile WHERE config_hash = :config_hash"),
            {"config_hash": config_hash},
        ).one()
    return cast(uuid.UUID, row[0])


def seed_job(
    engine: Engine,
    storage: DocumentBlobStore,
    *,
    profile: IndexProfileContract = PROFILE,
    parser_version: str = MARKDOWN_PARSER_VERSION,
    source_type: str = "markdown",
    version_no: int = 1,
    content: bytes = MARKDOWN_BODY.encode(),
    kb_id: uuid.UUID | None = None,
    organization_id: uuid.UUID | None = None,
    kb_active_profile_id: uuid.UUID | None = None,
    document_active_version: bool = False,
    profile_bound: bool = True,
    receive_marker: bool = False,
    error_code: str | None = None,
    write_blob: bool = True,
) -> SeededJob:
    profile_id = ensure_profile(engine, profile)
    if kb_id is None:
        kb_id = uuid.uuid4()
    if organization_id is None:
        organization_id = uuid.uuid4()
    document_id = uuid.uuid4()
    version_id = uuid.uuid4()
    job_id = uuid.uuid4()
    file_hash = hashlib.sha256(content).hexdigest()
    file_ref = storage.blob_ref(kb_id, file_hash)
    mime = {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }.get(source_type, "text/markdown")
    if write_blob:
        storage.publish(kb_id, file_hash, content)

    lease_owner = f"event:{uuid.uuid4()}" if receive_marker else None
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO knowledge_base (id, organization_id, name, kb_revision, "
                "acl_revision, active_index_profile_id) "
                "VALUES (:id, :organization_id, 'kb', 0, 0, :active_profile) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": kb_id,
                "organization_id": organization_id,
                "active_profile": kb_active_profile_id,
            },
        )
        connection.execute(
            text(
                "INSERT INTO document (id, kb_id, title, source_type, lifecycle_status) "
                "VALUES (:id, :kb_id, 'doc', :source_type, 'CREATED')"
            ),
            {"id": document_id, "kb_id": kb_id, "source_type": source_type},
        )
        connection.execute(
            text(
                "INSERT INTO document_version (id, document_id, version_no, file_ref, "
                "file_hash, mime, parser_version, status) "
                "VALUES (:id, :document_id, :version_no, :file_ref, :file_hash, "
                ":mime, :parser_version, 'PENDING')"
            ),
            {
                "id": version_id,
                "document_id": document_id,
                "version_no": version_no,
                "file_ref": file_ref,
                "file_hash": file_hash,
                "mime": mime,
                "parser_version": parser_version,
            },
        )
        if document_active_version:
            connection.execute(
                text(
                    "UPDATE document SET active_version_id = :version_id "
                    "WHERE id = :document_id"
                ),
                {"version_id": version_id, "document_id": document_id},
            )
        connection.execute(
            text(
                "INSERT INTO ingest_job (id, document_id, version_id, profile_id, status, "
                "attempt, next_run_at, dedupe_key, error_code, lease_owner, lease_token, "
                "lease_until, heartbeat_at) "
                "VALUES (:id, :document_id, :version_id, :profile_id, 'QUEUED', 0, now(), "
                ":dedupe_key, :error_code, CAST(:lease_owner AS text), :lease_token, "
                "CASE WHEN CAST(:lease_owner AS text) IS NULL THEN NULL "
                "ELSE now() + interval '1 hour' END, "
                "CASE WHEN CAST(:lease_owner AS text) IS NULL THEN NULL ELSE now() END)"
            ),
            {
                "id": job_id,
                "document_id": document_id,
                "version_id": version_id,
                "profile_id": profile_id if profile_bound else None,
                "dedupe_key": uuid.uuid4().hex,
                "error_code": error_code,
                "lease_owner": lease_owner,
                "lease_token": "1" if lease_owner else None,
            },
        )
        connection.execute(
            text(
                "INSERT INTO outbox_event (id, job_id, event_type, status, dispatch_attempt, "
                "next_send_at) VALUES (:id, :job_id, 'ingest.requested', 'PENDING', 0, now())"
            ),
            {"id": uuid.uuid4(), "job_id": job_id},
        )
    return SeededJob(job_id, kb_id, document_id, version_id, profile_id, file_ref)


def seed_update_job(
    engine: Engine,
    storage: DocumentBlobStore,
    prior: SeededJob,
    *,
    content: bytes,
    profile: IndexProfileContract = PROFILE,
) -> SeededJob:
    """为既有文档追加一个新版本 job；去重键携带可解析的 expected active 版本。"""

    profile_id = ensure_profile(engine, profile)
    version_id = uuid.uuid4()
    job_id = uuid.uuid4()
    file_hash = hashlib.sha256(content).hexdigest()
    file_ref = storage.blob_ref(prior.kb_id, file_hash)
    storage.publish(prior.kb_id, file_hash, content)
    dedupe_key = f"ver1:{prior.document_id}:{'a' * 64}:{prior.version_id}"
    with engine.begin() as connection:
        next_version_no = int(
            connection.scalar(
                text(
                    "SELECT coalesce(max(version_no), 0) + 1 FROM document_version "
                    "WHERE document_id = :document_id"
                ),
                {"document_id": prior.document_id},
            )
        )
        connection.execute(
            text(
                "INSERT INTO document_version (id, document_id, version_no, file_ref, "
                "file_hash, mime, parser_version, status) "
                "VALUES (:id, :document_id, :version_no, :file_ref, :file_hash, "
                "'text/markdown', :parser_version, 'PENDING')"
            ),
            {
                "id": version_id,
                "document_id": prior.document_id,
                "version_no": next_version_no,
                "file_ref": file_ref,
                "file_hash": file_hash,
                "parser_version": MARKDOWN_PARSER_VERSION,
            },
        )
        connection.execute(
            text(
                "INSERT INTO ingest_job (id, document_id, version_id, profile_id, status, "
                "attempt, next_run_at, dedupe_key) "
                "VALUES (:id, :document_id, :version_id, :profile_id, 'QUEUED', 0, now(), "
                ":dedupe_key)"
            ),
            {
                "id": job_id,
                "document_id": prior.document_id,
                "version_id": version_id,
                "profile_id": profile_id,
                "dedupe_key": dedupe_key,
            },
        )
        connection.execute(
            text(
                "INSERT INTO outbox_event (id, job_id, event_type, status, dispatch_attempt, "
                "next_send_at) VALUES (:id, :job_id, 'ingest.requested', 'PENDING', 0, now())"
            ),
            {"id": uuid.uuid4(), "job_id": job_id},
        )
    return SeededJob(job_id, prior.kb_id, prior.document_id, version_id, profile_id, file_ref)


def make_dependencies(
    session_factory: SyncSessionFactory,
    storage: DocumentBlobStore,
    *,
    embedder: FakeEmbedder | None = None,
    identity: FakeIdentity | None = None,
) -> iw.PipelineDependencies:
    resolved_identity = identity or FakeIdentity(
        profile=PROFILE,
        parser_version=MARKDOWN_PARSER_VERSION,
        pdf_parser_version=PDF_PARSER_VERSION,
        docx_parser_version="python-docx-1.2.0-v1",
        web_parser_version=WEB_PARSER_VERSION,
        token_counter=FakeCounter(),
        keyword_analyzer=FakeAnalyzer(),
    )
    resolved_embedder = embedder or FakeEmbedder()
    return iw.PipelineDependencies(
        session_factory=session_factory,
        storage=storage,
        identity_provider=lambda: resolved_identity,
        embedder_factory=lambda counter: resolved_embedder,
    )


def read_job(engine: Engine, job_id: uuid.UUID) -> dict[str, Any]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT status, error_code, attempt, generation_id, lease_owner, lease_token, "
                "lease_until, heartbeat_at FROM ingest_job WHERE id = :id"
            ),
            {"id": job_id},
        ).mappings().one()
    return dict(row)


def read_document(engine: Engine, document_id: uuid.UUID) -> dict[str, Any]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT lifecycle_status, active_version_id FROM document WHERE id = :id"
            ),
            {"id": document_id},
        ).mappings().one()
    return dict(row)


def read_generation_status(engine: Engine, generation_id: uuid.UUID) -> str:
    with engine.connect() as connection:
        return str(
            connection.scalar(
                text("SELECT status FROM index_generation WHERE id = :id"),
                {"id": generation_id},
            )
        )


def read_kb(engine: Engine, kb_id: uuid.UUID) -> dict[str, Any]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT active_index_profile_id, kb_revision FROM knowledge_base WHERE id = :id"
            ),
            {"id": kb_id},
        ).mappings().one()
    return dict(row)


def count_rows(engine: Engine, table: str, column: str, value: uuid.UUID) -> int:
    with engine.connect() as connection:
        return int(
            connection.scalar(
                text(f"SELECT count(*) FROM {table} WHERE {column} = :value"),
                {"value": value},
            )
        )


# --- 成功与幂等 ---------------------------------------------------------------


def test_pipeline_publishes_ready_and_first_kb_pointer(
    pipeline_schema: Engine,
    worker_sessions: SyncSessionFactory,
    storage: DocumentBlobStore,
) -> None:
    seeded = seed_job(pipeline_schema, storage)
    embedder = FakeEmbedder()
    dependencies = make_dependencies(worker_sessions, storage, embedder=embedder)

    status = iw.process_ingest_event(
        dependencies, job_id=seeded.job_id, event_id=str(uuid.uuid4())
    )

    assert status == iw.PROCESS_STATUS_READY
    assert embedder.closed is True
    job = read_job(pipeline_schema, seeded.job_id)
    assert job["status"] == "READY"
    assert job["error_code"] is None
    assert job["lease_owner"] is None and job["heartbeat_at"] is None
    assert job["attempt"] == 1
    generation_id = job["generation_id"]
    assert generation_id is not None
    assert read_generation_status(pipeline_schema, generation_id) == "READY"
    document = read_document(pipeline_schema, seeded.document_id)
    assert document["lifecycle_status"] == "READY"
    assert document["active_version_id"] == seeded.version_id
    assert count_rows(pipeline_schema, "chunk", "generation_id", generation_id) == 1
    assert (
        count_rows(pipeline_schema, "chunk_embedding", "profile_id", seeded.profile_id) == 1
    )
    with pipeline_schema.connect() as connection:
        version_status = connection.scalar(
            text("SELECT status FROM document_version WHERE id = :id"),
            {"id": seeded.version_id},
        )
        fts = connection.scalar(
            text("SELECT fts FROM chunk WHERE generation_id = :id"),
            {"id": generation_id},
        )
    assert version_status == "READY"
    assert fts is not None and str(fts) != ""
    kb = read_kb(pipeline_schema, seeded.kb_id)
    assert kb["active_index_profile_id"] == seeded.profile_id
    assert kb["kb_revision"] == 1


def test_repeat_delivery_after_ready_is_not_queued(
    pipeline_schema: Engine,
    worker_sessions: SyncSessionFactory,
    storage: DocumentBlobStore,
) -> None:
    seeded = seed_job(pipeline_schema, storage)
    dependencies = make_dependencies(worker_sessions, storage)
    assert (
        iw.process_ingest_event(
            dependencies, job_id=seeded.job_id, event_id=str(uuid.uuid4())
        )
        == iw.PROCESS_STATUS_READY
    )
    before = read_job(pipeline_schema, seeded.job_id)

    status = iw.process_ingest_event(
        dependencies, job_id=seeded.job_id, event_id=str(uuid.uuid4())
    )

    assert status == iw.PROCESS_STATUS_NOT_QUEUED
    assert read_job(pipeline_schema, seeded.job_id) == before


# --- 旧任务/诊断/marker 保护 ---------------------------------------------------


def test_legacy_unbound_job_is_failed_without_processing(
    pipeline_schema: Engine,
    worker_sessions: SyncSessionFactory,
    storage: DocumentBlobStore,
) -> None:
    seeded = seed_job(pipeline_schema, storage, profile_bound=False)

    status = iw.process_ingest_event(
        make_dependencies(worker_sessions, storage),
        job_id=seeded.job_id,
        event_id=str(uuid.uuid4()),
    )

    assert status == iw.PROCESS_STATUS_LEGACY_UNSUPPORTED
    job = read_job(pipeline_schema, seeded.job_id)
    assert job["status"] == "FAILED"
    assert job["error_code"] == "LEGACY_JOB_UNSUPPORTED"
    assert job["lease_owner"] is None
    assert read_document(pipeline_schema, seeded.document_id)["active_version_id"] is None


def test_legacy_parser_job_is_failed(
    pipeline_schema: Engine,
    worker_sessions: SyncSessionFactory,
    storage: DocumentBlobStore,
) -> None:
    seeded = seed_job(pipeline_schema, storage, parser_version="markdown-v1")

    status = iw.process_ingest_event(
        make_dependencies(worker_sessions, storage),
        job_id=seeded.job_id,
        event_id=str(uuid.uuid4()),
    )

    assert status == iw.PROCESS_STATUS_LEGACY_UNSUPPORTED
    assert read_job(pipeline_schema, seeded.job_id)["error_code"] == "LEGACY_JOB_UNSUPPORTED"


def test_existing_diagnostic_is_preserved_without_marker(
    pipeline_schema: Engine,
    worker_sessions: SyncSessionFactory,
    storage: DocumentBlobStore,
) -> None:
    seeded = seed_job(pipeline_schema, storage, error_code="DELIVERY_UNCONFIRMED")
    before = read_job(pipeline_schema, seeded.job_id)

    status = iw.process_ingest_event(
        make_dependencies(worker_sessions, storage),
        job_id=seeded.job_id,
        event_id=str(uuid.uuid4()),
    )

    assert status == iw.PROCESS_STATUS_EXISTING_DIAGNOSTIC
    assert read_job(pipeline_schema, seeded.job_id) == before


def test_existing_receive_marker_is_preserved(
    pipeline_schema: Engine,
    worker_sessions: SyncSessionFactory,
    storage: DocumentBlobStore,
) -> None:
    seeded = seed_job(pipeline_schema, storage, receive_marker=True)
    before = read_job(pipeline_schema, seeded.job_id)

    status = iw.process_ingest_event(
        make_dependencies(worker_sessions, storage),
        job_id=seeded.job_id,
        event_id=str(uuid.uuid4()),
    )

    assert status == iw.PROCESS_STATUS_ALREADY_RECEIVED
    assert read_job(pipeline_schema, seeded.job_id) == before


def test_second_version_is_rejected_as_unsupported_update(
    pipeline_schema: Engine,
    worker_sessions: SyncSessionFactory,
    storage: DocumentBlobStore,
) -> None:
    seeded = seed_job(pipeline_schema, storage, version_no=2)

    status = iw.process_ingest_event(
        make_dependencies(worker_sessions, storage),
        job_id=seeded.job_id,
        event_id=str(uuid.uuid4()),
    )

    assert status == iw.PROCESS_STATUS_UNSUPPORTED_UPDATE
    job = read_job(pipeline_schema, seeded.job_id)
    assert job["status"] == "FAILED"
    assert job["error_code"] == iw.ERROR_PIPELINE_UNSUPPORTED_UPDATE
    assert read_document(pipeline_schema, seeded.document_id)["active_version_id"] is None


# --- 坏输入与编码失败 ---------------------------------------------------------


def test_missing_blob_fails_statically(
    pipeline_schema: Engine,
    worker_sessions: SyncSessionFactory,
    storage: DocumentBlobStore,
) -> None:
    seeded = seed_job(pipeline_schema, storage, write_blob=False)

    status = iw.process_ingest_event(
        make_dependencies(worker_sessions, storage),
        job_id=seeded.job_id,
        event_id=str(uuid.uuid4()),
    )

    assert status == iw.PROCESS_STATUS_FAILED
    job = read_job(pipeline_schema, seeded.job_id)
    assert job["error_code"] == iw.ERROR_PIPELINE_BLOB_INVALID
    assert read_document(pipeline_schema, seeded.document_id)["active_version_id"] is None


def test_empty_body_fails_statically(
    pipeline_schema: Engine,
    worker_sessions: SyncSessionFactory,
    storage: DocumentBlobStore,
) -> None:
    seeded = seed_job(pipeline_schema, storage, content="# 只有标题\n".encode())

    status = iw.process_ingest_event(
        make_dependencies(worker_sessions, storage),
        job_id=seeded.job_id,
        event_id=str(uuid.uuid4()),
    )

    assert status == iw.PROCESS_STATUS_FAILED
    assert (
        read_job(pipeline_schema, seeded.job_id)["error_code"]
        == iw.ERROR_PIPELINE_CONTENT_EMPTY
    )


def test_permanent_embedding_failure_fails_without_retry(
    pipeline_schema: Engine,
    worker_sessions: SyncSessionFactory,
    storage: DocumentBlobStore,
) -> None:
    seeded = seed_job(pipeline_schema, storage)
    embedder = FakeEmbedder("permanent")

    status = iw.process_ingest_event(
        make_dependencies(worker_sessions, storage, embedder=embedder),
        job_id=seeded.job_id,
        event_id=str(uuid.uuid4()),
    )

    assert status == iw.PROCESS_STATUS_FAILED
    job = read_job(pipeline_schema, seeded.job_id)
    assert job["error_code"] == iw.ERROR_PIPELINE_EMBEDDING_REJECTED
    assert job["generation_id"] is None
    assert embedder.calls == 1
    assert embedder.closed is True


def test_transient_embedding_failure_exhausts_budget_then_fails(
    pipeline_schema: Engine,
    worker_sessions: SyncSessionFactory,
    storage: DocumentBlobStore,
) -> None:
    seeded = seed_job(pipeline_schema, storage)
    embedder = FakeEmbedder("busy")

    status = iw.process_ingest_event(
        make_dependencies(worker_sessions, storage, embedder=embedder),
        job_id=seeded.job_id,
        event_id=str(uuid.uuid4()),
        max_embedding_attempts=2,
        sleep=lambda _seconds: None,
    )

    assert status == iw.PROCESS_STATUS_FAILED
    job = read_job(pipeline_schema, seeded.job_id)
    assert job["error_code"] == iw.ERROR_PIPELINE_EMBEDDING_FAILED
    assert embedder.calls == 2


# --- 租约与发布冲突 -----------------------------------------------------------


def seed_staged_generation(
    engine: Engine,
    storage: DocumentBlobStore,
    *,
    lease_token: str,
    lease_seconds_from_now: int,
    kb_id: uuid.UUID | None = None,
) -> tuple[SeededJob, uuid.UUID]:
    seeded = seed_job(engine, storage, kb_id=kb_id)
    generation_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO index_generation (id, version_id, profile_id, status, "
                "expected_chunks, actual_chunks) "
                "VALUES (:id, :version_id, :profile_id, 'BUILDING', 1, 0)"
            ),
            {
                "id": generation_id,
                "version_id": seeded.version_id,
                "profile_id": seeded.profile_id,
            },
        )
        connection.execute(
            text(
                "INSERT INTO chunk (id, generation_id, organization_id, kb_id, document_id, "
                "version_id, chunk_index, text, text_hash, model_input_hash, parser_version, "
                "chunker_version, token_count, heading_path, source_locator, fts) "
                "SELECT :id, :generation_id, kb.organization_id, :kb_id, :document_id, "
                ":version_id, 0, 'hello', 'h', 'h', :parser_version, 'heading-pack-v1', 1, "
                "'[]'::jsonb, '{}'::jsonb, to_tsvector('simple', 'hello') "
                "FROM knowledge_base AS kb WHERE kb.id = :kb_id"
            ),
            {
                "id": chunk_id,
                "generation_id": generation_id,
                "kb_id": seeded.kb_id,
                "document_id": seeded.document_id,
                "version_id": seeded.version_id,
                "parser_version": MARKDOWN_PARSER_VERSION,
            },
        )
        connection.execute(
            text(
                "INSERT INTO chunk_embedding (chunk_id, profile_id, embedding) "
                "VALUES (:chunk_id, :profile_id, CAST(:embedding AS vector))"
            ),
            {
                "chunk_id": chunk_id,
                "profile_id": seeded.profile_id,
                "embedding": VECTOR_512,
            },
        )
        connection.execute(
            text(
                "UPDATE ingest_job SET status = 'INDEXING', attempt = 1, "
                "lease_owner = 'pipeline:test', lease_token = :lease_token, "
                "lease_until = clock_timestamp() + (:seconds * interval '1 second'), "
                "heartbeat_at = clock_timestamp(), generation_id = :generation_id "
                "WHERE id = :job_id"
            ),
            {
                "job_id": seeded.job_id,
                "lease_token": lease_token,
                "seconds": lease_seconds_from_now,
                "generation_id": generation_id,
            },
        )
    return seeded, generation_id


def test_publish_rejects_wrong_or_expired_lease_without_activation(
    pipeline_schema: Engine,
    worker_sessions: SyncSessionFactory,
    storage: DocumentBlobStore,
) -> None:
    seeded, generation_id = seed_staged_generation(
        pipeline_schema, storage, lease_token="current", lease_seconds_from_now=3600
    )

    wrong = iw.publish_ingest_generation(
        worker_sessions,
        job_id=seeded.job_id,
        lease_token="stale",
        generation_id=generation_id,
        expected_chunks=1,
    )
    assert wrong is iw.PublishOutcome.LEASE_LOST
    assert read_generation_status(pipeline_schema, generation_id) == "BUILDING"
    assert read_document(pipeline_schema, seeded.document_id)["active_version_id"] is None

    # 过期租约即使 token 相同也必须拒绝。
    with pipeline_schema.begin() as connection:
        connection.execute(
            text(
                "UPDATE ingest_job SET lease_until = clock_timestamp() - interval '1 minute' "
                "WHERE id = :id"
            ),
            {"id": seeded.job_id},
        )
    expired = iw.publish_ingest_generation(
        worker_sessions,
        job_id=seeded.job_id,
        lease_token="current",
        generation_id=generation_id,
        expected_chunks=1,
    )
    assert expired is iw.PublishOutcome.LEASE_LOST
    assert read_generation_status(pipeline_schema, generation_id) == "BUILDING"
    assert read_document(pipeline_schema, seeded.document_id)["active_version_id"] is None


def test_kb_with_different_active_profile_blocks_activation(
    pipeline_schema: Engine,
    worker_sessions: SyncSessionFactory,
    storage: DocumentBlobStore,
) -> None:
    other_profile_id = ensure_profile(pipeline_schema, OTHER_PROFILE)
    seeded = seed_job(
        pipeline_schema, storage, kb_active_profile_id=other_profile_id
    )

    status = iw.process_ingest_event(
        make_dependencies(worker_sessions, storage),
        job_id=seeded.job_id,
        event_id=str(uuid.uuid4()),
    )

    assert status == iw.PROCESS_STATUS_FAILED
    job = read_job(pipeline_schema, seeded.job_id)
    assert job["error_code"] == iw.ERROR_PIPELINE_PUBLISH_CONFLICT
    assert job["generation_id"] is not None
    assert read_generation_status(pipeline_schema, job["generation_id"]) == "FAILED"
    document = read_document(pipeline_schema, seeded.document_id)
    assert document["active_version_id"] is None
    assert document["lifecycle_status"] == "FAILED"
    kb = read_kb(pipeline_schema, seeded.kb_id)
    # 既不覆盖别的 profile，也不递增 revision。
    assert kb["active_index_profile_id"] == other_profile_id
    assert kb["kb_revision"] == 0


def test_two_documents_in_same_kb_keep_the_pointer(    pipeline_schema: Engine,
    worker_sessions: SyncSessionFactory,
    storage: DocumentBlobStore,
) -> None:
    first = seed_job(pipeline_schema, storage, content="# A\n\n第一份正文。\n".encode())
    second = seed_job(
        pipeline_schema,
        storage,
        content="# B\n\n第二份正文。\n".encode(),
        kb_id=first.kb_id,
    )
    dependencies = make_dependencies(worker_sessions, storage)

    assert (
        iw.process_ingest_event(
            dependencies, job_id=first.job_id, event_id=str(uuid.uuid4())
        )
        == iw.PROCESS_STATUS_READY
    )
    assert (
        iw.process_ingest_event(
            dependencies, job_id=second.job_id, event_id=str(uuid.uuid4())
        )
        == iw.PROCESS_STATUS_READY
    )

    kb = read_kb(pipeline_schema, first.kb_id)
    assert kb["active_index_profile_id"] == first.profile_id
    assert kb["kb_revision"] == 2
    for seeded in (first, second):
        document = read_document(pipeline_schema, seeded.document_id)
        assert document["lifecycle_status"] == "READY"
        assert document["active_version_id"] == seeded.version_id


def test_concurrent_publish_of_two_documents_in_same_kb_keeps_pointer(
    pipeline_schema: Engine,
    worker_sessions: SyncSessionFactory,
    storage: DocumentBlobStore,
) -> None:
    """两个文档在同一 KB 并发发布：条件 UPDATE 串行化，指针不丢、revision 递增两次。"""

    first, first_generation = seed_staged_generation(
        pipeline_schema, storage, lease_token="first", lease_seconds_from_now=3600
    )
    second, second_generation = seed_staged_generation(
        pipeline_schema,
        storage,
        lease_token="second",
        lease_seconds_from_now=3600,
        kb_id=first.kb_id,
    )
    assert first.kb_id == second.kb_id

    results: list[iw.PublishOutcome] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)
    specs = [
        (first, first_generation, "first"),
        (second, second_generation, "second"),
    ]

    def publish(seeded: SeededJob, generation_id: uuid.UUID, lease_token: str) -> None:
        try:
            barrier.wait(timeout=10)
            results.append(
                iw.publish_ingest_generation(
                    worker_sessions,
                    job_id=seeded.job_id,
                    lease_token=lease_token,
                    generation_id=generation_id,
                    expected_chunks=1,
                )
            )
        except BaseException as error:  # 线程内异常留作诊断，不静默丢失
            errors.append(error)

    threads = [
        threading.Thread(target=publish, args=spec, name=f"publish-{spec[2]}")
        for spec in specs
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert sorted(outcome.value for outcome in results) == ["PUBLISHED", "PUBLISHED"]
    assert read_generation_status(pipeline_schema, first_generation) == "READY"
    assert read_generation_status(pipeline_schema, second_generation) == "READY"
    kb = read_kb(pipeline_schema, first.kb_id)
    assert kb["active_index_profile_id"] == first.profile_id
    assert kb["kb_revision"] == 2
    for seeded in (first, second):
        document = read_document(pipeline_schema, seeded.document_id)
        assert document["lifecycle_status"] == "READY"
        assert document["active_version_id"] == seeded.version_id


# --- 文本 PDF 真实入库（真解析子进程 + 假计数/编码器） -------------------------


def _build_pdf(pages: list[str]) -> bytes:
    """用 pypdf 构造带真实文本层的最小 PDF；空字符串表示空白页。"""

    import io

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    for page_text in pages:
        page = writer.add_blank_page(width=200, height=200)
        if page_text:
            stream = DecodedStreamObject()
            stream.set_data(
                ("BT /F1 12 Tf 10 100 Td (" + page_text + ") Tj ET").encode("latin-1")
            )
            page[NameObject("/Contents")] = writer._add_object(stream)
            page[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
            )
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_pdf_pipeline_publishes_ready_with_page_locator(
    pipeline_schema: Engine,
    worker_sessions: SyncSessionFactory,
    storage: DocumentBlobStore,
) -> None:
    raw = _build_pdf(["PDF page one body", "PDF page two body"])
    seeded = seed_job(
        pipeline_schema,
        storage,
        parser_version=PDF_PARSER_VERSION,
        source_type="pdf",
        content=raw,
    )
    dependencies = make_dependencies(worker_sessions, storage)

    status = iw.process_ingest_event(
        dependencies, job_id=seeded.job_id, event_id=str(uuid.uuid4())
    )

    assert status == iw.PROCESS_STATUS_READY
    job = read_job(pipeline_schema, seeded.job_id)
    assert job["status"] == "READY"
    document = read_document(pipeline_schema, seeded.document_id)
    assert document["active_version_id"] == seeded.version_id
    with pipeline_schema.connect() as connection:
        locators = [
            row[0]
            for row in connection.execute(
                text(
                    "SELECT source_locator FROM chunk WHERE document_id = :id "
                    "ORDER BY chunk_index"
                ),
                {"id": seeded.document_id},
            )
        ]
    assert locators
    for locator in locators:
        assert locator["locator_version"] == 2
        assert locator["source_type"] == "pdf"
        assert len(locator["pages"]) == 1
        assert "start_line" not in locator


def test_pdf_pipeline_publishes_ready_for_all_positive_samples(
    pipeline_schema: Engine,
    worker_sessions: SyncSessionFactory,
    storage: DocumentBlobStore,
) -> None:
    """真实解析子进程（假编码器）把 5 份自制 PDF 正样本发布为 READY，并落 v2 页定位。"""

    samples = pdf_positive_samples()
    assert len(samples) == 5
    for name, raw in samples.items():
        seeded = seed_job(
            pipeline_schema,
            storage,
            parser_version=PDF_PARSER_VERSION,
            source_type="pdf",
            content=raw,
        )
        dependencies = make_dependencies(worker_sessions, storage)

        status = iw.process_ingest_event(
            dependencies, job_id=seeded.job_id, event_id=str(uuid.uuid4())
        )

        assert status == iw.PROCESS_STATUS_READY, name
        assert read_job(pipeline_schema, seeded.job_id)["status"] == "READY"
        document = read_document(pipeline_schema, seeded.document_id)
        assert document["active_version_id"] == seeded.version_id, name
        with pipeline_schema.connect() as connection:
            locators = [
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT source_locator FROM chunk WHERE document_id = :id "
                        "ORDER BY chunk_index"
                    ),
                    {"id": seeded.document_id},
                )
            ]
        assert locators, name
        for locator in locators:
            assert locator["locator_version"] == 2, name
            assert locator["source_type"] == "pdf", name
            assert "start_line" not in locator, name
            assert locator["segments"], name
            assert all(
                segment["page"] is not None for segment in locator["segments"]
            ), name


def test_pdf_zero_text_marks_version_needs_ocr(
    pipeline_schema: Engine,
    worker_sessions: SyncSessionFactory,
    storage: DocumentBlobStore,
) -> None:
    raw = _build_pdf([""])
    seeded = seed_job(
        pipeline_schema,
        storage,
        parser_version=PDF_PARSER_VERSION,
        source_type="pdf",
        content=raw,
    )
    dependencies = make_dependencies(worker_sessions, storage)

    status = iw.process_ingest_event(
        dependencies, job_id=seeded.job_id, event_id=str(uuid.uuid4())
    )

    assert status == iw.PROCESS_STATUS_FAILED
    job = read_job(pipeline_schema, seeded.job_id)
    assert job["status"] == "FAILED"
    assert job["error_code"] == iw.ERROR_PIPELINE_NEEDS_OCR
    with pipeline_schema.connect() as connection:
        version_status = connection.scalar(
            text("SELECT status FROM document_version WHERE id = :id"),
            {"id": seeded.version_id},
        )
    assert version_status == "NEEDS_OCR"
    assert count_rows(pipeline_schema, "index_generation", "version_id", seeded.version_id) == 0
    assert read_document(pipeline_schema, seeded.document_id)["lifecycle_status"] == "FAILED"


def test_docx_pipeline_publishes_ready_for_all_positive_samples(
    pipeline_schema: Engine,
    worker_sessions: SyncSessionFactory,
    storage: DocumentBlobStore,
) -> None:
    """真实解析子进程（假编码器）把 5 份自制 DOCX 正样本发布为 READY，并落 v3 locator。"""

    for name, raw in positive_samples().items():
        seeded = seed_job(
            pipeline_schema,
            storage,
            parser_version=DOCX_PARSER_VERSION,
            source_type="docx",
            content=raw,
        )
        dependencies = make_dependencies(worker_sessions, storage)

        status = iw.process_ingest_event(
            dependencies, job_id=seeded.job_id, event_id=str(uuid.uuid4())
        )

        assert status == iw.PROCESS_STATUS_READY, name
        assert read_job(pipeline_schema, seeded.job_id)["status"] == "READY"
        document = read_document(pipeline_schema, seeded.document_id)
        assert document["active_version_id"] == seeded.version_id
        with pipeline_schema.connect() as connection:
            locators = [
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT source_locator FROM chunk WHERE document_id = :id "
                        "ORDER BY chunk_index"
                    ),
                    {"id": seeded.document_id},
                )
            ]
        assert locators, name
        for locator in locators:
            assert locator["locator_version"] == 3, name
            assert locator["source_type"] == "docx", name
            assert "start_line" not in locator
            assert "pages" not in locator
            assert locator["segments"]


def test_docx_pipeline_nested_table_fails_unsupported(
    pipeline_schema: Engine,
    worker_sessions: SyncSessionFactory,
    storage: DocumentBlobStore,
) -> None:
    """嵌套表是收窄子集外的结构：静态失败，不索引残缺内容。"""

    seeded = seed_job(
        pipeline_schema,
        storage,
        parser_version=DOCX_PARSER_VERSION,
        source_type="docx",
        content=nested_table_docx(),
    )
    dependencies = make_dependencies(worker_sessions, storage)

    status = iw.process_ingest_event(
        dependencies, job_id=seeded.job_id, event_id=str(uuid.uuid4())
    )

    assert status == iw.PROCESS_STATUS_FAILED
    job = read_job(pipeline_schema, seeded.job_id)
    assert job["status"] == "FAILED"
    assert job["error_code"] == iw.ERROR_PIPELINE_DOCX_UNSUPPORTED
    assert count_rows(pipeline_schema, "index_generation", "version_id", seeded.version_id) == 0


# --- 增量 embedding 缓存 ------------------------------------------------------

PARTIAL_V1 = b"# A\n\npara a\n\n# B\n\npara b\n"
PARTIAL_V2 = b"# A\n\npara a\n\n# B\n\npara b two\n"


def other_identity() -> FakeIdentity:
    return FakeIdentity(
        profile=OTHER_PROFILE,
        parser_version=MARKDOWN_PARSER_VERSION,
        pdf_parser_version=PDF_PARSER_VERSION,
        docx_parser_version="python-docx-1.2.0-v1",
        web_parser_version=WEB_PARSER_VERSION,
        token_counter=FakeCounter(),
        keyword_analyzer=FakeAnalyzer(),
    )


def count_generation_embeddings(engine: Engine, generation_id: Any) -> int:
    with engine.connect() as connection:
        return int(
            connection.scalar(
                text(
                    "SELECT count(*) FROM chunk_embedding AS ce "
                    "JOIN chunk AS c ON c.id = ce.chunk_id "
                    "WHERE c.generation_id = :generation_id"
                ),
                {"generation_id": generation_id},
            )
        )


def run_ready(
    engine: Engine,
    sessions: SyncSessionFactory,
    storage: DocumentBlobStore,
    seeded: SeededJob,
    *,
    embedder: FakeEmbedder | None = None,
    identity: FakeIdentity | None = None,
) -> str:
    return iw.process_ingest_event(
        make_dependencies(sessions, storage, embedder=embedder, identity=identity),
        job_id=seeded.job_id,
        event_id=str(uuid.uuid4()),
    )


def test_embedding_cache_reuses_identical_content_across_documents_in_same_org(
    pipeline_schema: Engine,
    worker_sessions: SyncSessionFactory,
    storage: DocumentBlobStore,
) -> None:
    org_id = uuid.uuid4()
    first = seed_job(pipeline_schema, storage, organization_id=org_id)
    assert run_ready(pipeline_schema, worker_sessions, storage, first) == iw.PROCESS_STATUS_READY

    recorder = RecordingEmbedder()
    second = seed_job(pipeline_schema, storage, organization_id=org_id)
    status = run_ready(
        pipeline_schema, worker_sessions, storage, second, embedder=recorder
    )

    assert status == iw.PROCESS_STATUS_READY
    # 同组织、同 profile、内容相同：全部命中缓存，编码器一次都不调用。
    assert recorder.calls == 0
    job = read_job(pipeline_schema, second.job_id)
    generation_id = job["generation_id"]
    chunks = count_rows(pipeline_schema, "chunk", "generation_id", generation_id)
    embeddings = count_generation_embeddings(pipeline_schema, generation_id)
    assert chunks == embeddings == 1


def test_embedding_cache_does_not_cross_organizations(
    pipeline_schema: Engine,
    worker_sessions: SyncSessionFactory,
    storage: DocumentBlobStore,
) -> None:
    first = seed_job(pipeline_schema, storage, organization_id=uuid.uuid4())
    assert run_ready(pipeline_schema, worker_sessions, storage, first) == iw.PROCESS_STATUS_READY

    recorder = RecordingEmbedder()
    second = seed_job(pipeline_schema, storage, organization_id=uuid.uuid4())
    status = run_ready(
        pipeline_schema, worker_sessions, storage, second, embedder=recorder
    )

    assert status == iw.PROCESS_STATUS_READY
    # 不同组织即使内容、profile 相同也必须 miss 并重新编码。
    assert recorder.calls == 1
    assert recorder.received == [["标题\n\n这是正文内容。"]]


def test_embedding_cache_does_not_cross_profiles(
    pipeline_schema: Engine,
    worker_sessions: SyncSessionFactory,
    storage: DocumentBlobStore,
) -> None:
    org_id = uuid.uuid4()
    first = seed_job(pipeline_schema, storage, organization_id=org_id)
    assert run_ready(pipeline_schema, worker_sessions, storage, first) == iw.PROCESS_STATUS_READY

    recorder = RecordingEmbedder()
    second = seed_job(
        pipeline_schema, storage, organization_id=org_id, profile=OTHER_PROFILE
    )
    status = run_ready(
        pipeline_schema,
        worker_sessions,
        storage,
        second,
        embedder=recorder,
        identity=other_identity(),
    )

    assert status == iw.PROCESS_STATUS_READY
    # profile/revision 不同：不命中其它 profile 的向量，必须重新编码。
    assert recorder.calls == 1
    assert len(recorder.received[0]) == 1


def test_embedding_cache_partial_update_reuses_unchanged_chunk(
    pipeline_schema: Engine,
    worker_sessions: SyncSessionFactory,
    storage: DocumentBlobStore,
) -> None:
    first = seed_job(
        pipeline_schema, storage, organization_id=uuid.uuid4(), content=PARTIAL_V1
    )
    assert run_ready(pipeline_schema, worker_sessions, storage, first) == iw.PROCESS_STATUS_READY

    recorder = RecordingEmbedder()
    update = seed_update_job(pipeline_schema, storage, first, content=PARTIAL_V2)
    status = run_ready(
        pipeline_schema, worker_sessions, storage, update, embedder=recorder
    )

    assert status == iw.PROCESS_STATUS_READY
    # 未改动的 A 段命中，只有改动的 B 段进入编码；最终仍写满 2 条向量。
    assert recorder.calls == 1
    assert recorder.received == [["B\n\npara b two"]]
    job = read_job(pipeline_schema, update.job_id)
    generation_id = job["generation_id"]
    assert count_rows(pipeline_schema, "chunk", "generation_id", generation_id) == 2
    assert count_generation_embeddings(pipeline_schema, generation_id) == 2
    # 发布 CAS 未被缓存改变：指针切到新版本。
    document = read_document(pipeline_schema, first.document_id)
    assert document["active_version_id"] == update.version_id


def test_embedding_cache_reuses_old_version_after_update_publish(
    pipeline_schema: Engine,
    worker_sessions: SyncSessionFactory,
    storage: DocumentBlobStore,
) -> None:
    org_id = uuid.uuid4()
    first = seed_job(
        pipeline_schema, storage, organization_id=org_id, content=PARTIAL_V1
    )
    assert run_ready(pipeline_schema, worker_sessions, storage, first) == iw.PROCESS_STATUS_READY
    update = seed_update_job(pipeline_schema, storage, first, content=PARTIAL_V2)
    assert run_ready(pipeline_schema, worker_sessions, storage, update) == iw.PROCESS_STATUS_READY

    recorder = RecordingEmbedder()
    third = seed_job(
        pipeline_schema, storage, organization_id=org_id, content=PARTIAL_V1
    )
    status = run_ready(
        pipeline_schema, worker_sessions, storage, third, embedder=recorder
    )

    assert status == iw.PROCESS_STATUS_READY
    # 旧版本（已不是 active）的 READY generation 仍可被同组织复用。
    assert recorder.calls == 0
    document = read_document(pipeline_schema, first.document_id)
    assert document["active_version_id"] == update.version_id
