from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """API 进程的启动配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="CITEMIND_",
        extra="ignore",
    )

    app_name: str = "CiteMind API"
    environment: Literal["development", "test", "production"] = "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()
