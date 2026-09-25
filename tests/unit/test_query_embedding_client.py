"""API 侧受限查询编码客户端的纯逻辑测试：只注入 ``httpx.MockTransport``，绝不真实联网。

覆盖本地类型/上限 failfast、受限基址、有界流式读取、503 分类、单条向量/范数/revision/token
契约、代理隔离、无重定向、无自动重试与资源生命周期；查询契约常量用 inference 源码 AST
防漂移。另一个子进程用例证明导入本模块不会拉入 ``tokenizers``/``rag_backend.ingestion``，
因此 API 进程可在不安装 tokenizer 的镜像里安全导入。真实 inference 端到端连通性不在本文件
声称。
"""

from __future__ import annotations

import ast
import json
import math
import subprocess
import sys
import zlib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from pydantic.alias_generators import to_camel
from rag_backend.config import Settings
from rag_backend.retrieval.query_embedding_client import (
    ALLOWED_HOSTS,
    DEFAULT_INFERENCE_BASE_URL,
    EMBEDDING_DIMENSION,
    ERROR_CODE_BUSY,
    ERROR_CODE_NOT_READY,
    ERROR_CODE_QUEUE_TIMEOUT,
    EXPECTED_MODEL_REVISION,
    MAX_ERROR_BODY_BYTES,
    MAX_MODEL_INPUT_BYTES,
    MAX_MODEL_INPUT_CHARS,
    MAX_RESPONSE_BODY_BYTES,
    MAX_TOKEN_COUNT,
    PREFIX_BYTES,
    PREFIX_CHARS,
    QUERY_ENCODING_CONTRACT,
    QUERY_INSTRUCTION_PREFIX,
    REQUEST_KIND_KEY,
    REQUEST_TEXTS_KEY,
    RESPONSE_DIMENSION_KEY,
    RESPONSE_MODEL_REVISION_KEY,
    RESPONSE_QUERY_CONTRACT_KEY,
    RESPONSE_TOKEN_COUNTS_KEY,
    RESPONSE_VECTORS_KEY,
    EmbeddedQuery,
    QueryEmbeddingAuthError,
    QueryEmbeddingBusyError,
    QueryEmbeddingClient,
    QueryEmbeddingInputError,
    QueryEmbeddingNotReadyError,
    QueryEmbeddingPermanentError,
    QueryEmbeddingResponseError,
    QueryEmbeddingTransportError,
    QueryEmbeddingUnavailableError,
    QueryEmbeddingUpstreamError,
    parse_retry_after,
    validate_inference_base_url,
)

SECRET_TOKEN = "internal-query-unit-test-secret"
REPO_ROOT = Path(__file__).parents[2]
INFERENCE_SRC = REPO_ROOT / "inference" / "src" / "citemind_inference"
MODULE_PATH = (
    REPO_ROOT / "backend" / "src" / "rag_backend" / "retrieval" / "query_embedding_client.py"
)
Handler = Callable[[httpx.Request], httpx.Response]


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"_env_file": None, "environment": "test"}
    values.update(overrides)
    return Settings(**values)


def unit_vector(seed: str) -> list[float]:
    vector = [0.0] * EMBEDDING_DIMENSION
    vector[zlib.crc32(seed.encode("utf-8")) % EMBEDDING_DIMENSION] = 1.0
    return vector


def success_body(
    text: str,
    *,
    revision: str = EXPECTED_MODEL_REVISION,
    token_count: int = 5,
    vectors: object | None = None,
    counts: object | None = None,
    dimension: int = EMBEDDING_DIMENSION,
    query_contract: object | None = QUERY_ENCODING_CONTRACT,
) -> bytes:
    payload: dict[str, object] = {
        RESPONSE_VECTORS_KEY: vectors if vectors is not None else [unit_vector(text)],
        RESPONSE_DIMENSION_KEY: dimension,
        RESPONSE_MODEL_REVISION_KEY: revision,
        RESPONSE_TOKEN_COUNTS_KEY: counts if counts is not None else [token_count],
    }
    if query_contract is not None:
        payload[RESPONSE_QUERY_CONTRACT_KEY] = query_contract
    return json.dumps(payload).encode("utf-8")


