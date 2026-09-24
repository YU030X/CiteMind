"""受限内部 embedding 客户端的纯逻辑测试：只注入 ``httpx.MockTransport``，绝不真实联网。

覆盖本地预算/类型 failfast、跨批、有界流式响应、503 分类、响应严格核对、代理环境隔离与
资源生命周期；线协议与预算用 inference 源码 AST 防漂移。真实 inference 端到端连通性由
独立 tester 另行运行，本文件不声称任何真实调用。
"""

from __future__ import annotations

import ast
import json
import math
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from pydantic.alias_generators import to_camel
from rag_backend.config import Settings
from rag_backend.ingestion.embedding_client import (
    ALLOWED_HOSTS,
    DEFAULT_INFERENCE_BASE_URL,
    EMBEDDING_DIMENSION,
    ERROR_CODE_BUSY,
    ERROR_CODE_NOT_READY,
    ERROR_CODE_QUEUE_TIMEOUT,
    EXPECTED_MODEL_REVISION,
    MAX_BATCH_SIZE,
    MAX_BATCH_TEXT_BYTES,
    MAX_CHARS_PER_TEXT,
    MAX_ERROR_BODY_BYTES,
    MAX_PADDED_POSITIONS,
    MAX_REQUEST_BODY_BYTES,
    MAX_RESPONSE_BODY_BYTES,
    MAX_TOKENS_PER_TEXT,
    REQUEST_KIND_KEY,
    REQUEST_TEXTS_KEY,
    RESPONSE_DIMENSION_KEY,
    RESPONSE_MODEL_REVISION_KEY,
    RESPONSE_TOKEN_COUNTS_KEY,
    RESPONSE_VECTORS_KEY,
    EmbeddingAuthError,
    EmbeddingBusyError,
    EmbeddingInputError,
    EmbeddingNotReadyError,
    EmbeddingPermanentError,
    EmbeddingResponseError,
    EmbeddingTransportError,
    EmbeddingUnavailableError,
    EmbeddingUpstreamError,
    InternalEmbeddingClient,
    parse_retry_after,
    validate_inference_base_url,
)

SECRET_TOKEN = "internal-unit-test-secret"
REPO_ROOT = Path(__file__).parents[2]
INFERENCE_SRC = REPO_ROOT / "inference" / "src" / "citemind_inference"
Handler = Callable[[httpx.Request], httpx.Response]


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"_env_file": None, "environment": "test"}
    values.update(overrides)
    return Settings(**values)


class FakeCounter:
    """只按字符数计数的假计数器；记录每次调用，证明不重复加载真实 tokenizer。"""

    def __init__(self, *, overrides: dict[str, int] | None = None) -> None:
        self.calls: list[str] = []
        self.overrides = overrides or {}

    def count_tokens(self, text: str) -> int:
        self.calls.append(text)
        if text in self.overrides:
            return self.overrides[text]
        return max(1, len(text))


class ChunkedStream(httpx.SyncByteStream):
    """模拟无 Content-Length 的分块响应，并记录是否被迭代。"""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.iterated = False

    def __iter__(self) -> Iterator[bytes]:
        self.iterated = True
        yield from self.chunks


def vector_for(text: str) -> list[float]:
    """可区分输入顺序的有限 512 维向量：首元素为文本长度。"""

    return [float(len(text))] + [0.0] * (EMBEDDING_DIMENSION - 1)


def success_body(
    texts: list[str],
    *,
    revision: str = EXPECTED_MODEL_REVISION,
    counts_offset: int = 0,
    extra_vectors: int = 0,
    dimension: int = EMBEDDING_DIMENSION,
) -> bytes:
    vectors = [vector_for(text) for text in texts]
    if extra_vectors:
        vectors.extend([vector_for("x")] * extra_vectors)
    return json.dumps(
        {
            RESPONSE_VECTORS_KEY: vectors,
            RESPONSE_DIMENSION_KEY: dimension,
            RESPONSE_MODEL_REVISION_KEY: revision,
            RESPONSE_TOKEN_COUNTS_KEY: [
                max(1, len(text)) + counts_offset for text in texts
            ],
        }
    ).encode("utf-8")


