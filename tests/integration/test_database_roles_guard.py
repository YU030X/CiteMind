"""角色 DSN 守卫的纯逻辑测试：不连接数据库，因此在任何环境都应运行。"""

import conftest
import pytest
from database_roles_guard import (
    API_DATABASE_URL_ENV,
    API_ROLE,
    MIGRATOR_DATABASE_URL_ENV,
    MIGRATOR_ROLE,
    ROLE_ENV_VARS,
    WORKER_DATABASE_URL_ENV,
    WORKER_ROLE,
    GuardError,
    MissingTestDatabasesError,
    resolve_role_test_databases,
    validate_role_database_url,
)

TEST_HOST = "127.0.0.1"
DEFAULT_TEST_PORT = "55432"
TEST_DATABASE = "citemind_test"

MISSING_API_AND_WORKER = [API_DATABASE_URL_ENV, WORKER_DATABASE_URL_ENV]
MISSING_WORKER = [WORKER_DATABASE_URL_ENV]
MISSING_MIGRATOR = [MIGRATOR_DATABASE_URL_ENV]


def role_url(
    role: str,
    *,
    host: str = TEST_HOST,
    port: str | None = DEFAULT_TEST_PORT,
    database: str = TEST_DATABASE,
) -> str:
    authority = host if port is None else f"{host}:{port}"
    return f"postgresql+psycopg://{role}:citemind@{authority}/{database}"


MIGRATOR_URL = role_url(MIGRATOR_ROLE)
API_URL = role_url(API_ROLE)
WORKER_URL = role_url(WORKER_ROLE)


def configured(**overrides: str) -> dict[str, str]:
    environment = {
        MIGRATOR_DATABASE_URL_ENV: MIGRATOR_URL,
        API_DATABASE_URL_ENV: API_URL,
        WORKER_DATABASE_URL_ENV: WORKER_URL,
    }
    environment.update(overrides)
    return environment


def test_missing_all_three_dsns_is_a_skip_not_a_failure() -> None:
    with pytest.raises(MissingTestDatabasesError, match=MIGRATOR_DATABASE_URL_ENV):
        resolve_role_test_databases({})


def test_missing_error_is_a_guard_error() -> None:
    assert issubclass(MissingTestDatabasesError, GuardError)


@pytest.mark.parametrize(
    ("provided_env", "missing_env"),
    [
        ({MIGRATOR_DATABASE_URL_ENV: MIGRATOR_URL}, MISSING_API_AND_WORKER),
        ({MIGRATOR_DATABASE_URL_ENV: MIGRATOR_URL, API_DATABASE_URL_ENV: API_URL}, MISSING_WORKER),
        ({API_DATABASE_URL_ENV: API_URL, WORKER_DATABASE_URL_ENV: WORKER_URL}, MISSING_MIGRATOR),
        (
            {MIGRATOR_DATABASE_URL_ENV: MIGRATOR_URL, API_DATABASE_URL_ENV: ""},
            MISSING_API_AND_WORKER,
        ),
    ],
    ids=["only-migrator", "missing-worker", "missing-migrator", "empty-api-value"],
)
def test_partial_configuration_fails_instead_of_connecting(
    provided_env: dict[str, str], missing_env: list[str]
) -> None:
    with pytest.raises(GuardError) as error:
        resolve_role_test_databases(provided_env)

    assert not isinstance(error.value, MissingTestDatabasesError)
    for env_var in missing_env:
        assert env_var in str(error.value)


def test_resolves_three_matching_dsns() -> None:
    resolved = resolve_role_test_databases(configured())

    assert resolved.host == TEST_HOST
    assert resolved.port == int(DEFAULT_TEST_PORT)
    assert resolved.database_name == TEST_DATABASE
    assert resolved.urls == {
        MIGRATOR_ROLE: MIGRATOR_URL,
        API_ROLE: API_URL,
        WORKER_ROLE: WORKER_URL,
    }
    assert resolved.runtime_roles == (API_ROLE, WORKER_ROLE)


def test_port_defaults_to_postgres_default_when_omitted() -> None:
    resolved = resolve_role_test_databases(
        {
            MIGRATOR_DATABASE_URL_ENV: role_url(MIGRATOR_ROLE, port=None),
            API_DATABASE_URL_ENV: role_url(API_ROLE, port=None),
            WORKER_DATABASE_URL_ENV: role_url(WORKER_ROLE, port=None),
        }
    )

    assert resolved.port == 5432


