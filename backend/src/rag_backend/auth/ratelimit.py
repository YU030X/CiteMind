"""登录限流：基于 Redis 的原子计数，跨 API 进程共享。

- 计数用 Lua 脚本在 Redis 内原子完成 ``INCR`` 与首次 ``EXPIRE``，不依赖进程内缓存，
  因此多个 API 进程、多台实例看到同一个限流状态。
- Redis 不可用时抛 ``RateLimiterUnavailable``，登录端据此拒绝请求（fail closed），
  而不是回退到内存计数或放行。
- 限流键只保存 IP 与用户名的 SHA-256，不把明文用户名写进 Redis；限流先于用户查询，
  因此限流响应不区分用户名是否存在。
"""

from __future__ import annotations

from typing import Any, Protocol, cast

from redis.asyncio import Redis
from redis.exceptions import RedisError

from rag_backend.auth.tokens import hash_token
from rag_backend.config import Settings

RATE_LIMIT_KEY_PREFIX = "citemind:ratelimit:login"

# 原子地自增并只在首次写入时设置窗口过期，避免 INCR 成功而 EXPIRE 失败留下永不过期的键。
INCREMENT_WITH_EXPIRY = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return current
"""


class LoginRateLimiterClient(Protocol):
    """限流器实际使用的最小 Redis 命令集，便于测试替换。"""

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any: ...

    async def ttl(self, name: str) -> int: ...

    async def aclose(self) -> None: ...


class RateLimitExceeded(RuntimeError):
    """登录尝试超过窗口限额；调用方返回统一 429，不泄露用户名是否存在。"""

    def __init__(self, *, retry_after: int) -> None:
        super().__init__("登录尝试过于频繁")
        self.retry_after = retry_after


class RateLimiterUnavailable(RuntimeError):
    """Redis 不可用；登录必须拒绝，而不是静默放行。"""


class LoginRateLimiter:
    """按客户端 IP 与用户名分别限流的 Redis 计数器。"""

    def __init__(
        self,
        client: LoginRateLimiterClient,
        *,
        per_ip_limit: int,
        per_username_limit: int,
        window_seconds: int,
    ) -> None:
        self._client = client
        self._per_ip_limit = per_ip_limit
        self._per_username_limit = per_username_limit
        self._window_seconds = window_seconds

    async def check(self, *, client_ip: str, username: str) -> None:
        """记录一次登录尝试；超限或 Redis 故障时抛异常。"""

        checks = (
            (f"{RATE_LIMIT_KEY_PREFIX}:ip:{hash_token(client_ip)}", self._per_ip_limit),
            (
                f"{RATE_LIMIT_KEY_PREFIX}:user:{hash_token(username)}",
                self._per_username_limit,
            ),
        )
        try:
            for key, limit in checks:
                count = int(
                    cast(
                        Any,
                        await self._client.eval(
                            INCREMENT_WITH_EXPIRY, 1, key, str(self._window_seconds)
                        ),
                    )
                )
                if count > limit:
                    retry_after = await self._retry_after(key)
                    raise RateLimitExceeded(retry_after=retry_after)
        except RedisError as error:
            raise RateLimiterUnavailable("Redis 不可用，拒绝登录") from error

    async def _retry_after(self, key: str) -> int:
        ttl = int(cast(Any, await self._client.ttl(key)))
        if ttl < 0:
            return self._window_seconds
        return ttl

    async def close(self) -> None:
        await self._client.aclose()


def create_login_rate_limiter(settings: Settings) -> LoginRateLimiter | None:
    """按配置创建限流器；未配置 Redis 时返回 None，由登录端显式返回不可用。"""

    if settings.redis_url is None:
        return None
    # 连接/读取超时必须有限：Redis 不可达时登录要尽快 fail closed，不能无限挂起。
    client: Redis = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    return LoginRateLimiter(
        cast(LoginRateLimiterClient, client),
        per_ip_limit=settings.login_rate_limit_per_ip,
        per_username_limit=settings.login_rate_limit_per_username,
        window_seconds=settings.login_rate_limit_window_seconds,
    )
