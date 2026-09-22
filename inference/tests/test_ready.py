"""`GET /ready` 契约：只有模型确实可用时才 ready，未就绪返回 503。"""

from fastapi.testclient import TestClient
from support import build_settings

from citemind_inference.app import create_app


def test_ready_reports_ready_model(client: TestClient) -> None:
    response = client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["embedding"] == {
        "ready": True,
        "reason": None,
        "dimension": 512,
        "modelRevision": "7999e1d3359715c523056ef9478215996d62a620",
    }


def test_ready_returns_503_without_loaded_model() -> None:
    app = create_app(build_settings())

    with TestClient(app) as not_ready_client:
        response = not_ready_client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["embedding"]["ready"] is False
    assert body["embedding"]["dimension"] is None
    assert body["embedding"]["modelRevision"] is None


def test_ready_never_returns_vectors(client: TestClient) -> None:
    response = client.get("/ready")

    assert "vectors" not in response.text
