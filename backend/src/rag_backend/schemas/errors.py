"""统一错误响应体：code、message、requestId、details，字段 camelCase。"""

from __future__ import annotations

from typing import Any

from rag_backend.schemas.base import CamelModel


class ErrorResponse(CamelModel):
    """所有非 2xx 响应共用的错误体。"""

    code: str
    message: str
    request_id: str
    details: Any = None
