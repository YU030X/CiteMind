"""inference FastAPI 应用：健康、就绪、能力与受限的内部 embedding 接口。

真实模型在 lifespan 启动时从本地目录加载；缺失或校验不符会直接使启动失败。单测通过
``create_app(..., embedder_factory=...)`` 注入 stub，不加载任何权重。

准入顺序（从外到内）：请求体字节上限（JSON 解析前）→ Bearer 鉴权 → 廉价语义上限 →
CPU 许可（tokenize + 预算检查 + encode 都在许可内）→ 输出契约校验。
"""

import asyncio
import math
import secrets
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from citemind_inference.admission import (
    BodySizeLimitMiddleware,
    CpuGate,
    EmbeddingBusyError,
    EmbeddingQueueTimeoutError,
    EmbeddingTaskRegistry,
)
from citemind_inference.config import Settings, get_settings
from citemind_inference.embeddings import Embedder, EmbedderFactory
from citemind_inference.schemas import (
    CapabilitiesResponse,
    EmbeddingCapability,
    EmbedRequest,
    EmbedResponse,
    ErrorResponse,
    HealthResponse,
    ReadyResponse,
    RerankCapability,
)

SERVICE_NAME: Literal["inference"] = "inference"

# 需要限制请求体字节数的路径；只有内部编码接口接收正文。
GUARDED_BODY_PATHS = ("/internal/embed",)
# 关闭时等待运行中编码任务的有界时间；torch 线程无法中断，超时后不再等待。
SHUTDOWN_DRAIN_SECONDS = 30.0
# float32 归一化的舍入量级远小于该值，明显偏离 1 的范数会被拦下。
OUTPUT_NORM_TOLERANCE = 1e-3

EMBEDDING_NOT_READY_CODE = "EMBEDDING_NOT_READY"
EMBEDDING_NOT_READY_REASON = "embedding 模型尚未加载"
RERANK_NOT_READY_REASON = "rerank 路由尚未实现"

UNAUTHORIZED_CODE = "UNAUTHORIZED"
INVALID_REQUEST_CODE = "INVALID_REQUEST"
PAYLOAD_TOO_LARGE_CODE = "EMBEDDING_PAYLOAD_TOO_LARGE"
INPUT_TOO_LONG_CODE = "EMBEDDING_INPUT_TOO_LONG"
BUSY_CODE = "EMBEDDING_BUSY"
QUEUE_TIMEOUT_CODE = "EMBEDDING_QUEUE_TIMEOUT"
OUTPUT_INVALID_CODE = "EMBEDDING_OUTPUT_INVALID"


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


class EmbeddingInputTooLong(Exception):
    """单条文本超过 token 上限；不截断，直接拒绝。"""


class EmbeddingBudgetExceeded(Exception):
    """批次超过 token 位置预算。"""


@dataclass(frozen=True)
class EncodeOutcome:
    """一次批次编码的结果；字段与 :class:`EmbedResponse` 对应。"""

    token_counts: list[int]
    vectors: list[list[float]]


