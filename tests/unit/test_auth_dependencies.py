"""认证依赖的纯逻辑测试：Origin 解析与白名单校验。"""

from typing import Any

import pytest
from rag_backend.api.errors import ApiError
from rag_backend.auth.dependencies import enforce_allowed_origin, request_origin
from rag_backend.config import Settings
from starlette.requests import Request

ALLOWED_ORIGINS = "http://127.0.0.1,https://kb.example.com"


def make_request(*, origin: str | None = None, referer: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if referer is not None:
        headers.append((b"referer", referer.encode()))
    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/auth/login",
        "headers": headers,
        "state": {},
    }
    return Request(scope)


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "allowed_origins": ALLOWED_ORIGINS,
    }
    values.update(overrides)
    return Settings(**values)


def test_request_origin_prefers_origin_header() -> None:
    request = make_request(origin="https://kb.example.com", referer="https://other/x")

    assert request_origin(request) == "https://kb.example.com"


def test_request_origin_falls_back_to_referer_netloc() -> None:
    request = make_request(referer="https://kb.example.com/page?q=1")

    assert request_origin(request) == "https://kb.example.com"


def test_request_origin_is_none_without_headers() -> None:
    assert request_origin(make_request()) is None


def test_request_origin_ignores_malformed_referer() -> None:
    # 非法 IPv6 主机让 urlsplit 抛 ValueError；不可信输入必须当作“来源未知”。
    assert request_origin(make_request(referer="http://[::1")) is None


def test_enforce_allowed_origin_accepts_whitelisted_origin() -> None:
    enforce_allowed_origin(make_request(origin="https://kb.example.com"), settings())


def test_enforce_allowed_origin_rejects_missing_origin() -> None:
    with pytest.raises(ApiError) as error:
        enforce_allowed_origin(make_request(), settings())

    assert error.value.status_code == 403
    assert error.value.code == "ORIGIN_NOT_ALLOWED"


def test_enforce_allowed_origin_rejects_foreign_origin() -> None:
    with pytest.raises(ApiError) as error:
        enforce_allowed_origin(make_request(origin="https://evil.example"), settings())

    assert error.value.code == "ORIGIN_NOT_ALLOWED"


def test_enforce_allowed_origin_rejects_invalid_origin() -> None:
    with pytest.raises(ApiError) as error:
        enforce_allowed_origin(make_request(origin="not-a-url"), settings())

    assert error.value.code == "ORIGIN_NOT_ALLOWED"


def test_enforce_allowed_origin_rejects_malformed_referer() -> None:
    with pytest.raises(ApiError) as error:
        enforce_allowed_origin(make_request(referer="http://[::1"), settings())

    assert error.value.status_code == 403
    assert error.value.code == "ORIGIN_NOT_ALLOWED"
