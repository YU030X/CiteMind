"""``20260928_0012`` 文档 ACL 迁移的真实 PostgreSQL 验收。

从 ``20260927_0011`` 升级到 ``20260928_0012``，核对：

- ``document`` 增加非空 ``acl_mode``（默认 ``INHERIT``）与具名 CHECK；
- 新建 ``document_acl``，具名唯一/外键/CHECK，只允许 ``USER``/``READ``；
- api 只有 ``SELECT/INSERT/DELETE``，worker 与 ``PUBLIC`` 无权限；
- api 可插入/删除名单行但不能 UPDATE；worker 读写均被拒；
- 降级只删除 ``document_acl``、``acl_mode`` 与其 CHECK。

只使用破坏性测试库与三角色 DSN 守卫；缺少 DSN 时按既有契约跳过。清理由所有权把手：
只有确认升级前库名与空 schema 之后才允许降级。
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

QUERY_RUN_OPTIONS_REVISION = "20260927_0011"
DOCUMENT_ACL_REVISION = "20260928_0012"

CORE_TABLES = (
    "index_profile",
    "knowledge_base",
    "document",
    "document_version",
    "ingest_job",
    "outbox_event",
)
SECOND_SLICE_TABLES = ("index_generation", "chunk", "chunk_embedding")
IDENTITY_TABLES = ("user_account", "auth_session", "kb_member")
CONVERSATION_TABLES = ("conversation", "message", "query_run", "citation")
ALL_TABLES = (
    CORE_TABLES
    + SECOND_SLICE_TABLES
    + ("llm_usage",)
    + IDENTITY_TABLES
    + CONVERSATION_TABLES
    + ("document_acl",)
)

ACL_MODE_CONSTRAINT = "ck_document_acl_mode"
DOCUMENT_ACL_CONSTRAINTS = {
    "pk_document_acl",
    "fk_document_acl_document_id_document",
    "fk_document_acl_principal_id_user_account",
    "uq_document_acl_document_principal_permission",
    "ck_document_acl_principal_type",
    "ck_document_acl_permission",
}

ACL_MODE_QUERY = text(
    """
    SELECT data_type, is_nullable, column_default
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'document' AND column_name = 'acl_mode'
    """
)
CHECK_CONSTRAINTS_QUERY = text(
    "SELECT conname FROM pg_constraint c "
    "JOIN pg_class t ON t.oid = c.conrelid "
    "JOIN pg_namespace n ON n.oid = t.relnamespace "
    "WHERE n.nspname = 'public' AND t.relname = 'document_acl' AND c.contype = 'c'"
)

INSERT_USER_SQL = text(
    "INSERT INTO user_account (id, organization_id, username, password_hash, enabled, is_admin) "
    "VALUES (:id, :organization_id, :username, 'x', true, false)"
)
INSERT_KB_SQL = text(
    "INSERT INTO knowledge_base (id, organization_id, name) VALUES (:id, :organization_id, 'kb')"
)
INSERT_DOCUMENT_SQL = text(
    "INSERT INTO document (id, kb_id, title, source_type, lifecycle_status) "
    "VALUES (:id, :kb_id, 'doc', 'markdown', 'CREATED')"
)
INSERT_ACL_SQL = text(
    "INSERT INTO document_acl (id, document_id, principal_type, principal_id, permission) "
    "VALUES (:id, :document_id, 'USER', :principal_id, 'READ')"
)
COUNT_ACL_SQL = text("SELECT count(*) FROM document_acl WHERE document_id = :id")


def open_document_acl_schema(
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

        command.upgrade(config, QUERY_RUN_OPTIONS_REVISION)
        with engine.connect() as connection:
            assert alembic_revision(connection) == QUERY_RUN_OPTIONS_REVISION
            assert "document_acl" not in business_tables(connection)

        command.upgrade(config, DOCUMENT_ACL_REVISION)
        yield engine
    finally:
        try:
            if owns_schema:
                command.downgrade(config, QUERY_RUN_OPTIONS_REVISION)
                with engine.connect() as connection:
                    assert alembic_revision(connection) == QUERY_RUN_OPTIONS_REVISION
                    assert "document_acl" not in business_tables(connection)
                    assert (
                        connection.scalar(
                            text(
                                "SELECT count(*) FROM information_schema.columns "
                                "WHERE table_schema='public' AND table_name='document' "
                                "AND column_name='acl_mode'"
                            )
                        )
                        == 0
                    )
                command.downgrade(config, "base")
                with engine.connect() as connection:
                    assert alembic_revision(connection) is None
                    assert business_tables(connection) == set()
        finally:
            engine.dispose()


@pytest.fixture(scope="module")
def document_acl_schema(
    destructive_test_database: DestructiveTestDatabase,
    role_test_databases: RoleTestDatabases,
) -> Iterator[Engine]:
    assert_destructive_matches_roles(destructive_test_database.url, role_test_databases)
    yield from open_document_acl_schema(destructive_test_database)


def seed_rows(engine: Engine) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    kb_id = uuid.uuid4()
    document_id = uuid.uuid4()
    user_id = uuid.uuid4()
    other_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            INSERT_USER_SQL,
            {"id": user_id, "organization_id": uuid.uuid4(), "username": f"u-{user_id.hex[:8]}"},
        )
        connection.execute(
            INSERT_USER_SQL,
            {
                "id": other_id,
                "organization_id": uuid.uuid4(),
                "username": f"u-{other_id.hex[:8]}",
            },
        )
        connection.execute(
            INSERT_KB_SQL, {"id": kb_id, "organization_id": uuid.uuid4()}
        )
        connection.execute(INSERT_DOCUMENT_SQL, {"id": document_id, "kb_id": kb_id})
    return kb_id, document_id, user_id, other_id


def test_upgrade_adds_acl_mode_and_table(document_acl_schema: Engine) -> None:
    engine = document_acl_schema
    with engine.connect() as connection:
        assert business_tables(connection) == set(ALL_TABLES)
        mode = connection.execute(ACL_MODE_QUERY).one()
        assert mode[0] == "text"
        assert mode[1] == "NO"
        assert mode[2] is not None and "INHERIT" in str(mode[2])
    with engine.connect() as connection:
        names = {
            str(name)
            for name in connection.scalars(
                text(
                    "SELECT conname FROM pg_constraint c "
                    "JOIN pg_class t ON t.oid = c.conrelid "
                    "JOIN pg_namespace n ON n.oid = t.relnamespace "
                    "WHERE n.nspname='public' AND t.relname='document' AND c.contype='c'"
                )
            )
        }
        assert ACL_MODE_CONSTRAINT in names
        acl_checks = {
            str(name)
            for name in connection.scalars(CHECK_CONSTRAINTS_QUERY)
        }
        assert acl_checks == {"ck_document_acl_principal_type", "ck_document_acl_permission"}
        acl_constraints = {
            str(name)
            for name in connection.scalars(
                text(
                    "SELECT conname FROM pg_constraint c "
                    "JOIN pg_class t ON t.oid = c.conrelid "
                    "JOIN pg_namespace n ON n.oid = t.relnamespace "
                    "WHERE n.nspname='public' AND t.relname='document_acl'"
                )
            )
        }
        assert acl_constraints == DOCUMENT_ACL_CONSTRAINTS


def test_grants_are_exact(document_acl_schema: Engine) -> None:
    engine = document_acl_schema
    with engine.connect() as connection:
        assert role_grants(connection, "document_acl", API_ROLE) == {
            "SELECT",
            "INSERT",
            "DELETE",
        }
        assert role_grants(connection, "document_acl", WORKER_ROLE) == set()
        # worker 不能读也不能写；PUBLIC 无权限。
        public_privileges = connection.scalar(
            text(
                "SELECT count(*) FROM information_schema.role_table_grants "
                "WHERE table_schema='public' AND table_name='document_acl' AND grantee='PUBLIC'"
            )
        )
        assert public_privileges == 0


def test_api_can_replace_rows_but_not_update(
    document_acl_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    _, document_id, user_id, other_id = seed_rows(document_acl_schema)
    api_engine = create_engine(role_test_databases.api_url, pool_pre_ping=True)
    try:
        with api_engine.begin() as connection:
            connection.execute(
                INSERT_ACL_SQL,
                {"id": uuid.uuid4(), "document_id": document_id, "principal_id": user_id},
            )
            connection.execute(
                INSERT_ACL_SQL,
                {"id": uuid.uuid4(), "document_id": document_id, "principal_id": other_id},
            )
        with api_engine.connect() as connection:
            assert connection.scalar(COUNT_ACL_SQL, {"id": document_id}) == 2
        with api_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM document_acl WHERE document_id = :id"),
                {"id": document_id},
            )
        with api_engine.connect() as connection:
            assert connection.scalar(COUNT_ACL_SQL, {"id": document_id}) == 0
    finally:
        api_engine.dispose()

    # UPDATE 未授权；worker 读写都被拒。
    assert_statement_denied(
        role_test_databases.api_url,
        "UPDATE document_acl SET permission = 'READ'",
    )
    assert_statement_denied(
        role_test_databases.worker_url, "SELECT * FROM document_acl"
    )
    assert_statement_denied(
        role_test_databases.worker_url,
        "INSERT INTO document_acl (id, document_id, principal_type, principal_id, permission) "
        f"VALUES ('{uuid.uuid4()}', '{uuid.uuid4()}', 'USER', '{uuid.uuid4()}', 'READ')",
    )
