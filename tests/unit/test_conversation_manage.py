"""会话改名/置顶/删除路由的进程内 HTTP 契约测试：假仓储与假依赖，不连数据库、不调用模型。

覆盖：写操作强制 Origin 与 CSRF、owner/组织隔离统一 404、软删后列表/历史/引用/追加追问全部拒绝、
重复删除幂等、严格输入（空请求体/空白标题/超长标题 422），以及删除当前会话后回到可新建的空态。
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from rag_backend.api.conversations import (
    get_answer_generator,
    get_conversation_list_repository,
    get_conversation_repository,
    get_evidence_repository,
    get_prompt_estimator,
)
from rag_backend.api.errors import CODE_CONVERSATION_NOT_FOUND, CODE_CSRF_INVALID
from rag_backend.api.retrieval import get_query_analyzer, get_query_embedder
from rag_backend.app import create_app
from rag_backend.auth.context import AuthContext
from rag_backend.auth.dependencies import get_auth_context
from rag_backend.auth.tokens import CSRF_HEADER_NAME
from rag_backend.config import Settings
from rag_backend.conversation.repository import ConversationSummaryRow
from test_conversation_service import (
    CONVERSATION_ID,
    ORG_ID,
    USER_ID,
    FakeConversationRepository,
    FakeEvidenceRepository,
    _conversation,
    _prior_turn,
)

ORIGIN = "http://127.0.0.1"
CSRF_TOKEN = "unit-manage-csrf-token"


def _settings() -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "session_cookie_secure": False,
        "allowed_origins": ORIGIN,
        "csrf_secret": "unit-conversation-manage-secret",
    }
    return Settings(**values)


def _context() -> AuthContext:
    return AuthContext(
        user_id=USER_ID,
        organization_id=ORG_ID,
        username="owner",
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
        raise AssertionError("已删除或无权会话不应调用查询编码")

    def close(self) -> None:
        return None


class _FakeEstimator:
    def estimate_chat_tokens(self, messages: Any) -> int:
        return 1


class _UnusedGenerator:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, messages: Any, *, max_output_tokens: int) -> Any:
        self.calls += 1
        raise AssertionError("已删除会话不应调用生成模型")

    def close(self) -> None:
        return None


class ManageRepository(FakeConversationRepository):
    """在会话单测假仓储上补齐列表读取；删除后列表为空。"""

    async def list_conversations(
        self, *, owner_id: uuid.UUID, organization_id: uuid.UUID
    ) -> list[ConversationSummaryRow]:
        conversation = self.conversation
        if conversation is None or conversation.deleted_at is not None:
            return []
        if (
            conversation.owner_id != owner_id
            or conversation.organization_id != organization_id
        ):
            return []
        return [
            ConversationSummaryRow(
                id=conversation.id,
                kb_scope=conversation.kb_scope,
                created_at=conversation.created_at,
                last_message_at=max(
                    (message.created_at for message in self.messages), default=None
                ),
                title=conversation.title,
                pinned_at=conversation.pinned_at,
            )
        ]


@pytest.fixture
def manage_app() -> Any:
    app = create_app(_settings())
    app.dependency_overrides[get_auth_context] = _context
    app.dependency_overrides[get_prompt_estimator] = _FakeEstimator
    app.dependency_overrides[get_query_analyzer] = _FakeAnalyzer
    app.dependency_overrides[get_query_embedder] = _FakeEmbedder
    app.dependency_overrides[get_answer_generator] = _UnusedGenerator
    yield app


def _client(app: Any) -> AsyncClient:
    transport = ASGITransport(app=app, client=("10.20.0.1", 12345))
    return AsyncClient(transport=transport, base_url="http://127.0.0.1")


def _install(app: Any, repository: Any, evidence: Any | None = None) -> None:
    app.dependency_overrides[get_conversation_repository] = lambda: repository
    app.dependency_overrides[get_conversation_list_repository] = lambda: repository
    app.dependency_overrides[get_evidence_repository] = lambda: (
        evidence if evidence is not None else FakeEvidenceRepository([])
    )


WRITE_HEADERS = {CSRF_HEADER_NAME: CSRF_TOKEN, "Origin": ORIGIN}


# --- PATCH --------------------------------------------------------------------


@pytest.mark.anyio
async def test_patch_requires_csrf(manage_app: Any) -> None:
    _install(manage_app, ManageRepository(_conversation()))
    async with manage_app.router.lifespan_context(manage_app):
        async with _client(manage_app) as client:
            response = await client.patch(
                f"/api/v1/conversations/{CONVERSATION_ID}",
                json={"title": "新标题"},
                headers={"Origin": ORIGIN},
            )

    assert response.status_code == 403
    assert response.json()["code"] == CODE_CSRF_INVALID


@pytest.mark.anyio
async def test_patch_requires_origin(manage_app: Any) -> None:
    _install(manage_app, ManageRepository(_conversation()))
    async with manage_app.router.lifespan_context(manage_app):
        async with _client(manage_app) as client:
            response = await client.patch(
                f"/api/v1/conversations/{CONVERSATION_ID}",
                json={"title": "新标题"},
                headers={CSRF_HEADER_NAME: CSRF_TOKEN},
            )

    assert response.status_code == 403
    assert response.json()["code"] == "ORIGIN_NOT_ALLOWED"


@pytest.mark.anyio
async def test_patch_renames_and_pins_then_unpins(manage_app: Any) -> None:
    repository = ManageRepository(_conversation())
    _install(manage_app, repository)
    async with manage_app.router.lifespan_context(manage_app):
        async with _client(manage_app) as client:
            renamed = await client.patch(
                f"/api/v1/conversations/{CONVERSATION_ID}",
                json={"title": "  季度制度  "},
                headers=WRITE_HEADERS,
            )
            pinned = await client.patch(
                f"/api/v1/conversations/{CONVERSATION_ID}",
                json={"pinned": True},
                headers=WRITE_HEADERS,
            )
            unpinned = await client.patch(
                f"/api/v1/conversations/{CONVERSATION_ID}",
                json={"pinned": False},
                headers=WRITE_HEADERS,
            )

    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["title"] == "季度制度"
    assert renamed.json()["pinned"] is False
    assert pinned.status_code == 200
    assert pinned.json()["pinned"] is True
    assert pinned.json()["title"] == "季度制度"
    assert unpinned.status_code == 200
    assert unpinned.json()["pinned"] is False
    assert repository.conversation is not None
    assert repository.conversation.pinned_at is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"title": "   "},
        {"title": "字" * 201},
    ],
)
async def test_patch_rejects_invalid_input(manage_app: Any, payload: dict[str, Any]) -> None:
    _install(manage_app, ManageRepository(_conversation()))
    async with manage_app.router.lifespan_context(manage_app):
        async with _client(manage_app) as client:
            response = await client.patch(
                f"/api/v1/conversations/{CONVERSATION_ID}",
                json=payload,
                headers=WRITE_HEADERS,
            )

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.anyio
async def test_patch_returns_404_for_unknown_or_other_owner(manage_app: Any) -> None:
    _install(manage_app, ManageRepository(None))
    async with manage_app.router.lifespan_context(manage_app):
        async with _client(manage_app) as client:
            response = await client.patch(
                f"/api/v1/conversations/{CONVERSATION_ID}",
                json={"title": "新标题"},
                headers=WRITE_HEADERS,
            )

    assert response.status_code == 404
    assert response.json()["code"] == CODE_CONVERSATION_NOT_FOUND


@pytest.mark.anyio
async def test_patch_rejects_other_organization(manage_app: Any) -> None:
    other_org = replace(_conversation(), organization_id=uuid.uuid4())
    _install(manage_app, ManageRepository(other_org))
    async with manage_app.router.lifespan_context(manage_app):
        async with _client(manage_app) as client:
            response = await client.patch(
                f"/api/v1/conversations/{CONVERSATION_ID}",
                json={"title": "新标题"},
                headers=WRITE_HEADERS,
            )

    assert response.status_code == 404
    assert response.json()["code"] == CODE_CONVERSATION_NOT_FOUND


@pytest.mark.anyio
async def test_delete_rejects_other_organization(manage_app: Any) -> None:
    other_org = replace(_conversation(), organization_id=uuid.uuid4())
    _install(manage_app, ManageRepository(other_org))
    async with manage_app.router.lifespan_context(manage_app):
        async with _client(manage_app) as client:
            response = await client.delete(
                f"/api/v1/conversations/{CONVERSATION_ID}", headers=WRITE_HEADERS
            )

    assert response.status_code == 404
    assert response.json()["code"] == CODE_CONVERSATION_NOT_FOUND


@pytest.mark.anyio
async def test_patch_on_deleted_conversation_is_404(manage_app: Any) -> None:
    conversation = _conversation()
    repository = ManageRepository(conversation)
    repository.conversation = replace(conversation, deleted_at=datetime.now(UTC))
    _install(manage_app, repository)
    async with manage_app.router.lifespan_context(manage_app):
        async with _client(manage_app) as client:
            response = await client.patch(
                f"/api/v1/conversations/{CONVERSATION_ID}",
                json={"title": "新标题"},
                headers=WRITE_HEADERS,
            )

    assert response.status_code == 404
    assert response.json()["code"] == CODE_CONVERSATION_NOT_FOUND


# --- DELETE 与删除后读取拒绝 --------------------------------------------------


@pytest.mark.anyio
async def test_delete_soft_deletes_and_repeat_is_idempotent(manage_app: Any) -> None:
    repository = ManageRepository(_conversation())
    _install(manage_app, repository)
    async with manage_app.router.lifespan_context(manage_app):
        async with _client(manage_app) as client:
            first = await client.delete(
                f"/api/v1/conversations/{CONVERSATION_ID}", headers=WRITE_HEADERS
            )
            second = await client.delete(
                f"/api/v1/conversations/{CONVERSATION_ID}", headers=WRITE_HEADERS
            )

    assert first.status_code == 204
    assert second.status_code == 204
    assert repository.conversation is not None
    assert repository.conversation.deleted_at is not None


@pytest.mark.anyio
async def test_delete_requires_csrf(manage_app: Any) -> None:
    _install(manage_app, ManageRepository(_conversation()))
    async with manage_app.router.lifespan_context(manage_app):
        async with _client(manage_app) as client:
            response = await client.delete(
                f"/api/v1/conversations/{CONVERSATION_ID}", headers={"Origin": ORIGIN}
            )

    assert response.status_code == 403
    assert response.json()["code"] == CODE_CSRF_INVALID


@pytest.mark.anyio
async def test_delete_returns_404_for_unknown_or_other_owner(manage_app: Any) -> None:
    _install(manage_app, ManageRepository(None))
    async with manage_app.router.lifespan_context(manage_app):
        async with _client(manage_app) as client:
            response = await client.delete(
                f"/api/v1/conversations/{CONVERSATION_ID}", headers=WRITE_HEADERS
            )

    assert response.status_code == 404
    assert response.json()["code"] == CODE_CONVERSATION_NOT_FOUND


@pytest.mark.anyio
async def test_deleted_conversation_rejects_every_read_path(manage_app: Any) -> None:
    _user, _assistant, citation = _prior_turn("旧回答")
    repository = ManageRepository(_conversation(), citations=[citation])
    generator = _UnusedGenerator()
    manage_app.dependency_overrides[get_answer_generator] = lambda: generator
    _install(manage_app, repository)
    async with manage_app.router.lifespan_context(manage_app):
        async with _client(manage_app) as client:
            assert (
                await client.delete(
                    f"/api/v1/conversations/{CONVERSATION_ID}", headers=WRITE_HEADERS
                )
            ).status_code == 204
            listing = await client.get("/api/v1/conversations")
            messages = await client.get(
                f"/api/v1/conversations/{CONVERSATION_ID}/messages"
            )
            detail = await client.get(f"/api/v1/citations/{citation.id}")
            appended = await client.post(
                f"/api/v1/conversations/{CONVERSATION_ID}/messages",
                json={"question": "还能追问吗？"},
                headers=WRITE_HEADERS,
            )

    assert listing.status_code == 200
    assert listing.json()["conversations"] == []
    for response in (messages, detail, appended):
        assert response.status_code == 404, response.text
    assert messages.json()["code"] == CODE_CONVERSATION_NOT_FOUND
    assert detail.json()["code"] == "CITATION_NOT_FOUND"
    assert appended.json()["code"] == CODE_CONVERSATION_NOT_FOUND
    assert generator.calls == 0
