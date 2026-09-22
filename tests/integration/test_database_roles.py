"""真实 PostgreSQL 角色与 ACL 验收：只读取系统目录，不创建或修改任何对象。

角色、测试库与 ACL 由 `deploy/compose/initdb` 的脚本在空数据卷上建立；
三个角色 DSN 由守卫校验为同一个测试库，本测试不会连接其它数据库。
"""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from database_roles_guard import (
    API_ROLE,
    MIGRATOR_ROLE,
    ROLE_ENV_VARS,
    WORKER_ROLE,
    RoleTestDatabases,
)
from sqlalchemy import URL, Connection, create_engine, make_url, text

pytestmark = pytest.mark.integration

# PUBLIC 在 ACL 中没有角色名，只有 OID 0。
PUBLIC_GRANTEE_OID = 0
TEST_DATABASE_SUFFIX = "_test"
REBUILD_HINT = (
    "initdb 脚本只在空数据卷上执行；重建："
    "docker compose --env-file .env.example -f deploy/compose/compose.yml down -v"
)

DATABASE_PUBLIC_GRANTS = text(
    "SELECT count(*) FROM pg_database d, "
    "LATERAL aclexplode(COALESCE(d.datacl, acldefault('d', d.datdba))) AS a "
    "WHERE d.datname = :name AND a.grantee = :public_oid"
)
SCHEMA_PUBLIC_GRANTS = text(
    "SELECT count(*) FROM pg_namespace n, "
    "LATERAL aclexplode(COALESCE(n.nspacl, acldefault('n', n.nspowner))) AS a "
    "WHERE n.nspname = :name AND a.grantee = :public_oid"
)


@contextmanager
def connect(database_url: str | URL) -> Iterator[Connection]:
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()


def database_public_grant_count(connection: Connection, database_name: str) -> int:
    """统计数据库 ACL 中属于 PUBLIC 的授权条数；ACL 为空时按默认 ACL 计算。"""

    grants = connection.scalar(
        DATABASE_PUBLIC_GRANTS,
        {"name": database_name, "public_oid": PUBLIC_GRANTEE_OID},
    )
    return int(grants)


def schema_public_grant_count(connection: Connection, schema_name: str) -> int:
    """统计 schema ACL 中属于 PUBLIC 的授权条数；ACL 为空时按默认 ACL 计算。"""

    grants = connection.scalar(
        SCHEMA_PUBLIC_GRANTS,
        {"name": schema_name, "public_oid": PUBLIC_GRANTEE_OID},
    )
    return int(grants)


def existing_databases(connection: Connection, names: tuple[str, ...]) -> tuple[str, ...]:
    installed = set(connection.scalars(text("SELECT datname FROM pg_database")))
    return tuple(name for name in names if name in installed)


def database_names_to_verify(databases: RoleTestDatabases) -> tuple[str, ...]:
    """测试库，以及与它同名的应用库（去掉 _test 后缀）。"""

    if databases.database_name.endswith(TEST_DATABASE_SUFFIX):
        application_database = databases.database_name[: -len(TEST_DATABASE_SUFFIX)]
        return (application_database, databases.database_name)
    return (databases.database_name,)


def installed_databases_to_verify(
    connection: Connection, databases: RoleTestDatabases
) -> tuple[str, ...]:
    """要求应用库和测试库同时存在，避免验收范围静默缩小。"""

    expected = database_names_to_verify(databases)
    installed = existing_databases(connection, expected)
    assert installed == expected, f"数据库不完整，期望 {expected}，实际 {installed}；{REBUILD_HINT}"
    return installed


@pytest.fixture(scope="module")
def migrator_connection(role_test_databases: RoleTestDatabases) -> Iterator[Connection]:
    with connect(role_test_databases.migrator_url) as connection:
        current_database = connection.scalar(text("SELECT current_database()"))
        assert current_database == role_test_databases.database_name
        yield connection


def test_roles_exist_in_the_test_database(migrator_connection: Connection) -> None:
    existing = set(migrator_connection.scalars(text("SELECT rolname FROM pg_roles")))
    missing = sorted(set(ROLE_ENV_VARS) - existing)
    if missing:
        pytest.fail(f"测试库缺少角色 {missing}；{REBUILD_HINT}", pytrace=False)