def require_inference_token(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """校验内部接口的 Bearer token；缺失或错误时抛 401，不回显 token。

    用字节做常量时间比较：Starlette 按 latin-1 解码 header，非 ASCII 值直接交给
    ``secrets.compare_digest`` 会抛 ``TypeError`` 并变成 500；编码成 UTF-8 bytes
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


def safe_validation_details(exc: RequestValidationError) -> list[dict[str, str]]:
    """只保留位置、类型与安全文案。

    pydantic 的原始错误项包含 ``input``（以及部分 ``ctx``），会把请求正文回显给调用方；
    这里显式丢弃这两个字段。
    """

    details: list[dict[str, str]] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        details.append(
            {
                "location": location,
                "type": str(error.get("type", "")),
                "message": str(error.get("msg", "")),
            }
        )
    return details


def validate_vectors(
    vectors: Sequence[Sequence[float]],
    *,
    dimension: int,
    expected_count: int,
) -> None:
    """校验模型输出满足冻结契约：条数、维度、有限、非零且单位范数。

    NaN/Inf 会在 JSON 里变成 ``null`` 而看起来“正常”，所以必须在序列化前拦下。
    """

    if len(vectors) != expected_count:
        raise InferenceError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            OUTPUT_INVALID_CODE,
            f"模型返回 {len(vectors)} 条向量，期望 {expected_count} 条",
        )
    for index, vector in enumerate(vectors):
        if len(vector) != dimension:
            raise InferenceError(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                OUTPUT_INVALID_CODE,
                f"vectors[{index}] 维度为 {len(vector)}，期望 {dimension}",
            )
        squared = 0.0
        for value in vector:
            if not math.isfinite(value):
                raise InferenceError(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    OUTPUT_INVALID_CODE,
                    f"vectors[{index}] 含 NaN 或 Inf，无法作为向量返回",
                )
            squared += value * value
        norm = math.sqrt(squared)
        if norm == 0.0 or abs(norm - 1.0) > OUTPUT_NORM_TOLERANCE:
            raise InferenceError(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                OUTPUT_INVALID_CODE,
                f"vectors[{index}] 的 L2 范数不在 1±{OUTPUT_NORM_TOLERANCE} 内",
            )


def release_gate_threadsafe(gate: CpuGate, loop: asyncio.AbstractEventLoop) -> None:
    """由工作线程归还许可。

    许可必须跟随真实 CPU 工作的结束，而不是 HTTP 等待的结束：即使请求已被取消，
    这个从线程调度的回调仍会归还许可。
    """

    try:
        loop.call_soon_threadsafe(gate.release)
    except RuntimeError:
        # 事件循环已关闭（进程正在退出），许可不再有意义。
        return


def cpu_encode_batch(
    embedder: Embedder,
    settings: Settings,
    texts: list[str],
    gate: CpuGate,
    loop: asyncio.AbstractEventLoop,
) -> EncodeOutcome:
    """tokenize、预算检查与编码；整段在一个许可内，并由本线程归还许可。"""

    try:
        token_counts = embedder.token_counts(texts)
        token_limit = min(settings.embedding_max_tokens_per_text, embedder.max_tokens)
        for index, count in enumerate(token_counts):
            if count > token_limit:
                raise EmbeddingInputTooLong(
                    f"texts[{index}] 编码后为 {count} 个 token，超过上限 {token_limit}；"
                    "本服务不截断，请先切分文本"
                )

        # padding=True 会把整批补齐到批内最长序列，真实占用是 条数 × 最长 token 数。
        longest = max(token_counts)
        padded_positions = len(token_counts) * longest
        if padded_positions > settings.embedding_max_total_tokens:
            raise EmbeddingBudgetExceeded(
                f"本批次 padding 后占用 {padded_positions} 个 token 位置"
                f"（{len(token_counts)} 条 × 最长 {longest}），"
                f"超过上限 {settings.embedding_max_total_tokens}"
            )

        vectors = embedder.embed(texts)
        return EncodeOutcome(token_counts=token_counts, vectors=vectors)
    finally:
        release_gate_threadsafe(gate, loop)


async def encode_batch(
    embedder: Embedder,
    settings: Settings,
    texts: list[str],
    gate: CpuGate,
) -> EncodeOutcome:
    """在许可内跑一批 CPU 工作。

    用 ``to_thread`` 让出事件循环；取消只停止等待，工作线程会自行完成并归还许可。
    """

    loop = asyncio.get_running_loop()
    return await asyncio.to_thread(cpu_encode_batch, embedder, settings, texts, gate, loop)


def create_app(
    settings: Settings | None = None,
    *,
    embedder_factory: EmbedderFactory | None = None,
) -> FastAPI:
    """构建应用。

    ``embedder_factory`` 为 ``None`` 时不加载任何模型，进程仍会启动，但 embedding 能力
    如实报告为未就绪。真实入口 ``main.app`` 显式传入 ``load_embedder``。
    """

    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # 加载失败（目录缺失、维度不符、权重损坏）会直接抛出并中止启动。
        app.state.embedder = (
            None if embedder_factory is None else embedder_factory(resolved_settings)
        )
        app.state.embedding_gate = CpuGate(
            concurrency=resolved_settings.embedding_max_concurrency,
            queue_depth=resolved_settings.embedding_queue_depth,
        )
        registry = EmbeddingTaskRegistry()
        app.state.embedding_tasks = registry
        try:
            yield
        finally:
            app.state.embedder = None
            # 有界等待运行中的编码任务，并让登记表取走未观察的异常。
            await registry.drain(SHUTDOWN_DRAIN_SECONDS)

    app = FastAPI(
        title="CiteMind Inference",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        BodySizeLimitMiddleware,
        paths=GUARDED_BODY_PATHS,
        max_bytes=resolved_settings.request_byte_limit,
    )
    app.state.settings = resolved_settings
    app.state.embedder = None

    @app.exception_handler(InferenceError)
    async def inference_error_handler(
        _request: Request, error: InferenceError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={"code": error.code, "message": error.message},
            headers=error.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, error: RequestValidationError
    ) -> JSONResponse:
        # 不使用 FastAPI 默认体：默认体会把原始输入放进 input 字段。
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "code": INVALID_REQUEST_CODE,
                "message": "请求体不合法",
                "details": safe_validation_details(error),
            },
        )

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    async def health(request: Request) -> HealthResponse:
        """liveness：进程存活即 200，模型是否可用由 /ready 与 /capabilities 报告。"""

        return HealthResponse(
            status="ok",
            service=SERVICE_NAME,
            model_loaded=request.app.state.embedder is not None,
        )

    @app.get(
        "/ready",
        response_model=ReadyResponse,
        # 503 时响应体仍是 ReadyResponse（status=not_ready），不是错误体。
        responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadyResponse}},
        tags=["system"],
    )
    async def ready(request: Request) -> JSONResponse:
        embedder: Embedder | None = request.app.state.embedder
        if embedder is None:
            body = ReadyResponse(
                status="not_ready",
                embedding=EmbeddingCapability(
                    ready=False,
                    reason=EMBEDDING_NOT_READY_REASON,
                    dimension=None,
                    model_revision=None,
                ),
            )
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content=body.model_dump(by_alias=True),
            )
        body = ReadyResponse(
            status="ready",
            embedding=EmbeddingCapability(
                ready=True,
                reason=None,
                dimension=embedder.dimension,
                model_revision=embedder.model_revision,
            ),
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK, content=body.model_dump(by_alias=True)
        )

    @app.get("/capabilities", response_model=CapabilitiesResponse, tags=["system"])
    async def capabilities(request: Request) -> CapabilitiesResponse:
        embedder: Embedder | None = request.app.state.embedder
        if embedder is None:
            embedding = EmbeddingCapability(
                ready=False,
                reason=EMBEDDING_NOT_READY_REASON,
                dimension=None,
                model_revision=None,
            )
        else:
            embedding = EmbeddingCapability(
                ready=True,
                reason=None,
                dimension=embedder.dimension,
                model_revision=embedder.model_revision,
            )
        return CapabilitiesResponse(
            embedding=embedding,
            rerank=RerankCapability(ready=False, reason=RERANK_NOT_READY_REASON),
        )

    @app.post(
        "/internal/embed",
        dependencies=[Depends(require_inference_token)],
        response_model=EmbedResponse,
        responses={
            status.HTTP_413_CONTENT_TOO_LARGE: {"model": ErrorResponse},
            status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
            status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
        },
        tags=["internal"],
    )
    async def embed(request: Request, payload: EmbedRequest) -> EmbedResponse:
        settings: Settings = request.app.state.settings
        embedder: Embedder | None = request.app.state.embedder
        if embedder is None:
            raise InferenceError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                EMBEDDING_NOT_READY_CODE,
                EMBEDDING_NOT_READY_REASON,
            )

        # 廉价语义检查：不需要模型，也不占用 CPU 许可。
        texts = payload.texts
        if len(texts) > settings.embedding_max_batch_size:
            raise InferenceError(
                status.HTTP_413_CONTENT_TOO_LARGE,
                PAYLOAD_TOO_LARGE_CODE,
                f"请求包含 {len(texts)} 条文本，超过单次上限 {settings.embedding_max_batch_size}",
            )
        total_bytes = sum(len(text.encode("utf-8")) for text in texts)
        if total_bytes > settings.embedding_max_total_bytes:
            raise InferenceError(
                status.HTTP_413_CONTENT_TOO_LARGE,
                PAYLOAD_TOO_LARGE_CODE,
                f"请求文本合计 {total_bytes} 字节，超过上限 {settings.embedding_max_total_bytes}",
            )
        for index, text in enumerate(texts):
            if len(text) > settings.embedding_max_chars_per_text:
                raise InferenceError(
                    status.HTTP_413_CONTENT_TOO_LARGE,
                    PAYLOAD_TOO_LARGE_CODE,
                    f"texts[{index}] 长度为 {len(text)} 字符，超过上限 "
                    f"{settings.embedding_max_chars_per_text}",
                )

        gate: CpuGate = request.app.state.embedding_gate
        try:
            await gate.acquire(settings.embedding_queue_wait_seconds)
        except EmbeddingBusyError as error:
            raise InferenceError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                BUSY_CODE,
                "embedding 并发与等待队列已满，请稍后重试",
                headers={"Retry-After": "1"},
            ) from error
        except EmbeddingQueueTimeoutError as error:
            raise InferenceError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                QUEUE_TIMEOUT_CODE,
                "embedding 队列等待超时，请稍后重试",
                headers={"Retry-After": "1"},
            ) from error

        registry: EmbeddingTaskRegistry = request.app.state.embedding_tasks
        # 许可由工作线程归还；这里 shield 任务，使请求取消不会连带取消 CPU 工作。
        task = registry.start(encode_batch(embedder, settings, texts, gate))
        try:
            outcome = await asyncio.shield(task)
        except EmbeddingInputTooLong as error:
            raise InferenceError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                INPUT_TOO_LONG_CODE,
                str(error),
            ) from error
        except EmbeddingBudgetExceeded as error:
            raise InferenceError(
                status.HTTP_413_CONTENT_TOO_LARGE,
                PAYLOAD_TOO_LARGE_CODE,
                str(error),
            ) from error

        validate_vectors(
            outcome.vectors,
            dimension=embedder.dimension,
            expected_count=len(texts),
        )

        return EmbedResponse(
            vectors=outcome.vectors,
            dimension=embedder.dimension,
            model_revision=embedder.model_revision,
            token_counts=outcome.token_counts,
        )

    return app
