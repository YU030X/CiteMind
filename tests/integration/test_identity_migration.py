"""第四片身份与会话迁移的真实 PostgreSQL 验收。

从 ``20260923_0004`` 干净状态升级到 ``20260923_0005``，核对 ``user_account``、
``auth_session``、``kb_member`` 三张表、具名约束/外键、无 sequence/ENUM/ANN、
PUBLIC 收权与 api/worker 精确授权，并用真实 DML 正反用例证明唯一约束、角色 CHECK
与软撤销。只使用破坏性测试库与三角色 DSN 守卫；缺少 DSN 时按既有契约跳过。
"""

import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from database_guard import DestructiveTestDatabase
from database_roles_guard import API_ROLE, WORKER_ROLE, RoleTestDatabases
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import IntegrityError
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
from test_second_slice_migration import (
    CORE_TABLES,
    SECOND_SLICE_TABLES,
    ann_index_count,
    enum_type_count,
    forbidden_privilege_count,
)

pytestmark = pytest.mark.integration

LLM_USAGE_REVISION = "20260923_0004"
IDENTITY_REVISION = "20260923_0005"

LLM_USAGE_TABLES = ("llm_usage",)
IDENTITY_TABLES = ("user_account", "auth_session", "kb_member")
ALL_TABLES = CORE_TABLES + SECOND_SLICE_TABLES + LLM_USAGE_TABLES + IDENTITY_TABLES

IDENTITY_CONSTRAINTS = {
    "user_account": {
        "pk_user_account",
        "uq_user_account_organization_id_username",
    },
    "auth_session": {
        "pk_auth_session",
        "fk_auth_session_user_id_user_account",
        "uq_auth_session_token_hash",
    },
    "kb_member": {
        "pk_kb_member",
        "fk_kb_member_kb_id_knowledge_base",
        "fk_kb_member_user_id_user_account",
        "uq_kb_member_kb_id_user_id",
        "ck_kb_member_role",
    },
}

IDENTITY_ROLE_GRANTS = {
    "user_account": {API_ROLE: {"SELECT", "INSERT", "UPDATE"}, WORKER_ROLE: set()},
    "auth_session": {API_ROLE: {"SELECT", "INSERT", "UPDATE"}, WORKER_ROLE: set()},
    "kb_member": {API_ROLE: {"SELECT", "INSERT", "UPDATE"}, WORKER_ROLE: set()},
}

IDENTITY_FOREIGN_KEYS = (
    "fk_auth_session_user_id_user_account",
    "fk_kb_member_kb_id_knowledge_base",
    "fk_kb_member_user_id_user_account",
)

INSERT_USER_SQL = text(
    "INSERT INTO user_account (id, organization_id, username, password_hash, enabled, "
    "is_admin) VALUES (:id, :organization_id, :username, :password_hash, true, false)"
)
INSERT_SESSION_SQL = text(
    "INSERT INTO auth_session (id, user_id, token_hash, csrf_token_hash, expires_at) "
    "VALUES (:id, :user_id, :token_hash, :csrf_token_hash, now() + interval '1 hour')"
)
INSERT_KB_SQL = text(
    "INSERT INTO knowledge_base (id, organization_id, name) VALUES (:id, :organization_id, 'kb')"
)
INSERT_MEMBER_SQL = text(
    "INSERT INTO kb_member (id, kb_id, user_id, role) VALUES (:id, :kb_id, :user_id, :role)"
)


@pytest.fixture(scope="module")
def identity_schema(
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

        command.upgrade(config, IDENTITY_REVISION)
        yield engine
    finally:
        command.downgrade(config, LLM_USAGE_REVISION)
        with engine.connect() as connection:
            assert alembic_revision(connection) == LLM_USAGE_REVISION
            assert business_tables(connection) == set(
                CORE_TABLES + SECOND_SLICE_TABLES + LLM_USAGE_TABLES
            )
            for table in IDENTITY_TABLES:
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


def seed_user(engine: Engine, *, username: str, organization_id: uuid.UUID) -> uuid.UUID:
    user_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            INSERT_USER_SQL,
            {
                "id": user_id,
                "organization_id": organization_id,
                "username": username,
                "password_hash": "argon2-placeholder",
            },
        )
    return user_id


