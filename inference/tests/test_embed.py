"""`POST /internal/embed` 契约：Bearer 鉴权、真实向量、顺序保持与输入上限。"""

from typing import cast

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from support import TEST_TOKEN, StubEmbedder, build_settings

from citemind_inference.app import create_app

EMBED_URL = "/internal/embed"
AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}


def post_texts(
    client: TestClient,
    texts: list[str],
    *,
    kind: str | None = "document",
) -> Response:
    payload: dict[str, object] = {"texts": texts}
    if kind is not None:
        payload["kind"] = kind
    return cast(Response, client.post(EMBED_URL, headers=AUTH, json=payload))


# ---------------------------------------------------------------- 鉴权


def test_embed_without_token_returns_401(client: TestClient) -> None:
    response = client.post(EMBED_URL, json={"texts": ["任意输入"]})

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"
    assert response.headers["www-authenticate"] == "Bearer"


def test_embed_with_wrong_token_returns_401(client: TestClient) -> None:
    response = client.post(
        EMBED_URL, headers={"Authorization": "Bearer wrong-token"}, json={"texts": ["文本"]}
    )

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


def test_embed_with_non_bearer_scheme_returns_401(
    client: TestClient, test_token: str
) -> None:
    response = client.post(
        EMBED_URL, headers={"Authorization": f"Basic {test_token}"}, json={"texts": ["文本"]}
    )

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


def test_embed_with_non_ascii_authorization_returns_401_without_echo(
    client: TestClient, test_token: str
) -> None:
    """非 ASCII raw header 曾让 str compare_digest 抛 TypeError 变成 500；必须 fail-closed。"""

    response = client.post(EMBED_URL, headers={"Authorization": b"Bearer \xe9"})

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"
    assert "Traceback" not in response.text
    assert "Internal Server Error" not in response.text
    # 不回显输入的非 ASCII 值，也不回显正确 token。
    assert "é" not in response.text
    assert test_token not in response.text


# ---------------------------------------------------------------- 成功路径


def test_embed_returns_real_vectors_with_contract_fields(
    client: TestClient,
    embedder: StubEmbedder,
) -> None:
    texts = ["第一段文本", "second text"]

    response = post_texts(client, texts)

    assert response.status_code == 200
    body = response.json()
    assert body["dimension"] == 512
    assert body["modelRevision"] == "7999e1d3359715c523056ef9478215996d62a620"
    assert body["tokenCounts"] == [len(text) + 2 for text in texts]
    assert len(body["vectors"]) == len(texts)
    assert all(len(vector) == 512 for vector in body["vectors"])
    # 每条文本对应真实的非零向量，而不是占位值。
    assert all(any(value != 0.0 for value in vector) for vector in body["vectors"])


def test_embed_preserves_input_order(client: TestClient, embedder: StubEmbedder) -> None:
    texts = ["alpha", "beta", "gamma", "delta"]

    response = post_texts(client, texts)

    body = response.json()
    assert body["vectors"] == [embedder.vector_for(text) for text in texts]
    # 顺序一旦错位，向量就不再等于按文本推算的期望值。
    assert body["vectors"] != [embedder.vector_for(text) for text in reversed(texts)]


def test_embed_defaults_to_document_kind(client: TestClient) -> None:
    response = post_texts(client, ["未显式给 kind"], kind=None)

    assert response.status_code == 200


def test_embed_passes_texts_to_embedder_once(
    client: TestClient, embedder: StubEmbedder
) -> None:
    texts = ["一次", "两次"]

    post_texts(client, texts)

    assert embedder.embed_calls == [texts]


# ---------------------------------------------------------------- 422 与不回显


@pytest.mark.parametrize("kind", ["query", "document_chunk", "QUERY"])
def test_embed_rejects_non_document_kind(client: TestClient, kind: str) -> None:
    response = post_texts(client, ["文本"], kind=kind)

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "INVALID_REQUEST"
    assert body["details"][0]["location"] == "body.kind"


def test_embed_rejects_blank_and_empty_texts(client: TestClient) -> None:
    blank = post_texts(client, ["   \n\t "])
    empty = post_texts(client, [])

    assert blank.status_code == 422
    assert blank.json()["code"] == "INVALID_REQUEST"
    assert empty.status_code == 422
    assert empty.json()["code"] == "INVALID_REQUEST"


def test_schema_422_does_not_echo_raw_body(client: TestClient) -> None:
    sentinel = "SENTINEL-DO-NOT-ECHO-7f3a"

    response = post_texts(client, [sentinel], kind="query")

    assert response.status_code == 422
    assert sentinel not in response.text


