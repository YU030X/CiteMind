"""`GET /capabilities` 契约：embedding/rerank 均未就绪，且不暴露假维度或假修订。"""

from fastapi.testclient import TestClient


def test_capabilities_report_embedding_not_ready_without_fake_values(
    client: TestClient,
) -> None:
    response = client.get("/capabilities")

    assert response.status_code == 200
    body = response.json()
    embedding = body["embedding"]
    assert embedding["ready"] is False
    assert isinstance(embedding["reason"], str) and embedding["reason"]
    assert embedding["dimension"] is None
    assert embedding["modelRevision"] is None
    assert body["rerank"]["ready"] is False
    assert "vectors" not in response.text
