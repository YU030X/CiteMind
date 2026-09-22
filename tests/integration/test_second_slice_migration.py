"""第二切片业务迁移的真实 PostgreSQL 验收。

从 ``20260922_0002`` 干净状态升级到 ``20260922_0003``，核对三张新表、``ingest_job``
新外键、约束/索引（含 GIN 与部分唯一）、无 sequence/ENUM/ANN、PUBLIC 收权与
api/worker 精确授权，并用真实 DML 正反用例证明 512 维向量、主键与部分唯一约束。
只使用破坏性测试库与三角色 DSN 守卫；缺少 DSN 时按既有契约跳过。
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from alembic import command
from database_guard import DestructiveTestDatabase
from database_roles_guard import API_ROLE, WORKER_ROLE, RoleTestDatabases
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import DataError, IntegrityError
from test_core_migration import (
    alembic_config,
    alembic_revision,
    assert_statement_denied,
    business_tables,
    foreign_key_behaviours,
    named_constraints,
    named_indexes,
    public_grant_counts,
    role_grants,
    sequence_count,
)

pytestmark = pytest.mark.integration

CORE_REVISION = "20260922_0002"
SECOND_SLICE_REVISION = "20260922_0003"

CORE_TABLES = (
    "index_profile",
    "knowledge_base",
    "document",
    "document_version",
    "ingest_job",
    "outbox_event",
)
SECOND_SLICE_TABLES = ("index_generation", "chunk", "chunk_embedding")
ALL_TABLES = CORE_TABLES + SECOND_SLICE_TABLES

SECOND_SLICE_CONSTRAINTS = {
    "index_generation": {
        "pk_index_generation",
        "fk_index_generation_version_id_document_version",
        "fk_index_generation_profile_id_index_profile",
        "ck_index_generation_status",
        "ck_index_generation_expected_chunks_non_negative",
        "ck_index_generation_actual_chunks_non_negative",
        "ck_index_generation_actual_chunks_within_expected",
    },
    "chunk": {
        "pk_chunk",
        "fk_chunk_generation_id_index_generation",
        "fk_chunk_kb_id_knowledge_base",
        "fk_chunk_document_id_document",
        "fk_chunk_version_id_document_version",
        "uq_chunk_generation_id_chunk_index",
        "ck_chunk_chunk_index_non_negative",
        "ck_chunk_text_non_empty",
        "ck_chunk_token_count_non_negative",
    },
    "chunk_embedding": {
        "pk_chunk_embedding",
        "fk_chunk_embedding_chunk_id_chunk",
        "fk_chunk_embedding_profile_id_index_profile",
    },
}

EXPECTED_INGEST_JOB_CONSTRAINTS = {
    "pk_ingest_job",
    "fk_ingest_job_document_id_document",
    "fk_ingest_job_version_id_document_version",
    "fk_ingest_job_generation_id_index_generation",
    "uq_ingest_job_dedupe_key",
    "ck_ingest_job_status",
    "ck_ingest_job_attempt_non_negative",
    "ck_ingest_job_lease_consistent",
}

SECOND_SLICE_INDEXES = {
    "index_generation": {
        "ix_index_generation_version_id_profile_id_status",
        "uq_index_generation_version_id_profile_id_ready",
    },
    "chunk": {"ix_chunk_generation_id", "ix_chunk_fts"},
}

SECOND_SLICE_ROLE_GRANTS = {
    "index_generation": {
        API_ROLE: {"SELECT"},
        WORKER_ROLE: {"SELECT", "INSERT", "UPDATE"},
    },
    "chunk": {API_ROLE: {"SELECT"}, WORKER_ROLE: {"SELECT", "INSERT"}},
    "chunk_embedding": {API_ROLE: {"SELECT"}, WORKER_ROLE: {"SELECT", "INSERT"}},
}

SECOND_SLICE_FOREIGN_KEYS = (
    "fk_index_generation_version_id_document_version",
    "fk_index_generation_profile_id_index_profile",
    "fk_chunk_generation_id_index_generation",
    "fk_chunk_kb_id_knowledge_base",
    "fk_chunk_document_id_document",
    "fk_chunk_version_id_document_version",
    "fk_chunk_embedding_chunk_id_chunk",
    "fk_chunk_embedding_profile_id_index_profile",
    "fk_ingest_job_generation_id_index_generation",
)

INDEX_DEFINITIONS_QUERY = text(
    "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'"
)
INGEST_JOB_COLUMNS_QUERY = text(
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_schema = 'public' AND table_name = 'ingest_job'"
)
FORBIDDEN_PRIVILEGES_QUERY = text(
    "SELECT count(*) FROM information_schema.table_privileges "
    "WHERE table_schema = 'public' AND grantee IN ('citemind_api', 'citemind_worker') "
    "AND privilege_type IN ('DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER')"
)
ENUM_TYPES_QUERY = text(
    "SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
    "WHERE n.nspname = 'public' AND t.typtype = 'e'"
)
ANN_INDEX_QUERY = text(
    "SELECT count(*) FROM pg_index x "
    "JOIN pg_class i ON i.oid = x.indexrelid "
    "JOIN pg_am am ON am.oid = i.relam "
    "WHERE am.amname IN ('hnsw', 'ivfflat')"
)


@dataclass(frozen=True)
class SeededChain:
    """一整条 index_profile→…→index_generation 前置链的 id。"""

    organization_id: uuid.UUID
    profile_id: uuid.UUID
    kb_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    generation_id: uuid.UUID


def index_definitions(connection: Connection) -> dict[str, str]:
    return {str(row[0]): str(row[1]) for row in connection.execute(INDEX_DEFINITIONS_QUERY)}


def ingest_job_columns(connection: Connection) -> set[str]:
    return {str(name) for name in connection.scalars(INGEST_JOB_COLUMNS_QUERY)}


def forbidden_privilege_count(connection: Connection) -> int:
    return int(connection.scalar(FORBIDDEN_PRIVILEGES_QUERY) or 0)


def enum_type_count(connection: Connection) -> int:
    return int(connection.scalar(ENUM_TYPES_QUERY) or 0)


def ann_index_count(connection: Connection) -> int:
    return int(connection.scalar(ANN_INDEX_QUERY) or 0)


def seed_chain(engine: Engine, *, generation_status: str = "BUILDING") -> SeededChain:
    chain = SeededChain(
        organization_id=uuid.uuid4(),
        profile_id=uuid.uuid4(),
        kb_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        generation_id=uuid.uuid4(),
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO index_profile (id, embedding_model, model_revision, dimension, "
                "normalize, tokenizer_revision, chunker_version, keyword_analyzer_version, "
                "config_hash) VALUES (:id, 'model', 'rev', 512, true, 'tok', 'chunk', 'kw', "
                ":config_hash)"
            ),
            {"id": chain.profile_id, "config_hash": chain.profile_id.hex},
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_base (id, organization_id, name, kb_revision, "
                "acl_revision) VALUES (:id, :organization_id, 'kb', 0, 0)"
            ),
            {"id": chain.kb_id, "organization_id": chain.organization_id},
        )
        connection.execute(
            text(
                "INSERT INTO document (id, kb_id, title, source_type, lifecycle_status) "
                "VALUES (:id, :kb_id, 'doc', 'markdown', 'CREATED')"
            ),
            {"id": chain.document_id, "kb_id": chain.kb_id},
        )
        connection.execute(
            text(
                "INSERT INTO document_version (id, document_id, version_no, file_ref, "
                "file_hash, mime, parser_version, status) VALUES (:id, :document_id, 1, "
                "'ref', 'file-hash', 'text/markdown', 'parser', 'READY')"
            ),
            {"id": chain.version_id, "document_id": chain.document_id},
        )
        connection.execute(
            text(
                "INSERT INTO index_generation (id, version_id, profile_id, status) "
                "VALUES (:id, :version_id, :profile_id, :status)"
            ),
            {
                "id": chain.generation_id,
                "version_id": chain.version_id,
                "profile_id": chain.profile_id,
                "status": generation_status,
            },
        )
    return chain


def seed_chunk(engine: Engine, chain: SeededChain, *, chunk_index: int) -> uuid.UUID:
    chunk_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO chunk (id, generation_id, organization_id, kb_id, document_id, "
                "version_id, chunk_index, text, text_hash, model_input_hash, parser_version, "
                "chunker_version, token_count, heading_path, source_locator, fts) VALUES "
                "(:id, :generation_id, :organization_id, :kb_id, :document_id, :version_id, "
                ":chunk_index, :text, 'text-hash', 'input-hash', 'parser', 'chunker', 1, "
                "'[]'::jsonb, '{}'::jsonb, to_tsvector('simple', :text))"
            ),
            {
                "id": chunk_id,
                "generation_id": chain.generation_id,
                "organization_id": chain.organization_id,
                "kb_id": chain.kb_id,
                "document_id": chain.document_id,
                "version_id": chain.version_id,
                "chunk_index": chunk_index,
                "text": f"第 {chunk_index} 段内容",
            },
        )
    return chunk_id


def vector_literal(dimensions: int) -> str:
    return "[" + ",".join(["0"] * dimensions) + "]"


def insert_generation(connection: Connection, chain: SeededChain, status: str) -> None:
    connection.execute(
        text(
            "INSERT INTO index_generation (id, version_id, profile_id, status) "
            "VALUES (:id, :version_id, :profile_id, :status)"
        ),
        {
            "id": uuid.uuid4(),
            "version_id": chain.version_id,
            "profile_id": chain.profile_id,
            "status": status,
        },
    )


@pytest.fixture(scope="module")
def second_slice_schema(
    destructive_test_database: DestructiveTestDatabase,
) -> Iterator[Engine]:
    config = alembic_config(destructive_test_database.url)
    engine = create_engine(destructive_test_database.url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT current_database()"))
                == destructive_test_database.database_name
            )
            assert alembic_revision(connection) is None
            assert business_tables(connection) == set()

        command.upgrade(config, CORE_REVISION)
        with engine.connect() as connection:
            assert alembic_revision(connection) == CORE_REVISION
            assert business_tables(connection) == set(CORE_TABLES)

        command.upgrade(config, SECOND_SLICE_REVISION)
        yield engine
    finally:
        command.downgrade(config, CORE_REVISION)
        with engine.connect() as connection:
            assert alembic_revision(connection) == CORE_REVISION
            assert business_tables(connection) == set(CORE_TABLES)
            assert "generation_id" not in ingest_job_columns(connection)
            for table in SECOND_SLICE_TABLES:
                assert (
                    connection.scalar(text("SELECT to_regclass(:name)"), {"name": table})
                    is None
                )
                assert role_grants(connection, table, API_ROLE) == set()
                assert role_grants(connection, table, WORKER_ROLE) == set()
        command.downgrade(config, "base")
        with engine.connect() as connection:
            assert alembic_revision(connection) is None
            assert business_tables(connection) == set()
        engine.dispose()


def test_upgrade_creates_the_second_slice_tables(second_slice_schema: Engine) -> None:
    with second_slice_schema.connect() as connection:
        assert alembic_revision(connection) == SECOND_SLICE_REVISION
        assert business_tables(connection) == set(ALL_TABLES)
        assert "generation_id" in ingest_job_columns(connection)
        assert sequence_count(connection) == 0
        assert enum_type_count(connection) == 0
        assert ann_index_count(connection) == 0


def test_second_slice_constraints_and_indexes(second_slice_schema: Engine) -> None:
    with second_slice_schema.connect() as connection:
        constraints = named_constraints(connection)
        for table, expected in SECOND_SLICE_CONSTRAINTS.items():
            assert constraints.get(table, set()) == expected
        assert constraints.get("ingest_job", set()) == EXPECTED_INGEST_JOB_CONSTRAINTS

        indexes = named_indexes(connection)
        for table, expected in SECOND_SLICE_INDEXES.items():
            assert indexes.get(table, set()) == expected

        definitions = index_definitions(connection)

    assert "USING gin" in definitions["ix_chunk_fts"]
    ready_index = definitions["uq_index_generation_version_id_profile_id_ready"]
    assert "UNIQUE" in ready_index
    assert "WHERE" in ready_index
    assert "status = 'READY'" in ready_index


def test_second_slice_foreign_keys_restrict(second_slice_schema: Engine) -> None:
    with second_slice_schema.connect() as connection:
        behaviours = foreign_key_behaviours(connection)

    for name in SECOND_SLICE_FOREIGN_KEYS:
        assert behaviours[name] == ("r", "r"), f"{name} 必须默认 RESTRICT"


def test_second_slice_public_has_no_grants(second_slice_schema: Engine) -> None:
    with second_slice_schema.connect() as connection:
        counts = public_grant_counts(connection)

    for table in SECOND_SLICE_TABLES:
        assert counts.get(table, 0) == 0, f"{table} 仍向 PUBLIC 授权"


def test_second_slice_api_and_worker_grants(second_slice_schema: Engine) -> None:
    high_risk = {"DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}
    with second_slice_schema.connect() as connection:
        for table, expected_roles in SECOND_SLICE_ROLE_GRANTS.items():
            for role, expected in expected_roles.items():
                grants = role_grants(connection, table, role)
                assert grants == expected, f"{role} 在 {table} 的授权不符"
                assert grants.isdisjoint(high_risk)
        assert forbidden_privilege_count(connection) == 0


def test_second_slice_runtime_roles_are_denied_high_risk_dml(
    second_slice_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    with second_slice_schema.connect() as connection:
        assert connection.scalar(text("SELECT to_regclass('chunk')")) is not None
    for statement in (
        "UPDATE chunk SET text = 'x'",
        "DELETE FROM chunk",
        "TRUNCATE chunk",
        "UPDATE chunk_embedding SET profile_id = profile_id",
        "DELETE FROM chunk_embedding",
        "TRUNCATE chunk_embedding",
        "DELETE FROM index_generation",
        "TRUNCATE index_generation",
    ):
        assert_statement_denied(role_test_databases.worker_url, statement)

    for statement in (
        "INSERT INTO index_generation (id, version_id, profile_id, status) "
        "VALUES ('00000000-0000-0000-0000-000000000001', "
        "'00000000-0000-0000-0000-000000000002', "
        "'00000000-0000-0000-0000-000000000003', 'BUILDING')",
        "UPDATE chunk SET text = 'x'",
        "DELETE FROM chunk",
        "DELETE FROM chunk_embedding",
    ):
        assert_statement_denied(role_test_databases.api_url, statement)


def test_worker_can_insert_generation_and_both_runtime_roles_can_read(
    second_slice_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    chain = seed_chain(second_slice_schema)
    seed_chunk(second_slice_schema, chain, chunk_index=0)

    engine = create_engine(role_test_databases.worker_url, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            insert_generation(connection, chain, "BUILDING")
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT count(*) FROM index_generation WHERE version_id = :id"),
                {"id": chain.version_id},
            ) == 2
    finally:
        engine.dispose()

    api_engine = create_engine(role_test_databases.api_url, pool_pre_ping=True)
    try:
        with api_engine.connect() as connection:
            assert connection.scalar(
                text("SELECT count(*) FROM chunk WHERE generation_id = :id"),
                {"id": chain.generation_id},
            ) == 1
    finally:
        api_engine.dispose()


def test_512_dimension_vectors_are_accepted_and_other_dimensions_rejected(
    second_slice_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    chain = seed_chain(second_slice_schema)
    first_chunk = seed_chunk(second_slice_schema, chain, chunk_index=0)
    second_chunk = seed_chunk(second_slice_schema, chain, chunk_index=1)

    engine = create_engine(role_test_databases.worker_url, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO chunk_embedding (chunk_id, profile_id, embedding) VALUES "
                    "(:chunk_id, :profile_id, CAST(:embedding AS vector))"
                ),
                {
                    "chunk_id": first_chunk,
                    "profile_id": chain.profile_id,
                    "embedding": vector_literal(512),
                },
            )

        with engine.connect() as connection:
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO chunk_embedding (chunk_id, profile_id, embedding) VALUES "
                        "(:chunk_id, :profile_id, CAST(:embedding AS vector))"
                    ),
                    {
                        "chunk_id": first_chunk,
                        "profile_id": chain.profile_id,
                        "embedding": vector_literal(512),
                    },
                )
            connection.rollback()

        with engine.connect() as connection:
            with pytest.raises(DataError):
                connection.execute(
                    text(
                        "INSERT INTO chunk_embedding (chunk_id, profile_id, embedding) VALUES "
                        "(:chunk_id, :profile_id, CAST(:embedding AS vector))"
                    ),
                    {
                        "chunk_id": second_chunk,
                        "profile_id": chain.profile_id,
                        "embedding": vector_literal(511),
                    },
                )
            connection.rollback()
    finally:
        engine.dispose()


def test_only_one_ready_generation_per_version_and_profile(
    second_slice_schema: Engine,
) -> None:
    chain = seed_chain(second_slice_schema, generation_status="READY")

    with second_slice_schema.begin() as connection:
        with pytest.raises(IntegrityError):
            insert_generation(connection, chain, "READY")

    # 非 READY 状态可以并存，部分唯一索引只约束 READY。
    with second_slice_schema.begin() as connection:
        insert_generation(connection, chain, "BUILDING")
        insert_generation(connection, chain, "BUILDING")
        insert_generation(connection, chain, "FAILED")

    with second_slice_schema.connect() as connection:
        assert connection.scalar(
            text("SELECT count(*) FROM index_generation WHERE version_id = :id"),
            {"id": chain.version_id},
        ) == 4
