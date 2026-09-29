"""``POST /internal/rerank`` 契约：默认关闭静态 503、鉴权、请求上限、分数与输出校验。

所有用例都注入 :class:`StubReranker`，不加载任何真实权重、不联网。真实 bge-reranker-base 的
正向验收尚未进行（见 Agent Note）。
"""

from __future__ import annotations

from typing import cast

from fastapi.testclient import TestClient
from httpx import Response
from support import TEST_TOKEN, StubEmbedder, StubReranker, build_settings

from citemind_inference.app import create_app
from citemind_inference.config import FROZEN_RERANK_REVISION

RERANK_URL = "/internal/rerank"
AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}


def payload() -> dict[str, object]:
    return {
        "query": "如何申请年假",
        "candidates": [
            {"candidateId": "c1", "text": "年假申请流程正文"},
            {"candidateId": "c2", "text": "报销制度正文"},
        ],
    }


def rerank_app(**overrides: object) -> tuple[TestClient, StubReranker]:
    reranker = StubReranker()
    settings = build_settings(rerank_enabled=True, **overrides)
    app = create_app(
        settings,
        embedder_factory=lambda _resolved: StubEmbedder(),
        reranker_factory=lambda _resolved: reranker,
    )
    return TestClient(app), reranker


# ---------------------------------------------------------------- 默认关闭与鉴权


def test_rerank_is_statically_unavailable_when_disabled(client: TestClient) -> None:
    response = cast(Response, client.post(RERANK_URL, headers=AUTH, json=payload()))

    assert response.status_code == 503
    assert response.json() == {"code": "RERANK_NOT_READY", "message": "rerank 模型尚未加载"}
    # 关闭时绝不返回任何分数或回显候选。
    assert "score" not in response.text
    assert "年假" not in response.text


def test_rerank_requires_bearer_token() -> None:
    test_client, _ = rerank_app()

    with test_client:
        response = cast(Response, test_client.post(RERANK_URL, json=payload()))

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


# ---------------------------------------------------------------- 请求契约


def test_rerank_rejects_unknown_fields() -> None:
    test_client, _ = rerank_app()
    body = payload()
    body["endpoint"] = "http://evil.example"

    with test_client:
        response = cast(Response, test_client.post(RERANK_URL, headers=AUTH, json=body))

    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_REQUEST"


def test_rerank_rejects_duplicate_candidate_ids() -> None:
    test_client, _ = rerank_app()
    body = {
        "query": "问题",
        "candidates": [
            {"candidateId": "dup", "text": "a"},
            {"candidateId": "dup", "text": "b"},
        ],
    }

    with test_client:
        response = cast(Response, test_client.post(RERANK_URL, headers=AUTH, json=body))

    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_REQUEST"


def test_rerank_rejects_too_many_candidates() -> None:
    test_client, _ = rerank_app(rerank_max_candidates=1)

    with test_client:
        response = cast(Response, test_client.post(RERANK_URL, headers=AUTH, json=payload()))

    assert response.status_code == 413
    assert response.json()["code"] == "RERANK_PAYLOAD_TOO_LARGE"


def test_rerank_request_body_limit_is_enforced_before_parsing() -> None:
    test_client, _ = rerank_app(
        rerank_max_candidates=1,
        rerank_max_query_chars=32,
        rerank_max_text_chars=32,
        rerank_max_total_bytes=64,
        rerank_max_request_bytes=64,
    )
    body = {"query": "a" * 400, "candidates": [{"candidateId": "c1", "text": "b"}]}

    with test_client:
        response = cast(Response, test_client.post(RERANK_URL, headers=AUTH, json=body))

    assert response.status_code == 413
    assert response.json()["code"] == "RERANK_PAYLOAD_TOO_LARGE"


# ---------------------------------------------------------------- 成功路径


def test_rerank_returns_scores_in_input_order_with_revision() -> None:
    test_client, reranker = rerank_app()
    body = payload()

    with test_client:
        response = cast(Response, test_client.post(RERANK_URL, headers=AUTH, json=body))

    assert response.status_code == 200
    response_body = response.json()
    assert response_body["modelRevision"] == FROZEN_RERANK_REVISION
    assert [item["candidateId"] for item in response_body["scores"]] == ["c1", "c2"]
    # stub 按文本长度给分，只验证顺序与字段，不声称真实相关性。
    assert [item["score"] for item in response_body["scores"]] == [
        float(len("年假申请流程正文")),
        float(len("报销制度正文")),
    ]
    assert len(reranker.calls) == 1
    assert reranker.calls[0][0] == "如何申请年假"
    assert reranker.calls[0][1] == ["年假申请流程正文", "报销制度正文"]


def test_rerank_capabilities_report_ready_when_enabled() -> None:
    test_client, _ = rerank_app()

    with test_client:
        response = test_client.get("/capabilities")

    assert response.status_code == 200
    rerank = response.json()["rerank"]
    assert rerank["ready"] is True
    assert rerank["reason"] is None


def test_rerank_rejects_non_finite_model_output() -> None:
    reranker = StubReranker(corrupt="nan")
    settings = build_settings(rerank_enabled=True)
    app = create_app(
        settings,
        embedder_factory=lambda _resolved: StubEmbedder(),
        reranker_factory=lambda _resolved: reranker,
    )

    with TestClient(app) as test_client:
        response = cast(Response, test_client.post(RERANK_URL, headers=AUTH, json=payload()))

    assert response.status_code == 500
    assert response.json()["code"] == "RERANK_OUTPUT_INVALID"


def test_rerank_rejects_wrong_score_count() -> None:
    reranker = StubReranker(corrupt="count")
    settings = build_settings(rerank_enabled=True)
    app = create_app(
        settings,
        embedder_factory=lambda _resolved: StubEmbedder(),
        reranker_factory=lambda _resolved: reranker,
    )

    with TestClient(app) as test_client:
        response = cast(Response, test_client.post(RERANK_URL, headers=AUTH, json=payload()))

    assert response.status_code == 500
    assert response.json()["code"] == "RERANK_OUTPUT_INVALID"