def test_migrator_owns_the_database_and_can_install_extensions(
    role_test_databases: RoleTestDatabases, migrator_connection: Connection
) -> None:
    """vector.control 没有 trusted=true，迁移账号必须是库所有者兼超级用户。"""

    owner = migrator_connection.scalar(
        text("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = :name"),
        {"name": role_test_databases.database_name},
    )
    can_create = migrator_connection.scalar(
        text("SELECT has_database_privilege(:role, :name, 'CREATE')"),
        {"role": MIGRATOR_ROLE, "name": role_test_databases.database_name},
    )
    is_superuser = migrator_connection.scalar(
        text("SELECT rolsuper FROM pg_roles WHERE rolname = :role"),
        {"role": MIGRATOR_ROLE},
    )

    assert owner == MIGRATOR_ROLE
    assert can_create
    assert is_superuser


@pytest.mark.parametrize("role", [API_ROLE, WORKER_ROLE])
def test_runtime_roles_are_not_privileged(role: str, migrator_connection: Connection) -> None:
    privileges = migrator_connection.execute(
        text(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
            "FROM pg_roles WHERE rolname = :role"
        ),
        {"role": role},
    ).one()

    assert tuple(privileges) == (False, False, False, False, False)


@pytest.mark.parametrize("role", [API_ROLE, WORKER_ROLE])
def test_runtime_roles_can_connect_but_not_create(
    role: str, role_test_databases: RoleTestDatabases, migrator_connection: Connection
) -> None:
    for database_name in installed_databases_to_verify(
        migrator_connection, role_test_databases
    ):
        can_connect = migrator_connection.scalar(
            text("SELECT has_database_privilege(:role, :name, 'CONNECT')"),
            {"role": role, "name": database_name},
        )
        can_create = migrator_connection.scalar(
            text("SELECT has_database_privilege(:role, :name, 'CREATE')"),
            {"role": role, "name": database_name},
        )
        can_create_temp = migrator_connection.scalar(
            text("SELECT has_database_privilege(:role, :name, 'TEMP')"),
            {"role": role, "name": database_name},
        )

        assert can_connect, f"{role} 应能连接 {database_name}"
        assert not can_create, f"{role} 不应有 {database_name} 的 CREATE"
        assert not can_create_temp, f"{role} 不应有 {database_name} 的 TEMP"

    with connect(role_test_databases.urls[role]) as runtime_connection:
        assert runtime_connection.scalar(text("SELECT current_user")) == role
        assert runtime_connection.scalar(
            text("SELECT has_schema_privilege(current_user, 'public', 'USAGE')")
        )


def test_public_database_privileges_are_revoked(
    role_test_databases: RoleTestDatabases, migrator_connection: Connection
) -> None:
    installed = installed_databases_to_verify(migrator_connection, role_test_databases)

    for database_name in installed:
        grants = database_public_grant_count(migrator_connection, database_name)
        assert grants == 0, f"{database_name} 仍给 PUBLIC 授权；{REBUILD_HINT}"


def test_public_schema_is_restricted_to_usage(
    role_test_databases: RoleTestDatabases, migrator_connection: Connection
) -> None:
    installed = installed_databases_to_verify(migrator_connection, role_test_databases)

    for database_name in installed:
        database_url = make_url(role_test_databases.migrator_url).set(database=database_name)
        with connect(database_url) as connection:
            grants = schema_public_grant_count(connection, "public")
            assert grants == 0, (
                f"{database_name} 的 public schema 仍给 PUBLIC 授权；{REBUILD_HINT}"
            )
            for role in (API_ROLE, WORKER_ROLE):
                can_use = connection.scalar(
                    text("SELECT has_schema_privilege(:role, 'public', 'USAGE')"),
                    {"role": role},
                )
                can_create = connection.scalar(
                    text("SELECT has_schema_privilege(:role, 'public', 'CREATE')"),
                    {"role": role},
                )

                assert can_use, f"{role} 应能使用 {database_name} 的 public schema"
                assert not can_create, f"{role} 不应在 {database_name} 的 public schema 建对象"
