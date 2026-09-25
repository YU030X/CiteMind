"""``20260925_0007`` 的 ``knowledge_base`` 列级发布权限真实 PostgreSQL 验收。

从 ``20260925_0006`` 升级到 ``20260925_0007``，核对 worker 只获得
``active_index_profile_id`` 与 ``kb_revision`` 两列的 UPDATE，没有全表 UPDATE，也没有新增
结构或其它表授权；worker 在真实连接里能原子递增 ``kb_revision`` 并首次置位
``active_index_profile_id``，但不能改 ``name`` 等未授权列。降级只撤回这两列权限。

只使用破坏性测试库与三角色 DSN 守卫；缺少 DSN 时按既有契约跳过。清理由所有权把手：
只有确认升级前 ``current_database`` 正确、``alembic_version`` 与 public schema 表集合干净
之后才允许降级。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

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
    role_grants,
)

pytestmark = pytest.mark.integration

INGEST_JOB_PROFILE_REVISION = "20260925_0006"
WORKER_KB_PUBLISH_REVISION = "20260925_0007"

# 迁移只加列级 UPDATE，不新增任何业务表。
ALL_TABLES = (
    "index_profile",
    "knowledge_base",
    "document",
    "document_version",
    "ingest_job",
    "outbox_event",
    "index_generation",
    "chunk",
    "chunk_embedding",
    "llm_usage",
    "user_account",
    "auth_session",
    "kb_member",
)

PUBLISH_COLUMNS = ("active_index_profile_id", "kb_revision")
PRIVATE_COLUMNS = ("name", "acl_revision")

COLUMN_PRIVILEGE_QUERY = text(
    """
    SELECT
        has_table_privilege(to_regrole(:role), 'knowledge_base', 'UPDATE'),
        has_column_privilege(
            to_regrole(:role), 'knowledge_base', 'active_index_profile_id', 'UPDATE'
        ),
        has_column_privilege(to_regrole(:role), 'knowledge_base', 'kb_revision', 'UPDATE'),
        has_column_privilege(to_regrole(:role), 'knowledge_base', 'name', 'UPDATE'),
        has_column_privilege(
            to_regrole(:role), 'knowledge_base', 'acl_revision', 'UPDATE'
        ),
        has_table_privilege(to_regrole(:role), 'knowledge_base', 'SELECT')
    """
)

INSERT_PROFILE_SQL = text(
    "INSERT INTO index_profile (id, embedding_model, model_revision, dimension, normalize, "
    "tokenizer_revision, chunker_version, keyword_analyzer_version, config_hash) "
    "VALUES (:id, 'model', 'rev', 512, true, 'tok', 'chunk', 'kw', :config_hash)"
)
INSERT_KB_SQL = text(
    "INSERT INTO knowledge_base (id, organization_id, name, kb_revision, acl_revision) "
    "VALUES (:id, :organization_id, 'kb', 0, 0)"
)
COUNT_KB_SQL = text(
    "SELECT active_index_profile_id, kb_revision FROM knowledge_base WHERE id = :id"
)


def read_worker_column_privileges(engine: Engine) -> tuple[bool, ...]:
    with engine.connect() as connection:
        row = connection.execute(
            COLUMN_PRIVILEGE_QUERY, {"role": WORKER_ROLE}
        ).one()
    return tuple(bool(value) for value in row)


def seed_profile_and_kb(engine: Engine) -> tuple[uuid.UUID, uuid.UUID]:
    profile_id = uuid.uuid4()
    kb_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            INSERT_PROFILE_SQL, {"id": profile_id, "config_hash": profile_id.hex}
        )
        connection.execute(
            INSERT_KB_SQL, {"id": kb_id, "organization_id": uuid.uuid4()}
        )
    return profile_id, kb_id


def open_worker_kb_publish_schema(
    destructive_test_database: DestructiveTestDatabase,
) -> Iterator[tuple[Engine, tuple[uuid.UUID, uuid.UUID]]]:
    """0007 schema 生命周期；fixture 只是薄包装，前置不干净时不降级。"""

    config = alembic_config(destructive_test_database.url)
    engine = create_engine(destructive_test_database.url, pool_pre_ping=True)
    owns_schema = False
    seeded: tuple[uuid.UUID, uuid.UUID] | None = None
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT current_database()"))
                == destructive_test_database.database_name
            )
            assert alembic_revision(connection) is None
            assert business_tables(connection) == set()
        owns_schema = True

        command.upgrade(config, INGEST_JOB_PROFILE_REVISION)
        with engine.connect() as connection:
            assert alembic_revision(connection) == INGEST_JOB_PROFILE_REVISION
            assert business_tables(connection) == set(ALL_TABLES)

        # 升级前确认 worker 尚无 kb 的列级 UPDATE。
        before = read_worker_column_privileges(engine)
        assert before == (False, False, False, False, False, True)

        command.upgrade(config, WORKER_KB_PUBLISH_REVISION)
        seeded = seed_profile_and_kb(engine)
        yield engine, seeded
    finally:
        try:
            if owns_schema:
                try:
                    command.downgrade(config, INGEST_JOB_PROFILE_REVISION)
                    with engine.connect() as connection:
                        assert alembic_revision(connection) == INGEST_JOB_PROFILE_REVISION
                        assert role_grants(connection, "knowledge_base", WORKER_ROLE) == {
                            "SELECT"
                        }
                    after = read_worker_column_privileges(engine)
                    assert after == (False, False, False, False, False, True)
                finally:
                    command.downgrade(config, "base")
                    with engine.connect() as connection:
                        assert alembic_revision(connection) is None
                        assert business_tables(connection) == set()
        finally:
            engine.dispose()


@pytest.fixture(scope="module")
def worker_kb_publish_schema(
    destructive_test_database: DestructiveTestDatabase,
    role_test_databases: RoleTestDatabases,
) -> Iterator[tuple[Engine, tuple[uuid.UUID, uuid.UUID]]]:
    # 任何迁移/清理之前先确认破坏性 DSN 与三角色 DSN 指向同一 host/port/database。
    assert_destructive_matches_roles(destructive_test_database.url, role_test_databases)
    yield from open_worker_kb_publish_schema(destructive_test_database)


def test_upgrade_grants_only_two_publish_columns_to_worker(
    worker_kb_publish_schema: tuple[Engine, tuple[uuid.UUID, uuid.UUID]],
) -> None:
    engine, _ = worker_kb_publish_schema
    privileges = read_worker_column_privileges(engine)

    # 顺序：table-level UPDATE、两列 UPDATE、name/acl_revision UPDATE、SELECT。
    assert privileges == (False, True, True, False, False, True)


def test_upgrade_does_not_change_structure_or_other_grants(
    worker_kb_publish_schema: tuple[Engine, tuple[uuid.UUID, uuid.UUID]],
) -> None:
    engine, _ = worker_kb_publish_schema
    with engine.connect() as connection:
        assert business_tables(connection) == set(ALL_TABLES)
        assert role_grants(connection, "knowledge_base", API_ROLE) == {
            "SELECT",
            "INSERT",
            "UPDATE",
        }
        assert role_grants(connection, "knowledge_base", WORKER_ROLE) == {"SELECT"}


def test_worker_can_publish_columns_but_not_other_columns(
    worker_kb_publish_schema: tuple[Engine, tuple[uuid.UUID, uuid.UUID]],
    role_test_databases: RoleTestDatabases,
) -> None:
    _, (profile_id, kb_id) = worker_kb_publish_schema
    worker_engine = create_engine(role_test_databases.worker_url, pool_pre_ping=True)
    try:
        with worker_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE knowledge_base "
                    "SET active_index_profile_id = :profile_id, "
                    "    kb_revision = kb_revision + 1 "
                    "WHERE id = :id"
                ),
                {"profile_id": profile_id, "id": kb_id},
            )
        with worker_engine.connect() as connection:
            row = connection.execute(COUNT_KB_SQL, {"id": kb_id}).one()
        assert row[0] == profile_id
        assert row[1] == 1
    finally:
        worker_engine.dispose()

    # 未授权列与全表 UPDATE 都被拒。
    assert_statement_denied(
        role_test_databases.worker_url,
        "UPDATE knowledge_base SET name = 'x'",
    )
    assert_statement_denied(
        role_test_databases.worker_url,
        "UPDATE knowledge_base SET acl_revision = acl_revision + 1",
    )
