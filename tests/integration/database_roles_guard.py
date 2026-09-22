"""真实 PostgreSQL 角色权限测试的 DSN 守卫。

这些规则只服务于测试，故意放在测试目录而不是产品包，避免测试约定进入运行时路径。
三个 DSN 必须同时提供、指向同一个测试库，并使用 compose initdb 脚本固定的角色名。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

MIGRATOR_DATABASE_URL_ENV = "CITEMIND_TEST_MIGRATOR_DATABASE_URL"
API_DATABASE_URL_ENV = "CITEMIND_TEST_API_DATABASE_URL"
WORKER_DATABASE_URL_ENV = "CITEMIND_TEST_WORKER_DATABASE_URL"

MIGRATOR_ROLE = "citemind_migrator"
API_ROLE = "citemind_api"
WORKER_ROLE = "citemind_worker"

# 角色名到 DSN 环境变量的固定映射；用户名必须与键一致。
ROLE_ENV_VARS: Mapping[str, str] = {
    MIGRATOR_ROLE: MIGRATOR_DATABASE_URL_ENV,
    API_ROLE: API_DATABASE_URL_ENV,
    WORKER_ROLE: WORKER_DATABASE_URL_ENV,
}

TEST_DATABASE_SUFFIX = "_test"
DEFAULT_POSTGRES_PORT = 5432


class GuardError(RuntimeError):
    """角色权限测试环境不满足前置条件。"""


class MissingTestDatabasesError(GuardError):
    """三个测试 DSN 都未提供；这是正常跳过，不是失败。"""


@dataclass(frozen=True)
class RoleTestDatabases:
    """已验证的三个角色 DSN；它们指向同一个测试库。"""

    migrator_url: str
    api_url: str
    worker_url: str
    host: str
    port: int
    database_name: str

    @property
    def urls(self) -> dict[str, str]:
        return {
            MIGRATOR_ROLE: self.migrator_url,
            API_ROLE: self.api_url,
            WORKER_ROLE: self.worker_url,
        }

    @property
    def runtime_roles(self) -> tuple[str, ...]:
        return (API_ROLE, WORKER_ROLE)


def validate_role_database_url(role: str, database_url: str) -> URL:
    """校验单个角色 DSN；不满足规则时抛 GuardError，调用方不会建立连接。"""

    env_var = ROLE_ENV_VARS[role]
    try:
        url = make_url(database_url)
    except (ArgumentError, ValueError) as error:
        # 非法端口（如 host:/db）由 int() 抛 ValueError，不是 ArgumentError。
        raise GuardError(f"{env_var} 不是有效的数据库 URL: {error}") from error

    if url.drivername != "postgresql+psycopg":
        raise GuardError(f"{env_var} 必须使用 postgresql+psycopg 驱动")
    if not url.host:
        raise GuardError(f"{env_var} 必须包含非空 host")
    if not url.database:
        raise GuardError(f"{env_var} 必须包含非空 database")
    if not url.database.endswith(TEST_DATABASE_SUFFIX):
        raise GuardError(f"{env_var} 的数据库名必须以 {TEST_DATABASE_SUFFIX} 结尾")
    if url.username != role:
        raise GuardError(f"{env_var} 的用户名必须是 {role}")
    return url


def resolve_role_test_databases(environment: Mapping[str, str]) -> RoleTestDatabases:
    """结合三个角色 DSN 判断是否允许运行角色权限测试。

    三个都缺失时返回跳过信号；部分缺失或任意 DSN 不符合规则都在连接前失败。
    """

    configured = {role: environment.get(env_var) for role, env_var in ROLE_ENV_VARS.items()}
    provided = {role: value for role, value in configured.items() if value}
    if not provided:
        missing_all = "、".join(ROLE_ENV_VARS.values())
        raise MissingTestDatabasesError(f"未设置 {missing_all}，跳过角色权限集成测试")

    still_missing = sorted(
        env_var for role, env_var in ROLE_ENV_VARS.items() if role not in provided
    )
    if still_missing:
        raise GuardError(
            "角色权限测试必须同时提供三个角色 DSN，缺少：" + "、".join(still_missing)
        )

    parsed = {role: validate_role_database_url(role, value) for role, value in provided.items()}
    expected = parsed[MIGRATOR_ROLE]
    expected_host = cast(str, expected.host)
    expected_database = cast(str, expected.database)
    expected_port = expected.port or DEFAULT_POSTGRES_PORT

    for role, url in parsed.items():
        if (url.host, url.port or DEFAULT_POSTGRES_PORT, url.database) != (
            expected_host,
            expected_port,
            expected_database,
        ):
            raise GuardError(
                f"{ROLE_ENV_VARS[role]} 必须与 {MIGRATOR_DATABASE_URL_ENV} "
                "指向同一个 host、port 和 database"
            )

    return RoleTestDatabases(
        migrator_url=provided[MIGRATOR_ROLE],
        api_url=provided[API_ROLE],
        worker_url=provided[WORKER_ROLE],
        host=expected_host,
        port=expected_port,
        database_name=expected_database,
    )