def success_response(request: httpx.Request, **kwargs: Any) -> httpx.Response:
    payload = json.loads(request.content)
    return httpx.Response(200, content=success_body(payload[REQUEST_TEXTS_KEY], **kwargs))


def make_client(
    handler: Handler | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    token: SecretStr | str | None = SECRET_TOKEN,
    base_url: str = DEFAULT_INFERENCE_BASE_URL,
    counter: FakeCounter | None = None,
) -> InternalEmbeddingClient:
    resolved_transport = transport
    if resolved_transport is None:
        assert handler is not None
        resolved_transport = httpx.MockTransport(handler)
    return InternalEmbeddingClient(
        base_url=base_url,
        token=token,
        counter=counter or FakeCounter(),
        transport=resolved_transport,
    )


def _module_assignments(path: Path) -> dict[str, ast.expr]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                values[node.target.id] = node.value
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values[target.id] = node.value
    return values


def _class_field_values(path: Path, class_name: str) -> dict[str, ast.expr | None]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            fields: dict[str, ast.expr | None] = {}
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    fields[item.target.id] = item.value
            return fields
    raise AssertionError(f"{path.name} 缺少类 {class_name}")


# --------------------------------------------------------------------------------------
# 线协议与预算防漂移（只读 AST，不导入 inference，绝不引入 torch）
# --------------------------------------------------------------------------------------


def test_constants_match_frozen_embedding_contract() -> None:
    constants = _module_assignments(INFERENCE_SRC / "config.py")
    revision = constants["FROZEN_EMBEDDING_REVISION"]
    dimension = constants["EMBEDDING_DIMENSION"]
    max_tokens = constants["EMBEDDING_MAX_TOKENS"]
    assert isinstance(revision, ast.Constant)
    assert isinstance(dimension, ast.Constant)
    assert isinstance(max_tokens, ast.Constant)
    assert revision.value == EXPECTED_MODEL_REVISION
    assert dimension.value == EMBEDDING_DIMENSION
    assert max_tokens.value == MAX_TOKENS_PER_TEXT


def test_budget_defaults_match_inference_settings() -> None:
    constants = _module_assignments(INFERENCE_SRC / "config.py")
    settings = _class_field_values(INFERENCE_SRC / "config.py", "Settings")

    default_bytes = constants["DEFAULT_MAX_TEXT_BYTES"]
    multiplier = constants["REQUEST_BYTES_MULTIPLIER"]
    overhead = constants["REQUEST_BYTES_OVERHEAD"]
    batch_size = settings["embedding_max_batch_size"]
    chars = settings["embedding_max_chars_per_text"]
    total_tokens = settings["embedding_max_total_tokens"]
    per_text = settings["embedding_max_tokens_per_text"]
    assert isinstance(default_bytes, ast.Constant)
    assert isinstance(multiplier, ast.Constant)
    assert isinstance(overhead, ast.Constant)
    assert isinstance(batch_size, ast.Constant)
    assert isinstance(chars, ast.Constant)
    assert isinstance(total_tokens, ast.Constant)
    assert isinstance(per_text, ast.Name)

    assert default_bytes.value == MAX_BATCH_TEXT_BYTES
    assert multiplier.value == 3
    assert overhead.value == 1024
    assert MAX_REQUEST_BODY_BYTES == 3 * MAX_BATCH_TEXT_BYTES + 1024
    assert batch_size.value == MAX_BATCH_SIZE
    assert chars.value == MAX_CHARS_PER_TEXT
    assert total_tokens.value == MAX_PADDED_POSITIONS
    # 单条 token 上限通过常量间接声明为模型上限 512。
    assert per_text.id == "EMBEDDING_MAX_TOKENS"


