"""`GET /health` 契约：liveness。模型未加载时仍为 200，只如实报告状态。"""

from fastapi.testclient import TestClient
from support import StubEmbedder, build_settings

from citemind_inference.app import create_app
from citemind_inference.config import Settings


def test_health_reports_model_loaded_when_embedder_present(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "inference",
        "modelLoaded": True,
    }


def test_health_stays_live_without_model() -> None:
    app = create_app(build_settings())

    with TestClient(app) as not_ready_client:
        response = not_ready_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "inference",
        "modelLoaded": False,
    }


def test_health_does_not_trigger_model_load() -> None:
    calls: list[Settings] = []

    def factory(settings: Settings) -> StubEmbedder:
        calls.append(settings)
        return StubEmbedder()

    app = create_app(build_settings(), embedder_factory=factory)

    with TestClient(app) as ready_client:
        assert ready_client.get("/health").status_code == 200

    # lifespan 恰好加载一次，健康检查本身不重复加载。
    assert len(calls) == 1
