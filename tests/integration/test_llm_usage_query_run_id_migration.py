"""``20260929_0016`` llm_usage(query_run_id) 关联列与索引的真实 PostgreSQL 验收。

从 ``20260929_0015`` 升级到 ``20260929_0016``，核对：

- ``llm_usage`` 新增可空 UUID ``query_run_id``，没有外键、没有 server default；
- 恰好新增一个具名普通 btree 索引 ``ix_llm_usage_query_run_id``；
- 升级前写入的历史行升级后保持 NULL，不回填；
- api 角色能 SELECT/INSERT 新列，worker 角色仍被拒绝；
- 降级先删索引再删列，表与既有授权不变。

只使用破坏性测试库与三角色 DSN 守卫；缺少 DSN 时按既有契约跳过。清理由所有权把手：
只有确认升级前库名与空 schema 之后才允许降级。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from alembic import command
from database_guard import DestructiveTestDatabase
from database_roles_guard import (
    API_ROLE,
    WORKER_ROLE,
    RoleTestDatabases,
    assert_destructive_matches_roles,
)
from sqlalchemy import Engine, create_engine, text
from test_core_migration import (
    alembic_config,
    alembic_revision,
    assert_statement_denied,
    business_tables,
    named_constraints,
    named_indexes,
    role_grants,
)
from test_llm_usage_migration import (
    INSERT_FAILURE_SQL,
    LLM_USAGE_CONSTRAINTS,
    LLM_USAGE_TABLE,
)

pytestmark = pytest.mark.integration

PREVIOUS_REVISION = "20260929_0015"
QUERY_RUN_REVISION = "20260929_0016"
INDEX_NAME = "ix_llm_usage_query_run_id"
COLUMN_NAME = "query_run_id"

COLUMN_SQL = text(
    "SELECT data_type, is_nullable FROM information_schema.columns "
    "WHERE table_schema = 'public' AND table_name = 'llm_usage' "
    "AND column_name = :column_name"
)
INDEX_DEF_SQL = text(
    "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND indexname = :name"
)
INSERT_WITH_QUERY_RUN_SQL = text(
    "INSERT INTO llm_usage (id, provider, model, stage, status, error_code, "
    "usage_source, attempt, latency_ms, query_run_id) VALUES "
    "(:id, 'deepseek', 'deepseek-flash', 'qa_answer', 'FAILED', 'X', 'UNKNOWN', 1, 10, "
    ":query_run_id)"
)
SELECT_QUERY_RUN_SQL = text("SELECT query_run_id FROM llm_usage WHERE id = :id")


@dataclass(frozen=True)
class CorrelationSchema:
    engine: Engine
    legacy_usage_id: uuid.UUID


def column_definition(engine: Engine) -> tuple[str, str] | None:
    with engine.connect() as connection:
        row = connection.execute(COLUMN_SQL, {"column_name": COLUMN_NAME}).first()
    return None if row is None else (str(row[0]), str(row[1]))


def index_definition(engine: Engine, name: str) -> str | None:
    with engine.connect() as connection:
        value = connection.scalar(INDEX_DEF_SQL, {"name": name})
    return None if value is None else str(value)


def appends_usage_with_query_run(api_url: str, query_run_id: uuid.UUID) -> uuid.UUID:
    usage_id = uuid.uuid4()
    engine = create_engine(api_url, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                INSERT_WITH_QUERY_RUN_SQL,
                {"id": usage_id, "query_run_id": query_run_id},
            )
    finally:
        engine.dispose()
    return usage_id


def appends_legacy_failure(api_url: str) -> uuid.UUID:
    """在升级前写入一行不带 query_run_id 的历史失败事实。"""

    usage_id = uuid.uuid4()
    engine = create_engine(api_url, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(INSERT_FAILURE_SQL, {"id": usage_id})
    finally:
        engine.dispose()
    return usage_id


@pytest.fixture(scope="module")
def correlation_schema(
    destructive_test_database: DestructiveTestDatabase,
    role_test_databases: RoleTestDatabases,
) -> Iterator[CorrelationSchema]:
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

        command.upgrade(config, PREVIOUS_REVISION)
        with engine.connect() as connection:
            assert alembic_revision(connection) == PREVIOUS_REVISION
        # 升级前写入历史行：它没有 query_run_id，升级后必须保持 NULL。
        legacy_usage_id = appends_legacy_failure(role_test_databases.api_url)
        assert column_definition(engine) is None
        assert index_definition(engine, INDEX_NAME) is None

        command.upgrade(config, QUERY_RUN_REVISION)
        with engine.connect() as connection:
            assert alembic_revision(connection) == QUERY_RUN_REVISION
        yield CorrelationSchema(engine=engine, legacy_usage_id=legacy_usage_id)
    finally:
        try:
            if owns_schema:
                command.downgrade(config, "base")
                with engine.connect() as connection:
                    assert alembic_revision(connection) is None
                    assert business_tables(connection) == set()
        finally:
            engine.dispose()


def test_upgrade_adds_a_nullable_uuid_column_without_constraint(
    correlation_schema: CorrelationSchema,
) -> None:
    engine = correlation_schema.engine
    assert column_definition(engine) == ("uuid", "YES")
    with engine.connect() as connection:
        # 不建外键：llm_usage 的具名约束集合不变，且表级授权不因新列新增。
        assert named_constraints(connection).get(LLM_USAGE_TABLE, set()) == LLM_USAGE_CONSTRAINTS
        assert connection.scalar(
            text(
                "SELECT count(*) FROM pg_constraint WHERE conname = "
                "'fk_llm_usage_query_run_id_query_run'"
            )
        ) == 0


def test_upgrade_adds_exactly_one_named_btree_index(correlation_schema: CorrelationSchema) -> None:
    engine = correlation_schema.engine
    with engine.connect() as connection:
        assert named_indexes(connection).get(LLM_USAGE_TABLE, set()) == {INDEX_NAME}
    definition = index_definition(engine, INDEX_NAME)
    assert definition is not None
    assert "USING btree" in definition
    assert "(query_run_id)" in definition
    assert "UNIQUE" not in definition.upper()
    assert "WHERE" not in definition.upper()


def test_historical_row_keeps_null_query_run_id(correlation_schema: CorrelationSchema) -> None:
    with correlation_schema.engine.connect() as connection:
        value = connection.scalar(
            SELECT_QUERY_RUN_SQL, {"id": correlation_schema.legacy_usage_id}
        )
    assert value is None


def test_grants_are_unchanged_for_the_new_column(
    correlation_schema: CorrelationSchema, role_test_databases: RoleTestDatabases
) -> None:
    with correlation_schema.engine.connect() as connection:
        assert role_grants(connection, LLM_USAGE_TABLE, API_ROLE) == {"SELECT", "INSERT"}
        assert role_grants(connection, LLM_USAGE_TABLE, WORKER_ROLE) == set()

    expected = uuid.uuid4()
    usage_id = appends_usage_with_query_run(role_test_databases.api_url, expected)
    engine = create_engine(role_test_databases.api_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            assert connection.scalar(SELECT_QUERY_RUN_SQL, {"id": usage_id}) == expected
    finally:
        engine.dispose()

    assert_statement_denied(
        role_test_databases.worker_url, "SELECT query_run_id FROM llm_usage"
    )
    assert_statement_denied(
        role_test_databases.worker_url,
        "INSERT INTO llm_usage (id, provider, model, stage, status, error_code, "
        "usage_source, attempt, query_run_id) VALUES "
        "('00000000-0000-0000-0000-000000000099', 'deepseek', 'deepseek-flash', "
        "'qa_answer', 'FAILED', 'X', 'UNKNOWN', 1, "
        "'00000000-0000-0000-0000-000000000098')",
    )


def test_downgrade_removes_index_then_column(
    correlation_schema: CorrelationSchema,
    destructive_test_database: DestructiveTestDatabase,
) -> None:
    config = alembic_config(destructive_test_database.url)

    command.downgrade(config, PREVIOUS_REVISION)

    assert index_definition(correlation_schema.engine, INDEX_NAME) is None
    assert column_definition(correlation_schema.engine) is None
    with correlation_schema.engine.connect() as connection:
        assert alembic_revision(connection) == PREVIOUS_REVISION
        assert LLM_USAGE_TABLE in business_tables(connection)
        assert role_grants(connection, LLM_USAGE_TABLE, API_ROLE) == {"SELECT", "INSERT"}

    command.upgrade(config, QUERY_RUN_REVISION)
    assert index_definition(correlation_schema.engine, INDEX_NAME) is not None
    assert column_definition(correlation_schema.engine) == ("uuid", "YES")