def test_wire_field_names_match_inference_camel_case_schema() -> None:
    response_fields = _class_field_values(INFERENCE_SRC / "schemas.py", "EmbedResponse")
    request_fields = _class_field_values(INFERENCE_SRC / "schemas.py", "EmbedRequest")

    assert set(response_fields) == {"vectors", "dimension", "model_revision", "token_counts"}
    assert set(request_fields) == {"kind", "texts"}
    assert REQUEST_KIND_KEY == "kind"
    assert REQUEST_TEXTS_KEY == "texts"
    assert RESPONSE_VECTORS_KEY == "vectors"
    assert RESPONSE_DIMENSION_KEY == "dimension"
    # 客户端硬编码的 camelCase 键必须等于 inference 的 to_camel 输出。
    assert RESPONSE_MODEL_REVISION_KEY == to_camel("model_revision")
    assert RESPONSE_TOKEN_COUNTS_KEY == to_camel("token_counts")
    # 别名生成器必须是 to_camel，否则模型名对不上。
    assert "alias_generator=to_camel" in (INFERENCE_SRC / "schemas.py").read_text(encoding="utf-8")


def test_error_codes_match_inference_admission_codes() -> None:
    app_text = (INFERENCE_SRC / "app.py").read_text(encoding="utf-8")
    assert f'BUSY_CODE = "{ERROR_CODE_BUSY}"' in app_text
    assert f'QUEUE_TIMEOUT_CODE = "{ERROR_CODE_QUEUE_TIMEOUT}"' in app_text
    assert f'EMBEDDING_NOT_READY_CODE = "{ERROR_CODE_NOT_READY}"' in app_text


# --------------------------------------------------------------------------------------
# 构造与基址
# --------------------------------------------------------------------------------------


def test_base_url_validation_allows_only_internal_and_loopback() -> None:
    assert validate_inference_base_url("http://inference:9000") == "http://inference:9000"
    assert validate_inference_base_url("http://127.0.0.1:9000") == "http://127.0.0.1:9000"
    assert validate_inference_base_url("http://localhost:1234") == "http://localhost:1234"
    assert "inference" in ALLOWED_HOSTS

    rejected = (
        "https://inference:9000",
        "http://inference:8080",
        "http://evil.example.com:9000",
        "http://user:pass@inference:9000",
        "http://inference:9000/internal",
        "http://inference:9000?x=1",
        "http://inference:9000#frag",
        "http://169.254.169.254:9000",
    )
    for candidate in rejected:
        with pytest.raises(ValueError):
            validate_inference_base_url(candidate)


@pytest.mark.parametrize(
    "candidate",
    [
        "http://inference:9000\n",
        "http://inference:9000\t",
        "http://inference:9000\r",
        "http://inference:9000\x00",
        "http://evil.example\n.com:9000",
    ],
)
def test_base_url_rejects_control_characters_without_echoing(candidate: str) -> None:
    with pytest.raises(ValueError) as error:
        validate_inference_base_url(candidate)

    message = str(error.value)
    assert "inference:9000" not in message
    assert "evil" not in message


def test_invalid_url_error_does_not_echo_the_input() -> None:
    with pytest.raises(ValueError) as error:
        validate_inference_base_url("http://[::1")

    assert "::1" not in str(error.value)


def test_constructor_requires_a_non_blank_token() -> None:
    with pytest.raises(ValueError):
        make_client(success_response, token=None)
    with pytest.raises(ValueError):
        make_client(success_response, token="   ")


@pytest.mark.parametrize(
    "bad_token",
    [
        "abc\ndef",
        "abc\tdef",
        "abc\x00def",
        "abc\x7fdef",
        "令牌-中文",
        "caf\u00e9-accent",
        "abc\rdef",
    ],
)
def test_token_with_control_or_non_ascii_is_rejected(bad_token: str) -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return success_response(request)

    # str 与 SecretStr 两条构造路径都必须在发请求前静态失败且不回显 token。
    for candidate in (bad_token, SecretStr(bad_token)):
        with pytest.raises(ValueError) as error:
            make_client(handler, token=candidate)
        assert bad_token not in str(error.value)

    assert seen == []


def test_printable_ascii_token_is_accepted_and_not_leaked() -> None:
    token = "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8s9T0u1V2w3X4"
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return success_response(request)

    with make_client(handler, token=token) as client:
        assert token not in repr(client)
        client.embed_document_texts(["hello"])

    assert seen[0].headers["authorization"] == f"Bearer {token}"


