"""问答路由的进程内 HTTP 契约测试：假仓储与假依赖，不连数据库、不调用模型。

覆盖：业务生成未开启时的静态 503、所有者隔离、撤权来源历史隐藏、引用详情的二次鉴权，
以及写操作必须携带 CSRF。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from rag_backend.api.conversations import (
    get_answer_generator,
    get_conversation_repository,
    get_evidence_repository,
    get_prompt_estimator,
)
from rag_backend.api.errors import (
    CODE_CITATION_NOT_FOUND,
    CODE_CONVERSATION_NOT_FOUND,
    CODE_CSRF_INVALID,
    CODE_GENERATION_OPTION_UNSUPPORTED,
    CODE_LLM_UNAVAILABLE,
    CODE_VALIDATION_ERROR,
)
from rag_backend.api.retrieval import get_query_analyzer, get_query_embedder
from rag_backend.app import create_app
from rag_backend.auth.context import AuthContext
from rag_backend.auth.dependencies import get_auth_context
from rag_backend.auth.tokens import CSRF_HEADER_NAME
from rag_backend.config import Settings
from test_conversation_service import (
    CHUNK_ID,
    CONVERSATION_ID,
    ORG_ID,
    USER_ID,
    VERSION_ID,
    FakeConversationRepository,
    FakeEvidenceRepository,
    _conversation,
    _state,
)

ORIGIN = "http://127.0.0.1"
CSRF_TOKEN = "unit-csrf-token"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "session_cookie_secure": False,
        "allowed_origins": ORIGIN,
        "csrf_secret": "unit-conversation-csrf-secret",
    }
    values.update(overrides)
    return Settings(**values)


def _context() -> AuthContext:
    return AuthContext(
        user_id=USER_ID,
        organization_id=ORG_ID,
        username="content-user",
        is_admin=False,
        session_id=uuid.uuid4(),
        csrf_token=CSRF_TOKEN,
    )


class _FakeAnalyzer:
    @property
    def analyzer_id(self) -> str:
        return "unit-analyzer"

    def analyze(self, text: str) -> str:
        return text


class _FakeEmbedder:
    def embed_query(self, text: str, expected_model_revision: str) -> Any:
        raise AssertionError("生成关闭时不应调用查询编码")

    def close(self) -> None:
        return None


class _FakeEstimator:
    def estimate_chat_tokens(self, messages: Any, *, thinking: Any = None) -> int:
        return 1


@pytest.fixture
def conversation_app() -> Any:
    settings = _settings()
    app = create_app(settings)
    app.dependency_overrides[get_auth_context] = _context
    app.dependency_overrides[get_prompt_estimator] = _FakeEstimator
    app.dependency_overrides[get_query_analyzer] = _FakeAnalyzer
    app.dependency_overrides[get_query_embedder] = _FakeEmbedder
    yield app


def _client(app: Any) -> AsyncClient:
    transport = ASGITransport(app=app, client=("10.20.0.1", 12345))
    return AsyncClient(transport=transport, base_url="http://127.0.0.1")


@pytest.mark.anyio
async def test_append_message_returns_static_503_when_generation_disabled(
    conversation_app: Any,
) -> None:
    conversation_app.dependency_overrides[get_conversation_repository] = (
        lambda: FakeConversationRepository(_conversation())
    )
    conversation_app.dependency_overrides[get_evidence_repository] = (
        lambda: FakeEvidenceRepository([])
    )
    async with conversation_app.router.lifespan_context(conversation_app):
        async with _client(conversation_app) as client:
            response = await client.post(
                f"/api/v1/conversations/{CONVERSATION_ID}/messages",
                json={"question": "问题", "requestId": "r1"},
                headers={CSRF_HEADER_NAME: CSRF_TOKEN, "Origin": ORIGIN},
            )

    assert response.status_code == 503
    assert response.json()["code"] == CODE_LLM_UNAVAILABLE


@pytest.mark.anyio
async def test_append_message_requires_csrf(conversation_app: Any) -> None:
    async with conversation_app.router.lifespan_context(conversation_app):
        async with _client(conversation_app) as client:
            response = await client.post(
                f"/api/v1/conversations/{CONVERSATION_ID}/messages",
                json={"question": "问题"},
                headers={"Origin": ORIGIN},
            )

    assert response.status_code == 403
    assert response.json()["code"] == CODE_CSRF_INVALID


@pytest.mark.anyio
async def test_history_hides_assistant_message_from_revoked_source(
    conversation_app: Any,
) -> None:
    from test_conversation_service import _prior_turn

    user, assistant, citation = _prior_turn("旧回答")
    repository = FakeConversationRepository(
        _conversation(), messages=[user, assistant], citations=[citation]
    )
    evidence = FakeEvidenceRepository(
        [], states=[_state(CHUNK_ID, version_id=VERSION_ID, member_active=False)]
    )
    conversation_app.dependency_overrides[get_conversation_repository] = lambda: repository
    conversation_app.dependency_overrides[get_evidence_repository] = lambda: evidence

    async with conversation_app.router.lifespan_context(conversation_app):
        async with _client(conversation_app) as client:
            response = await client.get(
                f"/api/v1/conversations/{CONVERSATION_ID}/messages"
            )

    assert response.status_code == 200, response.text
    messages = response.json()["messages"]
    assert [message["role"] for message in messages] == ["user"]
    assert all("旧回答" not in message["content"] for message in messages)


@pytest.mark.anyio
async def test_history_returns_visible_citations_for_authorized_source(
    conversation_app: Any,
) -> None:
    from test_conversation_service import _prior_turn

    user, assistant, citation = _prior_turn("仍可回答")
    repository = FakeConversationRepository(
        _conversation(), messages=[user, assistant], citations=[citation]
    )
    evidence = FakeEvidenceRepository(
        [], states=[_state(CHUNK_ID, version_id=VERSION_ID)]
    )
    conversation_app.dependency_overrides[get_conversation_repository] = lambda: repository
    conversation_app.dependency_overrides[get_evidence_repository] = lambda: evidence

    async with conversation_app.router.lifespan_context(conversation_app):
        async with _client(conversation_app) as client:
            response = await client.get(
                f"/api/v1/conversations/{CONVERSATION_ID}/messages"
            )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert [message["role"] for message in payload["messages"]] == ["user", "assistant"]
    assistant_message = payload["messages"][1]
    assert assistant_message["citations"][0]["citationId"] == str(citation.id)
    assert assistant_message["citations"][0]["displayLabel"] == "E1"
    assert assistant_message["citations"][0]["isCurrentVersion"] is True


@pytest.mark.anyio
async def test_history_marks_superseded_citation_as_not_current(
    conversation_app: Any,
) -> None:
    from test_conversation_service import _prior_turn

    user, assistant, citation = _prior_turn("旧回答", is_current_version=False)
    repository = FakeConversationRepository(
        _conversation(), messages=[user, assistant], citations=[citation]
    )
    evidence = FakeEvidenceRepository(
        [], states=[_state(CHUNK_ID, version_id=VERSION_ID)]
    )
    conversation_app.dependency_overrides[get_conversation_repository] = lambda: repository
    conversation_app.dependency_overrides[get_evidence_repository] = lambda: evidence

    async with conversation_app.router.lifespan_context(conversation_app):
        async with _client(conversation_app) as client:
            response = await client.get(
                f"/api/v1/conversations/{CONVERSATION_ID}/messages"
            )

    assert response.status_code == 200, response.text
    assistant_message = response.json()["messages"][1]
    assert assistant_message["citations"][0]["isCurrentVersion"] is False


@pytest.mark.anyio
async def test_history_returns_404_for_other_owner(conversation_app: Any) -> None:
    other = FakeConversationRepository(None)
    conversation_app.dependency_overrides[get_conversation_repository] = lambda: other
    conversation_app.dependency_overrides[get_evidence_repository] = (
        lambda: FakeEvidenceRepository([])
    )

    async with conversation_app.router.lifespan_context(conversation_app):
        async with _client(conversation_app) as client:
            response = await client.get(
                f"/api/v1/conversations/{CONVERSATION_ID}/messages"
            )

    assert response.status_code == 404
    assert response.json()["code"] == CODE_CONVERSATION_NOT_FOUND


@pytest.mark.anyio
async def test_citation_detail_returns_404_when_source_revoked(
    conversation_app: Any,
) -> None:
    from test_conversation_service import _prior_turn

    _user, _assistant, citation = _prior_turn("旧回答")
    repository = FakeConversationRepository(_conversation(), citations=[citation])
    evidence = FakeEvidenceRepository(
        [], states=[_state(CHUNK_ID, version_id=VERSION_ID, member_active=False)]
    )
    conversation_app.dependency_overrides[get_conversation_repository] = lambda: repository
    conversation_app.dependency_overrides[get_evidence_repository] = lambda: evidence

    async with conversation_app.router.lifespan_context(conversation_app):
        async with _client(conversation_app) as client:
            response = await client.get(f"/api/v1/citations/{citation.id}")

    assert response.status_code == 404
    assert response.json()["code"] == CODE_CITATION_NOT_FOUND


@pytest.mark.anyio
async def test_citation_detail_maps_locator_for_authorized_source(
    conversation_app: Any,
) -> None:
    from test_conversation_service import _prior_turn

    _user, _assistant, citation = _prior_turn("仍可回答")
    repository = FakeConversationRepository(_conversation(), citations=[citation])
    evidence = FakeEvidenceRepository(
        [], states=[_state(CHUNK_ID, version_id=VERSION_ID)]
    )
    conversation_app.dependency_overrides[get_conversation_repository] = lambda: repository
    conversation_app.dependency_overrides[get_evidence_repository] = lambda: evidence

    async with conversation_app.router.lifespan_context(conversation_app):
        async with _client(conversation_app) as client:
            response = await client.get(f"/api/v1/citations/{citation.id}")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["citationId"] == str(citation.id)
    assert payload["documentTitle"] == "制度文档"
    assert payload["version"] == 1
    assert payload["isCurrentVersion"] is True
    assert payload["quote"] == "旧引文"
    assert payload["locator"] == {"page": 1}


@pytest.mark.anyio
async def test_citation_detail_returns_404_for_other_owner(
    conversation_app: Any,
) -> None:
    repository = FakeConversationRepository(_conversation())
    conversation_app.dependency_overrides[get_conversation_repository] = lambda: repository
    conversation_app.dependency_overrides[get_evidence_repository] = (
        lambda: FakeEvidenceRepository([])
    )

    async with conversation_app.router.lifespan_context(conversation_app):
        async with _client(conversation_app) as client:
            response = await client.get(f"/api/v1/citations/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["code"] == CODE_CITATION_NOT_FOUND


# --- 生成选项白名单 ---------------------------------------------------------


class _UnusedGenerator:
    """非法选项或无效请求不得进入生成；被调用即断言失败。"""

    def __init__(self) -> None:
        self.calls = 0

    def generate(
        self, messages: Any, *, model: str, max_output_tokens: int, thinking: Any = None
    ) -> Any:
        self.calls += 1
        raise AssertionError("生成模型不应被调用")

    def close(self) -> None:
        return None


def _generation_enabled_app() -> Any:
    """开启生成并覆盖生成客户端；用于把请求选项校验走到 422 而不联网。"""

    app = create_app(_settings(llm_enabled=True, llm_api_key="unit-key"))
    app.dependency_overrides[get_auth_context] = _context
    app.dependency_overrides[get_prompt_estimator] = _FakeEstimator
    app.dependency_overrides[get_query_analyzer] = _FakeAnalyzer
    app.dependency_overrides[get_query_embedder] = _FakeEmbedder
    app.dependency_overrides[get_conversation_repository] = (
        lambda: FakeConversationRepository(_conversation())
    )
    app.dependency_overrides[get_evidence_repository] = lambda: FakeEvidenceRepository([])
    app.dependency_overrides[get_answer_generator] = _UnusedGenerator
    return app


@pytest.mark.anyio
async def test_append_message_rejects_unverified_model() -> None:
    app = _generation_enabled_app()
    async with app.router.lifespan_context(app):
        async with _client(app) as client:
            response = await client.post(
                f"/api/v1/conversations/{CONVERSATION_ID}/messages",
                json={"question": "问题", "model": "deepseek-v4-pro"},
                headers={CSRF_HEADER_NAME: CSRF_TOKEN, "Origin": ORIGIN},
            )

    assert response.status_code == 422
    assert response.json()["code"] == CODE_GENERATION_OPTION_UNSUPPORTED


@pytest.mark.parametrize(
    "payload",
    [
        # 关闭/省略 thinking 时提交强度：组合无意义，必须拒绝。
        {"question": "问题", "reasoningEffort": "high"},
        {"question": "问题", "thinking": {"type": "disabled"}, "reasoningEffort": "max"},
        # 未知参数（如自定义 endpoint）不得被静默忽略。
        {"question": "问题", "baseUrl": "https://example.invalid"},
        {"question": "问题", "thinking": {"type": "enabled", "extra": 1}},
    ],
)
@pytest.mark.anyio
async def test_append_message_rejects_invalid_option_combinations(
    payload: dict[str, Any],
) -> None:
    app = _generation_enabled_app()
    async with app.router.lifespan_context(app):
        async with _client(app) as client:
            response = await client.post(
                f"/api/v1/conversations/{CONVERSATION_ID}/messages",
                json=payload,
                headers={CSRF_HEADER_NAME: CSRF_TOKEN, "Origin": ORIGIN},
            )

    assert response.status_code == 422
    assert response.json()["code"] == CODE_VALIDATION_ERROR


def test_resolve_thinking_maps_request_to_render_choice() -> None:
    """请求开关/强度到渲染选项的映射：省略或 disabled 归为非思考，省略强度用官方默认。"""

    from rag_backend.api.conversations import _resolve_thinking
    from rag_backend.generation.deepseek_prompt import NON_THINKING, ThinkingChoice
    from rag_backend.schemas.conversation import ThinkingRequest

    assert _resolve_thinking(None, None) == NON_THINKING
    assert _resolve_thinking(ThinkingRequest(type="disabled"), None) == NON_THINKING
    assert _resolve_thinking(ThinkingRequest(type="enabled"), None) == ThinkingChoice(
        enabled=True
    )
    assert _resolve_thinking(ThinkingRequest(type="enabled"), "max") == ThinkingChoice(
        enabled=True, effort="max"
    )
