"""第三切片 llm_usage 迁移的真实 PostgreSQL 验收。

从 ``20260922_0003`` 干净状态升级到 ``20260923_0004``，核对 append-only 账本表、
约束、PUBLIC 收权与 api/worker 精确授权，并用真实 DML 证明成功/失败事实的约束。
只使用破坏性测试库与三角色 DSN 守卫；缺少 DSN 时按既有契约跳过。
"""

import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from alembic import command
from database_guard import DestructiveTestDatabase
from database_roles_guard import API_ROLE, WORKER_ROLE, RoleTestDatabases
from rag_backend.config import Settings
from rag_backend.llm_probe import (
    EXIT_PROVIDER_FAILURE,
    LedgerPreflightError,
    SqlAlchemyUsageLedger,
    run_probe,
)
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
from test_second_slice_migration import (
    CORE_TABLES,
    SECOND_SLICE_TABLES,
    ann_index_count,
    enum_type_count,
    forbidden_privilege_count,
)

pytestmark = pytest.mark.integration

SECOND_SLICE_REVISION = "20260922_0003"
LLM_USAGE_REVISION = "20260923_0004"
LLM_USAGE_TABLE = "llm_usage"
ALL_TABLES = CORE_TABLES + SECOND_SLICE_TABLES + (LLM_USAGE_TABLE,)

LLM_USAGE_CONSTRAINTS = {
    "pk_llm_usage",
    "ck_llm_usage_status",
    "ck_llm_usage_usage_source",
    "ck_llm_usage_attempt_positive",
    "ck_llm_usage_prompt_tokens_non_negative",
    "ck_llm_usage_completion_tokens_non_negative",
    "ck_llm_usage_cache_hit_tokens_non_negative",
    "ck_llm_usage_cache_miss_tokens_non_negative",
    "ck_llm_usage_latency_ms_non_negative",
    "ck_llm_usage_cost_amount_non_negative",
    "ck_llm_usage_succeeded_requires_provider_usage",
    "ck_llm_usage_failure_has_error_code",
    "ck_llm_usage_price_consistent",
}

INSERT_SUCCESS_SQL = text(
    "INSERT INTO llm_usage (id, provider, model, stage, status, usage_source, attempt, "
    "prompt_tokens, completion_tokens, latency_ms) VALUES "
    "(:id, 'deepseek', 'deepseek-flash', 'llm_probe', 'SUCCEEDED', 'PROVIDER_REPORTED', 1, "
    "9, 1, 120)"
)
INSERT_FAILURE_SQL = text(
    "INSERT INTO llm_usage (id, provider, model, stage, status, error_code, usage_source, "
    "attempt, latency_ms) VALUES "
    "(:id, 'deepseek', 'deepseek-flash', 'llm_probe', 'TIMEOUT', 'TIMEOUT', 'UNKNOWN', 1, 15000)"
)

PROVIDER_SUCCESS_BODY: dict[str, object] = {
    "id": "completion-1",
    "usage": {
        "prompt_tokens": 9,
        "completion_tokens": 1,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 9,
    },
}
PROVIDER_NEGATIVE_CACHE_BODY: dict[str, object] = {
    "id": "completion-2",
    "usage": {
        "prompt_tokens": 9,
        "completion_tokens": 1,
        "prompt_cache_hit_tokens": -4,
        "prompt_cache_miss_tokens": 9,
    },
}


