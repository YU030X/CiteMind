"""真实 PostgreSQL 集成测试的破坏性操作守卫。

这些规则只服务于测试，故意放在测试目录而不是产品包，避免破坏性测试约定进入运行时路径。
"""

from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

TEST_DATABASE_URL_ENV = "CITEMIND_TEST_DATABASE_URL"
ALLOW_DESTRUCTIVE_TEST_DB_ENV = "CITEMIND_ALLOW_DESTRUCTIVE_TEST_DB"
ALLOW_DESTRUCTIVE_TEST_DB_VALUE = "1"


class GuardError(RuntimeError):
    """集成测试环境不满足破坏性操作的前置条件。"""


class MissingTestDatabaseError(GuardError):
    """未提供测试数据库 DSN；这是正常跳过，不是失败。"""


@dataclass(frozen=True)
class DestructiveTestDatabase:
    """已验证可用于破坏性迁移测试的专用测试库。"""

    url: str
    database_name: str


def validate_test_database_url(database_url: str) -> str:
    """校验测试 DSN 并返回其数据库名；不满足规则时抛 GuardError。"""

    try:
        url = make_url(database_url)
    except ArgumentError as error:
        raise GuardError(f"{TEST_DATABASE_URL_ENV} 不是有效的数据库 URL: {error}") from error

    if url.drivername != "postgresql+psycopg":
        raise GuardError(f"{TEST_DATABASE_URL_ENV} 必须使用 postgresql+psycopg 驱动")

    database_name = url.database
    if not database_name or not database_name.endswith("_test"):
        raise GuardError(f"{TEST_DATABASE_URL_ENV} 的数据库名必须以 _test 结尾")
    return database_name


def resolve_destructive_test_database(environment: Mapping[str, str]) -> DestructiveTestDatabase:
    """结合测试 DSN 与显式 opt-in 判断是否允许运行破坏性测试。

    DSN 规则先于 opt-in 检查，任何不满足规则的输入都会在连接数据库之前被拒绝。
    """

    database_url = environment.get(TEST_DATABASE_URL_ENV)
    if not database_url:
        raise MissingTestDatabaseError(
            f"未设置 {TEST_DATABASE_URL_ENV}，跳过真实 PostgreSQL 集成测试"
        )

    database_name = validate_test_database_url(database_url)

    if environment.get(ALLOW_DESTRUCTIVE_TEST_DB_ENV) != ALLOW_DESTRUCTIVE_TEST_DB_VALUE:
        raise GuardError(
            f"集成测试会 upgrade 并 downgrade {database_name}，必须显式设置 "
            f"{ALLOW_DESTRUCTIVE_TEST_DB_ENV}={ALLOW_DESTRUCTIVE_TEST_DB_VALUE}"
        )

    return DestructiveTestDatabase(url=database_url, database_name=database_name)
