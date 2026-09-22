"""broker 守卫的纯逻辑测试：不连接 Redis，因此在任何环境都应运行。"""

import pytest
from broker_guard import (
    ALLOW_TEST_REDIS_ENV,
    TEST_REDIS_URL_ENV,
    GuardError,
    MissingTestRedisError,
    resolve_test_redis,
    validate_test_redis_url,
)

VALID_TEST_REDIS_URL = "redis://:citemind@127.0.0.1:56379/15"


def opted_in(**overrides: str) -> dict[str, str]:
    environment = {
        TEST_REDIS_URL_ENV: VALID_TEST_REDIS_URL,
        ALLOW_TEST_REDIS_ENV: "1",
    }
    environment.update(overrides)
    return environment


def test_missing_test_redis_url_is_a_skip_not_a_failure() -> None:
    with pytest.raises(MissingTestRedisError):
        resolve_test_redis({})


def test_missing_test_redis_error_is_a_guard_error() -> None:
    assert issubclass(MissingTestRedisError, GuardError)


def test_missing_test_redis_url_also_requires_opt_in() -> None:
    with pytest.raises(MissingTestRedisError, match=TEST_REDIS_URL_ENV):
        resolve_test_redis({ALLOW_TEST_REDIS_ENV: "1"})


@pytest.mark.parametrize(
    "opt_in_value",
    ["", "0", "true", "yes", "TRUE"],
    ids=["empty", "zero", "true", "yes", "upper"],
)
def test_opt_in_must_be_exactly_one(opt_in_value: str) -> None:
    with pytest.raises(GuardError, match=f"{ALLOW_TEST_REDIS_ENV}=1"):
        resolve_test_redis(opted_in(**{ALLOW_TEST_REDIS_ENV: opt_in_value}))


@pytest.mark.parametrize(
    "redis_url",
    [
        "http://:citemind@127.0.0.1:56379/15",
        "redis+unix:///tmp/redis.sock",
        "postgresql://citemind:citemind@127.0.0.1:56379/15",
    ],
    ids=["http", "unix-socket", "postgresql"],
)
def test_rejects_non_redis_scheme(redis_url: str) -> None:
    with pytest.raises(GuardError, match="redis 或 rediss scheme"):
        resolve_test_redis(opted_in(**{TEST_REDIS_URL_ENV: redis_url}))


@pytest.mark.parametrize(
    "redis_url",
    [
        "redis://:citemind@192.168.1.10:56379/15",
        "redis://:citemind@redis:6379/15",
    ],
    ids=["private-network", "compose-service"],
)
def test_rejects_non_loopback_host(redis_url: str) -> None:
    with pytest.raises(GuardError, match="回环地址"):
        resolve_test_redis(opted_in(**{TEST_REDIS_URL_ENV: redis_url}))


def test_accepts_loopback_and_ipv6_loopback_hosts() -> None:
    ipv4 = "redis://:citemind@localhost:56379/15"
    ipv6 = "redis://:citemind@[::1]:56379/15"

    assert resolve_test_redis(opted_in(**{TEST_REDIS_URL_ENV: ipv4})).host == "localhost"
    assert resolve_test_redis(opted_in(**{TEST_REDIS_URL_ENV: ipv6})).host == "::1"


def test_rejects_broker_without_password() -> None:
    with pytest.raises(GuardError, match="必须包含密码"):
        resolve_test_redis(opted_in(**{TEST_REDIS_URL_ENV: "redis://127.0.0.1:56379/15"}))


def test_rejects_empty_host() -> None:
    with pytest.raises(GuardError, match="非空 host"):
        resolve_test_redis(opted_in(**{TEST_REDIS_URL_ENV: "redis://:citemind@:56379/15"}))


@pytest.mark.parametrize(
    "redis_url",
    [
        "redis://:citemind@127.0.0.1:56379",
        "redis://:citemind@127.0.0.1:56379/0",
    ],
    ids=["missing-db", "default-db"],
)
def test_rejects_missing_or_zero_logical_database(redis_url: str) -> None:
    with pytest.raises(GuardError, match="非 0 逻辑库"):
        resolve_test_redis(opted_in(**{TEST_REDIS_URL_ENV: redis_url}))


def test_rejects_invalid_port_before_connecting() -> None:
    invalid_port = "redis://:citemind@127.0.0.1:notaport/15"

    with pytest.raises(GuardError, match="不是有效的 Redis URL"):
        resolve_test_redis(opted_in(**{TEST_REDIS_URL_ENV: invalid_port}))


def test_url_rules_are_checked_before_opt_in() -> None:
    """即使没有 opt-in，非法 DSN 也应先被拒绝，不留任何连接机会。"""

    with pytest.raises(GuardError, match="非 0 逻辑库"):
        resolve_test_redis({TEST_REDIS_URL_ENV: "redis://:citemind@127.0.0.1:56379/0"})


def test_returns_logical_database_for_opted_in_loopback_broker() -> None:
    resolved = resolve_test_redis(opted_in())

    assert resolved.url == VALID_TEST_REDIS_URL
    assert resolved.host == "127.0.0.1"
    assert resolved.database == 15


def test_validate_test_redis_url_returns_database_without_opt_in() -> None:
    assert validate_test_redis_url(VALID_TEST_REDIS_URL).database == 15
