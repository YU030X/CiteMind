"""破坏性守卫的纯逻辑测试：不连接数据库，因此在任何环境都应运行。"""

import pytest
from database_guard import (
    ALLOW_DESTRUCTIVE_TEST_DB_ENV,
    TEST_DATABASE_URL_ENV,
    GuardError,
    MissingTestDatabaseError,
    resolve_destructive_test_database,
    validate_test_database_url,
)

VALID_TEST_DATABASE_URL = "postgresql+psycopg://citemind:citemind@localhost:5432/citemind_test"


def opted_in(**overrides: str) -> dict[str, str]:
    environment = {
        TEST_DATABASE_URL_ENV: VALID_TEST_DATABASE_URL,
        ALLOW_DESTRUCTIVE_TEST_DB_ENV: "1",
    }
    environment.update(overrides)
    return environment


def test_missing_test_database_url_is_a_skip_not_a_failure() -> None:
    with pytest.raises(MissingTestDatabaseError):
        resolve_destructive_test_database({})


def test_missing_test_database_error_is_a_guard_error() -> None:
    assert issubclass(MissingTestDatabaseError, GuardError)


def test_missing_test_database_url_also_requires_opt_in() -> None:
    with pytest.raises(MissingTestDatabaseError, match=TEST_DATABASE_URL_ENV):
        resolve_destructive_test_database({ALLOW_DESTRUCTIVE_TEST_DB_ENV: "1"})


@pytest.mark.parametrize(
    "opt_in_value",
    ["", "0", "true", "yes", "TRUE"],
    ids=["empty", "zero", "true", "yes", "upper"],
)
def test_opt_in_must_be_exactly_one(opt_in_value: str) -> None:
    with pytest.raises(GuardError, match=f"{ALLOW_DESTRUCTIVE_TEST_DB_ENV}=1"):
        resolve_destructive_test_database(opted_in(**{ALLOW_DESTRUCTIVE_TEST_DB_ENV: opt_in_value}))


@pytest.mark.parametrize(
    "database_url",
    [
        "sqlite+aiosqlite:///./citemind_test.db",
        "postgresql+asyncpg://citemind:citemind@localhost:5432/citemind_test",
        "postgresql://citemind:citemind@localhost:5432/citemind_test",
    ],
    ids=["sqlite", "asyncpg", "default-driver"],
)
def test_rejects_non_psycopg_driver(database_url: str) -> None:
    with pytest.raises(GuardError, match=r"postgresql\+psycopg"):
        resolve_destructive_test_database(opted_in(**{TEST_DATABASE_URL_ENV: database_url}))


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+psycopg://citemind:citemind@localhost:5432/",
        "postgresql+psycopg://citemind:citemind@localhost:5432/citemind",
        "postgresql+psycopg://citemind:citemind@localhost:5432/citemind_test_backup",
    ],
    ids=["missing-name", "missing-suffix", "suffix-not-at-end"],
)
def test_rejects_database_name_that_is_not_a_test_database(database_url: str) -> None:
    with pytest.raises(GuardError, match="_test"):
        resolve_destructive_test_database(opted_in(**{TEST_DATABASE_URL_ENV: database_url}))


def test_rejects_unparseable_database_url() -> None:
    with pytest.raises(GuardError, match="不是有效的数据库 URL"):
        resolve_destructive_test_database(opted_in(**{TEST_DATABASE_URL_ENV: "not a url"}))


def test_url_rules_are_checked_before_opt_in() -> None:
    """即使没有 opt-in，非法 DSN 也应先被拒绝，不留任何连接机会。"""

    with pytest.raises(GuardError, match="_test"):
        resolve_destructive_test_database(
            {TEST_DATABASE_URL_ENV: "postgresql+psycopg://citemind:citemind@localhost:5432/citemind"}
        )


def test_returns_database_name_for_opted_in_test_database() -> None:
    resolved = resolve_destructive_test_database(opted_in())

    assert resolved.url == VALID_TEST_DATABASE_URL
    assert resolved.database_name == "citemind_test"


def test_validate_test_database_url_returns_name_without_opt_in() -> None:
    assert validate_test_database_url(VALID_TEST_DATABASE_URL) == "citemind_test"