def success_response(request: httpx.Request, **kwargs: Any) -> httpx.Response:
    payload = json.loads(request.content)
    return httpx.Response(200, content=success_body(payload[REQUEST_TEXTS_KEY][0], **kwargs))


def make_client(
    handler: Handler | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    token: SecretStr | str | None = SECRET_TOKEN,
    base_url: str = DEFAULT_INFERENCE_BASE_URL,
    timeout_seconds: float = 5.0,
) -> QueryEmbeddingClient:
    resolved_transport = transport
    if resolved_transport is None:
        assert handler is not None
        resolved_transport = httpx.MockTransport(handler)
    return QueryEmbeddingClient(
        base_url=base_url,
        token=token,
        timeout_seconds=timeout_seconds,
        transport=resolved_transport,
    )


class ChunkedStream(httpx.SyncByteStream):
    """模拟无 Content-Length 的分块响应，并记录是否被迭代。"""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.iterated = False

    def __iter__(self) -> Iterator[bytes]:
        self.iterated = True
        yield from self.chunks


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


def _class_annotations(path: Path, class_name: str) -> dict[str, ast.expr]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            annotations: dict[str, ast.expr] = {}
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    annotations[item.target.id] = item.annotation
            return annotations
    raise AssertionError(f"{path.name} 缺少类 {class_name}")


def _string_constant(value: ast.expr | None) -> str:
    assert isinstance(value, ast.Constant) and isinstance(value.value, str)
    return value.value


# --------------------------------------------------------------------------------------
# 查询契约与线协议防漂移（只读 inference 源码 AST，绝不导入 torch）
# --------------------------------------------------------------------------------------


def test_frozen_constants_match_inference() -> None:
    constants = _module_assignments(INFERENCE_SRC / "config.py")
    revision = constants["FROZEN_EMBEDDING_REVISION"]
    dimension = constants["EMBEDDING_DIMENSION"]
    max_tokens = constants["EMBEDDING_MAX_TOKENS"]
    assert isinstance(revision, ast.Constant)
    assert isinstance(dimension, ast.Constant)
    assert isinstance(max_tokens, ast.Constant)
    assert revision.value == EXPECTED_MODEL_REVISION
    assert dimension.value == EMBEDDING_DIMENSION
    assert max_tokens.value == MAX_TOKEN_COUNT


def test_model_input_budget_matches_inference_settings_defaults() -> None:
    constants = _module_assignments(INFERENCE_SRC / "config.py")
    settings = _class_field_values(INFERENCE_SRC / "config.py", "Settings")
    chars = settings["embedding_max_chars_per_text"]
    total_bytes = settings["embedding_max_total_bytes"]

    assert isinstance(chars, ast.Constant) and chars.value == MAX_MODEL_INPUT_CHARS
    # 字节预算在 Settings 里引用常量，需要解析到实际字面量。
    assert isinstance(total_bytes, ast.Name)
    resolved_bytes = constants[total_bytes.id]
    assert isinstance(resolved_bytes, ast.Constant)
    assert resolved_bytes.value == MAX_MODEL_INPUT_BYTES


def test_query_contract_constant_matches_inference() -> None:
    constants = _module_assignments(INFERENCE_SRC / "config.py")

    assert _string_constant(constants["QUERY_ENCODING_CONTRACT"]) == QUERY_ENCODING_CONTRACT
    prefix = _string_constant(constants["BGE_ZH_QUERY_PREFIX"])
    assert prefix and prefix.endswith("：")


def test_query_prefix_and_contract_match_frozen_golden_literals() -> None:
    # 独立 literal golden：不引用被测常量本身，改动 prefix/契约而不同步这里就会失败。
    # 它只能证明“实现没有偏离已记录的前缀与契约版本”，不能自证前缀一定正确。
    assert QUERY_INSTRUCTION_PREFIX == "为这个句子生成表示以用于检索相关文章："
    assert QUERY_ENCODING_CONTRACT == "bge-zh-query-v1"
    assert PREFIX_CHARS == len(QUERY_INSTRUCTION_PREFIX)
    assert PREFIX_BYTES == len(QUERY_INSTRUCTION_PREFIX.encode("utf-8"))