def test_request_sends_accept_encoding_identity() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return success_response(request)

    with make_client(handler) as client:
        client.embed_document_texts(["hello"])

    assert seen[0].headers["accept-encoding"] == "identity"


def test_from_settings_requires_token_and_uses_settings_values() -> None:
    counter = FakeCounter()
    without_token = make_settings()
    assert without_token.inference_token is None
    with pytest.raises(ValueError):
        InternalEmbeddingClient.from_settings(without_token, counter=counter)

    with_token = make_settings(
        inference_token=SECRET_TOKEN,
        inference_base_url="http://127.0.0.1:9000",
        inference_timeout_seconds=12.5,
    )
    client = InternalEmbeddingClient.from_settings(with_token, counter=counter)
    try:
        assert client._base_url == "http://127.0.0.1:9000"
        assert client._timeout_seconds == 12.5
    finally:
        client.close()


def test_settings_without_inference_token_still_starts_api() -> None:
    from rag_backend.app import create_app

    settings = make_settings()

    assert settings.inference_token is None
    # 未配置内部 token 不应影响既有 API 应用构造。
    assert create_app(settings) is not None


def test_settings_does_not_render_inference_token() -> None:
    settings = make_settings(inference_token=SECRET_TOKEN)

    assert SECRET_TOKEN not in repr(settings)
    assert SECRET_TOKEN not in str(settings)


# --------------------------------------------------------------------------------------
# 请求形状、分批与本地 failfast
# --------------------------------------------------------------------------------------


def test_single_batch_request_shape_and_bearer_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return success_response(request)

    with make_client(handler) as client:
        vectors = client.embed_document_texts(["hello", "world"])

    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/internal/embed"
    assert seen[0].url.host == "inference"
    assert json.loads(seen[0].content) == {"kind": "document", "texts": ["hello", "world"]}
    assert seen[0].headers["authorization"] == f"Bearer {SECRET_TOKEN}"
    assert len(vectors) == 2
    assert all(len(vector) == EMBEDDING_DIMENSION for vector in vectors)


def test_multi_batch_splits_at_sixteen_and_preserves_order() -> None:
    texts = ["x" * length for length in range(1, MAX_BATCH_SIZE + 2)]
    seen: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload["texts"])
        return success_response(request)

    with make_client(handler) as client:
        vectors = client.embed_document_texts(texts)

    assert [len(batch) for batch in seen] == [MAX_BATCH_SIZE, 1]
    assert [vector[0] for vector in vectors] == [float(length) for length in range(1, 18)]


def test_later_batch_failure_returns_no_partial_result() -> None:
    texts = ["x" * length for length in range(1, MAX_BATCH_SIZE + 2)]
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        if len(seen) == 1:
            return success_response(request)
        return httpx.Response(500, content=b"boom")

    with make_client(handler) as client:
        with pytest.raises(EmbeddingUpstreamError):
            client.embed_document_texts(texts)

    assert len(seen) == 2


def test_blank_and_oversized_inputs_fail_before_any_request() -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return success_response(request)

    with make_client(handler) as client:
        with pytest.raises(EmbeddingInputError):
            client.embed_document_texts([])
        with pytest.raises(EmbeddingInputError):
            client.embed_document_texts(["ok", "   "])
        with pytest.raises(EmbeddingInputError):
            client.embed_document_texts(["x" * (MAX_CHARS_PER_TEXT + 1)])

    assert seen == []


def test_token_over_budget_fails_before_any_request() -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return success_response(request)

    counter = FakeCounter(overrides={"long": MAX_TOKENS_PER_TEXT + 1})
    with make_client(handler, counter=counter) as client:
        with pytest.raises(EmbeddingInputError):
            client.embed_document_texts(["long"])

    assert seen == []