@pytest.mark.parametrize(
    "database_url",
    [
        f"sqlite+aiosqlite:///./{TEST_DATABASE}.db",
        role_url(MIGRATOR_ROLE).replace("postgresql+psycopg", "postgresql+asyncpg"),
        role_url(MIGRATOR_ROLE).replace("postgresql+psycopg", "postgresql"),
    ],
    ids=["sqlite", "asyncpg", "default-driver"],
)
def test_rejects_non_psycopg_driver(database_url: str) -> None:
    with pytest.raises(GuardError, match=r"postgresql\+psycopg"):
        validate_role_database_url(MIGRATOR_ROLE, database_url)


@pytest.mark.parametrize(
    "database_url",
    [
        role_url(API_ROLE),
        role_url(WORKER_ROLE),
        role_url("postgres"),
    ],
    ids=["api-role", "worker-role", "default-superuser"],
)
def test_rejects_wrong_username_for_role(database_url: str) -> None:
    with pytest.raises(GuardError, match=f"用户名必须是 {MIGRATOR_ROLE}"):
        validate_role_database_url(MIGRATOR_ROLE, database_url)


def test_rejects_database_name_without_test_suffix() -> None:
    with pytest.raises(GuardError, match="_test"):
        validate_role_database_url(MIGRATOR_ROLE, role_url(MIGRATOR_ROLE, database="citemind"))


def test_rejects_unparseable_database_url() -> None:
    with pytest.raises(GuardError, match="不是有效的数据库 URL"):
        validate_role_database_url(MIGRATOR_ROLE, "not a url")


def test_rejects_empty_port_instead_of_leaking_value_error() -> None:
    """`host:/db` 让 make_url 抛 ValueError；守卫必须转成 GuardError。"""

    with pytest.raises(GuardError, match="不是有效的数据库 URL"):
        validate_role_database_url(MIGRATOR_ROLE, role_url(MIGRATOR_ROLE, port=""))


def test_rejects_missing_host() -> None:
    with pytest.raises(GuardError, match="非空 host"):
        validate_role_database_url(MIGRATOR_ROLE, f"postgresql+psycopg:///{TEST_DATABASE}")


def test_rejects_missing_database() -> None:
    with pytest.raises(GuardError, match="非空 database"):
        validate_role_database_url(MIGRATOR_ROLE, role_url(MIGRATOR_ROLE, database=""))


@pytest.mark.parametrize(
    "override",
    [
        {API_DATABASE_URL_ENV: role_url(API_ROLE, host="postgres")},
        {API_DATABASE_URL_ENV: role_url(API_ROLE, port="5432")},
        {API_DATABASE_URL_ENV: role_url(API_ROLE, database="other_test")},
        {WORKER_DATABASE_URL_ENV: role_url(WORKER_ROLE, port=None)},
    ],
    ids=["host", "port", "database", "port-omitted"],
)
def test_rejects_dsns_that_do_not_share_one_test_database(override: dict[str, str]) -> None:
    with pytest.raises(GuardError, match="同一个 host、port 和 database"):
        resolve_role_test_databases(configured(**override))


def test_role_env_var_mapping_covers_exactly_three_roles() -> None:
    assert ROLE_ENV_VARS == {
        MIGRATOR_ROLE: MIGRATOR_DATABASE_URL_ENV,
        API_ROLE: API_DATABASE_URL_ENV,
        WORKER_ROLE: WORKER_DATABASE_URL_ENV,
    }


def test_role_fixture_skips_when_all_three_dsns_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for env_var in ROLE_ENV_VARS.values():
        monkeypatch.delenv(env_var, raising=False)

    with pytest.raises(pytest.skip.Exception, match=MIGRATOR_DATABASE_URL_ENV):
        conftest.resolve_role_test_databases_or_skip_or_fail()


def test_role_fixture_fails_on_partial_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MIGRATOR_DATABASE_URL_ENV, MIGRATOR_URL)
    monkeypatch.setenv(API_DATABASE_URL_ENV, API_URL)
    monkeypatch.delenv(WORKER_DATABASE_URL_ENV, raising=False)

    with pytest.raises(pytest.fail.Exception, match=WORKER_DATABASE_URL_ENV):
        conftest.resolve_role_test_databases_or_skip_or_fail()


def test_role_fixture_returns_resolved_databases(monkeypatch: pytest.MonkeyPatch) -> None:
    for env_var, value in configured().items():
        monkeypatch.setenv(env_var, value)

    resolved = conftest.resolve_role_test_databases_or_skip_or_fail()

    assert resolved.database_name == TEST_DATABASE
