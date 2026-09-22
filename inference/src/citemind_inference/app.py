"""inference FastAPI 应用：健康、能力与受限的内部 embedding 接口。

本切片不包含任何模型权重或缓存：健康与能力端点如实报告未就绪，内部 embedding
接口在正确凭证下仍以 503 明确拒绝，绝不返回占位值或假向量，也不实现 rerank 路由。
"""

import secrets
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, Request, status
from fastapi.responses import JSONResponse

from citemind_inference.config import Settings, get_settings
from citemind_inference.schemas import (
    CapabilitiesResponse,
    EmbeddingCapability,
    ErrorResponse,
    HealthResponse,
    RerankCapability,
)

SERVICE_NAME: Literal["inference"] = "inference"
# 本切片不加载任何概率模型；如实固定为 False。
MODEL_LOADED = False

EMBEDDING_NOT_READY_CODE = "EMBEDDING_NOT_READY"
EMBEDDING_NOT_READY_REASON = "embedding 模型尚未加载：本切片不包含模型权重或缓存"
RERANK_NOT_READY_REASON = "rerank 路由尚未实现"

UNAUTHORIZED_CODE = "UNAUTHORIZED"


class InferenceError(Exception):
    """带机器可读 code 的内部错误；由应用处理器转成标准错误体。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers


def require_inference_token(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """校验内部接口的 Bearer token；缺失或错误时抛 401，不回显 token。

    用字节做常量时间比较：Starlette 按 latin-1 解码 header，非 ASCII 值直接交给
    ``secrets.compare_digest`` 会捿 ``TypeError`` 并变成 500；编码成 UTF-8 bytes
    后行为确定，且仍保持常量时间且 fail-closed。
    """

    settings: Settings = request.app.state.settings
    expected = settings.inference_token.get_secret_value().encode("utf-8")
    scheme, _, provided = (authorization or "").partition(" ")
    provided_bytes = provided.encode("utf-8")

    if (
        scheme.lower() != "bearer"
        or not provided
        or not secrets.compare_digest(provided_bytes, expected)
    ):
        raise InferenceError(
            status.HTTP_401_UNAUTHORIZED,
            UNAUTHORIZED_CODE,
            "缺少或无效的 Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()

    app = FastAPI(
        title="CiteMind Inference",
        version="0.1.0",
    )
    app.state.settings = resolved_settings

    @app.exception_handler(InferenceError)
    async def inference_error_handler(
        _request: Request, error: InferenceError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={"code": error.code, "message": error.message},
            headers=error.headers,
        )

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            service=SERVICE_NAME,
            model_loaded=MODEL_LOADED,
        )

    @app.get("/capabilities", response_model=CapabilitiesResponse, tags=["system"])
    async def capabilities() -> CapabilitiesResponse:
        return CapabilitiesResponse(
            embedding=EmbeddingCapability(
                ready=False,
                reason=EMBEDDING_NOT_READY_REASON,
                dimension=None,
                model_revision=None,
            ),
            rerank=RerankCapability(ready=False, reason=RERANK_NOT_READY_REASON),
        )

    @app.post(
        "/internal/embed",
        dependencies=[Depends(require_inference_token)],
        responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse}},
        tags=["internal"],
    )
    async def embed() -> None:
        """本切片没有 embedding 模型；凭证正确时仍返回 503，不返回任何向量。"""

        raise InferenceError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            EMBEDDING_NOT_READY_CODE,
            EMBEDDING_NOT_READY_REASON,
        )

    return app
