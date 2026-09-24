"""统一错误体与 request id 处理的纯逻辑测试。"""

import json
from typing import Any

from rag_backend.api.errors import (
    HTTP_STATUS_CODES,
    REQUEST_ID_HEADER,
    ApiError,
    error_response,
    sanitize_request_id,
    sanitize_validation_errors,
)
from starlette.requests import Request


def make_request(*, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/me",
        "headers": headers or [],
        "state": {},
    }
    return Request(scope)


def test_sanitize_request_id_accepts_safe_values() -> None:
    assert sanitize_request_id("abc-123") == "abc-123"
    assert sanitize_request_id("  spaced  ") == "spaced"


def test_sanitize_request_id_rejects_unsafe_values() -> None:
    assert sanitize_request_id(None) is None
    assert sanitize_request_id("") is None
    assert sanitize_request_id("a" * 129) is None
    assert sanitize_request_id("bad\nvalue") is None
    assert sanitize_request_id("bad\x7fvalue") is None


def test_error_response_uses_uniform_body_and_request_id() -> None:
    request = make_request(headers=[(REQUEST_ID_HEADER.lower().encode(), b"given-id")])
    request.state.request_id = sanitize_request_id("given-id")

    response = error_response(
        request,
        status_code=401,
        code="AUTH_REQUIRED",
        message="需要登录",
        details={"reason": "missing"},
    )

    assert response.status_code == 401
    payload = json.loads(bytes(response.body))
    assert payload == {
        "code": "AUTH_REQUIRED",
        "message": "需要登录",
        "requestId": "given-id",
        "details": {"reason": "missing"},
    }


def test_error_response_generates_request_id_when_absent() -> None:
    payload = json.loads(
        bytes(
            error_response(
                make_request(),
                status_code=403,
                code="CSRF_INVALID",
                message="CSRF 校验失败",
            ).body
        )
    )

    assert payload["requestId"]
    assert payload["details"] is None


def test_api_error_carries_status_code_and_code() -> None:
    error = ApiError(429, "AUTH_RATE_LIMITED", "太快了", details={"retryAfter": 5})

    assert error.status_code == 429
    assert error.code == "AUTH_RATE_LIMITED"
    assert error.details == {"retryAfter": 5}


def test_http_status_codes_cover_auth_and_validation() -> None:
    assert HTTP_STATUS_CODES[401] == "AUTH_REQUIRED"
    assert HTTP_STATUS_CODES[403] == "FORBIDDEN"
    assert HTTP_STATUS_CODES[422] == "VALIDATION_ERROR"
    assert HTTP_STATUS_CODES[429] == "TOO_MANY_REQUESTS"


def test_sanitize_validation_errors_drops_reflected_input() -> None:
    errors = [
        {
            "type": "string_too_long",
            "loc": ("body", "password"),
            "msg": "String should have at most 1024 characters",
            "input": "secret-value",
            "ctx": {"max_length": 1024},
            "url": "https://errors.pydantic.dev/2.0/v/string_too_long",
        }
    ]

    sanitized = sanitize_validation_errors(errors)

    assert sanitized == [
        {
            "type": "string_too_long",
            "loc": ("body", "password"),
            "msg": "String should have at most 1024 characters",
        }
    ]
    assert "secret-value" not in json.dumps(sanitized)
