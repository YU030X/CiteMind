"""inference HTTP 契约：外部字段使用 camelCase，标准错误体含 code 与 message。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """统一按 camelCase 序列化的响应基类。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ErrorResponse(CamelModel):
    """机器可读错误体；不携带正文、向量或凭据。"""

    code: str
    message: str


class HealthResponse(CamelModel):
    status: Literal["ok"]
    service: Literal["inference"]
    model_loaded: bool


class EmbeddingCapability(CamelModel):
    ready: bool
    reason: str | None = None
    dimension: int | None = None
    model_revision: str | None = None


class RerankCapability(CamelModel):
    ready: bool
    reason: str | None = None


class CapabilitiesResponse(CamelModel):
    embedding: EmbeddingCapability
    rerank: RerankCapability