def test_query_prefix_matches_inference_config() -> None:
    constants = _module_assignments(INFERENCE_SRC / "config.py")

    assert _string_constant(constants["BGE_ZH_QUERY_PREFIX"]) == QUERY_INSTRUCTION_PREFIX
    assert _string_constant(constants["QUERY_ENCODING_CONTRACT"]) == QUERY_ENCODING_CONTRACT


def test_inference_request_kind_accepts_document_and_query() -> None:
    annotations = _class_annotations(INFERENCE_SRC / "schemas.py", "EmbedRequest")
    kind = annotations["kind"]
    assert isinstance(kind, ast.Subscript)
    assert isinstance(kind.value, ast.Name) and kind.value.id == "Literal"
    slice_node = kind.slice
    assert isinstance(slice_node, ast.Tuple)
    values = [
        element.value
        for element in slice_node.elts
        if isinstance(element, ast.Constant) and isinstance(element.value, str)
    ]
    assert values == ["document", "query"]


def test_response_field_names_match_inference_camel_case() -> None:
    response_fields = _class_field_values(INFERENCE_SRC / "schemas.py", "EmbedResponse")

    assert set(response_fields) == {
        "vectors",
        "dimension",
        "model_revision",
        "token_counts",
        "query_encoding_contract",
    }
    assert RESPONSE_DIMENSION_KEY == "dimension"
    assert RESPONSE_MODEL_REVISION_KEY == to_camel("model_revision")
    assert RESPONSE_TOKEN_COUNTS_KEY == to_camel("token_counts")
    assert RESPONSE_QUERY_CONTRACT_KEY == to_camel("query_encoding_contract")
    assert REQUEST_KIND_KEY == "kind"
    assert REQUEST_TEXTS_KEY == "texts"


def test_query_error_codes_match_inference_app() -> None:
    app_text = (INFERENCE_SRC / "app.py").read_text(encoding="utf-8")
    assert f'BUSY_CODE = "{ERROR_CODE_BUSY}"' in app_text
    assert f'QUEUE_TIMEOUT_CODE = "{ERROR_CODE_QUEUE_TIMEOUT}"' in app_text
    assert f'EMBEDDING_NOT_READY_CODE = "{ERROR_CODE_NOT_READY}"' in app_text


def test_api_process_can_import_client_without_tokenizers() -> None:
    """子进程证明：导入客户端不会拉入 tokenizers/torch/rag_backend.ingestion。"""

    script = (
        "import sys\n"
        "sys.modules['tokenizers'] = None\n"
        "sys.modules['torch'] = None\n"
        "sys.modules['transformers'] = None\n"
        "import rag_backend.retrieval.query_embedding_client as module\n"
        "assert module.QUERY_ENCODING_CONTRACT\n"
        "forbidden = [name for name in sys.modules if name == 'rag_backend.ingestion' "
        "or name.startswith('rag_backend.ingestion.')]\n"
        "assert not forbidden, forbidden\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


def test_module_imports_stay_clear_of_worker_identity() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    for module_name in imported:
        assert module_name not in {"tokenizers", "torch", "transformers"}
        assert not module_name.startswith("rag_backend.ingestion")


# --------------------------------------------------------------------------------------
# 构造与受限基址
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
    ["http://inference:9000\n", "http://inference:9000\t", "http://evil.example\n.com:9000"],
)
def test_base_url_rejects_control_characters_without_echoing(candidate: str) -> None:
    with pytest.raises(ValueError) as error:
        validate_inference_base_url(candidate)

    assert "inference:9000" not in str(error.value)
    assert "evil" not in str(error.value)


def test_constructor_requires_a_non_blank_token() -> None:
    with pytest.raises(ValueError):
        make_client(success_response, token=None)
    with pytest.raises(ValueError):
        make_client(success_response, token="   ")


@pytest.mark.parametrize(
    "bad_token",
    ["abc\ndef", "abc\tdef", "abc\x00def", "abc\x7fdef", "令牌-中文", "caf\u00e9"],
)
def test_token_with_control_or_non_ascii_is_rejected(bad_token: str) -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return success_response(request)

    for candidate in (bad_token, SecretStr(bad_token)):
        with pytest.raises(ValueError) as error:
            make_client(handler, token=candidate)
        assert bad_token not in str(error.value)

    assert seen == []


