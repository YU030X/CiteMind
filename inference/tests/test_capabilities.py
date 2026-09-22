"""`GET /capabilities` 契约：如实报告 embedding 与 rerank 的就绪状态。"""

from fastapi.testclient import TestClient
from support import build_settings

from citemind_inference.app import create_app


def test_capabilities_report_ready_embedding_with_frozen_contract(
    client: TestClient,
) -> None:
    response = client.get("/capabilities")

    assert response.status_code == 200
    body = response.json()
    assert body["embedding"] == {
        "ready": True,
        "reason": None,
        "dimension": 512,
        "modelRevision": "7999e1d3359715c523056ef9478215996d62a620",
    }
    # rerank 仍未实现，不得报告为就绪。
    assert body["rerank"]["ready"] is False
    assert isinstance(body["rerank"]["reason"], str) and body["rerank"]["reason"]
    assert "vectors" not in response.text


def test_capabilities_report_not_ready_without_fake_values() -> None:
    app = create_app(build_settings())

    with TestClient(app) as not_ready_client:
        response = not_ready_client.get("/capabilities")

    assert response.status_code == 200
    embedding = response.json()["embedding"]
    assert embedding["ready"] is False
    assert isinstance(embedding["reason"], str) and embedding["reason"]
    assert embedding["dimension"] is None
    assert embedding["modelRevision"] is None
