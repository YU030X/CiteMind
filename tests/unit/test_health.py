from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from rag_backend.app import create_app
from rag_backend.config import Settings


def make_settings(**overrides: Any) -> Settings:
    """构造不读取仓库根 .env 的配置，避免测试受尚未迁移的旧变量影响。"""

    values: dict[str, Any] = {"_env_file": None}
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_health_reports_api_environment() -> None:
    transport = ASGITransport(app=create_app(make_settings(environment="test")))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "api",
        "environment": "test",
    }


@pytest.mark.anyio
async def test_openapi_is_available() -> None:
    transport = ASGITransport(app=create_app(make_settings(environment="test")))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"]["title"] == "CiteMind API"