@pytest.mark.parametrize(
    "bad_timeout",
    [0.0, -1.0, float("nan"), float("inf"), float("-inf")],
)
def test_timeout_must_be_finite_and_positive(bad_timeout: float) -> None:
    with pytest.raises(ValueError):
        make_client(success_response, timeout_seconds=bad_timeout)


def test_from_settings_requires_token_and_uses_settings_values() -> None:
    without_token = make_settings()
    assert without_token.inference_token is None
    with pytest.raises(ValueError):
        QueryEmbeddingClient.from_settings(without_token)

    with_token = make_settings(
        inference_token=SECRET_TOKEN,
        inference_base_url="http://127.0.0.1:9000",
        inference_timeout_seconds=12.5,
    )
    client = QueryEmbeddingClient.from_settings(with_token)
    try:
        assert client._base_url == "http://127.0.0.1:9000"
        assert client._timeout_seconds == 12.5
    finally:
        client.close()


def test_client_does_not_follow_redirects() -> None:
    with make_client(success_response) as client:
        assert client._client.follow_redirects is False


def test_printable_ascii_token_is_sent_and_not_leaked() -> None:
    token = "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6"
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return success_response(request)

    with make_client(handler, token=token) as client:
        assert token not in repr(client)
        client.embed_query("查询", EXPECTED_MODEL_REVISION)

    assert seen[0].headers["authorization"] == f"Bearer {token}"
    assert seen[0].headers["accept-encoding"] == "identity"


# --------------------------------------------------------------------------------------
# 请求形状与本地 failfast
# --------------------------------------------------------------------------------------


def test_request_sends_exactly_one_query_text_without_rewriting() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return success_response(request)

    # 用户输入本身以官方 instruction 开头：客户端仍原样发送，不去重、不改写、不自行追加。
    query = f"{QUERY_INSTRUCTION_PREFIX}用户自己写下的句子"
    with make_client(handler) as client:
        client.embed_query(query, EXPECTED_MODEL_REVISION)

    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/internal/embed"
    assert seen[0].url.host == "inference"
    assert json.loads(seen[0].content) == {"kind": "query", "texts": [query]}


def test_success_returns_typed_vector_and_token_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=success_body("查询", token_count=7))

    with make_client(handler) as client:
        result = client.embed_query("查询", EXPECTED_MODEL_REVISION)

    assert isinstance(result, EmbeddedQuery)
    assert isinstance(result.vector, tuple)
    assert len(result.vector) == EMBEDDING_DIMENSION
    assert math.isclose(math.sqrt(sum(v * v for v in result.vector)), 1.0, abs_tol=1e-9)
    assert result.token_count == 7
    assert result.model_revision == EXPECTED_MODEL_REVISION


@pytest.mark.parametrize("bad_text", [b"bytes", None, 42, ["list"], {"a": 1}])
def test_non_string_query_fails_before_any_request(bad_text: Any) -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return success_response(request)

    with make_client(handler) as client:
        with pytest.raises(QueryEmbeddingInputError):
            client.embed_query(bad_text, EXPECTED_MODEL_REVISION)

    assert seen == []


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_blank_query_fails_before_any_request(blank: str) -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return success_response(request)

    with make_client(handler) as client:
        with pytest.raises(QueryEmbeddingInputError):
            client.embed_query(blank, EXPECTED_MODEL_REVISION)

    assert seen == []


def test_oversized_and_illegal_unicode_fail_before_any_request() -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return success_response(request)

    with make_client(handler) as client:
        with pytest.raises(QueryEmbeddingInputError):
            client.embed_query(
                "x" * (MAX_MODEL_INPUT_CHARS - PREFIX_CHARS + 1), EXPECTED_MODEL_REVISION
            )
        with pytest.raises(QueryEmbeddingInputError):
            client.embed_query("bad\ud800surrogate", EXPECTED_MODEL_REVISION)

    assert seen == []


