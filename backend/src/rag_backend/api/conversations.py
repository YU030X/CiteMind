"""证据问答路由：会话创建、历史读取、追加追问与引用详情。

四个端点全部复核所有者隔离与 KB 成员关系；两个写操作额外要求合法 ``Origin`` 与
``X-CSRF-Token``。检索复用与 ``POST /retrieval/search`` 相同的授权 SQL 与查询编码
客户端；生成客户端按请求构造并在依赖退出时关闭，未开启业务生成时静态 503 且不联网。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from rag_backend.api.errors import (
    CODE_CITATION_NOT_FOUND,
    CODE_CONVERSATION_NOT_FOUND,
    CODE_CONVERSATION_QUESTION_TOO_LONG,
    CODE_CONVERSATION_SOURCES_CHANGED,
    CODE_GENERATION_FAILED,
    CODE_GENERATION_INVALID_RESPONSE,
    CODE_GENERATION_OPTION_UNSUPPORTED,
    CODE_KNOWLEDGE_BASE_NOT_FOUND,
    CODE_LLM_UNAVAILABLE,
    ApiError,
)
from rag_backend.api.retrieval import (
    get_query_analyzer,
    get_query_embedder,
    retrieval_error_to_api,
)
from rag_backend.auth.context import AuthContext
from rag_backend.auth.dependencies import (
    enforce_allowed_origin,
    get_auth_context,
    require_csrf,
)
from rag_backend.config import Settings
from rag_backend.conversation.errors import (
    CitationNotFound,
    ConversationNotFound,
    ConversationQuestionTooLong,
    ConversationSourcesChanged,
    GenerationFailed,
    GenerationInvalidResponse,
)
from rag_backend.conversation.repository import (
    ConversationRepository,
    SqlConversationRepository,
)
from rag_backend.conversation.service import (
    CitationView,
    ConversationListRepository,
    ConversationSummaryView,
    TurnResult,
    answer_question,
    create_conversation,
    delete_conversation,
    list_owned_conversations,
    load_citation_detail,
    load_conversation_history,
    update_conversation,
)
from rag_backend.database import get_database_session
from rag_backend.generation.capabilities import ReasoningEffort, is_supported_model
from rag_backend.generation.context_budget import ContextBudget
from rag_backend.generation.deepseek_client import (
    AnswerGenerator,
    DeepSeekAnswerGenerator,
    GenerationConfigError,
)
from rag_backend.generation.deepseek_prompt import (
    NON_THINKING,
    PromptTokenEstimator,
    ThinkingChoice,
)
from rag_backend.generation.deepseek_token_counting import (
    DeepSeekTokenizerError,
    LocalPromptTokenCounter,
)
from rag_backend.knowledge.service import resolve_knowledge_base_access
from rag_backend.retrieval.errors import RetrievalError
from rag_backend.retrieval.repository import (
    EvidenceRepository,
    SqlRetrievalRepository,
)
from rag_backend.retrieval.service import (
    KeywordAnalyzerLike,
    QueryEmbedder,
    RetrievalResult,
    search_authorized_chunks,
)
from rag_backend.schemas.conversation import (
    AnswerResponse,
    AnswerUsageResponse,
    AskQuestionRequest,
    CitationResponse,
    ConversationListResponse,
    ConversationMessageResponse,
    ConversationMessagesResponse,
    ConversationSummary,
    CreateConversationRequest,
    CreateConversationResponse,
    ThinkingRequest,
    UpdateConversationRequest,
)

router = APIRouter(prefix="/api/v1", tags=["conversations"])

LLM_UNAVAILABLE_MESSAGE = "问答生成服务暂时不可用"
GENERATION_FAILED_MESSAGE = "生成服务暂时不可用"
GENERATION_INVALID_MESSAGE = "生成结果不合法"
GENERATION_OPTION_UNSUPPORTED_MESSAGE = "不支持的模型或思考选项"
SOURCES_CHANGED_MESSAGE = "资料更新中，请重试"
CONVERSATION_NOT_FOUND_MESSAGE = "会话不存在"
CITATION_NOT_FOUND_MESSAGE = "引用不存在"
QUESTION_TOO_LONG_MESSAGE = "问题超出输入预算"


def get_conversation_repository(
    session: AsyncSession = Depends(get_database_session),
) -> ConversationRepository:
    """按请求构造会话仓储；不缓存、不跨请求复用事务。"""

    return SqlConversationRepository(session)


def get_evidence_repository(
    session: AsyncSession = Depends(get_database_session),
) -> EvidenceRepository:
    """证据读取与检索共用同一 ``AsyncSession`` 与权威授权链。"""

    return SqlRetrievalRepository(session)


def get_conversation_list_repository(
    session: AsyncSession = Depends(get_database_session),
) -> ConversationListRepository:
    """会话列表只依赖单条聚合读取；与完整会话仓储分开注入，保持接口最小。"""

    return SqlConversationRepository(session)


def get_prompt_estimator(request: Request) -> PromptTokenEstimator:
    """进程内复用本地 token 估算器；产物缺失时静态 503，不回退假计数。

    同步依赖由 FastAPI 在线程池内执行，因此这里的文件 IO 与 tokenizer 加载不阻塞事件循环。
    """

    counter = getattr(request.app.state, "prompt_token_counter", None)
    if counter is None:
        try:
            counter = LocalPromptTokenCounter()
        except DeepSeekTokenizerError as error:
            raise ApiError(503, CODE_LLM_UNAVAILABLE, LLM_UNAVAILABLE_MESSAGE) from error
        request.app.state.prompt_token_counter = counter
    return counter


def get_answer_generator(request: Request) -> Iterator[AnswerGenerator]:
    """按请求构造受限生成客户端；未开启或缺少密钥时静态 503 且不联网。"""

    settings: Settings = request.app.state.settings
    try:
        generator = DeepSeekAnswerGenerator.from_settings(settings)
    except GenerationConfigError as error:
        raise ApiError(503, CODE_LLM_UNAVAILABLE, LLM_UNAVAILABLE_MESSAGE) from error
    try:
        yield generator
    finally:
        generator.close()


def _summary_response(view: ConversationSummaryView) -> ConversationSummary:
    return ConversationSummary(
        id=view.conversation_id,
        title=view.title,
        pinned=view.pinned,
        kb_ids=list(view.kb_ids),
        created_at=view.created_at,
        last_message_at=view.last_message_at,
    )


def _citation_response(view: CitationView) -> CitationResponse:
    return CitationResponse(
        citation_id=view.citation_id,
        display_label=view.display_label,
        document_title=view.document_title,
        version=view.version,
        locator=view.locator,
        quote=view.quote,
        is_current_version=view.is_current_version,
    )


def _turn_response(result: TurnResult) -> AnswerResponse:
    return AnswerResponse(
        conversation_id=result.conversation_id,
        message_id=result.message_id,
        query_run_id=result.query_run_id,
        answer=result.answer,
        citations=[_citation_response(view) for view in result.citations],
        insufficient_evidence=result.insufficient_evidence,
        degraded_stages=list(result.degraded_stages),
        follow_up=result.follow_up,
        usage=AnswerUsageResponse(
            local_input_tokens=result.usage.local_input_tokens,
            input_token_budget=result.usage.input_token_budget,
            output_token_budget=result.usage.output_token_budget,
            provider_prompt_tokens=result.usage.provider_prompt_tokens,
            provider_completion_tokens=result.usage.provider_completion_tokens,
        ),
    )


@router.post("/conversations", response_model=CreateConversationResponse, status_code=201)
async def create_conversation_endpoint(
    request: Request,
    payload: CreateConversationRequest,
    context: AuthContext = Depends(require_csrf),
    repository: ConversationRepository = Depends(get_conversation_repository),
    session: AsyncSession = Depends(get_database_session),
) -> CreateConversationResponse:
    """创建所有者会话；KB 集合必须是本次会话可访问集合的子集。"""

    settings: Settings = request.app.state.settings
    enforce_allowed_origin(request, settings)

    kb_ids = list(dict.fromkeys(payload.kb_ids))
    for kb_id in kb_ids:
        access = await resolve_knowledge_base_access(
            session,
            kb_id=kb_id,
            user_id=context.user_id,
            organization_id=context.organization_id,
        )
        if access is None:
            # 不存在、跨组织、成员已撤销统一不暴露存在性的 404。
            raise ApiError(404, CODE_KNOWLEDGE_BASE_NOT_FOUND, "知识库不存在或无权访问")

    view = await create_conversation(
        repository,
        organization_id=context.organization_id,
        owner_id=context.user_id,
        kb_ids=kb_ids,
    )
    return CreateConversationResponse(
        conversation_id=view.conversation_id,
        kb_ids=list(view.kb_ids),
        created_at=view.created_at,
    )


@router.get("/conversations", response_model=ConversationListResponse)
async def list_conversations(
    context: AuthContext = Depends(get_auth_context),
    repository: ConversationListRepository = Depends(get_conversation_list_repository),
) -> ConversationListResponse:
    """列出当前用户的会话摘要；只按所有者隔离，不读取任何消息正文。"""

    views = await list_owned_conversations(
        repository,
        owner_id=context.user_id,
        organization_id=context.organization_id,
    )
    return ConversationListResponse(conversations=[_summary_response(view) for view in views])


@router.patch("/conversations/{conversation_id}", response_model=ConversationSummary)
async def update_conversation_endpoint(
    request: Request,
    conversation_id: uuid.UUID,
    payload: UpdateConversationRequest,
    context: AuthContext = Depends(require_csrf),
    repository: ConversationRepository = Depends(get_conversation_repository),
) -> ConversationSummary:
    """改名/置顶当前用户的会话；不存在、已删除或不属于本人统一返回 404。"""

    settings: Settings = request.app.state.settings
    enforce_allowed_origin(request, settings)
    try:
        view = await update_conversation(
            repository,
            conversation_id=conversation_id,
            owner_id=context.user_id,
            organization_id=context.organization_id,
            title=payload.title,
            pinned=payload.pinned,
        )
    except ConversationNotFound as error:
        raise ApiError(404, CODE_CONVERSATION_NOT_FOUND, CONVERSATION_NOT_FOUND_MESSAGE) from error
    return _summary_response(view)


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation_endpoint(
    request: Request,
    conversation_id: uuid.UUID,
    context: AuthContext = Depends(require_csrf),
    repository: ConversationRepository = Depends(get_conversation_repository),
) -> None:
    """逻辑删除当前用户的会话；重复删除幂等 204，不存在或不属于本人返回 404。"""

    settings: Settings = request.app.state.settings
    enforce_allowed_origin(request, settings)
    try:
        await delete_conversation(
            repository,
            conversation_id=conversation_id,
            owner_id=context.user_id,
            organization_id=context.organization_id,
        )
    except ConversationNotFound as error:
        raise ApiError(404, CODE_CONVERSATION_NOT_FOUND, CONVERSATION_NOT_FOUND_MESSAGE) from error


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=ConversationMessagesResponse,
)
async def list_conversation_messages(
    conversation_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    repository: ConversationRepository = Depends(get_conversation_repository),
    evidence: EvidenceRepository = Depends(get_evidence_repository),
) -> ConversationMessagesResponse:
    """读取当前合法历史；来源已撤权或删除的助手消息整体隐藏。"""

    try:
        view = await load_conversation_history(
            repository,
            evidence,
            conversation_id=conversation_id,
            user_id=context.user_id,
            organization_id=context.organization_id,
        )
    except ConversationNotFound as error:
        raise ApiError(404, CODE_CONVERSATION_NOT_FOUND, CONVERSATION_NOT_FOUND_MESSAGE) from error
    return ConversationMessagesResponse(
        conversation_id=view.conversation_id,
        messages=[
            ConversationMessageResponse(
                message_id=message.message_id,
                role="assistant" if message.role == "assistant" else "user",
                content=message.content,
                query_run_id=message.query_run_id,
                created_at=message.created_at,
                citations=[_citation_response(citation) for citation in message.citations],
            )
            for message in view.messages
        ],
    )


def _resolve_thinking(
    thinking: ThinkingRequest | None, reasoning_effort: ReasoningEffort | None
) -> ThinkingChoice:
    """把请求开关与强度映射为渲染/客户端选项；非法组合已在 schema 层拒绝。

    省略 ``thinking`` 或显式 ``disabled`` 都归一为非思考；开启时未指定强度则沿用官方默认
    ``high``（由 :class:`ThinkingChoice` 表达），不在这里另造默认值。
    """

    if thinking is None or thinking.type == "disabled":
        return NON_THINKING
    return ThinkingChoice(enabled=True, effort=reasoning_effort)


@router.post(
    "/conversations/{conversation_id}/messages", response_model=AnswerResponse
)
async def ask_question(
    request: Request,
    conversation_id: uuid.UUID,
    payload: AskQuestionRequest,
    context: AuthContext = Depends(require_csrf),
    repository: ConversationRepository = Depends(get_conversation_repository),
    evidence: EvidenceRepository = Depends(get_evidence_repository),
    embedder: QueryEmbedder = Depends(get_query_embedder),
    analyzer: KeywordAnalyzerLike = Depends(get_query_analyzer),
    estimator: PromptTokenEstimator = Depends(get_prompt_estimator),
    generator: AnswerGenerator = Depends(get_answer_generator),
    session: AsyncSession = Depends(get_database_session),
) -> AnswerResponse:
    """追加一次追问：检索 → 预算装配 → 生成 → 引用映射 → 持久化。"""

    settings: Settings = request.app.state.settings
    enforce_allowed_origin(request, settings)
    budget = ContextBudget.from_settings(settings)
    # 模型只接受服务端已验证白名单：未验证的模型（如 deepseek-v4-pro）在此静态拒绝。
    answer_model = settings.llm_model if payload.model is None else payload.model
    if not is_supported_model(answer_model):
        raise ApiError(
            422, CODE_GENERATION_OPTION_UNSUPPORTED, GENERATION_OPTION_UNSUPPORTED_MESSAGE
        )
    thinking = _resolve_thinking(payload.thinking, payload.reasoning_effort)

    async def retrieve(
        *, kb_ids: Sequence[uuid.UUID], query: str
    ) -> RetrievalResult:
        # 每次检索构造独立仓储实例（同一请求 Session）；检索内部自行 release。
        search_repository = SqlRetrievalRepository(session)
        return await search_authorized_chunks(
            search_repository,
            user_id=context.user_id,
            organization_id=context.organization_id,
            kb_ids=kb_ids,
            query=query,
            embedder=embedder,
            analyzer=analyzer,
        )

    try:
        result = await answer_question(
            repository,
            evidence,
            conversation_id=conversation_id,
            user_id=context.user_id,
            organization_id=context.organization_id,
            question=payload.question,
            request_id=payload.request_id,
            retrieve=retrieve,
            estimator=estimator,
            generator=generator,
            budget=budget,
            answer_model=answer_model,
            rewrite_model=settings.llm_model,
            thinking=thinking,
        )
    except ConversationNotFound as error:
        raise ApiError(404, CODE_CONVERSATION_NOT_FOUND, CONVERSATION_NOT_FOUND_MESSAGE) from error
    except ConversationQuestionTooLong as error:
        raise ApiError(
            422, CODE_CONVERSATION_QUESTION_TOO_LONG, QUESTION_TOO_LONG_MESSAGE
        ) from error
    except GenerationInvalidResponse as error:
        raise ApiError(
            502, CODE_GENERATION_INVALID_RESPONSE, GENERATION_INVALID_MESSAGE
        ) from error
    except GenerationFailed as error:
        raise ApiError(502, CODE_GENERATION_FAILED, GENERATION_FAILED_MESSAGE) from error
    except ConversationSourcesChanged as error:
        raise ApiError(
            409, CODE_CONVERSATION_SOURCES_CHANGED, SOURCES_CHANGED_MESSAGE
        ) from error
    except RetrievalError as error:
        raise retrieval_error_to_api(error) from error
    return _turn_response(result)


@router.get("/citations/{citation_id}", response_model=CitationResponse)
async def get_citation(
    citation_id: uuid.UUID,
    context: AuthContext = Depends(get_auth_context),
    repository: ConversationRepository = Depends(get_conversation_repository),
    evidence: EvidenceRepository = Depends(get_evidence_repository),
) -> CitationResponse:
    """读取一条引用；每次调用都复核所有者、成员关系与来源存在性。"""

    try:
        view = await load_citation_detail(
            repository,
            evidence,
            citation_id=citation_id,
            user_id=context.user_id,
            organization_id=context.organization_id,
        )
    except CitationNotFound as error:
        raise ApiError(404, CODE_CITATION_NOT_FOUND, CITATION_NOT_FOUND_MESSAGE) from error
    return _citation_response(view)


__all__ = [
    "get_answer_generator",
    "get_conversation_list_repository",
    "get_conversation_repository",
    "get_evidence_repository",
    "get_prompt_estimator",
    "router",
]