@pytest.mark.parametrize(
    "bad_texts",
    [
        {"a": 1, "b": 2},
        b"raw bytes",
        bytearray(b"raw bytes"),
        123,
        {"a"},
        (text for text in ["generator"]),
        ["ok", 42],
        ["ok", None],
        [None],
    ],
)
def test_non_text_inputs_fail_before_any_request(bad_texts: Any) -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return success_response(request)

    with make_client(handler) as client:
        with pytest.raises(EmbeddingInputError):
            client.embed_document_texts(bad_texts)

    assert seen == []


def test_single_string_is_rejected_as_non_sequence() -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return success_response(request)

    with make_client(handler) as client:
        with pytest.raises(EmbeddingInputError):
            client.embed_document_texts("single string")

    assert seen == []


def test_illegal_unicode_surrogate_fails_before_any_request() -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return success_response(request)

    with make_client(handler) as client:
        with pytest.raises(EmbeddingInputError):
            client.embed_document_texts(["ok", "bad\ud800surrogate"])

    assert seen == []


# --------------------------------------------------------------------------------------
# HTTP 状态分类
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_are_not_retryable_and_read_no_body(status: int) -> None:
    stream = ChunkedStream([b"x" * 1024])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, stream=stream)

    with make_client(handler) as client:
        with pytest.raises(EmbeddingAuthError) as error:
            client.embed_document_texts(["hello"])

    assert error.value.retryable is False
    assert stream.iterated is False


def test_busy_503_parses_retry_after_without_retrying() -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return httpx.Response(
            503,
            json={"code": ERROR_CODE_BUSY, "message": "busy"},
            headers={"Retry-After": "7"},
        )

    with make_client(handler) as client:
        with pytest.raises(EmbeddingBusyError) as error:
            client.embed_document_texts(["hello"])

    assert error.value.retryable is True
    assert error.value.retry_after_seconds == 7.0
    assert len(seen) == 1


def test_queue_timeout_503_is_retryable_busy() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"code": ERROR_CODE_QUEUE_TIMEOUT})

    with make_client(handler) as client:
        with pytest.raises(EmbeddingBusyError) as error:
            client.embed_document_texts(["hello"])

    assert error.value.retryable is True


def test_not_ready_503_is_not_busy_and_not_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"code": ERROR_CODE_NOT_READY})

    with make_client(handler) as client:
        with pytest.raises(EmbeddingNotReadyError) as error:
            client.embed_document_texts(["hello"])

    assert error.value.retryable is False
    assert not isinstance(error.value, EmbeddingBusyError)


@pytest.mark.parametrize(
    "response_factory",
    [
        lambda: httpx.Response(503, text="not json"),
        lambda: httpx.Response(503, json={"code": "SOMETHING_ELSE"}),
        lambda: httpx.Response(503),
    ],
)
def test_unknown_503_fails_closed_statically(
    response_factory: Callable[[], httpx.Response],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response_factory()

    with make_client(handler) as client:
        with pytest.raises(EmbeddingUnavailableError) as error:
            client.embed_document_texts(["hello"])

    assert error.value.retryable is False
    assert "not json" not in str(error.value)


def test_huge_503_body_is_capped_and_fails_closed() -> None:
    stream = ChunkedStream([b"{" + b"x" * (MAX_ERROR_BODY_BYTES * 4)])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, stream=stream)

    with make_client(handler) as client:
        with pytest.raises(EmbeddingUnavailableError):
            client.embed_document_texts(["hello"])

    assert stream.iterated is True


@pytest.mark.parametrize("status", [413, 422])
def test_permanent_input_errors_from_server(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=b"input")

    with make_client(handler) as client:
        with pytest.raises(EmbeddingPermanentError) as error:
            client.embed_document_texts(["hello"])

    assert error.value.retryable is False


@pytest.mark.parametrize("status", [500, 502, 504])
def test_upstream_failures_are_retryable_and_read_no_body(status: int) -> None:
    stream = ChunkedStream([b"x" * 1024])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, stream=stream)

    with make_client(handler) as client:
        with pytest.raises(EmbeddingUpstreamError) as error:
            client.embed_document_texts(["hello"])

    assert error.value.retryable is True
    assert stream.iterated is False


