"""API 侧受限内部查询编码客户端。

专用于把**单条**用户查询编码为检索向量，供未来检索切片依赖注入。与 worker 的
:mod:`rag_backend.ingestion.embedding_client` 分离：本模块不导入 tokenizer、worker 身份或
ingestion 预算代码，导入它也不会加载 ``tokenizers``；查询本地不做 token 预算，只做字符/字节
上限与响应契约校验（生成侧的输入预算估算另由 :mod:`rag_backend.generation` 负责）。

查询 instruction 前缀由 inference 服务端在编码前**恰好追加一次**（契约
:data:`QUERY_ENCODING_CONTRACT`）；客户端只发送原始查询文本，绝不去重或改写用户输入。
前缀是独立的具名契约，不并入 ``modelRevision``：仅凭 revision 无法识别 instruction 漂移。
客户端把本地字符/字节上限按该前缀折算到完整模型输入，并在响应中严格要求
``queryEncodingContract`` 精确等于 :data:`QUERY_ENCODING_CONTRACT`（缺失/非法/未知都失败）；
因此未来同 revision 换前缀可在 wire 上被检测到。

受限安全边界与 worker 客户端一致：同步 ``httpx.Client``、``trust_env=False``、
``retries=0``、显式不跟随重定向、显式 ``Accept-Encoding: identity``、Bearer token 只发往
内部 inference 或测试回环地址；成功响应有界流式读取（上限 1 MiB），503 错误体有界读取
（上限 4 KiB）并按机器码分类；任何失败消息静态，不回显查询文本、向量或凭据。客户端自身
零自动重试——单次请求重试归上层检索策略决定，本模块不引入总重试预算。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from rag_backend.config import (
    DEFAULT_INFERENCE_BASE_URL as DEFAULT_INFERENCE_BASE_URL,
)
from rag_backend.config import (
    DEFAULT_INFERENCE_TIMEOUT_SECONDS,
    Settings,
)

# 冻结的查询契约；与 inference 的 FROZEN_EMBEDDING_REVISION / QUERY_ENCODING_CONTRACT 一致。
EMBEDDING_DIMENSION = 512
EXPECTED_MODEL_REVISION = "7999e1d3359715c523056ef9478215996d62a620"
QUERY_ENCODING_CONTRACT = "bge-zh-query-v1"

# 服务端会在编码前追加的官方 instruction 前缀。客户端**只**用它把本地字符/字节预算折算到
# 完整模型输入，绝不拼进请求；因此用户输入本身以该前缀开头时也不会被去重或改写。
QUERY_INSTRUCTION_PREFIX = "为这个句子生成表示以用于检索相关文章："

EMBED_ENDPOINT = "/internal/embed"
REQUEST_KIND = "query"
REQUEST_KIND_KEY = "kind"
REQUEST_TEXTS_KEY = "texts"
RESPONSE_VECTORS_KEY = "vectors"
RESPONSE_DIMENSION_KEY = "dimension"
RESPONSE_MODEL_REVISION_KEY = "modelRevision"
RESPONSE_TOKEN_COUNTS_KEY = "tokenCounts"
RESPONSE_QUERY_CONTRACT_KEY = "queryEncodingContract"
ERROR_CODE_KEY = "code"

ERROR_CODE_NOT_READY = "EMBEDDING_NOT_READY"
ERROR_CODE_BUSY = "EMBEDDING_BUSY"
ERROR_CODE_QUEUE_TIMEOUT = "EMBEDDING_QUEUE_TIMEOUT"
BUSY_ERROR_CODES = frozenset({ERROR_CODE_BUSY, ERROR_CODE_QUEUE_TIMEOUT})

# 模型输入（instruction 前缀 + 用户查询）的本地上限；与 inference 语义预算一致。本地预算必须
# 计入前缀才能与服务端对齐；真实 token 预算（512）仍由服务端按完整模型输入判定。
MAX_MODEL_INPUT_CHARS = 8000
MAX_MODEL_INPUT_BYTES = 262144
PREFIX_CHARS = len(QUERY_INSTRUCTION_PREFIX)
PREFIX_BYTES = len(QUERY_INSTRUCTION_PREFIX.encode("utf-8"))
# 原始用户查询可接受的字符上限：完整模型输入上限减去服务端追加的前缀。请求层用同一常量
# 在解析阶段早拒超长输入，避免为必然被拒的查询先付分词/网络成本；本客户端仍自行校验。
MAX_QUERY_CHARS = MAX_MODEL_INPUT_CHARS - PREFIX_CHARS
MIN_TOKEN_COUNT = 1
MAX_TOKEN_COUNT = 512
# 成功响应体上限；Content-Length 只用于早拒，真实字节仍按流累计。
MAX_RESPONSE_BODY_BYTES = 1024 * 1024
# 503 错误体只用于读取 code；超过该上限按未知 503 fail closed。
MAX_ERROR_BODY_BYTES = 4096
# float32 归一化的舍入量级远小于该值；非归一化向量必须被拒绝。
OUTPUT_NORM_TOLERANCE = 1e-3

INFERENCE_HOST = "inference"
INFERENCE_PORT = 9000
ALLOWED_HOSTS = frozenset({INFERENCE_HOST, "127.0.0.1", "localhost", "::1"})

HTTP_OK = 200


class QueryEmbeddingError(Exception):
    """查询编码客户端错误基类；``retryable`` 只给调用方分类，客户端自身绝不重试。"""

    retryable = False
    retry_after_seconds: float | None = None

    def __init__(self, message: str) -> None:
        super().__init__(message)


class QueryEmbeddingInputError(QueryEmbeddingError):
    """本地类型或上限校验失败（空白、超长、非法 Unicode）；请求从未发出。"""


class QueryEmbeddingAuthError(QueryEmbeddingError):
    """401/403：凭据或权限错误，不可重试。"""


class QueryEmbeddingPermanentError(QueryEmbeddingError):
    """服务端 413/422 等输入错误；重试同一内容不会成功。"""


class QueryEmbeddingNotReadyError(QueryEmbeddingError):
    """503 ``EMBEDDING_NOT_READY``：模型尚未就绪，属服务状态；不可自动重试。"""


class QueryEmbeddingUnavailableError(QueryEmbeddingError):
    """无法识别的 503：fail closed，既不当成功也不擅自重试；消息静态。"""


class QueryEmbeddingBusyError(QueryEmbeddingError):
    """503 ``EMBEDDING_BUSY``/``EMBEDDING_QUEUE_TIMEOUT``：可重试，携带 ``Retry-After``。"""

    retryable = True

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class QueryEmbeddingTransportError(QueryEmbeddingError):
    """超时或连接失败；可按调用方策略重试。"""

    retryable = True


class QueryEmbeddingUpstreamError(QueryEmbeddingError):
    """502/5xx 上游错误；可按调用方策略重试。"""

    retryable = True


class QueryEmbeddingResponseError(QueryEmbeddingError):
    """响应超限、压缩、非法 JSON 或违反向量/revision/token/范数契约；消息静态脱敏。"""


@dataclass(frozen=True, slots=True)
class EmbeddedQuery:
    """单条查询的编码结果：检索向量与必要 token 元数据。

    ``token_count`` 是服务端对**完整模型输入**（instruction 前缀 + 用户查询）计得的 token 数
    （含特殊 token），明确区别于用户输入的原始 token 数，客户端无需本地 tokenizer。
    """

    vector: tuple[float, ...]
    token_count: int
    model_revision: str


def validate_inference_base_url(base_url: str) -> str:
    """校验受限基址：只允许 http、白名单 host、无控制字符/userinfo/query/fragment/子路径。"""

    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("内部查询编码基址不能为空")
    # 在任何 URL 解析之前拒绝控制字符，避免换行/制表符被解析器静默剥离后绕过白名单。
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in base_url):
        raise ValueError("内部查询编码基址不得包含控制字符")
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
        host = parsed.hostname
    except ValueError:
        # 不保留底层异常链，避免把原始字符串带进错误输出。
        raise ValueError("内部查询编码基址不是合法 URL") from None
    if parsed.scheme != "http":
        raise ValueError("内部查询编码基址必须使用 http")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("内部查询编码基址不得包含 userinfo")
    if parsed.query or parsed.fragment:
        raise ValueError("内部查询编码基址不得包含 query 或 fragment")
    if parsed.path not in ("", "/"):
        raise ValueError("内部查询编码基址不得包含子路径")
    if host is None or host not in ALLOWED_HOSTS:
        raise ValueError("内部查询编码基址只允许 inference 或回环测试地址")
    if host == INFERENCE_HOST and port != INFERENCE_PORT:
        raise ValueError("inference 基址端口必须是 9000")
    return base_url.rstrip("/")


def parse_retry_after(value: str | None) -> float | None:
    """把 ``Retry-After`` 解析为非负秒数；支持秒数与 HTTP-date，非法值返回 None。"""

    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        seconds = float(candidate)
    except ValueError:
        seconds = None
    if seconds is not None:
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    try:
        parsed = parsedate_to_datetime(candidate)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, (parsed - datetime.now(UTC)).total_seconds())


def _resolve_token(token: SecretStr | str | None) -> str:
    """解析并校验 token；缺失、空白或含非法字符时显式失败，绝不把明文写进错误消息。"""

    if token is None:
        raise ValueError("内部查询编码客户端缺少 INFERENCE_TOKEN；未配置时不得构造")
    value = token.get_secret_value() if isinstance(token, SecretStr) else token
    if not value.strip():
        raise ValueError("内部查询编码客户端 token 不能为空")
    for character in value:
        code = ord(character)
        if code < 0x20 or code == 0x7F or code > 0x7E:
            raise ValueError("内部查询编码客户端 token 只能包含可打印 ASCII 字符")
    return value


def _normalise_query(text: str) -> str:
    """单条查询的本地校验；任何失败都在发出请求前抛出静态脱敏错误。

    字符/字节上限按**完整模型输入**（instruction 前缀 + 用户查询）判定，与服务端对追加前缀后
    缓冲区的上限一致；错误消息不回显查询文本。
    """

    if not isinstance(text, str):
        raise QueryEmbeddingInputError("查询必须是字符串")
    if not text.strip():
        raise QueryEmbeddingInputError("查询不能为空或纯空白")
    if len(text) + PREFIX_CHARS > MAX_MODEL_INPUT_CHARS:
        raise QueryEmbeddingInputError(
            f"查询与 instruction 前缀合计超过 {MAX_MODEL_INPUT_CHARS} 字符模型输入上限"
        )
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        raise QueryEmbeddingInputError("查询含非法 Unicode 字符") from None
    if len(encoded) + PREFIX_BYTES > MAX_MODEL_INPUT_BYTES:
        raise QueryEmbeddingInputError(
            f"查询与 instruction 前缀合计超过 {MAX_MODEL_INPUT_BYTES} 字节模型输入上限"
        )
    return text


def _request_body(text: str) -> bytes:
    """用与请求一致的最小 JSON 编码，只发送原始查询文本（服务端追加前缀）。"""

    return json.dumps(
        {REQUEST_KIND_KEY: REQUEST_KIND, REQUEST_TEXTS_KEY: [text]},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _has_foreign_content_encoding(response: httpx.Response) -> bool:
    """只接受未压缩响应；拒绝任何非 identity 编码，避免解压炸弹。"""

    encoding = response.headers.get("Content-Encoding")
    return encoding is not None and encoding.strip().lower() not in ("", "identity")


def _declared_length_exceeds(response: httpx.Response, limit: int) -> bool:
    """Content-Length 只用于早拒，不作为可信长度。"""

    declared = response.headers.get("Content-Length")
    if declared is None:
        return False
    try:
        length = int(declared)
    except ValueError:
        return False
    return length > limit


def _read_capped(response: httpx.Response, limit: int, *, overflow_is_error: bool) -> bytes:
    """有界读取响应体；``overflow_is_error`` 为 False 时截断而不是报错。"""

    data = bytearray()
    for chunk in response.iter_bytes():
        remaining = limit - len(data)
        if remaining <= 0:
            if overflow_is_error:
                raise QueryEmbeddingResponseError("内部查询编码响应超出上限")
            break
        if len(chunk) > remaining:
            if overflow_is_error:
                raise QueryEmbeddingResponseError("内部查询编码响应超出上限")
            data.extend(chunk[:remaining])
            break
        data.extend(chunk)
    return bytes(data)


def _read_error_code(response: httpx.Response) -> str | None:
    """从 503 错误体读取 ``code``；压缩或超限体按未知处理，绝不回显正文。"""

    if _has_foreign_content_encoding(response):
        return None
    raw = _read_capped(response, MAX_ERROR_BODY_BYTES, overflow_is_error=False)
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(parsed, dict):
        return None
    code = parsed.get(ERROR_CODE_KEY)
    return code if isinstance(code, str) else None


def _classify_503(response: httpx.Response) -> QueryEmbeddingError:
    """503 按服务端 code 分类；未知 code 一律 fail closed 且不自动重试。"""

    retry_after = parse_retry_after(response.headers.get("Retry-After"))
    code = _read_error_code(response)
    if code == ERROR_CODE_NOT_READY:
        return QueryEmbeddingNotReadyError("内部查询编码模型尚未就绪")
    if code in BUSY_ERROR_CODES:
        return QueryEmbeddingBusyError(
            "内部查询编码繁忙或排队超时", retry_after_seconds=retry_after
        )
    return QueryEmbeddingUnavailableError("内部查询编码暂不可用")


def _http_error(response: httpx.Response) -> QueryEmbeddingError:
    """非 200 分类；除 503 外不读取错误正文。"""

    status = response.status_code
    if status in (401, 403):
        return QueryEmbeddingAuthError("内部查询编码认证失败")
    if status in (413, 422):
        return QueryEmbeddingPermanentError("内部查询编码拒绝输入")
    if status == 503:
        return _classify_503(response)
    if 500 <= status < 600:
        return QueryEmbeddingUpstreamError("内部查询编码上游错误")
    return QueryEmbeddingPermanentError("内部查询编码返回意外状态")


def _validate_query_response(
    payload: object, expected_model_revision: str
) -> EmbeddedQuery:
    """严格核对单条查询响应契约；错误消息静态，绝不回显响应正文或查询。"""

    if not isinstance(payload, dict):
        raise QueryEmbeddingResponseError("内部查询编码响应结构不合法")
    if payload.get(RESPONSE_DIMENSION_KEY) != EMBEDDING_DIMENSION:
        raise QueryEmbeddingResponseError("内部查询编码响应维度不匹配")
    if payload.get(RESPONSE_MODEL_REVISION_KEY) != expected_model_revision:
        raise QueryEmbeddingResponseError("内部查询编码响应模型 revision 不匹配")
    # 契约版本必须严格等于本地冻结值；缺失、None、非法或未知一律失败。
    if payload.get(RESPONSE_QUERY_CONTRACT_KEY) != QUERY_ENCODING_CONTRACT:
        raise QueryEmbeddingResponseError("内部查询编码响应 queryEncodingContract 不匹配")
    raw_vectors = payload.get(RESPONSE_VECTORS_KEY)
    raw_counts = payload.get(RESPONSE_TOKEN_COUNTS_KEY)
    if not isinstance(raw_vectors, list) or len(raw_vectors) != 1:
        raise QueryEmbeddingResponseError("内部查询编码响应向量条数不是 1")
    if not isinstance(raw_counts, list) or len(raw_counts) != 1:
        raise QueryEmbeddingResponseError("内部查询编码响应 token 计数条数不是 1")

    raw_vector = raw_vectors[0]
    if not isinstance(raw_vector, list) or len(raw_vector) != EMBEDDING_DIMENSION:
        raise QueryEmbeddingResponseError("内部查询编码响应向量维度不合法")
    vector: list[float] = []
    squared = 0.0
    for value in raw_vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise QueryEmbeddingResponseError("内部查询编码响应向量含非数值")
        resolved = float(value)
        if not math.isfinite(resolved):
            raise QueryEmbeddingResponseError("内部查询编码响应向量含 NaN 或 Inf")
        vector.append(resolved)
        squared += resolved * resolved
    norm = math.sqrt(squared)
    if norm == 0.0 or abs(norm - 1.0) > OUTPUT_NORM_TOLERANCE:
        raise QueryEmbeddingResponseError("内部查询编码响应向量未 L2 归一化")

    reported = raw_counts[0]
    if (
        isinstance(reported, bool)
        or not isinstance(reported, int)
        or not (MIN_TOKEN_COUNT <= reported <= MAX_TOKEN_COUNT)
    ):
        raise QueryEmbeddingResponseError("内部查询编码响应 token 计数不在合法范围")

    return EmbeddedQuery(
        vector=tuple(vector),
        token_count=reported,
        model_revision=expected_model_revision,
    )


class QueryEmbeddingClient:
    """受限内部查询编码客户端；同步、无自动重试、资源所有权显式。

    构造即完成 token、基址与超时校验（failfast）。注入 ``transport`` 时其生命周期归调用方：
    ``close`` 不关闭该 transport，客户端也保持可用；只有自建 transport 时 ``close`` 才关闭
    自有 Client。
    """

    def __init__(
        self,
        *,
        token: SecretStr | str | None = None,
        base_url: str = DEFAULT_INFERENCE_BASE_URL,
        timeout_seconds: float = DEFAULT_INFERENCE_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("内部查询编码超时必须是有限正数")
        resolved_token = _resolve_token(token)
        self._base_url = validate_inference_base_url(base_url)
        self._timeout_seconds = timeout_seconds
        # 只在自建 transport 时负责关闭 Client；注入 transport 的生命周期由调用方管理。
        self._owns_client = transport is None
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_seconds),
            transport=(
                transport
                if transport is not None
                else httpx.HTTPTransport(retries=0, trust_env=False)
            ),
            trust_env=False,
            follow_redirects=False,
            headers={
                "Authorization": f"Bearer {resolved_token}",
                # 只接受未压缩响应；与服务端默认无压缩行为一致，避免解压炸弹。
                "Accept-Encoding": "identity",
            },
        )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> QueryEmbeddingClient:
        """用进程配置构造客户端；未配置 ``INFERENCE_TOKEN`` 时构造即失败。"""

        return cls(
            token=settings.inference_token,
            base_url=settings.inference_base_url,
            timeout_seconds=settings.inference_timeout_seconds,
            transport=transport,
        )

    def __enter__(self) -> QueryEmbeddingClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """仅在自建 transport 时关闭自有 Client；注入 transport 时本调用是 no-op。"""

        if self._owns_client:
            self._client.close()

    def embed_query(self, text: str, expected_model_revision: str) -> EmbeddedQuery:
        """编码单条查询并返回类型化向量；任何失败都不返回部分结果。"""

        resolved = _normalise_query(text)
        body = _request_body(resolved)
        try:
            # stream 上下文保证无论成功、限流或异常都释放 HTTP 资源。
            with self._client.stream(
                "POST",
                EMBED_ENDPOINT,
                content=body,
                headers={"Content-Type": "application/json"},
            ) as response:
                if response.status_code != HTTP_OK:
                    raise _http_error(response)
                if _has_foreign_content_encoding(response):
                    raise QueryEmbeddingResponseError(
                        "内部查询编码响应使用了不受支持的压缩编码"
                    )
                if _declared_length_exceeds(response, MAX_RESPONSE_BODY_BYTES):
                    raise QueryEmbeddingResponseError("内部查询编码响应超出上限")
                raw = _read_capped(response, MAX_RESPONSE_BODY_BYTES, overflow_is_error=True)
        except httpx.TimeoutException:
            raise QueryEmbeddingTransportError("内部查询编码请求超时") from None
        except httpx.RequestError:
            raise QueryEmbeddingTransportError("内部查询编码连接失败") from None

        try:
            payload = json.loads(raw)
        except (ValueError, RecursionError):
            # 含非法 UTF-8、超深嵌套与截断体；消息静态，不回显正文。
            raise QueryEmbeddingResponseError("内部查询编码响应不是合法 JSON") from None
        return _validate_query_response(payload, expected_model_revision)
