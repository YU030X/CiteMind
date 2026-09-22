"""第一切片业务迁移的真实 PostgreSQL 验收。

只使用破坏性测试库守卫；表级 ACL 通过系统目录核对。真实写语句拒绝检查额外
请求三角色 DSN 守卫，缺少时按既有契约跳过。
"""

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from database_guard import DestructiveTestDatabase
from database_roles_guard import API_ROLE, WORKER_ROLE, RoleTestDatabases
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import ProgrammingError

pytestmark = pytest.mark.integration

PGVECTOR_REVISION = "20260921_0001"
CORE_REVISION = "20260922_0002"

CORE_TABLES = (
    "index_profile",
    "knowledge_base",
    "document",
    "document_version",
    "ingest_job",
    "outbox_event",
)
SECOND_SLICE_TABLES = ("index_generation", "chunk", "chunk_embedding")

EXPECTED_CONSTRAINTS = {
    "index_profile": {
        "pk_index_profile",
        "uq_index_profile_config_hash",
        "ck_index_profile_dimension_is_512",
    },
    "knowledge_base": {
        "pk_knowledge_base",
        "fk_knowledge_base_active_index_profile_id_index_profile",
        "ck_knowledge_base_kb_revision_non_negative",
        "ck_knowledge_base_acl_revision_non_negative",
    },
    "document": {
        "pk_document",
        "fk_document_kb_id_knowledge_base",
        "fk_document_active_version_id_document_version",
        "ck_document_source_type",
        "ck_document_lifecycle_status",
    },
    "document_version": {
        "pk_document_version",
        "fk_document_version_document_id_document",
        "uq_document_version_document_id_version_no",
        "ck_document_version_version_no_positive",
        "ck_document_version_status",
    },
    "ingest_job": {
        "pk_ingest_job",
        "fk_ingest_job_document_id_document",
        "fk_ingest_job_version_id_document_version",
        "uq_ingest_job_dedupe_key",
        "ck_ingest_job_status",
        "ck_ingest_job_attempt_non_negative",
        "ck_ingest_job_lease_consistent",
    },
    "outbox_event": {
        "pk_outbox_event",
        "fk_outbox_event_job_id_ingest_job",
        "ck_outbox_event_status",
        "ck_outbox_event_dispatch_attempt_non_negative",
        "ck_outbox_event_lease_consistent",
    },
}

EXPECTED_INDEXES = {
    "document": {"ix_document_kb_id_lifecycle_status"},
    "ingest_job": {"ix_ingest_job_status_next_run_at"},
    "outbox_event": {"ix_outbox_event_status_next_send_at"},
}

EXPECTED_ROLE_GRANTS = {
    "index_profile": {API_ROLE: {"SELECT", "INSERT"}, WORKER_ROLE: {"SELECT"}},
    "knowledge_base": {API_ROLE: {"SELECT", "INSERT", "UPDATE"}, WORKER_ROLE: {"SELECT"}},
    "document": {API_ROLE: {"SELECT", "INSERT", "UPDATE"}, WORKER_ROLE: {"SELECT", "UPDATE"}},
    "document_version": {
        API_ROLE: {"SELECT", "INSERT", "UPDATE"},
        WORKER_ROLE: {"SELECT", "UPDATE"},
    },
    "ingest_job": {API_ROLE: {"SELECT", "INSERT", "UPDATE"}, WORKER_ROLE: {"SELECT", "UPDATE"}},
    "outbox_event": {API_ROLE: {"SELECT", "INSERT", "UPDATE"}, WORKER_ROLE: set()},
}

TABLES_QUERY = text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
CONSTRAINTS_QUERY = text(
    "SELECT t.relname, c.conname "
    "FROM pg_constraint c "
    "JOIN pg_class t ON t.oid = c.conrelid "
    "JOIN pg_namespace n ON n.oid = t.relnamespace "
    "WHERE n.nspname = 'public' AND c.contype <> 'n'"
)
# 只取独立索引，排除主键与唯一约束背后的索引。
INDEXES_QUERY = text(
    "SELECT t.relname, i.relname "
    "FROM pg_index x "
    "JOIN pg_class t ON t.oid = x.indrelid "
    "JOIN pg_class i ON i.oid = x.indexrelid "
    "JOIN pg_namespace n ON n.oid = t.relnamespace "
    "WHERE n.nspname = 'public' "
    "AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = x.indexrelid)"
)
FOREIGN_KEYS_QUERY = text(
    "SELECT c.conname, c.confdeltype, c.confupdtype "
    "FROM pg_constraint c "
    "WHERE c.contype = 'f' AND c.connamespace = 'public'::regnamespace"
)
PUBLIC_GRANTS_QUERY = text(
    "SELECT t.relname, count(*) FILTER (WHERE a.grantee = 0) AS public_grants "
    "FROM pg_class t "
    "JOIN pg_namespace n ON n.oid = t.relnamespace "
    "CROSS JOIN LATERAL aclexplode(COALESCE(t.relacl, acldefault('r', t.relowner))) AS a "
    "WHERE n.nspname = 'public' AND t.relkind IN ('r', 'p') "
    "GROUP BY t.relname"
)
ROLE_GRANTS_QUERY = text(
    "SELECT privilege_type FROM information_schema.role_table_grants "
    "WHERE table_schema = 'public' AND table_name = :table_name AND grantee = :role"
)


def alembic_config(database_url: str) -> Config:
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def alembic_revision(connection: Connection) -> str | None:
    if connection.scalar(text("SELECT to_regclass('alembic_version')")) is None:
        return None
    revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
    return None if revision is None else str(revision)