def test_type_error_422_does_not_echo_raw_body(client: TestClient) -> None:
    sentinel = "SENTINEL-OBJECT-VALUE-4c1b"

    response = client.post(EMBED_URL, headers=AUTH, json={"texts": [{"secret": sentinel}]})

    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_REQUEST"
    assert sentinel not in response.text
    assert "input" not in response.json()


def test_invalid_json_422_does_not_echo_raw_body(client: TestClient) -> None:
    sentinel = "SENTINEL-BROKEN-JSON-9d2e"

    response = client.post(
        EMBED_URL,
        headers={**AUTH, "Content-Type": "application/json"},
        content=b'{"texts": ["' + sentinel.encode() + b'"',
    )

    assert response.status_code == 422
    assert sentinel not in response.text


# ---------------------------------------------------------------- 输入上限


def test_embed_rejects_batch_over_limit(client: TestClient) -> None:
    settings = build_settings(embedding_max_batch_size=2)
    app = create_app(settings, embedder_factory=lambda _resolved: StubEmbedder())

    with TestClient(app) as limited_client:
        response = post_texts(limited_client, ["a", "b", "c"])

    assert response.status_code == 413
    assert response.json()["code"] == "EMBEDDING_PAYLOAD_TOO_LARGE"


def test_embed_rejects_text_over_char_limit(client: TestClient) -> None:
    settings = build_settings(
        embedding_max_total_bytes=100, embedding_max_chars_per_text=4
    )
    app = create_app(settings, embedder_factory=lambda _resolved: StubEmbedder())

    with TestClient(app) as limited_client:
        response = post_texts(limited_client, ["abcdef"])

    assert response.status_code == 413
    assert response.json()["code"] == "EMBEDDING_PAYLOAD_TOO_LARGE"


def test_embed_rejects_request_over_total_bytes(client: TestClient) -> None:
    settings = build_settings(
        embedding_max_total_bytes=8, embedding_max_chars_per_text=8
    )
    app = create_app(settings, embedder_factory=lambda _resolved: StubEmbedder())

    with TestClient(app) as limited_client:
        response = post_texts(limited_client, ["abcdefgh", "abcdefgh"])

    assert response.status_code == 413
    assert response.json()["code"] == "EMBEDDING_PAYLOAD_TOO_LARGE"


def test_embed_rejects_token_count_over_model_limit(client: TestClient) -> None:
    # stub 的计数是“字符数 + 2”，510 字符恰好 512 token，511 字符即越界。
    allowed = post_texts(client, ["a" * 510])
    rejected = post_texts(client, ["a" * 511])

    assert allowed.status_code == 200
    assert allowed.json()["tokenCounts"] == [512]
    assert rejected.status_code == 422
    body = rejected.json()
    assert body["code"] == "EMBEDDING_INPUT_TOO_LONG"
    assert "本服务不截断" in body["message"]


def test_embed_rejects_request_over_the_padded_token_budget(client: TestClient) -> None:
    """预算按 padding 后的真实位置数计算，而不是未 padding 的 token 合计。"""

    settings = build_settings(
        embedding_max_batch_size=2,
        embedding_max_tokens_per_text=512,
        embedding_max_total_tokens=512,
    )
    app = create_app(settings, embedder_factory=lambda _resolved: StubEmbedder())

    with TestClient(app) as limited_client:
        response = post_texts(limited_client, ["a" * 510, "b" * 510])

    assert response.status_code == 413
    assert response.json()["code"] == "EMBEDDING_PAYLOAD_TOO_LARGE"


def test_embed_token_limit_never_truncates(client: TestClient, embedder: StubEmbedder) -> None:
    post_texts(client, ["a" * 511])

    # 越界请求在编码前就被拒绝，绝不截断后偷偷编码。
    assert embedder.embed_calls == []


# ---------------------------------------------------------------- 就绪


def test_embed_without_loaded_model_returns_503() -> None:
    app = create_app(build_settings())

    with TestClient(app) as not_ready_client:
        response = post_texts(not_ready_client, ["文本"])

    assert response.status_code == 503
    assert response.json() == {
        "code": "EMBEDDING_NOT_READY",
        "message": "embedding 模型尚未加载",
    }


def test_rerank_route_is_not_implemented(client: TestClient, test_token: str) -> None:
    response = client.post(
        "/internal/rerank",
        headers={"Authorization": f"Bearer {test_token}"},
    )

    assert response.status_code == 404
