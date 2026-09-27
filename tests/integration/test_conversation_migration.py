"""问答四表（``conversation``/``message``/``query_run``/``citation``）的真实迁移与授权验收。

从干净状态升级到 ``20260927_0009``，核对四张表、具名约束、无索引、PUBLIC 收权与
api/worker 精确授权，并用真实 DML 证明写入与唯一约束；最后降级回 ``20260926_0008``
确认四表与授权无残留。只使用破坏性测试库与三角色 DSN 守卫；缺少 DSN 时跳过。
"""

from __future__ import annotations

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
    named_constraints,
    named_indexes,
    public_grant_counts,
    role_grants,
    sequence_count,
)
from test_retrieval_flow import (
    activate_version,
    insert_chunk,
    insert_document,
    insert_embedding,
    insert_generation,
    insert_kb,
    insert_profile,
    insert_user,
    insert_version,
)
from test_second_slice_migration import (
    CORE_TABLES,
    SECOND_SLICE_TABLES,
    ann_index_count,
    enum_type_count,
    forbidden_privilege_count,
)

pytestmark = pytest.mark.integration

REQUEST_TITLE_REVISION = "20260926_0008"
CONVERSATION_REVISION = "20260927_0009"
CONVERSATION_TABLES = ("conversation", "message", "query_run", "citation")
ALL_TABLES = (
    CORE_TABLES
    + SECOND_SLICE_TABLES
    + ("llm_usage",)
    + ("user_account", "auth_session", "kb_member")
    + CONVERSATION_TABLES
)

CONVERSATION_CONSTRAINTS = {
    "conversation": {"pk_conversation", "fk_conversation_owner_id_user_account"},
    "query_run": {
        "pk_query_run",
        "fk_query_run_conversation_id_conversation",
        "fk_query_run_llm_usage_id_llm_usage",
        "ck_query_run_status",
        "ck_query_run_input_token_budget_positive",
        "ck_query_run_output_token_budget_positive",
        "ck_query_run_estimated_input_tokens_non_negative",
        "ck_query_run_evidence_count_non_negative",
        "ck_query_run_provider_prompt_tokens_non_negative",
        "ck_query_run_provider_completion_tokens_non_negative",
        "ck_query_run_question_non_empty",
    },
    "message": {
        "pk_message",
        "fk_message_conversation_id_conversation",
        "fk_message_query_run_id_query_run",
        "uq_message_conversation_id_sequence",
        "ck_message_role",
        "ck_message_sequence_positive",
    },
    "citation": {
        "pk_citation",
        "fk_citation_message_id_message",
        "fk_citation_query_run_id_query_run",
        "fk_citation_chunk_id_chunk",
        "fk_citation_version_id_document_version",
        "uq_citation_message_id_display_label",
        "ck_citation_display_label_non_empty",
    },
}