def test_local_budget_accounts_for_server_instruction_prefix() -> None:
    # 本地字符预算按完整模型输入（前缀 + 用户输入）判定，否则会放行服务端必然 413 的查询。
    boundary = "x" * (MAX_MODEL_INPUT_CHARS - PREFIX_CHARS)
    over = "x" * (MAX_MODEL_INPUT_CHARS - PREFIX_CHARS + 1)
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        return success_response(request)

    with make_client(handler) as client:
        accepted = client.embed_query(boundary, EXPECTED_MODEL_REVISION)
        with pytest.raises(QueryEmbeddingInputError):
            client.embed_query(over, EXPECTED_MODEL_REVISION)

    assert accepted.token_count == 5
    # 超限查询在发出前就被拒绝，零 HTTP。
    assert len(seen) == 1
    assert PREFIX_CHARS + len(boundary) == MAX_MODEL_INPUT_CHARS
    assert PREFIX_BYTES + len(boundary.encode("utf-8")) <= MAX_MODEL_INPUT_BYTES


def test_input_error_does_not_echo_the_query() -> None:
    sentinel = "SENTINEL-QUERY-DO-NOT-ECHO-1a2b"
    oversized = "x" * (MAX_MODEL_INPUT_CHARS - PREFIX_CHARS + 1) + sentinel

    with make_client(success_response) as client:
        with pytest.raises(QueryEmbeddingInputError) as error:
            client.embed_query(oversized, EXPECTED_MODEL_REVISION)

    assert sentinel not in str(error.value)
    # 错误明确说明上限面向完整模型输入，而不是用户原始查询。
    assert "模型输入" in str(error.value)


# --------------------------------------------------------------------------------------
# HTTP 状态分类
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_are_not_retryable_and_read_no_body(status: int) -> None:
    stream = ChunkedStream([b"x" * 1024])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, stream=stream)

    with make_client(handler) as client:
        with pytest.raises(QueryEmbeddingAuthError) as error:
            client.embed_query("查询", EXPECTED_MODEL_REVISION)

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
        with pytest.raises(QueryEmbeddingBusyError) as error:
            client.embed_query("查询", EXPECTED_MODEL_REVISION)

    assert error.value.retryable is True
    assert error.value.retry_after_seconds == 7.0
    assert len(seen) == 1


def test_queue_timeout_503_is_retryable_busy() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"code": ERROR_CODE_QUEUE_TIMEOUT})

    with make_client(handler) as client:
        with pytest.raises(QueryEmbeddingBusyError) as error:
            client.embed_query("查询", EXPECTED_MODEL_REVISION)

    assert error.value.retryable is True


def test_not_ready_503_is_not_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"code": ERROR_CODE_NOT_READY})

    with make_client(handler) as client:
        with pytest.raises(QueryEmbeddingNotReadyError) as error:
            client.embed_query("查询", EXPECTED_MODEL_REVISION)

    assert error.value.retryable is False


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
        with pytest.raises(QueryEmbeddingUnavailableError) as error:
            client.embed_query("查询", EXPECTED_MODEL_REVISION)

    assert error.value.retryable is False
    assert "not json" not in str(error.value)


def test_huge_503_body_is_capped_and_fails_closed() -> None:
    stream = ChunkedStream([b"{" + b"x" * (MAX_ERROR_BODY_BYTES * 4)])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, stream=stream)

    with make_client(handler) as client:
        with pytest.raises(QueryEmbeddingUnavailableError):
            client.embed_query("查询", EXPECTED_MODEL_REVISION)

    assert stream.iterated is True


@pytest.mark.parametrize("status", [413, 422])
def test_permanent_input_errors_from_server(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=b"input")

    with make_client(handler) as client:
        with pytest.raises(QueryEmbeddingPermanentError) as error:
            client.embed_query("查询", EXPECTED_MODEL_REVISION)

    assert error.value.retryable is False


@pytest.mark.parametrize("status", [500, 502, 504])
def test_upstream_failures_are_retryable_and_read_no_body(status: int) -> None:
    stream = ChunkedStream([b"x" * 1024])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, stream=stream)

    with make_client(handler) as client:
        with pytest.raises(QueryEmbeddingUpstreamError) as error:
            client.embed_query("查询", EXPECTED_MODEL_REVISION)

    assert error.value.retryable is True
    assert stream.iterated is False