def business_tables(connection: Connection) -> set[str]:
    return {str(name) for name in connection.scalars(TABLES_QUERY)} - {"alembic_version"}


def named_constraints(connection: Connection) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for row in connection.execute(CONSTRAINTS_QUERY):
        result.setdefault(str(row[0]), set()).add(str(row[1]))
    return result


def named_indexes(connection: Connection) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for row in connection.execute(INDEXES_QUERY):
        result.setdefault(str(row[0]), set()).add(str(row[1]))
    return result


def foreign_key_behaviours(connection: Connection) -> dict[str, tuple[str, str]]:
    return {
        str(row[0]): (str(row[1]), str(row[2])) for row in connection.execute(FOREIGN_KEYS_QUERY)
    }


def public_grant_counts(connection: Connection) -> dict[str, int]:
    return {str(row[0]): int(row[1]) for row in connection.execute(PUBLIC_GRANTS_QUERY)}


def role_grants(connection: Connection, table_name: str, role: str) -> set[str]:
    return {
        str(privilege)
        for privilege in connection.scalars(
            ROLE_GRANTS_QUERY, {"table_name": table_name, "role": role}
        )
    }


def sequence_count(connection: Connection) -> int:
    count = connection.scalar(
        text(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind = 'S' AND n.nspname = 'public'"
        )
    )
    return int(count or 0)


def assert_statement_denied(database_url: str, statement: str) -> None:
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            with pytest.raises(ProgrammingError, match="permission denied"):
                connection.execute(text(statement))
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def core_schema(destructive_test_database: DestructiveTestDatabase) -> Iterator[Engine]:
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
        yield engine
    finally:
        command.downgrade(config, "base")
        with engine.connect() as connection:
            assert alembic_revision(connection) is None
            assert business_tables(connection) == set()
        engine.dispose()


def test_upgrade_creates_exactly_the_first_slice_tables(core_schema: Engine) -> None:
    with core_schema.connect() as connection:
        assert alembic_revision(connection) == CORE_REVISION
        assert business_tables(connection) == set(CORE_TABLES)
        for table in SECOND_SLICE_TABLES:
            assert connection.scalar(text("SELECT to_regclass(:name)"), {"name": table}) is None
        assert sequence_count(connection) == 0


def test_key_constraints_and_indexes_exist(core_schema: Engine) -> None:
    with core_schema.connect() as connection:
        constraints = named_constraints(connection)
        for table, expected in EXPECTED_CONSTRAINTS.items():
            assert constraints.get(table, set()) == expected

        indexes = named_indexes(connection)
        for table in CORE_TABLES:
            assert indexes.get(table, set()) == EXPECTED_INDEXES.get(table, set())


def test_foreign_keys_restrict_except_active_version_set_null(core_schema: Engine) -> None:
    with core_schema.connect() as connection:
        behaviours = foreign_key_behaviours(connection)

    assert behaviours["fk_document_active_version_id_document_version"] == ("n", "r")
    for name, behaviour in behaviours.items():
        if name != "fk_document_active_version_id_document_version":
            assert behaviour == ("r", "r"), f"{name} 必须默认 RESTRICT"


def test_public_has_no_grants_on_first_slice_tables(core_schema: Engine) -> None:
    with core_schema.connect() as connection:
        counts = public_grant_counts(connection)

    for table in CORE_TABLES:
        assert counts.get(table, 0) == 0, f"{table} 仍向 PUBLIC 授权"


def test_api_and_worker_grants_differ_as_designed(core_schema: Engine) -> None:
    high_risk = {"DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}
    with core_schema.connect() as connection:
        for table, expected_roles in EXPECTED_ROLE_GRANTS.items():
            for role, expected in expected_roles.items():
                grants = role_grants(connection, table, role)
                assert grants == expected, f"{role} 在 {table} 的授权不符"
                assert grants.isdisjoint(high_risk)


def test_runtime_roles_are_rejected_for_high_risk_statements(
    core_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    assert_statement_denied(role_test_databases.api_url, "DELETE FROM index_profile")
    assert_statement_denied(role_test_databases.api_url, "TRUNCATE document")
    assert_statement_denied(
        role_test_databases.api_url, "UPDATE index_profile SET normalize = true"
    )
    assert_statement_denied(
        role_test_databases.worker_url,
        "INSERT INTO index_profile (id, embedding_model, model_revision, dimension, "
        "normalize, tokenizer_revision, chunker_version, keyword_analyzer_version, config_hash) "
        "VALUES ('00000000-0000-0000-0000-000000000001', 'm', 'r', 512, true, 't', 'c', 'k', 'h')",
    )
    assert_statement_denied(
        role_test_databases.worker_url, "UPDATE knowledge_base SET name = 'x'"
    )
    assert_statement_denied(role_test_databases.worker_url, "SELECT id FROM outbox_event")


def test_api_role_can_insert_inside_a_rolled_back_transaction(
    core_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    engine = create_engine(role_test_databases.api_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            connection.execute(
                text(
                    "INSERT INTO index_profile (id, embedding_model, model_revision, dimension, "
                    "normalize, tokenizer_revision, chunker_version, keyword_analyzer_version, "
                    "config_hash) VALUES (:id, 'm', 'r', 512, true, 't', 'c', 'k', :config_hash)"
                ),
                {"id": uuid.uuid4(), "config_hash": uuid.uuid4().hex},
            )
            transaction.rollback()
    finally:
        engine.dispose()
