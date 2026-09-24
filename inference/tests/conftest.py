"""inference 测试夹具：隔离本服务环境变量，并默认注入 stub 编码器（不加载模型）。"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from support import TEST_TOKEN, StubEmbedder, build_settings

from citemind_inference.app import create_app
from citemind_inference.config import Settings

# 本服务字段对应的环境变量名（env_prefix 已改为空）。逐项清理并让 monkeypatch 精确恢复，
# 同时覆盖旧 CITEMIND_ 前缀形式；不触碰第三方 HF_*/TOKENIZERS 等变量。
SERVICE_ENV_VARS = (
    "ENVIRONMENT",
    "INFERENCE_TOKEN",
    "EMBEDDING_MODEL_PATH",
    "EMBEDDING_MODEL_REVISION",
    "EMBEDDING_MAX_BATCH_SIZE",
    "EMBEDDING_MAX_TOTAL_BYTES",
    "EMBEDDING_MAX_REQUEST_BYTES",
    "EMBEDDING_MAX_CHARS_PER_TEXT",
    "EMBEDDING_MAX_TOKENS_PER_TEXT",
    "EMBEDDING_MAX_TOTAL_TOKENS",
    "EMBEDDING_MAX_CONCURRENCY",
    "EMBEDDING_QUEUE_DEPTH",
    "EMBEDDING_QUEUE_WAIT_SECONDS",
    "EMBEDDING_TORCH_THREADS",
)


@pytest.fixture(autouse=True)
def clear_inference_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """清理本服务字段环境变量及旧 CITEMIND_ 前缀形式。

    确保 ``Settings(_env_file=None)`` 断言不受外部干扰。
    """

    for name in SERVICE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"CITEMIND_{name}", raising=False)


@pytest.fixture
def test_token() -> str:
    return TEST_TOKEN


@pytest.fixture
def settings() -> Settings:
    return build_settings()


@pytest.fixture
def embedder() -> StubEmbedder:
    return StubEmbedder()


@pytest.fixture
def client(settings: Settings, embedder: StubEmbedder) -> Iterator[TestClient]:
    app = create_app(settings, embedder_factory=lambda _resolved: embedder)
    with TestClient(app) as test_client:
        yield test_client
