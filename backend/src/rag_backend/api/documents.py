"""文档路由：列表/详情读取，以及上传、新版本与逻辑删除。

读取端点（``GET /api/v1/knowledge-bases/{id}/documents``、``GET /api/v1/documents/{id}``）
只读取未删除文档的元数据，授权复用 ``require_kb_role``/``require_document_role``。

上传端点 ``POST /api/v1/knowledge-bases/{kb_id}/documents`` 的关键顺序约束：本端点**不声明**
``UploadFile``/``Form`` 参数。FastAPI 在含表单/文件参数的端点上会先 ``await request.form()``
再求解依赖（``fastapi/routing.py``），那样未授权请求也会先把正文落盘。这里只注入 ``Request``、
路径参数、``Header`` 与鉴权/CSRF/体积依赖，等 KB ``EDITOR`` 角色、Origin、CSRF 与体积上限都
通过后，才在处理器内调用 ``await request.form()``。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Request, Response
from python_multipart.exceptions import MultipartParseError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import FormData, UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException
from starlette.types import Message, Receive

from rag_backend.api.errors import (
    CODE_DOCUMENT_DELETED,
    CODE_DOCUMENT_EMPTY,
    CODE_DOCUMENT_NOT_FOUND,
    CODE_DOCUMENT_NOT_PDF,
    CODE_DOCUMENT_NOT_TEXT,
    CODE_DOCUMENT_TITLE_INVALID,
    CODE_DOCUMENT_TOO_LARGE,
    CODE_DOCUMENT_VERSION_CONFLICT,
    CODE_IDEMPOTENCY_KEY_INVALID,
    CODE_IDEMPOTENCY_KEY_REUSED,
    CODE_INGESTION_ERROR,
    CODE_UNSUPPORTED_DOCUMENT_TYPE,
    CODE_UPLOAD_MALFORMED,
    ApiError,
)
from rag_backend.auth.context import AuthContext
from rag_backend.auth.dependencies import (
    enforce_allowed_origin,
    require_csrf,
    require_document_role,
    require_kb_role,
)
from rag_backend.config import Settings
from rag_backend.database import get_database_session
from rag_backend.ingestion import service as ingestion_service
from rag_backend.ingestion.errors import (
    DocumentDeleted,
    DocumentEmpty,
    DocumentNotFound,
    DocumentNotPdf,
    DocumentNotText,
    DocumentTooLarge,
    ExpectedVersionConflict,
    IdempotencyConflict,
    IdempotencyKeyInvalid,
    IngestionError,
    TitleInvalid,
    UnsupportedDocumentType,
)
from rag_backend.ingestion.storage import DocumentBlobStore
from rag_backend.ingestion.validation import (
    MAX_DOCUMENT_BYTES,
    MAX_TITLE_LENGTH,
    SOURCE_TYPE_PDF,
    resolve_upload_format,
)
from rag_backend.knowledge.document_read import (
    DocumentReadRepository,
    DocumentView,
    SqlDocumentReadRepository,
    list_knowledge_base_documents,
    load_document_detail,
)
from rag_backend.knowledge.roles import KbRole, kb_role_rank
from rag_backend.knowledge.service import (
    DocumentAccess,
    KbAccess,
    resolve_document_access,
)
from rag_backend.schemas.documents import (
    DocumentJobSummary,
    DocumentListResponse,
    DocumentSummary,
    DocumentUploadResponse,
    DocumentVersionSummary,
)

router = APIRouter(prefix="/api/v1", tags=["documents"])

require_reader = require_kb_role(KbRole.READER)
require_editor = require_kb_role(KbRole.EDITOR)
require_document_reader = require_document_role(KbRole.READER)
require_document_editor = require_document_role(KbRole.EDITOR)

# 删除事务先锁 ``document`` 再锁 ``ingest_job``，而 worker 的失败/发布事务先锁
# ``ingest_job`` 再锁 ``document``；两者并发时 PostgreSQL 会以死锁（SQLSTATE ``40P01``）
# 中止其中一方。删除侧据此对**整个事务**做有限重试：回滚后用同一会话重新授权并重跑
# tombstone，绝不只重试某一条 SQL，也不复用死锁事务里读到的角色快照。
DELETE_DEADLOCK_MAX_ATTEMPTS = 3
DEADLOCK_DETECTED_SQLSTATE = "40P01"

# multipart 边界、头部与 title 字段的合理余量；文件本身仍按 MAX_DOCUMENT_BYTES 精确判定。
MAX_MULTIPART_OVERHEAD_BYTES = 64 * 1024
MAX_UPLOAD_REQUEST_BYTES = MAX_DOCUMENT_BYTES + MAX_MULTIPART_OVERHEAD_BYTES
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
    if isinstance(error, DocumentNotPdf):
        return ApiError(422, CODE_DOCUMENT_NOT_PDF, str(error))
    if isinstance(error, UnsupportedDocumentType):
        return ApiError(422, CODE_UNSUPPORTED_DOCUMENT_TYPE, str(error))
    if isinstance(error, TitleInvalid):
        return ApiError(422, CODE_DOCUMENT_TITLE_INVALID, str(error))
    if isinstance(error, IdempotencyKeyInvalid):
        return ApiError(422, CODE_IDEMPOTENCY_KEY_INVALID, str(error))
    if isinstance(error, IdempotencyConflict):
        return ApiError(409, CODE_IDEMPOTENCY_KEY_REUSED, str(error))
    if isinstance(error, DocumentNotFound):
        return ApiError(404, CODE_DOCUMENT_NOT_FOUND, str(error))
    if isinstance(error, DocumentDeleted):
        return ApiError(409, CODE_DOCUMENT_DELETED, str(error))
    if isinstance(error, ExpectedVersionConflict):
        return ApiError(409, CODE_DOCUMENT_VERSION_CONFLICT, str(error))
    return ApiError(400, CODE_INGESTION_ERROR, "上传请求失败")


def _parse_expected_version(value: object) -> uuid.UUID:
    """解析 ``expectedVersionId`` 表单字段；缺失或非法都返回 422 且不回显原值。"""

    if not isinstance(value, str) or not value.strip():
        raise ApiError(422, CODE_UPLOAD_MALFORMED, "缺少 expectedVersionId 表单字段")
    try:
        return uuid.UUID(value.strip())
    except ValueError as error:
        raise ApiError(422, CODE_UPLOAD_MALFORMED, "expectedVersionId 不是合法 UUID") from error


async def _parse_upload_form(request: Request, *, max_fields: int) -> FormData:
    """解析 multipart 表单并统一映射解析错误；未授权请求不会走到这里。

    ``max_part_size`` 只限制非文件字段；文件字节上限由接收阶段总量与 ``service`` 长度校验保证。
    """

    try:
        return await request.form(
            max_files=1, max_fields=max_fields, max_part_size=MAX_DOCUMENT_BYTES
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


def _document_summary(view: DocumentView) -> DocumentSummary:
    """把只读视图映射为外部 schema；视图已不含 fileRef/fileHash/租约或正文。"""

    return DocumentSummary(
        id=view.id,
        title=view.title,
        source_type=view.source_type,
        lifecycle_status=view.lifecycle_status,
        active_version=(
            None
            if view.active_version is None
            else DocumentVersionSummary(
                id=view.active_version.id,
                version_no=view.active_version.version_no,
                status=view.active_version.status,
            )
        ),
        latest_version=(
            None
            if view.latest_version is None
            else DocumentVersionSummary(
                id=view.latest_version.id,
                version_no=view.latest_version.version_no,
                status=view.latest_version.status,
            )
        ),
        latest_job=(
            None
            if view.latest_job is None
            else DocumentJobSummary(
                id=view.latest_job.id,
                status=view.latest_job.status,
                error_code=view.latest_job.error_code,
            )
        ),
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


def get_document_read_repository(
    session: AsyncSession = Depends(get_database_session),
) -> DocumentReadRepository:
    """按请求构造只读仓储；不缓存、不跨请求复用事务。"""

    return SqlDocumentReadRepository(session)


@router.get(
    "/knowledge-bases/{kb_id}/documents", response_model=DocumentListResponse
)
async def list_knowledge_base_documents_route(
    access: KbAccess = Depends(require_reader),
    repository: DocumentReadRepository = Depends(get_document_read_repository),
) -> DocumentListResponse:
    """列出 KB 内未删除文档；已删除文档不出现，本片不分页。"""

    views = await list_knowledge_base_documents(
        repository,
        kb_id=access.kb_id,
        organization_id=access.organization_id,
    )
    return DocumentListResponse(documents=[_document_summary(view) for view in views])


@router.get("/documents/{document_id}", response_model=DocumentSummary)
async def get_document(
    document_id: uuid.UUID,
    access: DocumentAccess = Depends(require_document_reader),
    repository: DocumentReadRepository = Depends(get_document_read_repository),
) -> DocumentSummary:
    """读取单个未删除文档；已删除或越权统一返回不暴露存在性的 404。"""

    view = await load_document_detail(
        repository,
        document_id=document_id,
        organization_id=access.organization_id,
    )
    if view is None:
        raise ApiError(404, CODE_DOCUMENT_NOT_FOUND, "文档不存在或无权访问")
    return _document_summary(view)


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
                            "file": {
                                "type": "string",
                                "format": "binary",
                                "description": "文本源文件：.md、.markdown 或 PDF（.pdf）",
                            },
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

    form = await _parse_upload_form(request, max_fields=1)
    try:
        title_value = form.get("title")
        upload = form.get("file")
        if not isinstance(title_value, str):
            raise ApiError(422, CODE_UPLOAD_MALFORMED, "缺少 title 表单字段")
        if not isinstance(upload, UploadFile):
            raise ApiError(422, CODE_UPLOAD_MALFORMED, "缺少 file 文件字段")
        upload_format = resolve_upload_format(upload.filename)
        content = await upload.read()

        store = DocumentBlobStore(settings.document_storage_directory)
        if upload_format.source_type == SOURCE_TYPE_PDF:
            outcome = await ingestion_service.create_pdf_document(
                session,
                store,
                kb_id=access.kb_id,
                organization_id=access.organization_id,
                title=title_value,
                content=content,
                idempotency_key=idempotency_key,
            )
        else:
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


@router.post(
    "/documents/{document_id}/versions",
    response_model=DocumentUploadResponse,
    status_code=202,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["title", "file", "expectedVersionId"],
                        "properties": {
                            "title": {"type": "string", "maxLength": MAX_TITLE_LENGTH},
                            "expectedVersionId": {
                                "type": "string",
                                "format": "uuid",
                                "description": "当前 active version 的 UUID；过期时返回 409",
                            },
                            "file": {
                                "type": "string",
                                "format": "binary",
                                "description": "文本源文件：.md、.markdown 或 PDF（.pdf）",
                            },
                        },
                    }
                }
            },
        }
    },
)
async def upload_document_version(
    request: Request,
    document_id: uuid.UUID,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    access: DocumentAccess = Depends(require_document_editor),
    _context: object = Depends(require_csrf),
    _limit: None = Depends(enforce_upload_body_limit),
    session: AsyncSession = Depends(get_database_session),
) -> DocumentUploadResponse:
    """受理文档新版本：文档行锁分配 ``version_no``，``202`` 只表示已持久化入库事实。"""

    settings: Settings = request.app.state.settings
    enforce_allowed_origin(request, settings)

    form = await _parse_upload_form(request, max_fields=2)
    try:
        title_value = form.get("title")
        upload = form.get("file")
        expected_value = form.get("expectedVersionId")
        if not isinstance(title_value, str):
            raise ApiError(422, CODE_UPLOAD_MALFORMED, "缺少 title 表单字段")
        if not isinstance(upload, UploadFile):
            raise ApiError(422, CODE_UPLOAD_MALFORMED, "缺少 file 文件字段")
        expected_active_version_id = _parse_expected_version(expected_value)
        upload_format = resolve_upload_format(upload.filename)
        content = await upload.read()

        store = DocumentBlobStore(settings.document_storage_directory)
        if upload_format.source_type == SOURCE_TYPE_PDF:
            outcome = await ingestion_service.create_pdf_version(
                session,
                store,
                kb_id=access.kb_id,
                organization_id=access.organization_id,
                document_id=document_id,
                title=title_value,
                content=content,
                idempotency_key=idempotency_key,
                expected_active_version_id=expected_active_version_id,
            )
        else:
            outcome = await ingestion_service.create_markdown_version(
                session,
                store,
                kb_id=access.kb_id,
                organization_id=access.organization_id,
                document_id=document_id,
                title=title_value,
                content=content,
                idempotency_key=idempotency_key,
                expected_active_version_id=expected_active_version_id,
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


def _is_deadlock_error(error: DBAPIError) -> bool:
    """只在 SQLSTATE 为 ``40P01`` 时判为可重试死锁；其它 DB 错误原样上抛。"""

    return (
        getattr(getattr(error, "orig", None), "sqlstate", None)
        == DEADLOCK_DETECTED_SQLSTATE
    )


async def _authorized_delete_document(
    session: AsyncSession,
    *,
    context: AuthContext,
    document_id: uuid.UUID,
) -> None:
    """在本请求事务内重新解析 OWNER 角色并执行 tombstone。

    重试与首次尝试走同一路径：每次都重新读 ``kb_member``，因此重试不会复用死锁事务中
    已失效的旧授权快照。
    """

    access = await resolve_document_access(
        session,
        document_id=document_id,
        user_id=context.user_id,
        organization_id=context.organization_id,
    )
    if access is None or kb_role_rank(access.role) < kb_role_rank(KbRole.OWNER):
        raise ApiError(404, CODE_DOCUMENT_NOT_FOUND, "文档不存在或无权访问")
    await ingestion_service.delete_document(
        session, kb_id=access.kb_id, document_id=document_id
    )


async def _delete_document_with_retry(
    session: AsyncSession,
    *,
    context: AuthContext,
    document_id: uuid.UUID,
) -> None:
    """完整事务重试删除：只重试死锁，且每次重新授权。

    死锁会整体回滚首次尝试，因此不会留下部分写入；重试成功也只提交一次，不产生重复
    副作用（``kb_revision`` 只递增一次、tombstone 只落一次）。
    """

    for attempt in range(1, DELETE_DEADLOCK_MAX_ATTEMPTS + 1):
        try:
            await _authorized_delete_document(
                session, context=context, document_id=document_id
            )
            return
        except IngestionError as error:
            raise _ingestion_error(error) from error
        except DBAPIError as error:
            if not _is_deadlock_error(error) or attempt == DELETE_DEADLOCK_MAX_ATTEMPTS:
                raise
            await session.rollback()


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(
    request: Request,
    document_id: uuid.UUID,
    context: AuthContext = Depends(require_csrf),
    session: AsyncSession = Depends(get_database_session),
) -> Response:
    """逻辑删除文档（tombstone）：二次删除幂等返回 ``204``，不做物理回收。

    授权在事务内重新解析（不跨请求缓存）；与 worker 并发时的死锁由
    :func:`_delete_document_with_retry` 以完整事务重试收敛。
    """

    settings: Settings = request.app.state.settings
    enforce_allowed_origin(request, settings)

    await _delete_document_with_retry(
        session, context=context, document_id=document_id
    )
    return Response(status_code=204)


__all__ = [
    "get_document_read_repository",
    "router",
]
