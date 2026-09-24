"""文档上传路由：``POST /api/v1/knowledge-bases/{kb_id}/documents``（multipart）。

关键顺序约束：本端点**不声明** ``UploadFile``/``Form`` 参数。FastAPI 在含表单/文件参数的
端点上会先 ``await request.form()`` 再求解依赖（``fastapi/routing.py``），那样未授权请求
也会先把正文落盘。这里只注入 ``Request``、路径参数、``Header`` 与鉴权/CSRF/体积依赖，
等 KB ``EDITOR`` 角色、Origin、CSRF 与体积上限都通过后，才在处理器内调用
``await request.form()``。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from python_multipart.exceptions import MultipartParseError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException
from starlette.types import Message, Receive

from rag_backend.api.errors import (
    CODE_DOCUMENT_EMPTY,
    CODE_DOCUMENT_NOT_TEXT,
    CODE_DOCUMENT_TITLE_INVALID,
    CODE_DOCUMENT_TOO_LARGE,
    CODE_IDEMPOTENCY_KEY_INVALID,
    CODE_IDEMPOTENCY_KEY_REUSED,
    CODE_INGESTION_ERROR,
    CODE_UNSUPPORTED_DOCUMENT_TYPE,
    CODE_UPLOAD_MALFORMED,
    ApiError,
)
from rag_backend.auth.dependencies import (
    enforce_allowed_origin,
    require_csrf,
    require_kb_role,
)
from rag_backend.config import Settings
from rag_backend.database import get_database_session
from rag_backend.ingestion import service as ingestion_service
from rag_backend.ingestion.errors import (
    DocumentEmpty,
    DocumentNotText,
    DocumentTooLarge,
    IdempotencyConflict,
    IdempotencyKeyInvalid,
    IngestionError,
    TitleInvalid,
    UnsupportedDocumentType,
)
from rag_backend.ingestion.storage import DocumentBlobStore
from rag_backend.ingestion.validation import (
    MAX_MARKDOWN_BYTES,
    MAX_TITLE_LENGTH,
    validate_markdown_filename,
)
from rag_backend.knowledge.roles import KbRole
from rag_backend.knowledge.service import KbAccess
from rag_backend.schemas.documents import DocumentUploadResponse

router = APIRouter(prefix="/api/v1", tags=["documents"])

require_editor = require_kb_role(KbRole.EDITOR)

# multipart 边界、头部与 title 字段的合理余量；文件本身仍按 MAX_MARKDOWN_BYTES 精确判定。
MAX_MULTIPART_OVERHEAD_BYTES = 64 * 1024
MAX_UPLOAD_REQUEST_BYTES = MAX_MARKDOWN_BYTES + MAX_MULTIPART_OVERHEAD_BYTES
UPLOAD_TOO_LARGE_MESSAGE = "上传内容超过 20 MB 上限"
UPLOAD_MALFORMED_MESSAGE = "上传表单格式不正确"


def _multipart_error(message: str) -> ApiError:
    """把解析器消息区分为 413 与 422，且不回显解析器消息、正文或文件名。

    ``max_part_size`` 只限制非文件字段（如 ``title``），因此“超过单段上限”只会来自这类
    字段；文件字节上限由接收阶段总量与 ``service`` 的长度校验保证。
    """

    if "exceeded maximum size" in message:
        return ApiError(413, CODE_DOCUMENT_TOO_LARGE, UPLOAD_TOO_LARGE_MESSAGE)
    return ApiError(422, CODE_UPLOAD_MALFORMED, UPLOAD_MALFORMED_MESSAGE)


def _ingestion_error(error: IngestionError) -> ApiError:
    """领域错误映射为具名 HTTP 错误；消息不含正文、文件名或 Idempotency-Key。"""

    if isinstance(error, DocumentTooLarge):
        return ApiError(413, CODE_DOCUMENT_TOO_LARGE, str(error))
    if isinstance(error, DocumentEmpty):
        return ApiError(422, CODE_DOCUMENT_EMPTY, str(error))
    if isinstance(error, DocumentNotText):
        return ApiError(422, CODE_DOCUMENT_NOT_TEXT, str(error))
    if isinstance(error, UnsupportedDocumentType):
        return ApiError(422, CODE_UNSUPPORTED_DOCUMENT_TYPE, str(error))
    if isinstance(error, TitleInvalid):
        return ApiError(422, CODE_DOCUMENT_TITLE_INVALID, str(error))
    if isinstance(error, IdempotencyKeyInvalid):
        return ApiError(422, CODE_IDEMPOTENCY_KEY_INVALID, str(error))
    if isinstance(error, IdempotencyConflict):
        return ApiError(409, CODE_IDEMPOTENCY_KEY_REUSED, str(error))
    return ApiError(400, CODE_INGESTION_ERROR, "上传请求失败")


async def enforce_upload_body_limit(request: Request) -> None:
    """在接收阶段限制请求体总字节，避免 multipart 完整落盘后才拒绝。

    ``Content-Length`` 存在时先做无需读取的快速拒绝；同时对 ASGI ``http.request`` 消息
    累计计数，覆盖 chunked 或无 ``Content-Length`` 的请求。文件字节上限由这里的累计总量
    与 ``service.create_markdown_document`` 的长度校验共同保证；``request.form`` 的
    ``max_part_size`` 只限制 ``title`` 这类非文件字段，不限制文件 part。
    """

    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_length = int(declared)
        except ValueError as error:
            raise ApiError(400, CODE_UPLOAD_MALFORMED, "Content-Length 不是合法整数") from error
        if declared_length < 0:
            raise ApiError(400, CODE_UPLOAD_MALFORMED, "Content-Length 不能为负数")
        if declared_length > MAX_UPLOAD_REQUEST_BYTES:
            raise ApiError(413, CODE_DOCUMENT_TOO_LARGE, UPLOAD_TOO_LARGE_MESSAGE)

    original_receive: Receive = request._receive  # Starlette 未公开替换点
    received = 0

    async def limited_receive() -> Message:
        nonlocal received
        message = await original_receive()
        if message["type"] == "http.request":
            received += len(message.get("body", b""))
            if received > MAX_UPLOAD_REQUEST_BYTES:
                raise ApiError(413, CODE_DOCUMENT_TOO_LARGE, UPLOAD_TOO_LARGE_MESSAGE)
        return message

    request._receive = limited_receive  # 在正文解析前安装流式上限


@router.post(
    "/knowledge-bases/{kb_id}/documents",
    response_model=DocumentUploadResponse,
    status_code=202,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["title", "file"],
                        "properties": {
                            "title": {"type": "string", "maxLength": MAX_TITLE_LENGTH},
                            "file": {"type": "string", "format": "binary"},
                        },
                    }
                }
            },
        }
    },
)
async def upload_markdown_document(
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    access: KbAccess = Depends(require_editor),
    _context: object = Depends(require_csrf),
    _limit: None = Depends(enforce_upload_body_limit),
    session: AsyncSession = Depends(get_database_session),
) -> DocumentUploadResponse:
    """受理 Markdown 上传：保存原文件并在单事务写入四张表，返回 ``202``。"""

    settings: Settings = request.app.state.settings
    enforce_allowed_origin(request, settings)

    try:
        form = await request.form(
            max_files=1, max_fields=1, max_part_size=MAX_MARKDOWN_BYTES
        )
    except MultiPartException as error:
        raise _multipart_error(error.message) from error
    except MultipartParseError as error:
        # python-multipart 对错 boundary、畸形正文抛解析错误，Starlette 不转换它（原为 500）。
        raise ApiError(422, CODE_UPLOAD_MALFORMED, UPLOAD_MALFORMED_MESSAGE) from error
    except StarletteHTTPException as error:
        # scope 含 app 时 Starlette 把 MultiPartException 包成 400 HTTPException（原样回显
        # 解析器 detail、原为 400 BAD_REQUEST）；只在解析调用周围捕获并转成具名错误，非 400
        # 的 HTTPException 与业务 ``ApiError`` 都原样抛出、不会被误捕。
        if error.status_code != 400 or not isinstance(error.detail, str):
            raise
        raise _multipart_error(error.detail) from error

    try:
        title_value = form.get("title")
        upload = form.get("file")
        if not isinstance(title_value, str):
            raise ApiError(422, CODE_UPLOAD_MALFORMED, "缺少 title 表单字段")
        if not isinstance(upload, UploadFile):
            raise ApiError(422, CODE_UPLOAD_MALFORMED, "缺少 file 文件字段")
        validate_markdown_filename(upload.filename)
        content = await upload.read()

        store = DocumentBlobStore(settings.document_storage_directory)
        outcome = await ingestion_service.create_markdown_document(
            session,
            store,
            kb_id=access.kb_id,
            organization_id=access.organization_id,
            title=title_value,
            content=content,
            idempotency_key=idempotency_key,
        )
    except IngestionError as error:
        raise _ingestion_error(error) from error
    finally:
        await form.close()

    return DocumentUploadResponse(
        document_id=outcome.document_id,
        version_id=outcome.version_id,
        job_id=outcome.job_id,
    )


__all__ = ["router"]