def test_redirect_is_not_followed_and_is_permanent_error() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(302, headers={"Location": "http://inference:9000/elsewhere"})

    with make_client(handler) as client:
        assert client._client.follow_redirects is False
        with pytest.raises(QueryEmbeddingPermanentError):
            client.embed_query("查询", EXPECTED_MODEL_REVISION)

    assert calls == ["/internal/embed"]


@pytest.mark.parametrize(
    "exc", [httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError]
)
def test_transport_failures_are_retryable(exc: type[httpx.RequestError]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc("late response", request=request)

    with make_client(handler) as client:
        with pytest.raises(QueryEmbeddingTransportError) as error:
            client.embed_query("查询", EXPECTED_MODEL_REVISION)

    assert error.value.retryable is True


# --------------------------------------------------------------------------------------
# 有界响应读取与响应契约
# --------------------------------------------------------------------------------------


def test_success_body_over_one_mib_is_rejected() -> None:
    captured: list[httpx.Response] = []
    stream = ChunkedStream([b"x" * (MAX_RESPONSE_BODY_BYTES + 1)])

    def handler(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(200, stream=stream)
        captured.append(response)
        return response

    with make_client(handler) as client:
        with pytest.raises(QueryEmbeddingResponseError):
            client.embed_query("查询", EXPECTED_MODEL_REVISION)

    assert captured[0].is_closed is True


def test_declared_content_length_above_limit_is_rejected_early() -> None:
    stream = ChunkedStream([b"{}"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, stream=stream, headers={"Content-Length": str(MAX_RESPONSE_BODY_BYTES + 1)}
        )

    with make_client(handler) as client:
        with pytest.raises(QueryEmbeddingResponseError):
            client.embed_query("查询", EXPECTED_MODEL_REVISION)

    assert stream.iterated is False


def test_content_length_understating_body_is_still_capped() -> None:
    stream = ChunkedStream([b"x" * (MAX_RESPONSE_BODY_BYTES + 1)])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, headers={"Content-Length": "10"})

    with make_client(handler) as client:
        with pytest.raises(QueryEmbeddingResponseError):
            client.embed_query("查询", EXPECTED_MODEL_REVISION)

    assert stream.iterated is True


def test_chunked_response_without_content_length_succeeds() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            200, stream=ChunkedStream([success_body(payload["texts"][0])])
        )

    with make_client(handler) as client:
        result = client.embed_query("查询", EXPECTED_MODEL_REVISION)

    assert len(result.vector) == EMBEDDING_DIMENSION


def test_http_resources_are_released_after_a_call() -> None:
    captured: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        response = success_response(request)
        captured.append(response)
        return response

    with make_client(handler) as client:
        client.embed_query("查询", EXPECTED_MODEL_REVISION)

    assert captured and captured[0].is_closed is True


def test_compressed_response_is_rejected_without_decoding() -> None:
    stream = ChunkedStream([b"\x1f\x8b\x08\x00decompression-bomb"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, headers={"Content-Encoding": "gzip"})

    with make_client(handler) as client:
        with pytest.raises(QueryEmbeddingResponseError):
            client.embed_query("查询", EXPECTED_MODEL_REVISION)

    assert stream.iterated is False


def test_malformed_json_is_rejected_without_echoing_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>secret body</html>")

    with make_client(handler) as client:
        with pytest.raises(QueryEmbeddingResponseError) as error:
            client.embed_query("查询", EXPECTED_MODEL_REVISION)

    assert "secret body" not in str(error.value)


def test_invalid_utf8_and_deep_nesting_are_rejected_statically() -> None:
    deep = b"[" * 20000 + b"]" * 20000

    def invalid_utf8(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\xff\xfe\x00")

    def nested(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=deep)

    for handler in (invalid_utf8, nested):
        with make_client(handler) as client:
            with pytest.raises(QueryEmbeddingResponseError):
                client.embed_query("查询", EXPECTED_MODEL_REVISION)


def test_nan_and_inf_vectors_are_rejected() -> None:
    for bad in (float("nan"), float("inf")):
        body = success_body("查询", vectors=[[bad] + [0.0] * (EMBEDDING_DIMENSION - 1)])

        def handler(request: httpx.Request, body: bytes = body) -> httpx.Response:
            return httpx.Response(200, content=body)

        with make_client(handler) as client:
            with pytest.raises(QueryEmbeddingResponseError):
                client.embed_query("查询", EXPECTED_MODEL_REVISION)


def test_non_unit_norm_vector_is_rejected() -> None:
    scaled = [value * 2.0 for value in unit_vector("查询")]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=success_body("查询", vectors=[scaled]))

    with make_client(handler) as client:
        with pytest.raises(QueryEmbeddingResponseError):
            client.embed_query("查询", EXPECTED_MODEL_REVISION)


def test_wrong_vector_count_dimension_and_revision_are_rejected() -> None:
    cases = [
        success_body("查询", vectors=[]),
        success_body("查询", vectors=[unit_vector("查询"), unit_vector("x")]),
        success_body("查询", dimension=EMBEDDING_DIMENSION + 1),
        success_body("查询", revision="deadbeef"),
    ]

    for body in cases:
        def handler(request: httpx.Request, body: bytes = body) -> httpx.Response:
            return httpx.Response(200, content=body)

        with make_client(handler) as client:
            with pytest.raises(QueryEmbeddingResponseError):
                client.embed_query("查询", EXPECTED_MODEL_REVISION)


@pytest.mark.parametrize(
    "contract", [None, "bge-zh-query-v2", "bge-zh-query-v1 ", 123, {"v": 1}]
)
def test_missing_or_unknown_wire_query_contract_is_rejected(contract: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=success_body("查询", query_contract=contract))

    with make_client(handler) as client:
        with pytest.raises(QueryEmbeddingResponseError):
            client.embed_query("查询", EXPECTED_MODEL_REVISION)


@pytest.mark.parametrize(
    "bad_counts",
    [[], [1, 2], [True], [0], [MAX_TOKEN_COUNT + 1], [1.0], ["5"], [None]],
)
def test_token_counts_must_be_a_single_integer_in_range(bad_counts: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=success_body("查询", counts=bad_counts))

    with make_client(handler) as client:
        with pytest.raises(QueryEmbeddingResponseError):
            client.embed_query("查询", EXPECTED_MODEL_REVISION)


def test_token_count_boundaries_are_accepted() -> None:
    for count in (1, MAX_TOKEN_COUNT):
        def handler(request: httpx.Request, count: int = count) -> httpx.Response:
            return httpx.Response(200, content=success_body("查询", token_count=count))

        with make_client(handler) as client:
            result = client.embed_query("查询", EXPECTED_MODEL_REVISION)

        assert result.token_count == count


# --------------------------------------------------------------------------------------
# 机密与生命周期
# --------------------------------------------------------------------------------------


def test_secret_never_appears_in_repr_or_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b"denied")

    with make_client(handler) as client:
        assert SECRET_TOKEN not in repr(client)
        with pytest.raises(QueryEmbeddingAuthError) as error:
            client.embed_query("查询", EXPECTED_MODEL_REVISION)

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

    target = "rag_backend.retrieval.query_embedding_client.httpx.HTTPTransport"
    monkeypatch.setattr(target, RecordingHTTPTransport)
    client = QueryEmbeddingClient(base_url=DEFAULT_INFERENCE_BASE_URL, token=SECRET_TOKEN)
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
        client.embed_query("查询", EXPECTED_MODEL_REVISION)

    assert closed == []
    assert client.embed_query("查询", EXPECTED_MODEL_REVISION).token_count == 5


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
        client.embed_query("查询", EXPECTED_MODEL_REVISION)

    assert len(seen) == 1
    assert seen[0].url.host == "inference"


def test_parse_retry_after_variants() -> None:
    assert parse_retry_after("12") == 12.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("soon") is None
    assert parse_retry_after("-3") is None
    future = parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT")
    assert future is not None and math.isfinite(future) and future > 0
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0
