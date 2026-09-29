"""``20260929_0014`` chunk(model_input_hash) 索引迁移的真实 PostgreSQL 验收。

从 ``20260929_0013`` 升级到 ``20260929_0014``，核对 ``chunk`` 上恰好新增一个具名普通 btree
索引 ``ix_chunk_model_input_hash``、列正确、非唯一、非部分；降级只删除该索引。

只使用破坏性测试库与三角色 DSN 守卫；缺少 DSN 时按既有契约跳过。清理由所有权把手：
只有确认升级前库名与空 schema 之后才允许降级。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from alembic import command
from database_guard import DestructiveTestDatabase
from database_roles_guard import RoleTestDatabases, assert_destructive_matches_roles
from sqlalchemy import Engine, create_engine, text
from test_core_migration import alembic_config, alembic_revision, business_tables

pytestmark = pytest.mark.integration

DOCUMENT_SOURCE_DOCX_REVISION = "20260929_0013"
CHUNK_MODEL_INPUT_HASH_REVISION = "20260929_0014"
INDEX_NAME = "ix_chunk_model_input_hash"

INDEX_DEF_SQL = text(
    "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND indexname = :name"
)


def index_definition(engine: Engine, name: str) -> str | None:
    with engine.connect() as connection:
        value = connection.scalar(INDEX_DEF_SQL, {"name": name})
    return None if value is None else str(value)


def open_index_schema(
    destructive_test_database: DestructiveTestDatabase,
) -> Iterator[Engine]:
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

        command.upgrade(config, DOCUMENT_SOURCE_DOCX_REVISION)
        with engine.connect() as connection:
            assert alembic_revision(connection) == DOCUMENT_SOURCE_DOCX_REVISION
        # 升级前该索引不存在，证明断言的是本迁移新增的对象。
        assert index_definition(engine, INDEX_NAME) is None
        command.upgrade(config, CHUNK_MODEL_INPUT_HASH_REVISION)
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


@pytest.fixture(scope="module")
def index_schema(
    destructive_test_database: DestructiveTestDatabase,
    role_test_databases: RoleTestDatabases,
) -> Iterator[Engine]:
    assert_destructive_matches_roles(destructive_test_database.url, role_test_databases)
    yield from open_index_schema(destructive_test_database)


def test_index_is_named_btree_on_model_input_hash(index_schema: Engine) -> None:
    definition = index_definition(index_schema, INDEX_NAME)

    assert definition is not None
    assert "USING btree" in definition
    assert "(model_input_hash)" in definition
    assert "UNIQUE" not in definition.upper()
    assert "WHERE" not in definition.upper()


def test_downgrade_removes_only_the_index(
    index_schema: Engine,
    destructive_test_database: DestructiveTestDatabase,
) -> None:
    config = alembic_config(destructive_test_database.url)

    command.downgrade(config, DOCUMENT_SOURCE_DOCX_REVISION)

    assert index_definition(index_schema, INDEX_NAME) is None
    with index_schema.connect() as connection:
        assert alembic_revision(connection) == DOCUMENT_SOURCE_DOCX_REVISION
        # 表与其它对象仍在：降级只删索引。
        assert "chunk" in business_tables(connection)

    command.upgrade(config, CHUNK_MODEL_INPUT_HASH_REVISION)
    assert index_definition(index_schema, INDEX_NAME) is not None
