"""inference HTTP 契约：外部字段使用 camelCase，标准错误体含 code 与 message。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """统一按 camelCase 序列化的响应基类。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ValidationErrorDetail(CamelModel):
    """单条 422 说明；只含位置、错误类型与安全文案，绝不回显原始输入。"""

    location: str
    type: str
    message: str


class ErrorResponse(CamelModel):
    """机器可读错误体；不携带正文、向量或凭据。"""

    code: str
    message: str
    details: list[ValidationErrorDetail] | None = None


class HealthResponse(CamelModel):
    """liveness：只表示进程存活，不表示模型可用。"""

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


class ReadyResponse(CamelModel):
    """readiness：embedding 是否真的可以服务请求。"""

    status: Literal["ready", "not_ready"]
    embedding: EmbeddingCapability


class EmbedRequest(CamelModel):
    """内部编码请求。

    ``kind=document`` 原样编码正文；``kind=query`` 由服务端按 ``QUERY_ENCODING_CONTRACT``
    在每条文本前恰好追加一次官方 instruction 前缀，token 计数与向量都基于追加后的完整
    模型输入。未知 kind 仍由 Literal 校验得到 422。
    """

    kind: Literal["document", "query"] = "document"
    texts: list[str]

    @field_validator("texts")
    @classmethod
    def validate_texts(cls, texts: list[str]) -> list[str]:
        if not texts:
            raise ValueError("texts 至少包含一条文本")
        for index, text in enumerate(texts):
            if not text.strip():
                raise ValueError(f"texts[{index}] 不能为空或纯空白")
        return texts


class EmbedResponse(CamelModel):
    vectors: list[list[float]]
    dimension: int
    model_revision: str
    # 与 vectors 同序的**完整模型输入**（``kind=query`` 时含服务端追加的 instruction 前缀）
    # 真实 token 数，包含特殊 token；不是用户原始查询文本的 token 数。
    token_counts: list[int]
    # 仅 ``kind=query`` 返回的具名查询契约版本；文档响应通过 ``exclude_none`` 省略该字段，
    # 保持既有 document 响应结构与旧客户端校验不变。
    query_encoding_contract: str | None = None