def seed_knowledge_base(engine: Engine, organization_id: uuid.UUID) -> uuid.UUID:
    kb_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            INSERT_KB_SQL, {"id": kb_id, "organization_id": organization_id}
        )
    return kb_id


def test_upgrade_creates_the_identity_tables(identity_schema: Engine) -> None:
    with identity_schema.connect() as connection:
        assert alembic_revision(connection) == IDENTITY_REVISION
        assert business_tables(connection) == set(ALL_TABLES)
        assert sequence_count(connection) == 0
        assert enum_type_count(connection) == 0
        assert ann_index_count(connection) == 0


def test_identity_constraints_match_the_frozen_contract(identity_schema: Engine) -> None:
    with identity_schema.connect() as connection:
        constraints = named_constraints(connection)
        indexes = named_indexes(connection)

    for table, expected in IDENTITY_CONSTRAINTS.items():
        assert constraints.get(table, set()) == expected
        assert indexes.get(table, set()) == set()


def test_identity_foreign_keys_restrict(identity_schema: Engine) -> None:
    with identity_schema.connect() as connection:
        behaviours = foreign_key_behaviours(connection)

    for name in IDENTITY_FOREIGN_KEYS:
        assert behaviours[name] == ("r", "r"), f"{name} 必须默认 RESTRICT"


def test_identity_public_has_no_grants(identity_schema: Engine) -> None:
    with identity_schema.connect() as connection:
        counts = public_grant_counts(connection)

    for table in IDENTITY_TABLES:
        assert counts.get(table, 0) == 0, f"{table} 仍向 PUBLIC 授权"


