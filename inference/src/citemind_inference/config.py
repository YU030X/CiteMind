"""inference 进程的启动配置。

token 使用 ``SecretStr`` 保存，``repr``/日志不会打印明文。生产环境使用开发占位值
时在启动阶段直接失败，并明确指出需要替换的变量。
"""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 开发占位 token：仅用于本地与测试。生产环境使用该值会在启动校验中失败。
DEVELOPMENT_INFERENCE_TOKEN = "citemind-inference"


class Settings(BaseSettings):
    """inference 进程配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="CITEMIND_",
        extra="ignore",
        # 校验失败时隐藏输入值，避免 ValidationError 打印 token 明文。
        hide_input_in_errors=True,
    )

    environment: Literal["development", "test", "production"] = "development"
    # 内部接口的 Bearer token。默认空值只用于让“环境变量缺失”与“显式空值”走同一条
    # 启动失败路径；真正的值必须由 CITEMIND_INFERENCE_TOKEN 注入。
    inference_token: SecretStr = SecretStr("")

    @model_validator(mode="after")
    def validate_token(self) -> "Settings":
        token = self.inference_token.get_secret_value()

        if not token:
            raise ValueError("CITEMIND_INFERENCE_TOKEN 不能为空")
        if self.environment == "production" and token == DEVELOPMENT_INFERENCE_TOKEN:
            raise ValueError(
                "生产环境不得使用开发占位 CITEMIND_INFERENCE_TOKEN；请注入独立密钥"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
