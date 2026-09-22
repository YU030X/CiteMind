"""输出契约回归：非有限值、全零、错误维度、错误条数与错误范数都必须 500 且不带向量。

NaN/Inf 在 JSON 里会序列化成 null，如果只校验维度就会以 200 返回“看起来正常”的响应。
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from support import TEST_TOKEN, StubEmbedder, build_settings

from citemind_inference.app import InferenceError, create_app, validate_vectors

EMBED_URL = "/internal/embed"
AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}


def embed_with(stub: StubEmbedder) -> tuple[int, dict[str, Any]]:
    """跑一次真实 HTTP 请求，返回 (状态码, 解析后的响应体)。"""

    app = create_app(build_settings(), embedder_factory=lambda _resolved: stub)
    with TestClient(app) as client:
        response = client.post(EMBED_URL, headers=AUTH, json={"texts": ["输出契约"]})
        return response.status_code, response.json()


def test_valid_stub_output_passes_the_contract() -> None:
    status, body = embed_with(StubEmbedder())

    assert status == 200
    assert len(body["vectors"]) == 1
    assert len(body["vectors"][0]) == 512


@pytest.mark.parametrize("corrupt", ["nan", "inf", "negative_inf"])
def test_non_finite_output_is_rejected(corrupt: str) -> None:
    status, body = embed_with(StubEmbedder(corrupt=corrupt))

    assert status == 500
    assert body["code"] == "EMBEDDING_OUTPUT_INVALID"
    # 失败响应绝不能带向量（NaN/Inf 会变成 null 占位）。
    assert set(body) == {"code", "message"}
    assert "vectors" not in body


def test_all_zero_output_is_rejected() -> None:
    status, body = embed_with(StubEmbedder(corrupt="zeros"))

    assert status == 500
    assert body["code"] == "EMBEDDING_OUTPUT_INVALID"
    assert "vectors" not in body


def test_wrong_dimension_output_is_rejected() -> None:
    status, body = embed_with(StubEmbedder(corrupt="short"))

    assert status == 500
    assert body["code"] == "EMBEDDING_OUTPUT_INVALID"
    assert "vectors" not in body


def test_wrong_vector_count_is_rejected() -> None:
    status, body = embed_with(StubEmbedder(corrupt="count"))

    assert status == 500
    assert body["code"] == "EMBEDDING_OUTPUT_INVALID"
    assert "vectors" not in body


def test_non_unit_norm_output_is_rejected() -> None:
    status, body = embed_with(StubEmbedder(corrupt="scaled"))

    assert status == 500
    assert body["code"] == "EMBEDDING_OUTPUT_INVALID"
    assert "vectors" not in body


def test_vector_validation_accepts_float32_rounding_but_rejects_real_deviation() -> None:
    dimension = 512
    unit = [1.0] + [0.0] * (dimension - 1)

    validate_vectors([unit], dimension=dimension, expected_count=1)

    # float32 归一化的舍入量级必须放行。
    rounded = [[value * (1.0 + 1e-6) for value in unit]]
    validate_vectors(rounded, dimension=dimension, expected_count=1)

    # 明显偏离单位范数必须拒绝。
    with pytest.raises(InferenceError):
        validate_vectors(
            [[value * 1.01 for value in unit]], dimension=dimension, expected_count=1
        )
