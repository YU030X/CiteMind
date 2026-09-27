"""授权混合检索路由：``POST /api/v1/retrieval/search``。

会话组织与成员身份由服务端确定，请求体只提交查询与 ``kbIds``。两条候选路的授权与版本
过滤都在 SQL 内完成；本层只做错误映射与 schema 转换，错误消息静态、不回显查询、向量、
KB 名称或组织数据。查询编码客户端按请求构造并在依赖退出时关闭；关键词分析器是进程内
单例，首次构造（含私有临时目录 IO）由 FastAPI 在线程池中执行。
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from rag_backend.api.errors import (
    CODE_KNOWLEDGE_BASE_NOT_FOUND,
    CODE_RETRIEVAL_ANALYZER_MISMATCH,
    CODE_RETRIEVAL_PROFILE_CONFLICT,
    CODE_RETRIEVAL_QUERY_INVALID,
    CODE_RETRIEVAL_UNAVAILABLE,
    ApiError,
)
from rag_backend.auth.context import AuthContext
from rag_backend.auth.dependencies import get_auth_context
from rag_backend.config import Settings
from rag_backend.database import get_database_session
from rag_backend.retrieval.errors import (
    KnowledgeBaseNotAccessible,
    RetrievalAnalyzerMismatch,
    RetrievalEmbeddingError,
    RetrievalError,
    RetrievalProfileConflict,
    RetrievalQueryInvalid,
)
from rag_backend.retrieval.keyword_analyzer import (
    KeywordAnalyzerError,
    get_keyword_analyzer,
)
from rag_backend.retrieval.query_embedding_client import QueryEmbeddingClient
from rag_backend.retrieval.repository import SqlRetrievalRepository
from rag_backend.retrieval.service import (
    KeywordAnalyzerLike,
    QueryEmbedder,
    search_authorized_chunks,
)
from rag_backend.schemas.retrieval import (
    RetrievalCandidate,
    RetrievalSearchRequest,
    RetrievalSearchResponse,
)

router = APIRouter(prefix="/api/v1", tags=["retrieval"])

RETRIEVAL_UNAVAILABLE_MESSAGE = "检索服务暂时不可用"


def get_query_analyzer() -> KeywordAnalyzerLike:
    """返回进程内关键词分析器；构造失败按依赖不可用静态失败，不回退共享缓存。"""

    try:
        return get_keyword_analyzer()
    except KeywordAnalyzerError as error:
        raise ApiError(503, CODE_RETRIEVAL_UNAVAILABLE, RETRIEVAL_UNAVAILABLE_MESSAGE) from error


def get_query_embedder(request: Request) -> Iterator[QueryEmbedder]:
    """按请求构造受限查询编码客户端；未配置 token 或基址非法时静态 503。"""

    settings: Settings = request.app.state.settings
    try:
        client = QueryEmbeddingClient.from_settings(settings)
    except ValueError as error:
        raise ApiError(503, CODE_RETRIEVAL_UNAVAILABLE, RETRIEVAL_UNAVAILABLE_MESSAGE) from error
    try:
        yield client
    finally:
        client.close()


def retrieval_error_to_api(error: RetrievalError) -> ApiError:
    """把领域错误映射为具名错误体；消息不含查询、向量或 KB 存在性信息。

    查询编码的暂时性失败已经由客户端分类；这里只把 ``retryAfter`` 透传到 ``details``，
    让调用方能在不解析日志的前提下决定是否重试（不复用 429 的 ``Retry-After`` 头，避免
    与登录限流语义混淆）。
    """

    if isinstance(error, KnowledgeBaseNotAccessible):
        return ApiError(404, CODE_KNOWLEDGE_BASE_NOT_FOUND, "知识库不存在或无权访问")
    if isinstance(error, RetrievalQueryInvalid):
        return ApiError(422, CODE_RETRIEVAL_QUERY_INVALID, "查询不合法")
    if isinstance(error, RetrievalProfileConflict):
        return ApiError(
            422,
            CODE_RETRIEVAL_PROFILE_CONFLICT,
            "所选知识库使用了不同的索引 profile",
        )
    if isinstance(error, RetrievalAnalyzerMismatch):
        return ApiError(
            503,
            CODE_RETRIEVAL_ANALYZER_MISMATCH,
            "检索索引与当前服务版本不一致",
        )
    if isinstance(error, RetrievalEmbeddingError) and error.retry_after_seconds is not None:
        return ApiError(
            503,
            CODE_RETRIEVAL_UNAVAILABLE,
            RETRIEVAL_UNAVAILABLE_MESSAGE,
            details={"retryAfter": error.retry_after_seconds},
        )
    return ApiError(503, CODE_RETRIEVAL_UNAVAILABLE, RETRIEVAL_UNAVAILABLE_MESSAGE)


@router.post("/retrieval/search", response_model=RetrievalSearchResponse)
async def search_retrieval(
    payload: RetrievalSearchRequest,
    context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_database_session),
    embedder: QueryEmbedder = Depends(get_query_embedder),
    analyzer: KeywordAnalyzerLike = Depends(get_query_analyzer),
) -> RetrievalSearchResponse:
    """对请求内的授权 KB 运行向量与关键词双路检索并返回 RRF 融合候选。"""

    repository = SqlRetrievalRepository(session)
    try:
        result = await search_authorized_chunks(
            repository,
            user_id=context.user_id,
            organization_id=context.organization_id,
            kb_ids=payload.kb_ids,
            query=payload.query,
            embedder=embedder,
            analyzer=analyzer,
        )
    except RetrievalError as error:
        raise retrieval_error_to_api(error) from error
    return RetrievalSearchResponse(
        candidates=[
            RetrievalCandidate(
                chunk_id=candidate.chunk_id,
                document_id=candidate.document_id,
                kb_id=candidate.kb_id,
                version_id=candidate.version_id,
                vector_rank=candidate.vector_rank,
                vector_score=candidate.vector_score,
                keyword_rank=candidate.keyword_rank,
                keyword_score=candidate.keyword_score,
                fusion_rank=candidate.fusion_rank,
                fusion_score=candidate.fusion_score,
            )
            for candidate in result.candidates
        ]
    )


__all__ = [
    "get_query_analyzer",
    "get_query_embedder",
    "retrieval_error_to_api",
    "router",
]
