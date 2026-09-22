"""真实 Redis broker 集成测试的环境守卫。

这些规则只服务于测试，故意放在测试目录而不是产品包。运行 probe 集成测试会在专用
Redis 逻辑库上派发任务，因此要求 DSN 指向回环地址、带密码、显式选定非 0 逻辑库，
并需要显式 opt-in，避免误用运行时 broker 或改动共享 Redis。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

TEST_REDIS_URL_ENV = "CITEMIND_TEST_REDIS_URL"
ALLOW_TEST_REDIS_ENV = "CITEMIND_ALLOW_TEST_REDIS"
ALLOW_TEST_REDIS_VALUE = "1"

REDIS_SCHEMES = frozenset({"redis", "rediss"})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class GuardError(RuntimeError):
    """集成测试环境不满足真实 Redis broker 的前置条件。"""


class MissingTestRedisError(GuardError):
    """未提供测试 broker DSN；这是正常跳过，不是失败。"""


@dataclass(frozen=True)
class RedisBrokerTarget:
    """已验证可用于 broker 集成测试的专用 Redis 逻辑库。"""

    url: str
    host: str
    database: int


def validate_test_redis_url(redis_url: str) -> RedisBrokerTarget:
    """校验测试 broker DSN；不满足规则时抛 GuardError，调用方不会建立连接。"""

    try:
        parsed = urlsplit(redis_url)
        # 访问 port 会触发非法端口的 ValueError；提前解析，避免留给连接阶段。
        _ = parsed.port
    except ValueError as error:
        raise GuardError(f"{TEST_REDIS_URL_ENV} 不是有效的 Redis URL: {error}") from error

    if parsed.scheme not in REDIS_SCHEMES:
        raise GuardError(f"{TEST_REDIS_URL_ENV} 必须使用 redis 或 rediss scheme")

    host = parsed.hostname
    if not host:
        raise GuardError(f"{TEST_REDIS_URL_ENV} 必须包含非空 host")
    if host not in LOOPBACK_HOSTS:
        raise GuardError(f"{TEST_REDIS_URL_ENV} 的 host 必须指向回环地址（127.0.0.1/localhost）")
    if not parsed.password:
        raise GuardError(f"{TEST_REDIS_URL_ENV} 必须包含密码，测试不接受无鉴权 broker")

    database_text = parsed.path.lstrip("/")
    if not database_text.isdigit() or int(database_text) == 0:
        raise GuardError(
            f"{TEST_REDIS_URL_ENV} 必须显式指定非 0 逻辑库（如 ...:56379/15），"
            "避免与运行时使用 db 0 的 broker 冲突"
        )
    return RedisBrokerTarget(url=redis_url, host=host, database=int(database_text))


def resolve_test_redis(environment: Mapping[str, str]) -> RedisBrokerTarget:
    """结合测试 broker DSN 与显式 opt-in 判断是否允许运行 broker 集成测试。

    DSN 规则先于 opt-in 检查，任何不满足规则的输入都会在建立连接之前被拒绝。
    """

    redis_url = environment.get(TEST_REDIS_URL_ENV)
    if not redis_url:
        raise MissingTestRedisError(
            f"未设置 {TEST_REDIS_URL_ENV}，跳过真实 Redis broker 集成测试"
        )

    resolved = validate_test_redis_url(redis_url)

    if environment.get(ALLOW_TEST_REDIS_ENV) != ALLOW_TEST_REDIS_VALUE:
        raise GuardError(
            f"broker 集成测试会在 {resolved.host} db {resolved.database} 上派发并清理任务，"
            f"必须显式设置 {ALLOW_TEST_REDIS_ENV}={ALLOW_TEST_REDIS_VALUE}"
        )

    return resolved