@pytest.fixture(scope="module")
def llm_usage_schema(
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

        command.upgrade(config, LLM_USAGE_REVISION)
        yield engine
    finally:
        command.downgrade(config, SECOND_SLICE_REVISION)
        with engine.connect() as connection:
            assert alembic_revision(connection) == SECOND_SLICE_REVISION
            assert business_tables(connection) == set(CORE_TABLES + SECOND_SLICE_TABLES)
            assert (
                connection.scalar(text("SELECT to_regclass(:name)"), {"name": LLM_USAGE_TABLE})
                is None
            )
            assert role_grants(connection, LLM_USAGE_TABLE, API_ROLE) == set()
            assert role_grants(connection, LLM_USAGE_TABLE, WORKER_ROLE) == set()
        command.downgrade(config, "base")
        with engine.connect() as connection:
            assert alembic_revision(connection) is None
            assert business_tables(connection) == set()
        engine.dispose()


def test_upgrade_creates_the_llm_usage_table(llm_usage_schema: Engine) -> None:
    with llm_usage_schema.connect() as connection:
        assert alembic_revision(connection) == LLM_USAGE_REVISION
        assert business_tables(connection) == set(ALL_TABLES)
        assert sequence_count(connection) == 0
        assert enum_type_count(connection) == 0
        assert ann_index_count(connection) == 0


def test_llm_usage_constraints_match_the_frozen_contract(llm_usage_schema: Engine) -> None:
    with llm_usage_schema.connect() as connection:
        constraints = named_constraints(connection)

    assert constraints.get(LLM_USAGE_TABLE, set()) == LLM_USAGE_CONSTRAINTS


def test_llm_usage_has_no_indexes(llm_usage_schema: Engine) -> None:
    with llm_usage_schema.connect() as connection:
        indexes = named_indexes(connection)

    assert indexes.get(LLM_USAGE_TABLE, set()) == set()


def test_llm_usage_public_has_no_grants(llm_usage_schema: Engine) -> None:
    with llm_usage_schema.connect() as connection:
        counts = public_grant_counts(connection)

    assert counts.get(LLM_USAGE_TABLE, 0) == 0


def test_llm_usage_api_and_worker_grants(llm_usage_schema: Engine) -> None:
    high_risk = {"DELETE", "TRUNCATE", "REFERENCES", "TRIGGER", "UPDATE"}
    with llm_usage_schema.connect() as connection:
        api_grants = role_grants(connection, LLM_USAGE_TABLE, API_ROLE)
        worker_grants = role_grants(connection, LLM_USAGE_TABLE, WORKER_ROLE)

    assert api_grants == {"SELECT", "INSERT"}
    assert api_grants.isdisjoint(high_risk)
    assert worker_grants == set()


def test_llm_usage_forbidden_privileges_are_absent(llm_usage_schema: Engine) -> None:
    with llm_usage_schema.connect() as connection:
        assert forbidden_privilege_count(connection) == 0


def test_api_role_appends_success_and_failure_facts(
    llm_usage_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    success_id = uuid.uuid4()
    failure_id = uuid.uuid4()
    engine = create_engine(role_test_databases.api_url, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(INSERT_SUCCESS_SQL, {"id": success_id})
            connection.execute(INSERT_FAILURE_SQL, {"id": failure_id})
        with engine.connect() as connection:
            rows = {
                str(row[0]): (str(row[1]), str(row[2]), row[3], row[4])
                for row in connection.execute(
                    text(
                        "SELECT id, status, usage_source, prompt_tokens, completion_tokens "
                        "FROM llm_usage WHERE id IN (:success_id, :failure_id)"
                    ),
                    {"success_id": success_id, "failure_id": failure_id},
                )
            }
    finally:
        engine.dispose()

    assert rows[str(success_id)] == ("SUCCEEDED", "PROVIDER_REPORTED", 9, 1)
    assert rows[str(failure_id)] == ("TIMEOUT", "UNKNOWN", None, None)


def test_success_row_requires_provider_reported_usage(
    llm_usage_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    engine = create_engine(role_test_databases.api_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO llm_usage (id, provider, model, stage, status, "
                        "usage_source, attempt) VALUES "
                        "(:id, 'deepseek', 'deepseek-flash', 'llm_probe', 'SUCCEEDED', "
                        "'UNKNOWN', 1)"
                    ),
                    {"id": uuid.uuid4()},
                )
            connection.rollback()
    finally:
        engine.dispose()


def test_failure_row_requires_an_error_code(
    llm_usage_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    engine = create_engine(role_test_databases.api_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO llm_usage (id, provider, model, stage, status, "
                        "usage_source, attempt) VALUES "
                        "(:id, 'deepseek', 'deepseek-flash', 'llm_probe', 'FAILED', "
                        "'UNKNOWN', 1)"
                    ),
                    {"id": uuid.uuid4()},
                )
            connection.rollback()
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO llm_usage (id, provider, model, stage, status, error_code, "
        "usage_source, attempt) VALUES ('00000000-0000-0000-0000-000000000001', "
        "'deepseek', 'deepseek-flash', 'llm_probe', 'FAILED', 'X', 'UNKNOWN', 0)",
        "INSERT INTO llm_usage (id, provider, model, stage, status, usage_source, "
        "attempt, prompt_tokens) VALUES ('00000000-0000-0000-0000-000000000002', "
        "'deepseek', 'deepseek-flash', 'llm_probe', 'FAILED', 'UNKNOWN', 1, -1)",
        "INSERT INTO llm_usage (id, provider, model, stage, status, error_code, "
        "usage_source, attempt, price_source) VALUES "
        "('00000000-0000-0000-0000-000000000003', 'deepseek', 'deepseek-flash', "
        "'llm_probe', 'FAILED', 'X', 'UNKNOWN', 1, 'peak-2026-09')",
    ],
    ids=["attempt-zero", "negative-tokens", "half-price-snapshot"],
)
def test_llm_usage_checks_reject_invalid_rows(
    llm_usage_schema: Engine, role_test_databases: RoleTestDatabases, statement: str
) -> None:
    engine = create_engine(role_test_databases.api_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            with pytest.raises(IntegrityError):
                connection.execute(text(statement))
            connection.rollback()
    finally:
        engine.dispose()


def test_llm_usage_is_append_only_for_runtime_roles(
    llm_usage_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    engine = create_engine(role_test_databases.api_url, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(INSERT_FAILURE_SQL, {"id": uuid.uuid4()})
    finally:
        engine.dispose()

    for statement in (
        "UPDATE llm_usage SET status = 'SUCCEEDED'",
        "DELETE FROM llm_usage",
        "TRUNCATE llm_usage",
    ):
        assert_statement_denied(role_test_databases.api_url, statement)

    assert_statement_denied(role_test_databases.worker_url, "SELECT id FROM llm_usage")
    assert_statement_denied(
        role_test_databases.worker_url,
        "INSERT INTO llm_usage (id, provider, model, stage, status, error_code, "
        "usage_source, attempt) VALUES ('00000000-0000-0000-0000-000000000004', "
        "'deepseek', 'deepseek-flash', 'llm_probe', 'FAILED', 'X', 'UNKNOWN', 1)",
    )


def test_preflight_accepts_the_api_role_and_required_columns(
    llm_usage_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    ledger = SqlAlchemyUsageLedger(role_test_databases.api_url)
    try:
        # 预检通过即证明 citemind_api 角色、llm_usage 存在、必要列可读且 SELECT+INSERT 可用。
        ledger.preflight()
    finally:
        ledger.close()


def test_preflight_rejects_non_api_roles(role_test_databases: RoleTestDatabases) -> None:
    for database_url in (role_test_databases.worker_url, role_test_databases.migrator_url):
        ledger = SqlAlchemyUsageLedger(database_url)
        try:
            with pytest.raises(LedgerPreflightError, match="citemind_api"):
                ledger.preflight()
        finally:
            ledger.close()


def test_preflight_rejects_an_unmigrated_database(
    llm_usage_schema: Engine,
    destructive_test_database: DestructiveTestDatabase,
    role_test_databases: RoleTestDatabases,
) -> None:
    # 本用例定义在模块最后：临时降级到 20260922_0003 证明预检拒绝未迁移库，再恢复到 head。
    config = alembic_config(destructive_test_database.url)
    ledger = SqlAlchemyUsageLedger(role_test_databases.api_url)
    try:
        command.downgrade(config, SECOND_SLICE_REVISION)
        with pytest.raises(LedgerPreflightError, match="llm_usage"):
            ledger.preflight()
    finally:
        command.upgrade(config, LLM_USAGE_REVISION)
        ledger.close()


def test_probe_records_sanitized_failures_against_real_database(
    llm_usage_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    """真实库 + MockTransport：302 与负数缓存 token 各落一行且退出非零。"""

    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "allow_llm_probe": True,
        "llm_api_key": "sk-integration-test-key",
        "database_url": role_test_databases.api_url,
    }
    probe_settings = Settings(**values)
    ledger = SqlAlchemyUsageLedger(role_test_databases.api_url)
    try:
        redirect_report = run_probe(
            probe_settings,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(302, json=PROVIDER_SUCCESS_BODY)
            ),
            ledger=ledger,
        )
        negative_report = run_probe(
            probe_settings,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=PROVIDER_NEGATIVE_CACHE_BODY)
            ),
            ledger=ledger,
        )
    finally:
        ledger.close()

    assert redirect_report.exit_code == EXIT_PROVIDER_FAILURE
    assert negative_report.exit_code == EXIT_PROVIDER_FAILURE
    assert redirect_report.usage_id is not None
    assert negative_report.usage_id is not None

    engine = create_engine(role_test_databases.api_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            rows = {
                str(row[0]): (str(row[1]), str(row[2]), row[3], row[4], row[5], row[6])
                for row in connection.execute(
                    text(
                        "SELECT id, status, error_code, prompt_tokens, completion_tokens, "
                        "prompt_cache_hit_tokens, prompt_cache_miss_tokens FROM llm_usage "
                        "WHERE id IN (:redirect_id, :negative_id)"
                    ),
                    {
                        "redirect_id": redirect_report.usage_id,
                        "negative_id": negative_report.usage_id,
                    },
                )
            }
    finally:
        engine.dispose()

    assert rows[str(redirect_report.usage_id)] == (
        "FAILED",
        "HTTP_302",
        None,
        None,
        None,
        None,
    )
    assert rows[str(negative_report.usage_id)] == (
        "FAILED",
        "INVALID_USAGE",
        None,
        None,
        None,
        None,
    )
