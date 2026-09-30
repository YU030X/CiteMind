"""授权混合检索在真实 PostgreSQL 上的验收（隔离破坏性测试库 + api 运行角色）。

覆盖正例融合与排序、跨组织/非成员/撤权、NULL active profile、非 READY generation、
generation/embedding profile 不符、分析器身份不符、旧版本、删除文档、权威归属链、参数化
关键词查询，以及真实 `KeywordAnalyzer` 与真实 `chunk.fts` 的端到端匹配和 HTTP 层授权。
向量编码器是显式假实现，**不**代表真实 inference 连通性；真实模型查询编码由隔离模型
tester 在真实权重上验收，不属于本文件。

缺少守卫 DSN 时按既有契约跳过，绝不触碰开发库。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from alembic import command
from database_guard import DestructiveTestDatabase
from database_roles_guard import RoleTestDatabases, assert_destructive_matches_roles
from httpx import ASGITransport, AsyncClient
from rag_backend.api.retrieval import get_query_embedder
from rag_backend.app import create_app
from rag_backend.auth.context import AuthContext
from rag_backend.auth.dependencies import get_auth_context
from rag_backend.config import Settings
from rag_backend.database import (
    create_database_engine,
    create_session_factory,
)
from rag_backend.retrieval import errors as retrieval_errors
from rag_backend.retrieval.fusion import FusedCandidate
from rag_backend.retrieval.keyword_analyzer import get_keyword_analyzer
from rag_backend.retrieval.query_embedding_client import EmbeddedQuery
from rag_backend.retrieval.repository import SqlRetrievalRepository
from rag_backend.retrieval.service import KeywordAnalyzerLike, search_authorized_chunks
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession
from test_core_migration import alembic_config, alembic_revision, business_tables

pytestmark = pytest.mark.integration

SCHEMA_REVISION = "20260928_0012"
# 集成用的假分析器身份；profile 行必须登记同一值，否则检索按契约 fail closed。
ANALYZER_ID = "test-analyzer"

TRUNCATE_SQL = (
    "TRUNCATE chunk_embedding, chunk, index_generation, outbox_event, ingest_job, "
    "document_version, document, knowledge_base, index_profile, kb_member, auth_session, "
    "user_account CASCADE"
)

VECTOR_512 = "[" + ",".join("0.0" for _ in range(512)) + "]"
EMBEDDING_DIMENSION = 512


def _vector(*values: float) -> str:
    """构造 512 维向量字面量；未给出的分量补 0。"""

    vector = [0.0] * EMBEDDING_DIMENSION
    for index, value in enumerate(values):
        vector[index] = value
    return json.dumps(vector)


@dataclass(frozen=True)
class Seeded:
    profile_id: uuid.UUID
    kb_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    generation_id: uuid.UUID
    chunk_id: uuid.UUID


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, Any]]:
    # Windows 默认 ProactorEventLoop 不能用于 psycopg 异步连接；与生产入口一致。
    return ("asyncio", {"loop_factory": asyncio.SelectorEventLoop})


@pytest.fixture(scope="module")
def retrieval_schema(
    destructive_test_database: DestructiveTestDatabase,
    role_test_databases: RoleTestDatabases,
) -> Iterator[Engine]:
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
def clean_business_rows(retrieval_schema: Engine) -> Iterator[None]:
    with retrieval_schema.begin() as connection:
        connection.execute(text(TRUNCATE_SQL))
    yield


def make_settings(database_url: str) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "database_url": database_url,
    }
    return Settings(**values)


@asynccontextmanager
async def api_session(database_url: str) -> AsyncIterator[AsyncSession]:
    """每个测试独立事件循环下自建引擎，避免跨 loop 复用连接。"""

    engine = create_database_engine(make_settings(database_url))
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


class FakeEmbedder:
    """确定性假编码器：把给定前缀补零到 512 维，记录 revision。"""

    def __init__(self, *, vector: Sequence[float] = (1.0, 0.0)) -> None:
        padded = list(vector[:EMBEDDING_DIMENSION])
        padded.extend([0.0] * (EMBEDDING_DIMENSION - len(padded)))
        self.vector = tuple(padded)
        self.revisions: list[str] = []
        self.closed = False

    def embed_query(self, text: str, expected_model_revision: str) -> EmbeddedQuery:
        self.revisions.append(expected_model_revision)
        return EmbeddedQuery(
            vector=self.vector, token_count=1, model_revision=expected_model_revision
        )

    def close(self) -> None:
        self.closed = True


class FakeAnalyzer:
    """假关键词分析器：只产出可交给 ``to_tsvector('simple', ...)`` 的词流，并记录实际入参。"""

    def __init__(self, *, terms: str = "hello", analyzer_id: str = ANALYZER_ID) -> None:
        self.terms = terms
        self._analyzer_id = analyzer_id
        self.inputs: list[str] = []

    @property
    def analyzer_id(self) -> str:
        return self._analyzer_id

    def analyze(self, text: str) -> str:
        self.inputs.append(text)
        return self.terms


# --- 造数据 -----------------------------------------------------------------


def insert_user(engine: Engine, *, organization_id: uuid.UUID) -> uuid.UUID:
    user_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO user_account (id, organization_id, username, password_hash, "
                "enabled, is_admin) VALUES (:id, :organization_id, :username, 'x', true, false)"
            ),
            {
                "id": user_id,
                "organization_id": organization_id,
                "username": f"u-{user_id.hex[:12]}",
            },
        )
    return user_id


def insert_profile(
    engine: Engine,
    *,
    revision: str,
    keyword_analyzer_version: str = ANALYZER_ID,
) -> uuid.UUID:
    profile_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO index_profile (id, embedding_model, model_revision, dimension, "
                "normalize, tokenizer_revision, chunker_version, keyword_analyzer_version, "
                "config_hash) VALUES (:id, 'test/model', :revision, 512, true, "
                "'test-tokenizer', 'heading-pack-v1', :keyword_analyzer_version, :config_hash)"
            ),
            {
                "id": profile_id,
                "revision": revision,
                "keyword_analyzer_version": keyword_analyzer_version,
                "config_hash": f"hash-{profile_id.hex}",
            },
        )
    return profile_id


def insert_kb(
    engine: Engine,
    *,
    organization_id: uuid.UUID,
    active_profile_id: uuid.UUID | None,
) -> uuid.UUID:
    kb_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO knowledge_base (id, organization_id, name, "
                "active_index_profile_id) VALUES (:id, :organization_id, :name, :profile_id)"
            ),
            {
                "id": kb_id,
                "organization_id": organization_id,
                "name": f"kb-{kb_id.hex[:12]}",
                "profile_id": active_profile_id,
            },
        )
    return kb_id


def insert_member(
    engine: Engine,
    *,
    kb_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str = "READER",
    revoked: bool = False,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO kb_member (id, kb_id, user_id, role, revoked_at) "
                "VALUES (:id, :kb_id, :user_id, :role, "
                "CASE WHEN :revoked THEN now() ELSE NULL END)"
            ),
            {
                "id": uuid.uuid4(),
                "kb_id": kb_id,
                "user_id": user_id,
                "role": role,
                "revoked": revoked,
            },
        )


def insert_document(
    engine: Engine,
    *,
    kb_id: uuid.UUID,
    deleted: bool = False,
    lifecycle: str = "READY",
    source_type: str = "markdown",
) -> uuid.UUID:
    document_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO document (id, kb_id, title, source_type, lifecycle_status, "
                "deleted_at) VALUES (:id, :kb_id, :title, :source_type, :lifecycle, "
                "CASE WHEN :deleted THEN now() ELSE NULL END)"
            ),
            {
                "id": document_id,
                "kb_id": kb_id,
                "title": f"doc-{document_id.hex[:12]}",
                "source_type": source_type,
                "lifecycle": lifecycle,
                "deleted": deleted,
            },
        )
    return document_id


def insert_version(
    engine: Engine, *, document_id: uuid.UUID, version_no: int = 1, status: str = "READY"
) -> uuid.UUID:
    version_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO document_version (id, document_id, version_no, file_ref, "
                "file_hash, mime, parser_version, status) VALUES (:id, :document_id, "
                ":version_no, :file_ref, :file_hash, 'text/markdown', 'p', :status)"
            ),
            {
                "id": version_id,
                "document_id": document_id,
                "version_no": version_no,
                "file_ref": f"ref/{version_id.hex}",
                "file_hash": version_id.hex,
                "status": status,
            },
        )
    return version_id


def activate_version(engine: Engine, *, document_id: uuid.UUID, version_id: uuid.UUID) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE document SET active_version_id = :version_id WHERE id = :document_id"),
            {"version_id": version_id, "document_id": document_id},
        )


def insert_generation(
    engine: Engine, *, version_id: uuid.UUID, profile_id: uuid.UUID, status: str = "READY"
) -> uuid.UUID:
    generation_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO index_generation (id, version_id, profile_id, status, "
                "expected_chunks, actual_chunks) VALUES (:id, :version_id, :profile_id, "
                ":status, 1, CASE WHEN :status = 'READY' THEN 1 ELSE 0 END)"
            ),
            {
                "id": generation_id,
                "version_id": version_id,
                "profile_id": profile_id,
                "status": status,
            },
        )
    return generation_id


def insert_chunk(
    engine: Engine,
    *,
    generation_id: uuid.UUID,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    organization_id: uuid.UUID,
    kb_id: uuid.UUID,
    chunk_index: int = 0,
    text_value: str = "hello world",
    fts_terms: str = "hello world",
    source_locator: str = "{}",
) -> uuid.UUID:
    chunk_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO chunk (id, generation_id, organization_id, kb_id, document_id, "
                "version_id, chunk_index, text, text_hash, model_input_hash, parser_version, "
                "chunker_version, token_count, heading_path, source_locator, fts) VALUES "
                "(:id, :generation_id, :organization_id, :kb_id, :document_id, :version_id, "
                ":chunk_index, :text_value, 'h', 'h', 'p', 'heading-pack-v1', 2, "
                "'[]'::jsonb, CAST(:source_locator AS jsonb), to_tsvector('simple', :fts_terms))"
            ),
            {
                "id": chunk_id,
                "generation_id": generation_id,
                "organization_id": organization_id,
                "kb_id": kb_id,
                "document_id": document_id,
                "version_id": version_id,
                "chunk_index": chunk_index,
                "text_value": text_value,
                "fts_terms": fts_terms,
                "source_locator": source_locator,
            },
        )
    return chunk_id


def insert_embedding(
    engine: Engine, *, chunk_id: uuid.UUID, profile_id: uuid.UUID, embedding: str = VECTOR_512
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO chunk_embedding (chunk_id, profile_id, embedding) "
                "VALUES (:chunk_id, :profile_id, CAST(:embedding AS vector))"
            ),
            {"chunk_id": chunk_id, "profile_id": profile_id, "embedding": embedding},
        )


def seed_ready_chain(
    engine: Engine,
    *,
    organization_id: uuid.UUID,
    kb_id: uuid.UUID,
    profile_id: uuid.UUID,
    document_id: uuid.UUID | None = None,
    version_no: int = 1,
    embedding: str = VECTOR_512,
    fts_terms: str = "hello world",
    source_type: str = "markdown",
    source_locator: str = "{}",
) -> Seeded:
    """在给定 KB 下建文档/版本/READY generation/chunk/embedding，并激活版本。"""

    resolved_document = document_id or insert_document(
        engine, kb_id=kb_id, source_type=source_type
    )
    version_id = insert_version(engine, document_id=resolved_document, version_no=version_no)
    activate_version(engine, document_id=resolved_document, version_id=version_id)
    generation_id = insert_generation(engine, version_id=version_id, profile_id=profile_id)
    chunk_id = insert_chunk(
        engine,
        generation_id=generation_id,
        document_id=resolved_document,
        version_id=version_id,
        organization_id=organization_id,
        kb_id=kb_id,
        fts_terms=fts_terms,
        source_locator=source_locator,
    )
    insert_embedding(engine, chunk_id=chunk_id, profile_id=profile_id, embedding=embedding)
    return Seeded(
        profile_id=profile_id,
        kb_id=kb_id,
        document_id=resolved_document,
        version_id=version_id,
        generation_id=generation_id,
        chunk_id=chunk_id,
    )


async def run_search(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    kb_ids: Sequence[uuid.UUID],
    query: str = "hello",
    embedder: FakeEmbedder | None = None,
    analyzer: KeywordAnalyzerLike | None = None,
) -> list[FusedCandidate]:
    repository = SqlRetrievalRepository(session)
    result = await search_authorized_chunks(
        repository,
        user_id=user_id,
        organization_id=organization_id,
        kb_ids=kb_ids,
        query=query,
        embedder=embedder if embedder is not None else FakeEmbedder(),
        analyzer=analyzer if analyzer is not None else FakeAnalyzer(),
    )
    return list(result.candidates)


# --- 正例 -------------------------------------------------------------------


@pytest.mark.anyio
async def test_positive_hybrid_recall_and_fusion(
    retrieval_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    organization_id = uuid.uuid4()
    user_id = insert_user(retrieval_schema, organization_id=organization_id)
    profile_id = insert_profile(retrieval_schema, revision="rev-1")
    kb_id = insert_kb(
        retrieval_schema, organization_id=organization_id, active_profile_id=profile_id
    )
    insert_member(retrieval_schema, kb_id=kb_id, user_id=user_id)
    document_id = insert_document(retrieval_schema, kb_id=kb_id)
    version_id = insert_version(retrieval_schema, document_id=document_id)
    activate_version(retrieval_schema, document_id=document_id, version_id=version_id)
    generation_id = insert_generation(
        retrieval_schema, version_id=version_id, profile_id=profile_id
    )
    near = insert_chunk(
        retrieval_schema,
        generation_id=generation_id,
        document_id=document_id,
        version_id=version_id,
        organization_id=organization_id,
        kb_id=kb_id,
        chunk_index=0,
        fts_terms="hello world",
    )
    far = insert_chunk(
        retrieval_schema,
        generation_id=generation_id,
        document_id=document_id,
        version_id=version_id,
        organization_id=organization_id,
        kb_id=kb_id,
        chunk_index=1,
        fts_terms="world",
    )
    insert_embedding(
        retrieval_schema, chunk_id=near, profile_id=profile_id, embedding=_vector(1.0, 0.0)
    )
    insert_embedding(
        retrieval_schema, chunk_id=far, profile_id=profile_id, embedding=_vector(0.0, 1.0)
    )
    embedder = FakeEmbedder(vector=(1.0, 0.0))

    async with api_session(role_test_databases.api_url) as session:
        candidates = await run_search(
            session,
            user_id=user_id,
            organization_id=organization_id,
            kb_ids=[kb_id],
            embedder=embedder,
        )

    assert embedder.revisions == ["rev-1"]
    assert len(candidates) == 2
    assert candidates[0].chunk_id == near
    assert candidates[0].vector_rank == 1
    assert candidates[0].keyword_rank == 1
    assert candidates[0].fusion_rank == 1
    assert candidates[1].chunk_id == far
    assert candidates[1].vector_rank == 2
    assert candidates[1].keyword_rank is None


# --- 授权负例 ---------------------------------------------------------------


@pytest.mark.anyio
async def test_non_member_is_not_accessible(
    retrieval_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    organization_id = uuid.uuid4()
    user_id = insert_user(retrieval_schema, organization_id=organization_id)
    profile_id = insert_profile(retrieval_schema, revision="rev-1")
    kb_id = insert_kb(
        retrieval_schema, organization_id=organization_id, active_profile_id=profile_id
    )
    seed_ready_chain(
        retrieval_schema,
        organization_id=organization_id,
        kb_id=kb_id,
        profile_id=profile_id,
    )

    async with api_session(role_test_databases.api_url) as session:
        with pytest.raises(retrieval_errors.KnowledgeBaseNotAccessible):
            await run_search(
                session,
                user_id=user_id,
                organization_id=organization_id,
                kb_ids=[kb_id],
            )


@pytest.mark.anyio
async def test_revoked_member_is_not_accessible(
    retrieval_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    organization_id = uuid.uuid4()
    user_id = insert_user(retrieval_schema, organization_id=organization_id)
    profile_id = insert_profile(retrieval_schema, revision="rev-1")
    kb_id = insert_kb(
        retrieval_schema, organization_id=organization_id, active_profile_id=profile_id
    )
    insert_member(retrieval_schema, kb_id=kb_id, user_id=user_id, revoked=True)
    seed_ready_chain(
        retrieval_schema,
        organization_id=organization_id,
        kb_id=kb_id,
        profile_id=profile_id,
    )

    async with api_session(role_test_databases.api_url) as session:
        with pytest.raises(retrieval_errors.KnowledgeBaseNotAccessible):
            await run_search(
                session,
                user_id=user_id,
                organization_id=organization_id,
                kb_ids=[kb_id],
            )


@pytest.mark.anyio
async def test_cross_organization_member_is_not_accessible(
    retrieval_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    user_organization = uuid.uuid4()
    other_organization = uuid.uuid4()
    user_id = insert_user(retrieval_schema, organization_id=user_organization)
    profile_id = insert_profile(retrieval_schema, revision="rev-1")
    kb_id = insert_kb(
        retrieval_schema, organization_id=other_organization, active_profile_id=profile_id
    )
    insert_member(retrieval_schema, kb_id=kb_id, user_id=user_id)
    seed_ready_chain(
        retrieval_schema,
        organization_id=other_organization,
        kb_id=kb_id,
        profile_id=profile_id,
    )

    async with api_session(role_test_databases.api_url) as session:
        with pytest.raises(retrieval_errors.KnowledgeBaseNotAccessible):
            await run_search(
                session,
                user_id=user_id,
                organization_id=user_organization,
                kb_ids=[kb_id],
            )


# --- 版本与 profile 负例 ------------------------------------------------------


@pytest.mark.anyio
async def test_null_active_profile_yields_no_candidates(
    retrieval_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    organization_id = uuid.uuid4()
    user_id = insert_user(retrieval_schema, organization_id=organization_id)
    kb_id = insert_kb(retrieval_schema, organization_id=organization_id, active_profile_id=None)
    insert_member(retrieval_schema, kb_id=kb_id, user_id=user_id)

    async with api_session(role_test_databases.api_url) as session:
        candidates = await run_search(
            session, user_id=user_id, organization_id=organization_id, kb_ids=[kb_id]
        )

    assert candidates == []


@pytest.mark.anyio
async def test_non_ready_generation_is_not_retrievable(
    retrieval_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    organization_id = uuid.uuid4()
    user_id = insert_user(retrieval_schema, organization_id=organization_id)
    profile_id = insert_profile(retrieval_schema, revision="rev-1")
    kb_id = insert_kb(
        retrieval_schema, organization_id=organization_id, active_profile_id=profile_id
    )
    insert_member(retrieval_schema, kb_id=kb_id, user_id=user_id)
    document_id = insert_document(retrieval_schema, kb_id=kb_id)
    version_id = insert_version(retrieval_schema, document_id=document_id)
    activate_version(retrieval_schema, document_id=document_id, version_id=version_id)
    generation_id = insert_generation(
        retrieval_schema, version_id=version_id, profile_id=profile_id, status="BUILDING"
    )
    chunk_id = insert_chunk(
        retrieval_schema,
        generation_id=generation_id,
        document_id=document_id,
        version_id=version_id,
        organization_id=organization_id,
        kb_id=kb_id,
    )
    insert_embedding(retrieval_schema, chunk_id=chunk_id, profile_id=profile_id)

    async with api_session(role_test_databases.api_url) as session:
        candidates = await run_search(
            session, user_id=user_id, organization_id=organization_id, kb_ids=[kb_id]
        )

    assert candidates == []


@pytest.mark.anyio
async def test_generation_profile_mismatch_is_not_retrievable(
    retrieval_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    organization_id = uuid.uuid4()
    user_id = insert_user(retrieval_schema, organization_id=organization_id)
    active_profile = insert_profile(retrieval_schema, revision="rev-1")
    other_profile = insert_profile(retrieval_schema, revision="rev-2")
    kb_id = insert_kb(
        retrieval_schema, organization_id=organization_id, active_profile_id=active_profile
    )
    insert_member(retrieval_schema, kb_id=kb_id, user_id=user_id)
    document_id = insert_document(retrieval_schema, kb_id=kb_id)
    version_id = insert_version(retrieval_schema, document_id=document_id)
    activate_version(retrieval_schema, document_id=document_id, version_id=version_id)
    generation_id = insert_generation(
        retrieval_schema, version_id=version_id, profile_id=other_profile
    )
    chunk_id = insert_chunk(
        retrieval_schema,
        generation_id=generation_id,
        document_id=document_id,
        version_id=version_id,
        organization_id=organization_id,
        kb_id=kb_id,
    )
    insert_embedding(retrieval_schema, chunk_id=chunk_id, profile_id=other_profile)

    async with api_session(role_test_databases.api_url) as session:
        candidates = await run_search(
            session, user_id=user_id, organization_id=organization_id, kb_ids=[kb_id]
        )

    assert candidates == []


@pytest.mark.anyio
async def test_embedding_profile_mismatch_is_not_retrievable(
    retrieval_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    organization_id = uuid.uuid4()
    user_id = insert_user(retrieval_schema, organization_id=organization_id)
    active_profile = insert_profile(retrieval_schema, revision="rev-1")
    other_profile = insert_profile(retrieval_schema, revision="rev-2")
    kb_id = insert_kb(
        retrieval_schema, organization_id=organization_id, active_profile_id=active_profile
    )
    insert_member(retrieval_schema, kb_id=kb_id, user_id=user_id)
    document_id = insert_document(retrieval_schema, kb_id=kb_id)
    version_id = insert_version(retrieval_schema, document_id=document_id)
    activate_version(retrieval_schema, document_id=document_id, version_id=version_id)
    generation_id = insert_generation(
        retrieval_schema, version_id=version_id, profile_id=active_profile
    )
    chunk_id = insert_chunk(
        retrieval_schema,
        generation_id=generation_id,
        document_id=document_id,
        version_id=version_id,
        organization_id=organization_id,
        kb_id=kb_id,
    )
    insert_embedding(retrieval_schema, chunk_id=chunk_id, profile_id=other_profile)

    async with api_session(role_test_databases.api_url) as session:
        candidates = await run_search(
            session, user_id=user_id, organization_id=organization_id, kb_ids=[kb_id]
        )

    assert candidates == []


@pytest.mark.anyio
async def test_deleted_document_is_not_retrievable(
    retrieval_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    organization_id = uuid.uuid4()
    user_id = insert_user(retrieval_schema, organization_id=organization_id)
    profile_id = insert_profile(retrieval_schema, revision="rev-1")
    kb_id = insert_kb(
        retrieval_schema, organization_id=organization_id, active_profile_id=profile_id
    )
    insert_member(retrieval_schema, kb_id=kb_id, user_id=user_id)
    seed_ready_chain(
        retrieval_schema,
        organization_id=organization_id,
        kb_id=kb_id,
        profile_id=profile_id,
    )
    with retrieval_schema.begin() as connection:
        connection.execute(text("UPDATE document SET deleted_at = now()"))

    async with api_session(role_test_databases.api_url) as session:
        candidates = await run_search(
            session, user_id=user_id, organization_id=organization_id, kb_ids=[kb_id]
        )

    assert candidates == []


@pytest.mark.anyio
async def test_old_version_is_not_retrievable_while_active_is(
    retrieval_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    organization_id = uuid.uuid4()
    user_id = insert_user(retrieval_schema, organization_id=organization_id)
    profile_id = insert_profile(retrieval_schema, revision="rev-1")
    kb_id = insert_kb(
        retrieval_schema, organization_id=organization_id, active_profile_id=profile_id
    )
    insert_member(retrieval_schema, kb_id=kb_id, user_id=user_id)

    document_id = insert_document(retrieval_schema, kb_id=kb_id)
    old_version = insert_version(retrieval_schema, document_id=document_id, version_no=1)
    old_generation = insert_generation(
        retrieval_schema, version_id=old_version, profile_id=profile_id
    )
    old_chunk = insert_chunk(
        retrieval_schema,
        generation_id=old_generation,
        document_id=document_id,
        version_id=old_version,
        organization_id=organization_id,
        kb_id=kb_id,
        fts_terms="old",
    )
    insert_embedding(retrieval_schema, chunk_id=old_chunk, profile_id=profile_id)

    new_version = insert_version(retrieval_schema, document_id=document_id, version_no=2)
    activate_version(retrieval_schema, document_id=document_id, version_id=new_version)
    new_generation = insert_generation(
        retrieval_schema, version_id=new_version, profile_id=profile_id
    )
    new_chunk = insert_chunk(
        retrieval_schema,
        generation_id=new_generation,
        document_id=document_id,
        version_id=new_version,
        organization_id=organization_id,
        kb_id=kb_id,
        fts_terms="new",
    )
    insert_embedding(retrieval_schema, chunk_id=new_chunk, profile_id=profile_id)

    async with api_session(role_test_databases.api_url) as session:
        candidates = await run_search(
            session, user_id=user_id, organization_id=organization_id, kb_ids=[kb_id]
        )

    assert [candidate.chunk_id for candidate in candidates] == [new_chunk]


@pytest.mark.anyio
async def test_authoritative_chain_ignores_redundant_chunk_columns(
    retrieval_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    organization_id = uuid.uuid4()
    foreign_organization = uuid.uuid4()
    user_id = insert_user(retrieval_schema, organization_id=organization_id)
    profile_id = insert_profile(retrieval_schema, revision="rev-1")
    kb_a = insert_kb(
        retrieval_schema, organization_id=organization_id, active_profile_id=profile_id
    )
    kb_b = insert_kb(
        retrieval_schema, organization_id=organization_id, active_profile_id=profile_id
    )
    insert_member(retrieval_schema, kb_id=kb_a, user_id=user_id)
    insert_member(retrieval_schema, kb_id=kb_b, user_id=user_id)

    document_id = insert_document(retrieval_schema, kb_id=kb_b)
    version_id = insert_version(retrieval_schema, document_id=document_id)
    activate_version(retrieval_schema, document_id=document_id, version_id=version_id)
    generation_id = insert_generation(
        retrieval_schema, version_id=version_id, profile_id=profile_id
    )
    # 冗余列指向 KB A 与外组织；权威链 document.kb_id 属于 KB B。
    chunk_id = insert_chunk(
        retrieval_schema,
        generation_id=generation_id,
        document_id=document_id,
        version_id=version_id,
        organization_id=foreign_organization,
        kb_id=kb_a,
        fts_terms="hello",
    )
    insert_embedding(retrieval_schema, chunk_id=chunk_id, profile_id=profile_id)

    async with api_session(role_test_databases.api_url) as session:
        for_kb_b = await run_search(
            session, user_id=user_id, organization_id=organization_id, kb_ids=[kb_b]
        )
        for_kb_a = await run_search(
            session, user_id=user_id, organization_id=organization_id, kb_ids=[kb_a]
        )

    assert [candidate.chunk_id for candidate in for_kb_b] == [chunk_id]
    assert for_kb_a == []


@pytest.mark.anyio
async def test_multiple_profiles_in_scope_conflict(
    retrieval_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    organization_id = uuid.uuid4()
    user_id = insert_user(retrieval_schema, organization_id=organization_id)
    profile_a = insert_profile(retrieval_schema, revision="rev-1")
    profile_b = insert_profile(retrieval_schema, revision="rev-2")
    kb_a = insert_kb(retrieval_schema, organization_id=organization_id, active_profile_id=profile_a)
    kb_b = insert_kb(retrieval_schema, organization_id=organization_id, active_profile_id=profile_b)
    insert_member(retrieval_schema, kb_id=kb_a, user_id=user_id)
    insert_member(retrieval_schema, kb_id=kb_b, user_id=user_id)

    async with api_session(role_test_databases.api_url) as session:
        with pytest.raises(retrieval_errors.RetrievalProfileConflict):
            await run_search(
                session,
                user_id=user_id,
                organization_id=organization_id,
                kb_ids=[kb_a, kb_b],
            )


@pytest.mark.anyio
async def test_keyword_terms_are_bound_not_interpolated(
    retrieval_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    organization_id = uuid.uuid4()
    user_id = insert_user(retrieval_schema, organization_id=organization_id)
    profile_id = insert_profile(retrieval_schema, revision="rev-1")
    kb_id = insert_kb(
        retrieval_schema, organization_id=organization_id, active_profile_id=profile_id
    )
    insert_member(retrieval_schema, kb_id=kb_id, user_id=user_id)
    document_id = insert_document(retrieval_schema, kb_id=kb_id)
    version_id = insert_version(retrieval_schema, document_id=document_id)
    activate_version(retrieval_schema, document_id=document_id, version_id=version_id)
    generation_id = insert_generation(
        retrieval_schema, version_id=version_id, profile_id=profile_id
    )
    matched = insert_chunk(
        retrieval_schema,
        generation_id=generation_id,
        document_id=document_id,
        version_id=version_id,
        organization_id=organization_id,
        kb_id=kb_id,
        chunk_index=0,
        # 词流包含注入串的全部可索引词，用于证明参数被当成文本而不是 SQL。
        fts_terms="hello drop table chunk",
    )
    insert_embedding(retrieval_schema, chunk_id=matched, profile_id=profile_id)
    injection = "hello'); DROP TABLE chunk; --"

    async with api_session(role_test_databases.api_url) as session:
        candidates = await run_search(
            session,
            user_id=user_id,
            organization_id=organization_id,
            kb_ids=[kb_id],
            embedder=FakeEmbedder(),
            analyzer=FakeAnalyzer(terms=injection),
        )

    assert [candidate.chunk_id for candidate in candidates] == [matched]
    assert candidates[0].keyword_rank == 1
    # 注入串若被拼进 SQL，DROP 会执行或报语法错误；绑定参数下表和行都应原样保留。
    with retrieval_schema.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM chunk")) == 1
        assert connection.scalar(text("SELECT to_regclass('chunk')")) is not None


@pytest.mark.anyio
async def test_keyword_analyzer_identity_mismatch_is_not_retrievable(
    retrieval_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    organization_id = uuid.uuid4()
    user_id = insert_user(retrieval_schema, organization_id=organization_id)
    # profile 登记的是旧分析器身份，运行期分析器是另一个；必须 fail closed。
    profile_id = insert_profile(
        retrieval_schema, revision="rev-1", keyword_analyzer_version="stale-analyzer"
    )
    kb_id = insert_kb(
        retrieval_schema, organization_id=organization_id, active_profile_id=profile_id
    )
    insert_member(retrieval_schema, kb_id=kb_id, user_id=user_id)
    seed_ready_chain(
        retrieval_schema,
        organization_id=organization_id,
        kb_id=kb_id,
        profile_id=profile_id,
    )

    async with api_session(role_test_databases.api_url) as session:
        with pytest.raises(retrieval_errors.RetrievalAnalyzerMismatch):
            await run_search(
                session,
                user_id=user_id,
                organization_id=organization_id,
                kb_ids=[kb_id],
            )


@pytest.mark.anyio
async def test_real_keyword_analyzer_matches_real_chunk_fts(
    retrieval_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    """真实 `KeywordAnalyzer` 与真实 `chunk.fts` 的端到端匹配（非假词流）。"""

    analyzer = get_keyword_analyzer()
    organization_id = uuid.uuid4()
    user_id = insert_user(retrieval_schema, organization_id=organization_id)
    profile_id = insert_profile(
        retrieval_schema, revision="rev-1", keyword_analyzer_version=analyzer.analyzer_id
    )
    kb_id = insert_kb(
        retrieval_schema, organization_id=organization_id, active_profile_id=profile_id
    )
    insert_member(retrieval_schema, kb_id=kb_id, user_id=user_id)
    document_id = insert_document(retrieval_schema, kb_id=kb_id)
    version_id = insert_version(retrieval_schema, document_id=document_id)
    activate_version(retrieval_schema, document_id=document_id, version_id=version_id)
    generation_id = insert_generation(
        retrieval_schema, version_id=version_id, profile_id=profile_id
    )
    text_value = "检索测试文档"
    # 索引词流与查询词流来自同一分析器，与入库管线的写入方式一致。
    terms = analyzer.analyze(text_value)
    assert terms.strip() != ""
    chunk_id = insert_chunk(
        retrieval_schema,
        generation_id=generation_id,
        document_id=document_id,
        version_id=version_id,
        organization_id=organization_id,
        kb_id=kb_id,
        text_value=text_value,
        fts_terms=terms,
    )
    insert_embedding(
        retrieval_schema,
        chunk_id=chunk_id,
        profile_id=profile_id,
        embedding=_vector(1.0, 0.0),
    )

    async with api_session(role_test_databases.api_url) as session:
        candidates = await run_search(
            session,
            user_id=user_id,
            organization_id=organization_id,
            kb_ids=[kb_id],
            query=text_value,
            analyzer=analyzer,
        )

    assert [candidate.chunk_id for candidate in candidates] == [chunk_id]
    assert candidates[0].keyword_rank == 1


@asynccontextmanager
async def http_retrieval_client(
    settings: Settings, *, context: AuthContext, embedder: FakeEmbedder
) -> AsyncIterator[AsyncClient]:
    """真实 `create_app` + 真实 DB 会话，仅用依赖覆盖替换会话身份与查询编码器。"""

    app = create_app(settings)
    app.dependency_overrides[get_auth_context] = lambda: context
    app.dependency_overrides[get_query_embedder] = lambda: embedder
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, client=("10.20.0.1", 12345))
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            yield client


def _http_context(user_id: uuid.UUID, organization_id: uuid.UUID) -> AuthContext:
    return AuthContext(
        user_id=user_id,
        organization_id=organization_id,
        username="http-user",
        is_admin=False,
        session_id=uuid.uuid4(),
        csrf_token="csrf",
    )


@pytest.mark.anyio
async def test_http_retrieval_over_real_database(
    retrieval_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    analyzer = get_keyword_analyzer()
    organization_id = uuid.uuid4()
    user_id = insert_user(retrieval_schema, organization_id=organization_id)
    profile_id = insert_profile(
        retrieval_schema, revision="rev-1", keyword_analyzer_version=analyzer.analyzer_id
    )
    kb_id = insert_kb(
        retrieval_schema, organization_id=organization_id, active_profile_id=profile_id
    )
    insert_member(retrieval_schema, kb_id=kb_id, user_id=user_id)
    document_id = insert_document(retrieval_schema, kb_id=kb_id)
    version_id = insert_version(retrieval_schema, document_id=document_id)
    activate_version(retrieval_schema, document_id=document_id, version_id=version_id)
    generation_id = insert_generation(
        retrieval_schema, version_id=version_id, profile_id=profile_id
    )
    terms = analyzer.analyze("检索测试文档")
    chunk_id = insert_chunk(
        retrieval_schema,
        generation_id=generation_id,
        document_id=document_id,
        version_id=version_id,
        organization_id=organization_id,
        kb_id=kb_id,
        fts_terms=terms,
    )
    insert_embedding(
        retrieval_schema,
        chunk_id=chunk_id,
        profile_id=profile_id,
        embedding=_vector(1.0, 0.0),
    )

    async with http_retrieval_client(
        make_settings(role_test_databases.api_url),
        context=_http_context(user_id, organization_id),
        embedder=FakeEmbedder(),
    ) as client:
        response = await client.post(
            "/api/v1/retrieval/search",
            json={"query": "检索测试文档", "kbIds": [str(kb_id)]},
        )

    assert response.status_code == 200, response.text
    candidates = response.json()["candidates"]
    assert [candidate["chunkId"] for candidate in candidates] == [str(chunk_id)]
    assert candidates[0]["keywordRank"] == 1
    assert candidates[0]["fusionRank"] == 1


@pytest.mark.anyio
async def test_http_retrieval_rejects_kb_outside_session_scope(
    retrieval_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    organization_id = uuid.uuid4()
    user_id = insert_user(retrieval_schema, organization_id=organization_id)
    profile_id = insert_profile(retrieval_schema, revision="rev-1")
    kb_id = insert_kb(
        retrieval_schema, organization_id=organization_id, active_profile_id=profile_id
    )
    insert_member(retrieval_schema, kb_id=kb_id, user_id=user_id)
    seed_ready_chain(
        retrieval_schema,
        organization_id=organization_id,
        kb_id=kb_id,
        profile_id=profile_id,
    )

    async with http_retrieval_client(
        make_settings(role_test_databases.api_url),
        context=_http_context(user_id, organization_id),
        embedder=FakeEmbedder(),
    ) as client:
        response = await client.post(
            "/api/v1/retrieval/search",
            json={"query": "hello", "kbIds": [str(uuid.uuid4())]},
        )

    assert response.status_code == 404
    assert response.json()["code"] == "KNOWLEDGE_BASE_NOT_FOUND"


# --- 同 KB 混合 Markdown + PDF -------------------------------------------------


@pytest.mark.anyio
async def test_mixed_markdown_and_pdf_same_kb_are_both_recalled(
    retrieval_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    """同一 KB 内 Markdown 与 PDF 的 READY chunk 使用同一 active profile，被一起召回。

    该用例只验证授权混合检索对 source_type 中立：PDF chunk 只要按同一 profile 发布即可被
    向量路与关键词路一起命中。查询编码器是显式假实现，不代表真实 inference 连通性。
    """

    organization_id = uuid.uuid4()
    user_id = insert_user(retrieval_schema, organization_id=organization_id)
    profile_id = insert_profile(retrieval_schema, revision="rev-1")
    kb_id = insert_kb(
        retrieval_schema, organization_id=organization_id, active_profile_id=profile_id
    )
    insert_member(retrieval_schema, kb_id=kb_id, user_id=user_id)

    markdown = seed_ready_chain(
        retrieval_schema,
        organization_id=organization_id,
        kb_id=kb_id,
        profile_id=profile_id,
        fts_terms="hello markdown",
    )
    pdf_locator = json.dumps(
        {
            "locator_version": 2,
            "source_type": "pdf",
            "parser_version": "pypdf-6.19.0+pdfplumber-0.11.10-v1",
            "source_sha256": "a" * 64,
            "pages": [1],
            "block_ordinals": [0],
            "segments": [
                {"block_ordinal": 0, "block_char_start": 0, "block_char_end": 5, "page": 1}
            ],
        }
    )
    pdf = seed_ready_chain(
        retrieval_schema,
        organization_id=organization_id,
        kb_id=kb_id,
        profile_id=profile_id,
        fts_terms="hello pdf",
        source_type="pdf",
        source_locator=pdf_locator,
    )

    async with api_session(role_test_databases.api_url) as session:
        candidates = await run_search(
            session,
            user_id=user_id,
            organization_id=organization_id,
            kb_ids=[kb_id],
        )

    recalled = {candidate.document_id for candidate in candidates}
    assert {markdown.document_id, pdf.document_id} <= recalled

    with retrieval_schema.connect() as connection:
        active = connection.scalar(
            text("SELECT active_index_profile_id FROM knowledge_base WHERE id = :id"),
            {"id": kb_id},
        )
        assert active == profile_id
        stored_locator = connection.scalar(
            text("SELECT source_locator FROM chunk WHERE document_id = :id"),
            {"id": pdf.document_id},
        )
    assert stored_locator["locator_version"] == 2
    assert stored_locator["source_type"] == "pdf"
    assert stored_locator["pages"] == [1]
    assert "start_line" not in stored_locator


# --- 相邻证据 ----------------------------------------------------------------


def _add_chunk(
    engine: Engine,
    *,
    seeded: Seeded,
    organization_id: uuid.UUID,
    chunk_index: int,
    text_value: str,
) -> uuid.UUID:
    chunk_id = insert_chunk(
        engine,
        generation_id=seeded.generation_id,
        document_id=seeded.document_id,
        version_id=seeded.version_id,
        organization_id=organization_id,
        kb_id=seeded.kb_id,
        chunk_index=chunk_index,
        text_value=text_value,
        fts_terms=text_value,
    )
    insert_embedding(engine, chunk_id=chunk_id, profile_id=seeded.profile_id)
    return chunk_id


@pytest.mark.anyio
async def test_adjacent_evidence_reauthorizes_full_chain(
    retrieval_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    """相邻证据与直接证据一样经完整授权链：同 generation 命中，非成员/跨组织/旧版本拒绝。

    真实 SQL 负例：本用例定义后由具备守卫 DSN 的环境运行；未运行不声称已实测 SQL 行为。
    """

    organization_id = uuid.uuid4()
    user_id = insert_user(retrieval_schema, organization_id=organization_id)
    profile_id = insert_profile(retrieval_schema, revision="rev-adjacent")
    kb_id = insert_kb(
        retrieval_schema, organization_id=organization_id, active_profile_id=profile_id
    )
    insert_member(retrieval_schema, kb_id=kb_id, user_id=user_id)
    seeded = seed_ready_chain(
        retrieval_schema,
        organization_id=organization_id,
        kb_id=kb_id,
        profile_id=profile_id,
    )
    middle = _add_chunk(
        retrieval_schema,
        seeded=seeded,
        organization_id=organization_id,
        chunk_index=1,
        text_value="middle",
    )
    last = _add_chunk(
        retrieval_schema,
        seeded=seeded,
        organization_id=organization_id,
        chunk_index=2,
        text_value="last",
    )
    outsider_id = insert_user(retrieval_schema, organization_id=uuid.uuid4())

    async with api_session(role_test_databases.api_url) as session:
        repository = SqlRetrievalRepository(session)
        rows = await repository.load_adjacent_evidence_chunks(
            user_id=user_id, organization_id=organization_id, chunk_ids=[middle]
        )
        await repository.release()
    by_chunk = {row.chunk.chunk_id: row for row in rows}
    assert set(by_chunk) == {seeded.chunk_id, last}
    assert by_chunk[seeded.chunk_id].chunk_index == 0
    assert by_chunk[last].chunk_index == 2
    assert all(row.seed_chunk_id == middle and row.seed_chunk_index == 1 for row in rows)

    # 非本 KB 成员即使同组织也读不到邻居。
    async with api_session(role_test_databases.api_url) as session:
        repository = SqlRetrievalRepository(session)
        denied = await repository.load_adjacent_evidence_chunks(
            user_id=outsider_id, organization_id=organization_id, chunk_ids=[middle]
        )
        await repository.release()
    assert denied == []

    # 邻居所在 generation 属于非 active version 时一律拒绝。
    document_id = insert_document(retrieval_schema, kb_id=kb_id)
    old_version = insert_version(retrieval_schema, document_id=document_id, version_no=1)
    old_generation = insert_generation(
        retrieval_schema, version_id=old_version, profile_id=profile_id
    )
    old_seed = insert_chunk(
        retrieval_schema,
        generation_id=old_generation,
        document_id=document_id,
        version_id=old_version,
        organization_id=organization_id,
        kb_id=kb_id,
        chunk_index=0,
        text_value="old-0",
    )
    old_neighbor = insert_chunk(
        retrieval_schema,
        generation_id=old_generation,
        document_id=document_id,
        version_id=old_version,
        organization_id=organization_id,
        kb_id=kb_id,
        chunk_index=1,
        text_value="old-1",
    )
    insert_embedding(retrieval_schema, chunk_id=old_seed, profile_id=profile_id)
    insert_embedding(retrieval_schema, chunk_id=old_neighbor, profile_id=profile_id)
    active_version = insert_version(
        retrieval_schema, document_id=document_id, version_no=2, status="READY"
    )
    activate_version(retrieval_schema, document_id=document_id, version_id=active_version)

    async with api_session(role_test_databases.api_url) as session:
        repository = SqlRetrievalRepository(session)
        stale = await repository.load_adjacent_evidence_chunks(
            user_id=user_id, organization_id=organization_id, chunk_ids=[old_seed]
        )
        await repository.release()
    assert stale == []
