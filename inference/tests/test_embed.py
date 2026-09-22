"""`POST /internal/embed` 契约：必须 Bearer 鉴权，未加载模型时返回机器可读 503。"""

from fastapi.testclient import TestClient

EMBED_URL = "/internal/embed"


def test_embed_without_token_returns_401(client: TestClient) -> None:
    response = client.post(EMBED_URL)

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"
    assert response.headers["www-authenticate"] == "Bearer"


def test_embed_with_wrong_token_returns_401(client: TestClient) -> None:
    response = client.post(EMBED_URL, headers={"Authorization": "Bearer wrong-token"})

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


def test_embed_with_non_bearer_scheme_returns_401(
    client: TestClient, test_token: str
) -> None:
    response = client.post(EMBED_URL, headers={"Authorization": f"Basic {test_token}"})

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


def test_embed_with_non_ascii_authorization_returns_401_without_echo(
    client: TestClient, test_token: str
) -> None:
    """非 ASCII raw header 曾让 str compare_digest 损 TypeError 变成 500；必须 fail-closed。"""

    response = client.post(EMBED_URL, headers={"Authorization": b"Bearer \xe9"})

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"
    assert "Traceback" not in response.text
    assert "Internal Server Error" not in response.text
    # 不回显输入的非 ASCII 值，也不回显正确 token。
    assert "é" not in response.text
    assert test_token not in response.text


def test_embed_with_valid_token_returns_machine_readable_503(
    client: TestClient, test_token: str
) -> None:
    response = client.post(
        EMBED_URL,
        headers={"Authorization": f"Bearer {test_token}"},
        json={"texts": ["任意输入都不会产生向量"]},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "EMBEDDING_NOT_READY"
    # 503 响应体只含 code 与 message，绝不出现任何向量字段或数值数组。
    assert set(response.json()) == {"code", "message"}
    assert "vectors" not in response.text


def test_rerank_route_is_not_implemented(client: TestClient, test_token: str) -> None:
    response = client.post(
        "/internal/rerank",
        headers={"Authorization": f"Bearer {test_token}"},
    )

    assert response.status_code == 404
