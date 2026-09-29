"""``20260929_0013`` DOCX 来源迁移的真实 PostgreSQL 验收。

从 ``20260928_0012`` 升级到 ``20260929_0013``，核对：

- ``document`` 的具名 CHECK ``ck_document_source_type`` 允许 ``markdown``/``pdf``/``docx``；
- 非法来源仍被拒；
- 库中存在 ``source_type='docx'`` 行时，降级被拒绝且原行保留（绝不静默删除数据）；
- 无 DOCX 行时降级恢复旧约束，且旧约束再次拒绝 ``docx``。

只使用破坏性测试库与三角色 DSN 守卫；缺少 DSN 时按既有契约跳过。清理由所有权把手：
只有确认升级前库名与空 schema 之后才允许降级。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from database_guard import DestructiveTestDatabase
from database_roles_guard import RoleTestDatabases, assert_destructive_matches_roles
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import IntegrityError
from test_core_migration import alembic_config, alembic_revision, business_tables

pytestmark = pytest.mark.integration

DOCUMENT_ACL_REVISION = "20260928_0012"
DOCUMENT_SOURCE_DOCX_REVISION = "20260929_0013"

INSERT_KB_SQL = text(
    "INSERT INTO knowledge_base (id, organization_id, name) VALUES (:id, :organization_id, 'kb')"
)
INSERT_DOCUMENT_SQL = text(
    "INSERT INTO document (id, kb_id, title, source_type, lifecycle_status) "
    "VALUES (:id, :kb_id, 'doc', :source_type, 'CREATED')"
)
COUNT_DOCX_SQL = text("SELECT count(*) FROM document WHERE source_type = 'docx'")


def _insert_document(engine: Engine, kb_id: uuid.UUID, source_type: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            INSERT_DOCUMENT_SQL,
            {"id": uuid.uuid4(), "kb_id": kb_id, "source_type": source_type},
        )


def open_document_source_docx_schema(
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

        command.upgrade(config, DOCUMENT_ACL_REVISION)
        with engine.connect() as connection:
            assert alembic_revision(connection) == DOCUMENT_ACL_REVISION
        command.upgrade(config, DOCUMENT_SOURCE_DOCX_REVISION)
        yield engine
    finally:
        try:
            if owns_schema:
                # 先清掉本测试自建的行（只可能来自本次模块），再把当前 revision 降到 base。
                with engine.begin() as connection:
                    connection.execute(text("DELETE FROM document"))
                    connection.execute(text("DELETE FROM knowledge_base"))
                command.downgrade(config, "base")
                with engine.connect() as connection:
                    assert alembic_revision(connection) is None
                    assert business_tables(connection) == set()
        finally:
            engine.dispose()


@pytest.fixture(scope="module")
def document_source_docx_schema(
    destructive_test_database: DestructiveTestDatabase,
    role_test_databases: RoleTestDatabases,
) -> Iterator[Engine]:
    assert_destructive_matches_roles(destructive_test_database.url, role_test_databases)
    yield from open_document_source_docx_schema(destructive_test_database)


def _seed_kb(engine: Engine) -> uuid.UUID:
    kb_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            INSERT_KB_SQL, {"id": kb_id, "organization_id": uuid.uuid4()}
        )
    return kb_id


def test_upgrade_allows_docx_and_rejects_unknown(
    document_source_docx_schema: Engine,
) -> None:
    engine = document_source_docx_schema
    kb_id = _seed_kb(engine)
    _insert_document(engine, kb_id, "docx")
    with engine.connect() as connection:
        assert connection.scalar(COUNT_DOCX_SQL) == 1
    with pytest.raises(IntegrityError):
        _insert_document(engine, kb_id, "html")


def test_downgrade_refuses_and_preserves_docx_rows(
    document_source_docx_schema: Engine,
    destructive_test_database: DestructiveTestDatabase,
) -> None:
    engine = document_source_docx_schema
    with engine.connect() as connection:
        before = connection.scalar(COUNT_DOCX_SQL)
    kb_id = _seed_kb(engine)
    _insert_document(engine, kb_id, "docx")
    config = alembic_config(destructive_test_database.url)

    with pytest.raises(RuntimeError, match="docx"):
        command.downgrade(config, DOCUMENT_ACL_REVISION)

    with engine.connect() as connection:
        assert alembic_revision(connection) == DOCUMENT_SOURCE_DOCX_REVISION
        assert connection.scalar(COUNT_DOCX_SQL) == before + 1


def test_downgrade_without_docx_restores_old_constraint(
    document_source_docx_schema: Engine,
    destructive_test_database: DestructiveTestDatabase,
) -> None:
    engine = document_source_docx_schema
    config = alembic_config(destructive_test_database.url)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM document WHERE source_type = 'docx'"))

    command.downgrade(config, DOCUMENT_ACL_REVISION)
    with engine.connect() as connection:
        assert alembic_revision(connection) == DOCUMENT_ACL_REVISION
    kb_id = _seed_kb(engine)
    _insert_document(engine, kb_id, "markdown")
    with pytest.raises(IntegrityError):
        _insert_document(engine, kb_id, "docx")

    command.upgrade(config, DOCUMENT_SOURCE_DOCX_REVISION)
    with engine.connect() as connection:
        assert alembic_revision(connection) == DOCUMENT_SOURCE_DOCX_REVISION
