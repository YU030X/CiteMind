"""`GET /health` 契约：如实报告 inference 进程与模型未加载状态。"""

from fastapi.testclient import TestClient


def test_health_reports_inference_and_model_not_loaded(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "inference",
        "modelLoaded": False,
    }
