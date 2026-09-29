"""API 侧受限重排客户端的纯逻辑测试：只注入 ``httpx.MockTransport``，绝不真实联网。

覆盖本地长度/字节 failfast、成功响应契约（revision、集合完全、有限分数）、以及所有失败都收敛
为统一的可降级异常 ``RerankUnavailableError``（非 2xx、非法 JSON、重复/缺失 candidateId、非
finite、响应超限、压缩编码）。真实 inference 端到端与真实 bge-reranker-base 不在本文件声称。
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from rag_backend.retrieval.rerank_client import (
    EXPECTED_RERANK_MODEL_REVISION,
    MAX_CANDIDATES,
    MAX_QUERY_CHARS,
    MAX_TEXT_CHARS,
    RerankClient,
    RerankInput,
    RerankScore,
    RerankUnavailableError,
)

TOKEN = "internal-rerank-unit-test"
Handler = Callable[[httpx.Request], httpx.Response]


def candidates(count: int = 2) -> list[RerankInput]:
    return [RerankInput(candidate_id=f"c{index}", text=f"text-{index}") for index in range(count)]


def success_body(
    items: list[dict[str, object]] | None = None,
    *,
    revision: str = EXPECTED_RERANK_MODEL_REVISION,
) -> bytes:
    if items is None:
        items = [{"candidateId": "c0", "score": 0.5}, {"candidateId": "c1", "score": -1.25}]
    return json.dumps({"scores": items, "modelRevision": revision}).encode("utf-8")


def make_client(handler: Handler) -> RerankClient:
    return RerankClient(
        token=TOKEN,
        base_url="http://inference:9000",
        timeout_seconds=1.0,
        transport=httpx.MockTransport(handler),
    )


def test_success_returns_scores_in_response_order() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=success_body())

    client = make_client(handler)
    try:
        scores = client.rerank("query", candidates())
    finally:
        client.close()

    assert scores == [
        RerankScore(candidate_id="c0", score=0.5),
        RerankScore(candidate_id="c1", score=-1.25),
    ]
    assert seen[0].url.path == "/internal/rerank"
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert seen[0].headers["accept-encoding"] == "identity"


def test_max_candidates_constant_is_frozen() -> None:
    assert MAX_CANDIDATES == 10


@pytest.mark.parametrize(
    "query, inputs",
    [
        ("   ", candidates()),
        ("q" * (MAX_QUERY_CHARS + 1), candidates()),
        ("query", []),
        ("query", candidates(MAX_CANDIDATES + 1)),
        ("query", [RerankInput(candidate_id="c0", text="x" * (MAX_TEXT_CHARS + 1))]),
        (
            "query",
            [RerankInput(candidate_id="dup", text="a"), RerankInput(candidate_id="dup", text="b")],
        ),
    ],
)
def test_local_input_violations_degrade_without_network(
    query: str, inputs: list[RerankInput]
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不应被调用
        raise AssertionError("本地校验失败时不得发请求")

    client = make_client(handler)
    try:
        with pytest.raises(RerankUnavailableError):
            client.rerank(query, inputs)
    finally:
        client.close()


@pytest.mark.parametrize("status", [400, 404, 413, 422, 500, 503])
def test_non_2xx_is_degradable(status: int) -> None:
    client = make_client(lambda _request: httpx.Response(status, content=b"{}"))
    try:
        with pytest.raises(RerankUnavailableError):
            client.rerank("query", candidates())
    finally:
        client.close()


def test_foreign_content_encoding_is_degradable() -> None:
    client = make_client(
        lambda _request: httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, content=b"x"
        )
    )
    try:
        with pytest.raises(RerankUnavailableError):
            client.rerank("query", candidates())
    finally:
        client.close()


def test_invalid_json_is_degradable() -> None:
    client = make_client(lambda _request: httpx.Response(200, content=b"not json"))
    try:
        with pytest.raises(RerankUnavailableError):
            client.rerank("query", candidates())
    finally:
        client.close()


def test_revision_mismatch_is_degradable() -> None:
    client = make_client(
        lambda _request: httpx.Response(200, content=success_body(revision="0" * 40))
    )
    try:
        with pytest.raises(RerankUnavailableError):
            client.rerank("query", candidates())
    finally:
        client.close()


@pytest.mark.parametrize(
    "items",
    [
        [{"candidateId": "c0", "score": 0.1}],
        [
            {"candidateId": "c0", "score": 0.1},
            {"candidateId": "c0", "score": 0.2},
        ],
        [
            {"candidateId": "c0", "score": 0.1},
            {"candidateId": "unknown", "score": 0.2},
        ],
        [
            {"candidateId": "c0", "score": 0.1},
            {"candidateId": "c1", "score": float("nan")},
        ],
        [
            {"candidateId": "c0", "score": 0.1},
            {"candidateId": "c1", "score": "high"},
        ],
    ],
)
def test_invalid_score_sets_are_degradable(items: list[dict[str, object]]) -> None:
    client = make_client(lambda _request: httpx.Response(200, content=success_body(items)))
    try:
        with pytest.raises(RerankUnavailableError):
            client.rerank("query", candidates())
    finally:
        client.close()


def test_response_over_declared_limit_is_degradable() -> None:
    from rag_backend.retrieval.rerank_client import MAX_RESPONSE_BODY_BYTES

    # Content-Length 声明超过上限时早拒；真实流式读取上限由共享读取器本模块外用例覆盖。
    response = httpx.Response(
        200,
        headers={"Content-Length": str(MAX_RESPONSE_BODY_BYTES + 1)},
        content=success_body(),
    )
    client = make_client(lambda _request: response)
    try:
        with pytest.raises(RerankUnavailableError):
            client.rerank("query", candidates())
    finally:
        client.close()