@pytest.mark.parametrize(
    "exc",
    [httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError],
)
def test_transport_failures_are_retryable(exc: type[httpx.RequestError]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc("late response", request=request)

    with make_client(handler) as client:
        with pytest.raises(EmbeddingTransportError) as error:
            client.embed_document_texts(["hello"])

    assert error.value.retryable is True


# --------------------------------------------------------------------------------------
# 有界响应读取与响应校验
# --------------------------------------------------------------------------------------


def test_success_body_over_one_mib_is_rejected() -> None:
    captured: list[httpx.Response] = []
    stream = ChunkedStream([b"x" * (MAX_RESPONSE_BODY_BYTES + 1)])

    def handler(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(200, stream=stream)
        captured.append(response)
        return response

    with make_client(handler) as client:
        with pytest.raises(EmbeddingResponseError):
            client.embed_document_texts(["hello"])

    assert captured[0].is_closed is True


def test_declared_content_length_above_limit_is_rejected_early() -> None:
    captured: list[httpx.Response] = []
    stream = ChunkedStream([b"{}"])

    def handler(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(
            200,
            stream=stream,
            headers={"Content-Length": str(MAX_RESPONSE_BODY_BYTES + 1)},
        )
        captured.append(response)
        return response

    with make_client(handler) as client:
        with pytest.raises(EmbeddingResponseError):
            client.embed_document_texts(["hello"])

    assert stream.iterated is False
    assert captured[0].is_closed is True


def test_content_length_understating_body_is_still_capped() -> None:
    # Content-Length 不可信：声明很小但实际超限仍须被流式累计拒绝。
    stream = ChunkedStream([b"x" * (MAX_RESPONSE_BODY_BYTES + 1)])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, headers={"Content-Length": "10"})

    with make_client(handler) as client:
        with pytest.raises(EmbeddingResponseError):
            client.embed_document_texts(["hello"])

    assert stream.iterated is True


def test_chunked_response_without_content_length_succeeds() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(200, stream=ChunkedStream([success_body(payload["texts"])]))

    with make_client(handler) as client:
        vectors = client.embed_document_texts(["hello"])

    assert len(vectors) == 1


def test_http_resources_are_released_after_a_call() -> None:
    captured: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        response = success_response(request)
        captured.append(response)
        return response

    with make_client(handler) as client:
        client.embed_document_texts(["hello"])

    assert captured and captured[0].is_closed is True


def test_compressed_response_is_rejected_without_decoding() -> None:
    stream = ChunkedStream([b"\x1f\x8b\x08\x00decompression-bomb"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, headers={"Content-Encoding": "gzip"})

    with make_client(handler) as client:
        with pytest.raises(EmbeddingResponseError):
            client.embed_document_texts(["hello"])

    assert stream.iterated is False


def test_malformed_json_is_rejected_without_echoing_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>secret body</html>")

    with make_client(handler) as client:
        with pytest.raises(EmbeddingResponseError) as error:
            client.embed_document_texts(["hello"])

    assert "secret body" not in str(error.value)


def test_invalid_utf8_and_deep_nesting_are_rejected_statically() -> None:
    deep = b"[" * 20000 + b"]" * 20000

    def invalid_utf8(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\xff\xfe\x00")

    def nested(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=deep)

    for handler in (invalid_utf8, nested):
        with make_client(handler) as client:
            with pytest.raises(EmbeddingResponseError) as error:
                client.embed_document_texts(["hello"])
        assert "[" not in str(error.value)


def test_nan_and_inf_vectors_are_rejected() -> None:
    for bad in (float("nan"), float("inf")):
        body = json.dumps(
            {
                RESPONSE_VECTORS_KEY: [[bad] * EMBEDDING_DIMENSION],
                RESPONSE_DIMENSION_KEY: EMBEDDING_DIMENSION,
                RESPONSE_MODEL_REVISION_KEY: EXPECTED_MODEL_REVISION,
                RESPONSE_TOKEN_COUNTS_KEY: [5],
            }
        ).encode("utf-8")

        def handler(request: httpx.Request, body: bytes = body) -> httpx.Response:
            return httpx.Response(200, content=body)

        with make_client(handler) as client:
            with pytest.raises(EmbeddingResponseError):
                client.embed_document_texts(["hello"])


def test_count_drift_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return success_response(request, counts_offset=1)

    with make_client(handler) as client:
        with pytest.raises(EmbeddingResponseError):
            client.embed_document_texts(["hello"])


def test_model_revision_mismatch_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return success_response(request, revision="deadbeef")

    with make_client(handler) as client:
        with pytest.raises(EmbeddingResponseError):
            client.embed_document_texts(["hello"])


def test_vector_count_and_dimension_mismatch_are_rejected() -> None:
    def extra_vector(request: httpx.Request) -> httpx.Response:
        return success_response(request, extra_vectors=1)

    def wrong_dimension(request: httpx.Request) -> httpx.Response:
        return success_response(request, dimension=EMBEDDING_DIMENSION + 1)

    with make_client(extra_vector) as client:
        with pytest.raises(EmbeddingResponseError):
            client.embed_document_texts(["hello"])
    with make_client(wrong_dimension) as client:
        with pytest.raises(EmbeddingResponseError):
            client.embed_document_texts(["hello"])


# --------------------------------------------------------------------------------------
# 代理隔离、机密与生命周期
# --------------------------------------------------------------------------------------


def test_embedding_ignores_environment_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    malicious_proxy = "http://malicious.proxy.invalid:8080"
    monkeypatch.setenv("HTTP_PROXY", malicious_proxy)
    monkeypatch.setenv("HTTPS_PROXY", malicious_proxy)
    monkeypatch.setenv("ALL_PROXY", malicious_proxy)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return success_response(request)

    with make_client(handler) as client:
        assert client._client.trust_env is False
        assert client._client._mounts == {}
        client.embed_document_texts(["hello"])

    assert len(seen) == 1
    assert seen[0].url.host == "inference"


def test_secret_never_appears_in_repr_or_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b"denied")

    with make_client(handler) as client:
        assert SECRET_TOKEN not in repr(client)
        with pytest.raises(EmbeddingAuthError) as error:
            client.embed_document_texts(["hello"])

    assert SECRET_TOKEN not in str(error.value)
    assert SECRET_TOKEN not in repr(error.value)


def test_owned_client_and_transport_are_closed_on_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[Any] = []

    class RecordingHTTPTransport(httpx.BaseTransport):
        def __init__(self, **kwargs: Any) -> None:
            created.append(self)
            self.closed = False

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            raise AssertionError("本测试不应真正发请求")

        def close(self) -> None:
            self.closed = True

    target = "rag_backend.ingestion.embedding_client.httpx.HTTPTransport"
    monkeypatch.setattr(target, RecordingHTTPTransport)
    client = InternalEmbeddingClient(
        base_url=DEFAULT_INFERENCE_BASE_URL,
        token=SECRET_TOKEN,
        counter=FakeCounter(),
    )
    assert client._client.is_closed is False
    client.close()

    assert client._client.is_closed is True
    assert created and created[0].closed is True


def test_external_transport_is_not_closed_and_client_stays_usable() -> None:
    closed: list[bool] = []

    class RecordingTransport(httpx.MockTransport):
        def close(self) -> None:
            closed.append(True)
            super().close()

    transport = RecordingTransport(success_response)
    with make_client(transport=transport) as client:
        client.embed_document_texts(["hello"])

    # 外部 transport 生命周期归调用方；close 后客户端仍可继续使用。
    assert closed == []
    assert client.embed_document_texts(["hello"])


def test_parse_retry_after_variants() -> None:
    assert parse_retry_after("12") == 12.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("soon") is None
    assert parse_retry_after("-3") is None
    # HTTP-date 形式（未来时间）应解析为有限正秒数，而不是非法值。
    future = parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT")
    assert future is not None and math.isfinite(future) and future > 0
    # 已过去的 HTTP-date 归零，不返回负数。
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0


def test_embed_method_signature_accepts_plain_sequence() -> None:
    values: Sequence[str] = ["a", "b"]
    with make_client(success_response) as client:
        vectors = client.embed_document_texts(values)

    assert len(vectors) == 2
