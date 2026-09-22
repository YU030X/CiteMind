from functools import lru_cache
from typing import Literal
from urllib.parse import SplitResult, urlsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

# 与 deploy/compose/compose.yml 默认暴露的本机端口一致；用户名是运行时 api 角色，
# 密码是开发占位值，生产环境会被下面的校验拒绝。
DEFAULT_DATABASE_URL = "postgresql+psycopg://citemind_api:citemind@127.0.0.1:55432/citemind"

# Redis broker 只接受 redis/rediss scheme；密码是开发占位值，生产环境会被下面的校验拒绝。
REDIS_SCHEMES = ("redis", "rediss")
DEFAULT_REDIS_PASSWORD = "citemind"


def validate_redis_url(redis_url: str) -> SplitResult:
    """校验 Redis broker URL 的通用规则；无效时抛 ValueError。

    这里只做启动时可独立判断的校核，不建立连接；连接失败由 Celery 启动或派发显式报错。
    """

    try:
        parsed = urlsplit(redis_url)
    except ValueError as error:
        raise ValueError("Redis URL 不是有效的 URL") from error

    if parsed.scheme not in REDIS_SCHEMES:
        raise ValueError("Redis URL 必须使用 redis 或 rediss scheme")
    if not parsed.hostname:
        raise ValueError("Redis URL 必须包含非空 host")
    return parsed


def validate_database_url(database_url: str) -> URL:
    """校验数据库 URL 的通用规则；无效时抛 ValueError。

    迁移入口也复用这里的规则，避免迁移 DSN 与应用运行配置的校验产生偏差。
    """

    try:
        url = make_url(database_url)
    except ArgumentError as error:
        raise ValueError("数据库 URL 不是有效的 SQLAlchemy URL") from error

    if url.drivername != "postgresql+psycopg":
        raise ValueError("数据库 URL 必须使用 postgresql+psycopg 驱动")
    if not url.host:
        raise ValueError("数据库 URL 必须包含非空 host")
    if not url.database:
        raise ValueError("数据库 URL 必须包含非空 database")
    return url


class Settings(BaseSettings):
    """API 进程的启动配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="CITEMIND_",
        extra="ignore",
    )

    app_name: str = "CiteMind API"
    environment: Literal["development", "test", "production"] = "development"
    database_url: str = DEFAULT_DATABASE_URL
    database_echo: bool = False
    # 只有 worker 进程需要 broker；API 进程暂不连接 Redis，因此这里保持可选，
    # 由 worker 入口在缺失时显式失败，而不是回退到 localhost 或占位连接。
    redis_url: str | None = None
    # 仅当显式设置时，worker 才把 probe 执行结果写成受信目录下的诊断 marker；
    # 默认 None 表示纯回显，不产生文件副作用。路径由该目录与 Celery task id 推导，
    # payload 不能控制。
    probe_marker_directory: str | None = None
    # queue-probe 一次性验收入口等待 marker 的时限；只用于 Linux Compose 验收。
    queue_probe_timeout_seconds: float = 60.0

    @model_validator(mode="after")
    def validate_configuration(self) -> "Settings":
        url = validate_database_url(self.database_url)

        if self.environment == "production":
            if not url.username:
                raise ValueError("生产环境数据库 URL 必须包含非空 username")
            if not url.password:
                raise ValueError("生产环境数据库 URL 必须包含非空 password")
            if url.password == "citemind":
                raise ValueError("生产环境不得使用默认数据库凭据")
            if self.database_echo:
                raise ValueError("生产环境不得开启 database_echo")

        if self.redis_url is not None:
            redis_url = validate_redis_url(self.redis_url)
            if self.environment == "production":
                if not redis_url.password:
                    raise ValueError("生产环境 Redis URL 必须包含非空 password")
                if redis_url.password == DEFAULT_REDIS_PASSWORD:
                    raise ValueError("生产环境不得使用默认 Redis 凭据")

        if self.probe_marker_directory is not None and not self.probe_marker_directory:
            raise ValueError("probe_marker_directory 不能为空字符串；应留空或提供受信目录")
        if self.queue_probe_timeout_seconds <= 0:
            raise ValueError("queue_probe_timeout_seconds 必须为正数")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
