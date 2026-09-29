"""统一错误体与异常处理。

所有非 2xx 响应体都是 ``code``/``message``/``requestId``/``details``（camelCase），
并由 ``X-Request-ID`` 透传或生成关联 ID；内部异常不向客户端泄露堆栈或数据库细节。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from rag_backend.auth.ratelimit import RateLimiterUnavailable, RateLimitExceeded
from rag_backend.schemas.errors import ErrorResponse

logger = logging.getLogger("rag_backend.api")

REQUEST_ID_HEADER = "X-Request-ID"
MAX_REQUEST_ID_LENGTH = 128

# Starlette 抛出的 HTTPException 默认没有业务 code；这里给出稳定的映射。
HTTP_STATUS_CODES: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "AUTH_REQUIRED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    412: "PRECONDITION_FAILED",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    422: "VALIDATION_ERROR",
    429: "TOO_MANY_REQUESTS",
    500: "INTERNAL_ERROR",
    503: "SERVICE_UNAVAILABLE",
}

# 已定义的业务错误码，供路由与测试复用。
CODE_AUTH_REQUIRED = "AUTH_REQUIRED"
CODE_AUTH_INVALID_CREDENTIALS = "AUTH_INVALID_CREDENTIALS"
CODE_AUTH_RATE_LIMITED = "AUTH_RATE_LIMITED"
CODE_AUTH_DEPENDENCY_UNAVAILABLE = "AUTH_DEPENDENCY_UNAVAILABLE"
CODE_CSRF_INVALID = "CSRF_INVALID"
CODE_ORIGIN_NOT_ALLOWED = "ORIGIN_NOT_ALLOWED"
CODE_FORBIDDEN = "FORBIDDEN"
CODE_KNOWLEDGE_BASE_NOT_FOUND = "KNOWLEDGE_BASE_NOT_FOUND"
CODE_KB_MEMBERS_INVALID = "KB_MEMBERS_INVALID"
CODE_KB_MEMBER_USER_INVALID = "KB_MEMBER_USER_INVALID"
CODE_KB_MEMBER_OWNER_DISABLED = "KB_MEMBER_OWNER_DISABLED"
CODE_LAST_OWNER_REQUIRED = "LAST_OWNER_REQUIRED"
CODE_DOCUMENT_TOO_LARGE = "DOCUMENT_TOO_LARGE"
CODE_DOCUMENT_EMPTY = "DOCUMENT_EMPTY"
CODE_DOCUMENT_NOT_TEXT = "DOCUMENT_NOT_TEXT"
CODE_DOCUMENT_NOT_PDF = "DOCUMENT_NOT_PDF"
CODE_DOCUMENT_NOT_DOCX = "DOCUMENT_NOT_DOCX"
CODE_DOCUMENT_DOCX_UNSUPPORTED = "DOCUMENT_DOCX_UNSUPPORTED"
CODE_DOCUMENT_TITLE_INVALID = "DOCUMENT_TITLE_INVALID"
CODE_DOCUMENT_NOT_FOUND = "DOCUMENT_NOT_FOUND"
CODE_DOCUMENT_DELETED = "DOCUMENT_DELETED"
CODE_DOCUMENT_VERSION_CONFLICT = "DOCUMENT_VERSION_CONFLICT"
CODE_DOCUMENT_ACL_INVALID = "DOCUMENT_ACL_INVALID"
CODE_DOCUMENT_ACL_MEMBER_INVALID = "DOCUMENT_ACL_MEMBER_INVALID"
CODE_DOCUMENT_CONTENT_UNAVAILABLE = "DOCUMENT_CONTENT_UNAVAILABLE"
CODE_UNSUPPORTED_DOCUMENT_TYPE = "UNSUPPORTED_DOCUMENT_TYPE"
CODE_IDEMPOTENCY_KEY_INVALID = "IDEMPOTENCY_KEY_INVALID"
CODE_IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
CODE_UPLOAD_MALFORMED = "UPLOAD_MALFORMED"
CODE_INGESTION_ERROR = "INGESTION_ERROR"
CODE_RETRIEVAL_UNAVAILABLE = "RETRIEVAL_UNAVAILABLE"
CODE_RETRIEVAL_QUERY_INVALID = "RETRIEVAL_QUERY_INVALID"
CODE_RETRIEVAL_PROFILE_CONFLICT = "RETRIEVAL_PROFILE_CONFLICT"
CODE_RETRIEVAL_ANALYZER_MISMATCH = "RETRIEVAL_ANALYZER_MISMATCH"
CODE_CONVERSATION_NOT_FOUND = "CONVERSATION_NOT_FOUND"
CODE_CITATION_NOT_FOUND = "CITATION_NOT_FOUND"
CODE_CONVERSATION_QUESTION_TOO_LONG = "CONVERSATION_QUESTION_TOO_LONG"
CODE_LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
CODE_GENERATION_FAILED = "GENERATION_FAILED"
CODE_GENERATION_OPTION_UNSUPPORTED = "GENERATION_OPTION_UNSUPPORTED"
CODE_GENERATION_INVALID_RESPONSE = "GENERATION_INVALID_RESPONSE"
CODE_CONVERSATION_SOURCES_CHANGED = "CONVERSATION_SOURCES_CHANGED"
CODE_VALIDATION_ERROR = "VALIDATION_ERROR"
CODE_INTERNAL_ERROR = "INTERNAL_ERROR"

# pydantic 校验错误条目可能含 ``input``（原样回显请求体，包括登录密码）、``ctx`` 与 ``url``；
# 对外只保留类型、位置与消息，绝不把请求输入反射进错误响应。
REDACTED_VALIDATION_ERROR_KEYS = frozenset({"input", "ctx", "url"})


def sanitize_validation_errors(errors: Sequence[Any]) -> list[Any]:
    """剔除校验错误条目里会回显请求体的字段。"""

    return [
        {
            key: value
            for key, value in entry.items()
            if key not in REDACTED_VALIDATION_ERROR_KEYS
        }
        for entry in errors
    ]


class ApiError(Exception):
    """带稳定业务码的应用错误，由统一处理器转成错误体。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def sanitize_request_id(value: str | None) -> str | None:
    """只接受可打印 ASCII 且长度受限的客户端 request id，其余忽略。"""

    if not value:
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_REQUEST_ID_LENGTH:
        return None
    if not all(33 <= ord(character) <= 126 for character in candidate):
        return None
    return candidate