@pytest.fixture(scope="module")
def conversation_schema(
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

        command.upgrade(config, CONVERSATION_REVISION)
        yield engine
    finally:
        command.downgrade(config, REQUEST_TITLE_REVISION)
        with engine.connect() as connection:
            assert alembic_revision(connection) == REQUEST_TITLE_REVISION
            for table in CONVERSATION_TABLES:
                assert (
                    connection.scalar(
                        text("SELECT to_regclass(:name)"), {"name": table}
                    )
                    is None
                )
                assert role_grants(connection, table, API_ROLE) == set()
                assert role_grants(connection, table, WORKER_ROLE) == set()
        command.downgrade(config, "base")
        with engine.connect() as connection:
            assert alembic_revision(connection) is None
            assert business_tables(connection) == set()
        engine.dispose()


def test_upgrade_creates_the_conversation_tables(conversation_schema: Engine) -> None:
    with conversation_schema.connect() as connection:
        assert alembic_revision(connection) == CONVERSATION_REVISION
        assert business_tables(connection) == set(ALL_TABLES)
        assert sequence_count(connection) == 0
        assert enum_type_count(connection) == 0
        assert ann_index_count(connection) == 0


def test_conversation_constraints_match_the_frozen_contract(
    conversation_schema: Engine,
) -> None:
    with conversation_schema.connect() as connection:
        constraints = named_constraints(connection)

    for table, expected in CONVERSATION_CONSTRAINTS.items():
        assert constraints.get(table, set()) == expected


def test_conversation_tables_have_no_secondary_indexes(
    conversation_schema: Engine,
) -> None:
    with conversation_schema.connect() as connection:
        indexes = named_indexes(connection)

    for table in CONVERSATION_TABLES:
        assert indexes.get(table, set()) == set()


def test_conversation_public_has_no_grants(conversation_schema: Engine) -> None:
    with conversation_schema.connect() as connection:
        counts = public_grant_counts(connection)

    for table in CONVERSATION_TABLES:
        assert counts.get(table, 0) == 0


def test_conversation_api_and_worker_grants(conversation_schema: Engine) -> None:
    high_risk = {"UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"}
    with conversation_schema.connect() as connection:
        for table in CONVERSATION_TABLES:
            api_grants = role_grants(connection, table, API_ROLE)
            worker_grants = role_grants(connection, table, WORKER_ROLE)
            assert api_grants == {"SELECT", "INSERT"}, table
            assert api_grants.isdisjoint(high_risk), table
            assert worker_grants == set(), table


def test_conversation_forbidden_privileges_are_absent(
    conversation_schema: Engine,
) -> None:
    with conversation_schema.connect() as connection:
        assert forbidden_privilege_count(connection) == 0


def test_api_role_writes_conversation_turn_facts(
    conversation_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    organization_id = uuid.uuid4()
    user_id = insert_user(conversation_schema, organization_id=organization_id)
    profile_id = insert_profile(conversation_schema, revision="rev-1")
    kb_id = insert_kb(
        conversation_schema, organization_id=organization_id, active_profile_id=profile_id
    )
    document_id = insert_document(conversation_schema, kb_id=kb_id)
    version_id = insert_version(conversation_schema, document_id=document_id)
    activate_version(
        conversation_schema, document_id=document_id, version_id=version_id
    )
    generation_id = insert_generation(
        conversation_schema, version_id=version_id, profile_id=profile_id
    )
    chunk_id = insert_chunk(
        conversation_schema,
        generation_id=generation_id,
        document_id=document_id,
        version_id=version_id,
        organization_id=organization_id,
        kb_id=kb_id,
    )
    insert_embedding(conversation_schema, chunk_id=chunk_id, profile_id=profile_id)

    conversation_id = uuid.uuid4()
    query_run_id = uuid.uuid4()
    user_message_id = uuid.uuid4()
    assistant_message_id = uuid.uuid4()
    citation_id = uuid.uuid4()
    engine = create_engine(role_test_databases.api_url, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO conversation (id, organization_id, owner_id, kb_scope) "
                    "VALUES (:id, :organization_id, :owner_id, CAST(:kb_scope AS jsonb))"
                ),
                {
                    "id": conversation_id,
                    "organization_id": organization_id,
                    "owner_id": user_id,
                    "kb_scope": f'["{kb_id}"]',
                },
            )
            connection.execute(
                text(
                    "INSERT INTO query_run (id, conversation_id, question, "
                    "standalone_question, scope_snapshot, input_token_budget, "
                    "output_token_budget, estimated_input_tokens, evidence_count, status, "
                    "insufficient_evidence, degraded_stages) VALUES "
                    "(:id, :conversation_id, '问题', '问题', '[]'::jsonb, 4000, 800, 10, 1, "
                    "'SUCCEEDED', false, '[]'::jsonb)"
                ),
                {"id": query_run_id, "conversation_id": conversation_id},
            )
            connection.execute(
                text(
                    "INSERT INTO message (id, conversation_id, sequence, role, content, "
                    "query_run_id) VALUES (:id, :conversation_id, 1, 'user', '问题', "
                    ":query_run_id)"
                ),
                {
                    "id": user_message_id,
                    "conversation_id": conversation_id,
                    "query_run_id": query_run_id,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO message (id, conversation_id, sequence, role, content, "
                    "query_run_id) VALUES (:id, :conversation_id, 2, 'assistant', '回答', "
                    ":query_run_id)"
                ),
                {
                    "id": assistant_message_id,
                    "conversation_id": conversation_id,
                    "query_run_id": query_run_id,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO citation (id, message_id, query_run_id, chunk_id, version_id, "
                    "display_label, locator_snapshot, quote, quote_hash) VALUES "
                    "(:id, :message_id, :query_run_id, :chunk_id, :version_id, 'E1', "
                    "'{}'::jsonb, '引文', 'hash')"
                ),
                {
                    "id": citation_id,
                    "message_id": assistant_message_id,
                    "query_run_id": query_run_id,
                    "chunk_id": chunk_id,
                    "version_id": version_id,
                },
            )
        with engine.connect() as connection:
            row_count = connection.scalar(
                text(
                    "SELECT count(*) FROM citation WHERE message_id = :message_id"
                ),
                {"message_id": assistant_message_id},
            )
    finally:
        engine.dispose()

    assert row_count == 1


def test_message_sequence_is_unique_per_conversation(
    conversation_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    organization_id = uuid.uuid4()
    user_id = insert_user(conversation_schema, organization_id=organization_id)
    conversation_id = uuid.uuid4()
    engine = create_engine(role_test_databases.api_url, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO conversation (id, organization_id, owner_id, kb_scope) "
                    "VALUES (:id, :organization_id, :owner_id, '[]'::jsonb)"
                ),
                {
                    "id": conversation_id,
                    "organization_id": organization_id,
                    "owner_id": user_id,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO message (id, conversation_id, sequence, role, content) "
                    "VALUES (:id, :conversation_id, 1, 'user', 'a')"
                ),
                {"id": uuid.uuid4(), "conversation_id": conversation_id},
            )
        with engine.connect() as connection:
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO message (id, conversation_id, sequence, role, content) "
                        "VALUES (:id, :conversation_id, 1, 'assistant', 'b')"
                    ),
                    {"id": uuid.uuid4(), "conversation_id": conversation_id},
                )
            connection.rollback()
    finally:
        engine.dispose()


def test_citation_is_read_only_and_worker_has_no_access(
    conversation_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    for statement in (
        "UPDATE conversation SET kb_scope = '[]'::jsonb",
        "DELETE FROM conversation",
        "TRUNCATE conversation",
        "UPDATE citation SET quote = 'x'",
    ):
        assert_statement_denied(role_test_databases.api_url, statement)

    for table in CONVERSATION_TABLES:
        assert_statement_denied(role_test_databases.worker_url, f"SELECT id FROM {table}")

    assert_statement_denied(
        role_test_databases.worker_url,
        "INSERT INTO message (id, conversation_id, sequence, role, content) VALUES "
        "('00000000-0000-0000-0000-000000000009', "
        "'00000000-0000-0000-0000-000000000008', 1, 'user', 'x')",
    )


def test_conversation_status_and_budget_checks_reject_invalid_rows(
    conversation_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    organization_id = uuid.uuid4()
    user_id = insert_user(conversation_schema, organization_id=organization_id)
    conversation_id = uuid.uuid4()
    engine = create_engine(role_test_databases.api_url, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO conversation (id, organization_id, owner_id, kb_scope) "
                    "VALUES (:id, :organization_id, :owner_id, '[]'::jsonb)"
                ),
                {
                    "id": conversation_id,
                    "organization_id": organization_id,
                    "owner_id": user_id,
                },
            )

        invalid_statements = (
            "INSERT INTO query_run (id, conversation_id, question, standalone_question, "
            "scope_snapshot, input_token_budget, output_token_budget, evidence_count, status, "
            "insufficient_evidence, degraded_stages) VALUES "
            "('00000000-0000-0000-0000-000000000011', :conversation_id, 'q', 'q', "
            "'[]'::jsonb, 0, 800, 0, 'SUCCEEDED', false, '[]'::jsonb)",
            "INSERT INTO query_run (id, conversation_id, question, standalone_question, "
            "scope_snapshot, input_token_budget, output_token_budget, evidence_count, status, "
            "insufficient_evidence, degraded_stages) VALUES "
            "('00000000-0000-0000-0000-000000000012', :conversation_id, 'q', 'q', "
            "'[]'::jsonb, 4000, 800, -1, 'SUCCEEDED', false, '[]'::jsonb)",
            "INSERT INTO query_run (id, conversation_id, question, standalone_question, "
            "scope_snapshot, input_token_budget, output_token_budget, evidence_count, status, "
            "insufficient_evidence, degraded_stages) VALUES "
            "('00000000-0000-0000-0000-000000000013', :conversation_id, 'q', 'q', "
            "'[]'::jsonb, 4000, 800, 0, 'BROKEN', false, '[]'::jsonb)",
        )
        with engine.connect() as connection:
            for statement in invalid_statements:
                with pytest.raises(IntegrityError):
                    connection.execute(text(statement), {"conversation_id": conversation_id})
                connection.rollback()
    finally:
        engine.dispose()
