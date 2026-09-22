from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

# 与 deploy/compose/compose.yml 默认暴露的本机端口一致；用户名是运行时 api 角色，
# 密码是开发占位值，生产环境会被下面的校验拒绝。
DEFAULT_DATABASE_URL = "postgresql+psycopg://citemind_api:citemind@127.0.0.1:55432/citemind"


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

    @model_validator(mode="after")
    def validate_database_configuration(self) -> "Settings":
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
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
