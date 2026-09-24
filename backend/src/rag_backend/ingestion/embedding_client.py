"""worker 侧受限的内部 embedding 客户端（本切片不接 Celery/DB）。

客户端只访问内部 inference 或测试回环地址，使用同步 ``httpx.Client`` 且
``trust_env=False``、``retries=0``：绝不继承宿主 ``HTTP(S)_PROXY`` 或 CA 环境，Bearer
不会随环境代理泄漏。请求在本地完成预算校验后才发出；响应在返回给调用方前做严格契约核对，
且用有界流式读取，成功体与错误体都不允许无上限增长。任何失败都不写数据库、不产生副作用，
也不返回部分结果。单测注入 ``httpx.MockTransport``，绝不发出真实网络请求；本模块不声称
真实 inference 连通性。

预算与冻结契约（与 inference 进程配置一致）：

- 每批最多 16 条、正文合计最多 262144 UTF-8 字节、单条最多 8000 字符与 512 真实 token；
- 每批 padding 位置（条数 × 批内最长 token 数）最多 8192；
- 每批序列化后的请求体最多 787456 字节；
- 成功响应体最多 1 MiB，``Content-Length`` 只用于早拒且不可信；
- 响应必须严格给出 512 维有限向量、``dimension==512``、``modelRevision`` 等于冻结
  profile revision，且 ``tokenCounts`` 与本地真实计数逐条相等。

资源所有权：自建 transport 时 ``close`` 关闭自有 Client；注入 ``transport`` 时其生命周期
归调用方，``close`` 不关闭任何东西，客户端仍可继续使用。
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
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
from rag_backend.ingestion.chunking import TokenCounter

# 冻结的编码契约；与 inference 的 FROZEN_EMBEDDING_REVISION / EMBEDDING_DIMENSION 一致。
EMBEDDING_DIMENSION = 512
EXPECTED_MODEL_REVISION = "7999e1d3359715c523056ef9478215996d62a620"

EMBED_ENDPOINT = "/internal/embed"
REQUEST_KIND = "document"
REQUEST_KIND_KEY = "kind"
REQUEST_TEXTS_KEY = "texts"
RESPONSE_VECTORS_KEY = "vectors"
RESPONSE_DIMENSION_KEY = "dimension"
RESPONSE_MODEL_REVISION_KEY = "modelRevision"
RESPONSE_TOKEN_COUNTS_KEY = "tokenCounts"
ERROR_CODE_KEY = "code"

ERROR_CODE_NOT_READY = "EMBEDDING_NOT_READY"
ERROR_CODE_BUSY = "EMBEDDING_BUSY"
ERROR_CODE_QUEUE_TIMEOUT = "EMBEDDING_QUEUE_TIMEOUT"
BUSY_ERROR_CODES = frozenset({ERROR_CODE_BUSY, ERROR_CODE_QUEUE_TIMEOUT})

MAX_BATCH_SIZE = 16
MAX_BATCH_TEXT_BYTES = 262144
MAX_CHARS_PER_TEXT = 8000
MAX_TOKENS_PER_TEXT = 512
MAX_PADDED_POSITIONS = 8192
# inference 侧由文本预算推导的传输预算：3 × 262144 + 1024。
MAX_REQUEST_BODY_BYTES = 787456
# 成功响应体上限；Content-Length 只用于早拒，真实字节仍按流累计。
MAX_RESPONSE_BODY_BYTES = 1024 * 1024
# 503 错误体只用于读取 code；超过该上限按未知 503 fail closed。
MAX_ERROR_BODY_BYTES = 4096

INFERENCE_HOST = "inference"
INFERENCE_PORT = 9000
ALLOWED_HOSTS = frozenset({INFERENCE_HOST, "127.0.0.1", "localhost", "::1"})

# 只接受成功；非 200 一律按状态码分类。
HTTP_OK = 200


class EmbeddingClientError(Exception):
    """内部 embedding 客户端错误基类；``retryable`` 只给调用方分类，客户端自身绝不重试。"""

    retryable = False
    retry_after_seconds: float | None = None

    def __init__(self, message: str) -> None:
        super().__init__(message)


class EmbeddingInputError(EmbeddingClientError):
    """本地预算或类型校验失败（非文本序列、空白、超长、超 token）；请求从未发出。"""


class EmbeddingAuthError(EmbeddingClientError):
    """401/403：凭据或权限错误，不可重试。"""


class EmbeddingPermanentError(EmbeddingClientError):
    """服务端 413/422 等输入错误；重试同一内容不会成功。"""


class EmbeddingNotReadyError(EmbeddingClientError):
    """503 ``EMBEDDING_NOT_READY``：模型尚未就绪，属服务状态而非繁忙；不可自动重试。"""


class EmbeddingUnavailableError(EmbeddingClientError):
    """无法识别的 503：fail closed，既不当成功也不擅自重试；消息静态。"""


class EmbeddingBusyError(EmbeddingClientError):
    """503 ``EMBEDDING_BUSY``/``EMBEDDING_QUEUE_TIMEOUT``：可重试，携带 ``Retry-After``。"""

    retryable = True

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class EmbeddingTransportError(EmbeddingClientError):
    """超时或连接失败；可按调用方策略重试。"""

    retryable = True


class EmbeddingUpstreamError(EmbeddingClientError):
    """502/5xx 上游错误；可按调用方策略重试。"""

    retryable = True


class EmbeddingResponseError(EmbeddingClientError):
    """响应超限、压缩、非法 JSON 或违反向量/revision/token 契约；消息静态脱敏。"""


def validate_inference_base_url(base_url: str) -> str:
    """校验受限基址：只允许 http、白名单 host、无控制字符/userinfo/query/fragment/子路径。"""

    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("内部 embedding 基址不能为空")
    # 在任何 URL 解析之前拒绝控制字符，避免换行/制表符被解析器静默剥离后绕过白名单。
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in base_url):
        raise ValueError("内部 embedding 基址不得包含控制字符")
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
        host = parsed.hostname
    except ValueError:
        # 不保留底层异常链，避免把原始字符串带进错误输出。
        raise ValueError("内部 embedding 基址不是合法 URL") from None
    if parsed.scheme != "http":
        raise ValueError("内部 embedding 基址必须使用 http")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("内部 embedding 基址不得包含 userinfo")
    if parsed.query or parsed.fragment:
        raise ValueError("内部 embedding 基址不得包含 query 或 fragment")
    if parsed.path not in ("", "/"):
        raise ValueError("内部 embedding 基址不得包含子路径")
    if host is None or host not in ALLOWED_HOSTS:
        raise ValueError("内部 embedding 基址只允许 inference 或回环测试地址")
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
    """解析并校验 token；缺失、空白或含非法字符时显式失败，绝不把明文写进错误消息。

    Bearer 值最终会进入 HTTP 头；非 ASCII 或 ASCII 控制字符（0x00-0x1F/0x7F）会让
    h11 在发请求时抛本地协议错，因此必须在构造阶段静态拒绝，做到启动无效配置 failfast。
    """

    if token is None:
        raise ValueError("内部 embedding 客户端缺少 INFERENCE_TOKEN；未配置时不得构造")
    value = token.get_secret_value() if isinstance(token, SecretStr) else token
    if not value.strip():
        raise ValueError("内部 embedding 客户端 token 不能为空")
    for character in value:
        code = ord(character)
        if code < 0x20 or code == 0x7F or code > 0x7E:
            raise ValueError("内部 embedding 客户端 token 只能包含可打印 ASCII 字符")
    return value


def _normalise_texts(texts: Sequence[str]) -> list[str]:
    """先把输入收敛为 ``list[str]``；非文本序列与逐元素非字符串都在此静态失败。"""

    if isinstance(texts, (str, bytes, bytearray, memoryview, Mapping)) or not isinstance(
        texts, Sequence
    ):
        raise EmbeddingInputError("texts 必须是字符串序列")
    resolved = list(texts)
    for index, text in enumerate(resolved):
        if not isinstance(text, str):
            raise EmbeddingInputError(f"texts[{index}] 必须是字符串")
    return resolved


def _request_body(texts: list[str]) -> bytes:
    """用与请求一致的最小 JSON 编码，使预算核对与实际发送字节完全对应。"""

    return json.dumps(
        {REQUEST_KIND_KEY: REQUEST_KIND, REQUEST_TEXTS_KEY: texts},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _batch_fits(texts: list[str], counts: list[int], total_bytes: int) -> bool:
    if len(texts) > MAX_BATCH_SIZE:
        return False
    if total_bytes > MAX_BATCH_TEXT_BYTES:
        return False
    if max(counts) * len(counts) > MAX_PADDED_POSITIONS:
        return False
    return len(_request_body(texts)) <= MAX_REQUEST_BODY_BYTES


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
                raise EmbeddingResponseError("内部 embedding 响应超出上限")
            break
        if len(chunk) > remaining:
            if overflow_is_error:
                raise EmbeddingResponseError("内部 embedding 响应超出上限")
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


def _classify_503(response: httpx.Response) -> EmbeddingClientError:
    """503 按服务端 code 分类；未知 code 一律 fail closed 且不自动重试。"""

    retry_after = parse_retry_after(response.headers.get("Retry-After"))
    code = _read_error_code(response)
    if code == ERROR_CODE_NOT_READY:
        return EmbeddingNotReadyError("内部 embedding 模型尚未就绪")
    if code in BUSY_ERROR_CODES:
        return EmbeddingBusyError(
            "内部 embedding 繁忙或排队超时", retry_after_seconds=retry_after
        )
    return EmbeddingUnavailableError("内部 embedding 暂不可用")


def _http_error(response: httpx.Response) -> EmbeddingClientError:
    """非 200 分类；除 503 外不读取错误正文。"""

    status = response.status_code
    if status in (401, 403):
        return EmbeddingAuthError("内部 embedding 认证失败")
    if status in (413, 422):
        return EmbeddingPermanentError("内部 embedding 拒绝输入")
    if status == 503:
        return _classify_503(response)
    if 500 <= status < 600:
        return EmbeddingUpstreamError("内部 embedding 上游错误")
    return EmbeddingPermanentError("内部 embedding 返回意外状态")


def _validate_response(
    payload: object,
    texts: list[str],
    counts: list[int],
    expected_model_revision: str,
) -> list[list[float]]:
    """严格核对响应契约；错误消息静态，绝不回显响应正文或输入。"""

    if not isinstance(payload, dict):
        raise EmbeddingResponseError("内部 embedding 响应结构不合法")
    if payload.get(RESPONSE_DIMENSION_KEY) != EMBEDDING_DIMENSION:
        raise EmbeddingResponseError("内部 embedding 响应维度不匹配")
    if payload.get(RESPONSE_MODEL_REVISION_KEY) != expected_model_revision:
        raise EmbeddingResponseError("内部 embedding 响应模型 revision 不匹配")
    raw_vectors = payload.get(RESPONSE_VECTORS_KEY)
    raw_counts = payload.get(RESPONSE_TOKEN_COUNTS_KEY)
    if not isinstance(raw_vectors, list) or len(raw_vectors) != len(texts):
        raise EmbeddingResponseError("内部 embedding 响应向量条数与输入不一致")
    if not isinstance(raw_counts, list) or len(raw_counts) != len(counts):
        raise EmbeddingResponseError("内部 embedding 响应 token 计数条数与输入不一致")

    vectors: list[list[float]] = []
    for raw_vector in raw_vectors:
        if not isinstance(raw_vector, list) or len(raw_vector) != EMBEDDING_DIMENSION:
            raise EmbeddingResponseError("内部 embedding 响应向量维度不合法")
        vector: list[float] = []
        for value in raw_vector:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise EmbeddingResponseError("内部 embedding 响应向量含非数值")
            resolved = float(value)
            if not math.isfinite(resolved):
                raise EmbeddingResponseError("内部 embedding 响应向量含 NaN 或 Inf")
            vector.append(resolved)
        vectors.append(vector)

    for reported, local in zip(raw_counts, counts):
        if isinstance(reported, bool) or not isinstance(reported, int) or reported != local:
            raise EmbeddingResponseError("内部 embedding 响应 token 计数与本地计数不一致")
    return vectors


class InternalEmbeddingClient:
    """受限内部 embedding 客户端；同步、无自动重试、资源所有权显式。

    构造即完成 token、基址与超时校验（failfast）。注入 ``transport`` 时其生命周期归调用方：
    ``close`` 不关闭该 transport，客户端也保持可用；只有自建 transport 时 ``close`` 才关闭
    自有 Client，关闭后禁止再调用（httpx 会拒绝已关闭的 Client）。
    """

    def __init__(
        self,
        *,
        counter: TokenCounter,
        token: SecretStr | str | None = None,
        base_url: str = DEFAULT_INFERENCE_BASE_URL,
        timeout_seconds: float = DEFAULT_INFERENCE_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
        expected_model_revision: str = EXPECTED_MODEL_REVISION,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("内部 embedding 超时必须是有限正数")
        resolved_token = _resolve_token(token)
        self._base_url = validate_inference_base_url(base_url)
        self._timeout_seconds = timeout_seconds
        self._counter = counter
        self._expected_model_revision = expected_model_revision
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
        counter: TokenCounter,
        transport: httpx.BaseTransport | None = None,
    ) -> InternalEmbeddingClient:
        """用进程配置构造客户端；未配置 ``INFERENCE_TOKEN`` 时构造即失败。"""

        return cls(
            counter=counter,
            token=settings.inference_token,
            base_url=settings.inference_base_url,
            timeout_seconds=settings.inference_timeout_seconds,
            transport=transport,
        )

    def __enter__(self) -> InternalEmbeddingClient:
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

    def embed_document_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """按输入顺序返回全部向量；任一批失败即抛出，绝不返回部分结果。"""

        vectors: list[list[float]] = []
        for batch_texts, batch_counts in self._plan_batches(_normalise_texts(texts)):
            vectors.extend(self._embed_batch(batch_texts, batch_counts))
        return vectors

    def _plan_batches(self, texts: list[str]) -> list[tuple[list[str], list[int]]]:
        """先对全部输入做本地预算校验，再贪心分批；网络请求在此之后才会发出。"""

        if not texts:
            raise EmbeddingInputError("texts 不能为空")
        counts: list[int] = []
        byte_lengths: list[int] = []
        for index, text in enumerate(texts):
            if not text.strip():
                raise EmbeddingInputError(f"texts[{index}] 不能为空或纯空白")
            if len(text) > MAX_CHARS_PER_TEXT:
                raise EmbeddingInputError(f"texts[{index}] 超过 {MAX_CHARS_PER_TEXT} 字符上限")
            try:
                byte_length = len(text.encode("utf-8"))
            except UnicodeEncodeError:
                # 孤立代理项等非法 Unicode 在编码前静态失败，绝不发请求。
                raise EmbeddingInputError(f"texts[{index}] 含非法 Unicode 字符") from None
            count = self._counter.count_tokens(text)
            if count > MAX_TOKENS_PER_TEXT:
                raise EmbeddingInputError(f"texts[{index}] 超过 {MAX_TOKENS_PER_TEXT} token 上限")
            counts.append(count)
            byte_lengths.append(byte_length)

        batches: list[tuple[list[str], list[int]]] = []
        current_texts: list[str] = []
        current_counts: list[int] = []
        current_bytes = 0
        for text, count, byte_length in zip(texts, counts, byte_lengths):
            candidate_texts = current_texts + [text]
            candidate_counts = current_counts + [count]
            candidate_bytes = current_bytes + byte_length
            if current_texts and not _batch_fits(
                candidate_texts, candidate_counts, candidate_bytes
            ):
                batches.append((current_texts, current_counts))
                current_texts, current_counts, current_bytes = [], [], 0
                candidate_texts, candidate_counts, candidate_bytes = [text], [count], byte_length
            if not _batch_fits(candidate_texts, candidate_counts, candidate_bytes):
                raise EmbeddingInputError("单条文本超出 embedding 批次预算")
            current_texts.append(text)
            current_counts.append(count)
            current_bytes = candidate_bytes
        if current_texts:
            batches.append((current_texts, current_counts))
        return batches

    def _embed_batch(self, texts: list[str], counts: list[int]) -> list[list[float]]:
        body = _request_body(texts)
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
                    raise EmbeddingResponseError("内部 embedding 响应使用了不受支持的压缩编码")
                if _declared_length_exceeds(response, MAX_RESPONSE_BODY_BYTES):
                    raise EmbeddingResponseError("内部 embedding 响应超出上限")
                raw = _read_capped(response, MAX_RESPONSE_BODY_BYTES, overflow_is_error=True)
        except httpx.TimeoutException:
            raise EmbeddingTransportError("内部 embedding 请求超时") from None
        except httpx.RequestError:
            raise EmbeddingTransportError("内部 embedding 连接失败") from None

        try:
            payload = json.loads(raw)
        except (ValueError, RecursionError):
            # 含非法 UTF-8、超深嵌套与截断体；消息静态，不回显正文。
            raise EmbeddingResponseError("内部 embedding 响应不是合法 JSON") from None
        return _validate_response(payload, texts, counts, self._expected_model_revision)
