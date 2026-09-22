"""inference 测试夹具：显式隔离环境变量与 .env，并使用固定测试 token。"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from citemind_inference.app import create_app
from citemind_inference.config import Settings

TEST_TOKEN = "test-inference-token"


@pytest.fixture(autouse=True)
def clear_citemind_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """清空相关环境变量，确保 Settings(_env_file=None) 的断言不受外部环境干扰。"""

    for name in ("CITEMIND_ENVIRONMENT", "CITEMIND_INFERENCE_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def test_token() -> str:
    return TEST_TOKEN


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="test",
        inference_token=SecretStr(TEST_TOKEN),
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client