def test_identity_api_and_worker_grants(identity_schema: Engine) -> None:
    high_risk = {"DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}
    with identity_schema.connect() as connection:
        for table, expected_roles in IDENTITY_ROLE_GRANTS.items():
            for role, expected in expected_roles.items():
                grants = role_grants(connection, table, role)
                assert grants == expected, f"{role} 在 {table} 的授权不符"
                assert grants.isdisjoint(high_risk)
        assert forbidden_privilege_count(connection) == 0


def test_identity_runtime_roles_are_denied_high_risk_dml(
    identity_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    with identity_schema.connect() as connection:
        assert connection.scalar(text("SELECT to_regclass('user_account')")) is not None

    for table in IDENTITY_TABLES:
        assert_statement_denied(role_test_databases.worker_url, f"SELECT id FROM {table}")
        assert_statement_denied(role_test_databases.worker_url, f"DELETE FROM {table}")
        assert_statement_denied(role_test_databases.worker_url, f"TRUNCATE {table}")
        assert_statement_denied(role_test_databases.api_url, f"DELETE FROM {table}")
        assert_statement_denied(role_test_databases.api_url, f"TRUNCATE {table}")

    assert_statement_denied(
        role_test_databases.worker_url,
        "INSERT INTO user_account (id, organization_id, username, password_hash, "
        "enabled, is_admin) VALUES ('00000000-0000-0000-0000-000000000001', "
        "'00000000-0000-0000-0000-000000000002', 'x', 'y', true, false)",
    )
    assert_statement_denied(
        role_test_databases.worker_url, "UPDATE user_account SET enabled = false"
    )


def test_api_role_persists_identity_and_membership(
    identity_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    organization_id = uuid.uuid4()
    user_id = seed_user(identity_schema, username="alice", organization_id=organization_id)
    kb_id = seed_knowledge_base(identity_schema, organization_id)

    session_id = uuid.uuid4()
    member_id = uuid.uuid4()
    engine = create_engine(role_test_databases.api_url, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                INSERT_SESSION_SQL,
                {
                    "id": session_id,
                    "user_id": user_id,
                    "token_hash": uuid.uuid4().hex,
                    "csrf_token_hash": uuid.uuid4().hex,
                },
            )
            connection.execute(
                INSERT_MEMBER_SQL,
                {"id": member_id, "kb_id": kb_id, "user_id": user_id, "role": "OWNER"},
            )
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT count(*) FROM user_account WHERE id = :id"), {"id": user_id}
            ) == 1
            assert connection.scalar(
                text("SELECT count(*) FROM auth_session WHERE id = :id"), {"id": session_id}
            ) == 1
            assert connection.scalar(
                text("SELECT role FROM kb_member WHERE id = :id"), {"id": member_id}
            ) == "OWNER"
    finally:
        engine.dispose()


def test_identity_unique_constraints_reject_duplicates(
    identity_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    organization_id = uuid.uuid4()
    user_id = seed_user(identity_schema, username="bob", organization_id=organization_id)
    kb_id = seed_knowledge_base(identity_schema, organization_id)
    token_hash = uuid.uuid4().hex

    engine = create_engine(role_test_databases.api_url, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                INSERT_SESSION_SQL,
                {
                    "id": uuid.uuid4(),
                    "user_id": user_id,
                    "token_hash": token_hash,
                    "csrf_token_hash": uuid.uuid4().hex,
                },
            )
            connection.execute(
                INSERT_MEMBER_SQL,
                {"id": uuid.uuid4(), "kb_id": kb_id, "user_id": user_id, "role": "EDITOR"},
            )

        with engine.connect() as connection:
            with pytest.raises(IntegrityError):
                connection.execute(
                    INSERT_USER_SQL,
                    {
                        "id": uuid.uuid4(),
                        "organization_id": organization_id,
                        "username": "bob",
                        "password_hash": "argon2-placeholder",
                    },
                )
            connection.rollback()

        with engine.connect() as connection:
            with pytest.raises(IntegrityError):
                connection.execute(
                    INSERT_SESSION_SQL,
                    {
                        "id": uuid.uuid4(),
                        "user_id": user_id,
                        "token_hash": token_hash,
                        "csrf_token_hash": uuid.uuid4().hex,
                    },
                )
            connection.rollback()

        with engine.connect() as connection:
            with pytest.raises(IntegrityError):
                connection.execute(
                    INSERT_MEMBER_SQL,
                    {"id": uuid.uuid4(), "kb_id": kb_id, "user_id": user_id, "role": "READER"},
                )
            connection.rollback()
    finally:
        engine.dispose()


def test_kb_member_role_check_rejects_unknown_role(
    identity_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    organization_id = uuid.uuid4()
    user_id = seed_user(identity_schema, username="carol", organization_id=organization_id)
    kb_id = seed_knowledge_base(identity_schema, organization_id)

    engine = create_engine(role_test_databases.api_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            with pytest.raises(IntegrityError):
                connection.execute(
                    INSERT_MEMBER_SQL,
                    {"id": uuid.uuid4(), "kb_id": kb_id, "user_id": user_id, "role": "ADMIN"},
                )
            connection.rollback()
    finally:
        engine.dispose()


def test_api_role_soft_revokes_session_and_membership(
    identity_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    organization_id = uuid.uuid4()
    user_id = seed_user(identity_schema, username="dave", organization_id=organization_id)
    kb_id = seed_knowledge_base(identity_schema, organization_id)
    session_id = uuid.uuid4()
    member_id = uuid.uuid4()

    engine = create_engine(role_test_databases.api_url, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                INSERT_SESSION_SQL,
                {
                    "id": session_id,
                    "user_id": user_id,
                    "token_hash": uuid.uuid4().hex,
                    "csrf_token_hash": uuid.uuid4().hex,
                },
            )
            connection.execute(
                INSERT_MEMBER_SQL,
                {"id": member_id, "kb_id": kb_id, "user_id": user_id, "role": "READER"},
            )
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE auth_session SET revoked_at = now() WHERE id = :id"),
                {"id": session_id},
            )
            connection.execute(
                text("UPDATE kb_member SET revoked_at = now() WHERE id = :id"),
                {"id": member_id},
            )
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT revoked_at IS NOT NULL FROM auth_session WHERE id = :id"),
                {"id": session_id},
            ) is True
            assert connection.scalar(
                text("SELECT revoked_at IS NOT NULL FROM kb_member WHERE id = :id"),
                {"id": member_id},
            ) is True
    finally:
        engine.dispose()