def resolve_request_id(request: Request) -> str:
    """取请求上已确定的 request id；缺失时现场生成。"""

    existing = getattr(request.state, "request_id", None)
    if isinstance(existing, str) and existing:
        return existing
    generated = uuid.uuid4().hex
    request.state.request_id = generated
    return generated


def error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """构造统一错误响应。"""

    body = ErrorResponse(
        code=code,
        message=message,
        request_id=resolve_request_id(request),
        details=details,
    )
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(body.model_dump(by_alias=True)),
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """注册统一异常处理器；顺序不影响具体类型优先匹配。"""

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, error: ApiError) -> JSONResponse:
        return error_response(
            request,
            status_code=error.status_code,
            code=error.code,
            message=error.message,
            details=error.details,
        )

    @app.exception_handler(RateLimitExceeded)
    async def handle_rate_limited(
        request: Request, error: RateLimitExceeded
    ) -> JSONResponse:
        return error_response(
            request,
            status_code=429,
            code=CODE_AUTH_RATE_LIMITED,
            message="登录尝试过于频繁，请稍后再试",
            details={"retryAfter": error.retry_after},
            headers={"Retry-After": str(error.retry_after)},
        )

    @app.exception_handler(RateLimiterUnavailable)
    async def handle_rate_limiter_unavailable(
        request: Request, error: RateLimiterUnavailable
    ) -> JSONResponse:
        return error_response(
            request,
            status_code=503,
            code=CODE_AUTH_DEPENDENCY_UNAVAILABLE,
            message="登录服务暂时不可用",
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        return error_response(
            request,
            status_code=422,
            code=CODE_VALIDATION_ERROR,
            message="请求参数校验失败",
            details=jsonable_encoder(sanitize_validation_errors(error.errors())),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, error: StarletteHTTPException
    ) -> JSONResponse:
        message = error.detail if isinstance(error.detail, str) else "请求失败"
        return error_response(
            request,
            status_code=error.status_code,
            code=HTTP_STATUS_CODES.get(error.status_code, "HTTP_ERROR"),
            message=message,
            details=None if isinstance(error.detail, str) else error.detail,
            headers=error.headers,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, error: Exception) -> JSONResponse:
        logger.exception("未处理的 API 异常 path=%s", request.url.path, exc_info=error)
        return error_response(
            request,
            status_code=500,
            code=CODE_INTERNAL_ERROR,
            message="服务器内部错误",
        )
