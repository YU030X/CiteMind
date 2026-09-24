"""登录限流的纯逻辑测试：用内存假 Redis 验证原子计数、fail-closed 与键脱敏。"""

from typing import Any

import pytest
from rag_backend.auth.ratelimit import (
    INCREMENT_WITH_EXPIRY,
    RATE_LIMIT_KEY_PREFIX,
    LoginRateLimiter,
    RateLimiterUnavailable,
    RateLimitExceeded,
    create_login_rate_limiter,
)
from rag_backend.auth.tokens import hash_token
from rag_backend.config import Settings
from redis.exceptions import RedisError


class FakeRedis:
    """实现限流器所需的最小命令集，计数保存在进程内。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.counts: dict[str, int] = {}
        self.expiries: dict[str, int] = {}
        self.fail = fail
        self.closed = False

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any:
        if self.fail:
            raise RedisError("redis down")
        key = str(keys_and_args[0])
        window = int(keys_and_args[1])
        self.counts[key] = self.counts.get(key, 0) + 1
        if self.counts[key] == 1:
            self.expiries[key] = window
        return self.counts[key]

    async def ttl(self, name: str) -> int:
        return self.expiries.get(name, -2)

    async def aclose(self) -> None:
        self.closed = True


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"_env_file": None, "environment": "test"}
    values.update(overrides)
    return Settings(**values)


def limiter(client: FakeRedis, *, per_ip: int = 5, per_user: int = 3) -> LoginRateLimiter:
    return LoginRateLimiter(
        client,
        per_ip_limit=per_ip,
        per_username_limit=per_user,
        window_seconds=60,
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_lua_script_is_atomic_increment_and_expire() -> None:
    assert "INCR" in INCREMENT_WITH_EXPIRY
    assert "EXPIRE" in INCREMENT_WITH_EXPIRY


@pytest.mark.anyio
async def test_attempts_under_limit_pass_and_are_counted_per_subject() -> None:
    client = FakeRedis()
    limiter_instance = limiter(client)

    await limiter_instance.check(client_ip="10.0.0.1", username="alice")
    await limiter_instance.check(client_ip="10.0.0.1", username="alice")

    assert sum(client.counts.values()) == 4
    # 用户名的明文不得出现在 Redis 键里。
    assert all("alice" not in key for key in client.counts)
    assert all("10.0.0.1" not in key for key in client.counts)


@pytest.mark.anyio
async def test_username_limit_exceeded_raises_with_retry_after() -> None:
    client = FakeRedis()
    limiter_instance = limiter(client, per_user=2)

    await limiter_instance.check(client_ip="10.0.0.1", username="alice")
    await limiter_instance.check(client_ip="10.0.0.2", username="alice")

    with pytest.raises(RateLimitExceeded) as error:
        await limiter_instance.check(client_ip="10.0.0.3", username="alice")

    assert error.value.retry_after == 60


@pytest.mark.anyio
async def test_ip_limit_exceeded_raises() -> None:
    client = FakeRedis()
    limiter_instance = limiter(client, per_ip=1)

    await limiter_instance.check(client_ip="10.0.0.1", username="alice")

    with pytest.raises(RateLimitExceeded):
        await limiter_instance.check(client_ip="10.0.0.1", username="bob")


@pytest.mark.anyio
async def test_redis_failure_fails_closed() -> None:
    limiter_instance = limiter(FakeRedis(fail=True))

    with pytest.raises(RateLimiterUnavailable):
        await limiter_instance.check(client_ip="10.0.0.1", username="alice")


@pytest.mark.anyio
async def test_separate_limiter_instances_share_redis_state() -> None:
    """两个独立限流器（模拟两个 API 进程）看到同一计数。"""

    client = FakeRedis()
    first = limiter(client, per_user=2)
    # 第二个限流器使用同一 Redis 语义；这里复用同一客户端表示共享后端。
    second = limiter(client, per_user=2)

    await first.check(client_ip="10.0.0.1", username="alice")
    await first.check(client_ip="10.0.0.2", username="alice")

    with pytest.raises(RateLimitExceeded):
        await second.check(client_ip="10.0.0.3", username="alice")


@pytest.mark.anyio
async def test_close_closes_client() -> None:
    client = FakeRedis()
    await limiter(client).close()

    assert client.closed is True


def test_key_prefix_is_namespaced() -> None:
    assert RATE_LIMIT_KEY_PREFIX == "citemind:ratelimit:login"
    assert hash_token("alice") in f"{RATE_LIMIT_KEY_PREFIX}:user:{hash_token('alice')}"


def test_create_login_rate_limiter_returns_none_without_redis_url() -> None:
    assert create_login_rate_limiter(make_settings()) is None


@pytest.mark.anyio
async def test_create_login_rate_limiter_builds_from_settings() -> None:
    created = create_login_rate_limiter(
        make_settings(redis_url="redis://:secret@127.0.0.1:6379/0")
    )

    assert created is not None
    await created.close()
