"""inference 测试夹具：隔离环境变量，并默认注入 stub 编码器（不加载模型）。"""

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from support import TEST_TOKEN, StubEmbedder, build_settings

from citemind_inference.app import create_app
from citemind_inference.config import Settings


@pytest.fixture(autouse=True)
def clear_citemind_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """清空所有 CITEMIND_* 环境变量，确保 Settings(_env_file=None) 的断言不受外部干扰。"""

    for name in list(os.environ):
        if name.startswith("CITEMIND_"):
            monkeypatch.delenv(name, raising=False)


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
