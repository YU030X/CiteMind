"""会话列表接口单测：排序、所有者隔离与不泄正文，不连数据库、不调用模型。

覆盖：按最近消息时间（无消息则创建时间）稳定倒序、``kbIds`` 只来自会话 ``kb_scope``、
响应只含 `id`/`kbIds`/`createdAt`/`lastMessageAt`，以及仓储单次查询用相关子查询聚合
且不选消息正文（编译语句静态检查，不连真实数据库）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from rag_backend.api.conversations import get_conversation_list_repository
from rag_backend.app import create_app
from rag_backend.auth.context import AuthContext
from rag_backend.auth.dependencies import get_auth_context
from rag_backend.config import Settings
from rag_backend.conversation.repository import (
    ConversationSummaryRow,
    SqlConversationRepository,
)
from rag_backend.conversation.service import list_owned_conversations

ORIGIN = "http://127.0.0.1"
USER_ID = uuid.uuid4()
ORG_ID = uuid.uuid4()
KB_A = uuid.uuid4()
KB_B = uuid.uuid4()
CONVERSATION_RECENT = uuid.uuid4()
CONVERSATION_EMPTY = uuid.uuid4()
CONVERSATION_OLD = uuid.uuid4()

CONVERSATION_KEYS = {"id", "title", "pinned", "kbIds", "createdAt", "lastMessageAt"}


def _row(
    conversation_id: uuid.UUID,
    *,
    kb_scope: tuple[uuid.UUID, ...],
    created_at: datetime,
    last_message_at: datetime | None,
    title: str | None = None,
    pinned_at: datetime | None = None,
) -> ConversationSummaryRow:
    return ConversationSummaryRow(
        id=conversation_id,
        kb_scope=kb_scope,
        created_at=created_at,
        last_message_at=last_message_at,
        title=title,
        pinned_at=pinned_at,
    )


class FakeConversationRepository:
    def __init__(self, rows: list[ConversationSummaryRow] | None = None) -> None:
        self.rows = list(rows or [])
        self.calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def list_conversations(
        self, *, owner_id: uuid.UUID, organization_id: uuid.UUID
    ) -> list[ConversationSummaryRow]:
        self.calls.append((owner_id, organization_id))
        return list(self.rows)


class _CapturingResult:
    def __init__(self) -> None:
        self._rows: list[Any] = []

    def all(self) -> list[Any]:
        return list(self._rows)

    def mappings(self) -> _CapturingResult:
        return self


class _CapturingSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> _CapturingResult:
        self.statements.append(statement)
        return _CapturingResult()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_list_owned_conversations_orders_by_recent_activity() -> None:
    rows = [
        _row(
            CONVERSATION_OLD,
            kb_scope=(KB_A,),
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            last_message_at=datetime(2026, 1, 2, tzinfo=UTC),
        ),
        _row(
            CONVERSATION_RECENT,
            kb_scope=(KB_A, KB_B),
            created_at=datetime(2025, 12, 1, tzinfo=UTC),
            last_message_at=datetime(2026, 1, 5, tzinfo=UTC),
        ),
        _row(
            CONVERSATION_EMPTY,
            kb_scope=(KB_B,),
            created_at=datetime(2026, 1, 4, tzinfo=UTC),
            last_message_at=None,
        ),
    ]

    views = await list_owned_conversations(
        FakeConversationRepository(rows), owner_id=USER_ID, organization_id=ORG_ID
    )

    assert [view.conversation_id for view in views] == [
        CONVERSATION_RECENT,
        CONVERSATION_EMPTY,
        CONVERSATION_OLD,
    ]
    assert views[0].kb_ids == (KB_A, KB_B)
    assert views[1].last_message_at is None


@pytest.mark.anyio
async def test_pinned_conversations_sort_before_recent_activity() -> None:
    rows = [
        _row(
            CONVERSATION_OLD,
            kb_scope=(KB_A,),
            created_at=datetime(2025, 12, 1, tzinfo=UTC),
            last_message_at=datetime(2026, 1, 2, tzinfo=UTC),
            title="已置顶",
            pinned_at=datetime(2025, 12, 31, tzinfo=UTC),
        ),
        _row(
            CONVERSATION_RECENT,
            kb_scope=(KB_A,),
            created_at=datetime(2025, 12, 1, tzinfo=UTC),
            last_message_at=datetime(2026, 1, 5, tzinfo=UTC),
        ),
    ]

    views = await list_owned_conversations(
        FakeConversationRepository(rows), owner_id=USER_ID, organization_id=ORG_ID
    )

    assert [view.conversation_id for view in views] == [CONVERSATION_OLD, CONVERSATION_RECENT]
    assert views[0].pinned is True
    assert views[0].title == "已置顶"
    assert views[1].pinned is False


@pytest.mark.anyio
async def test_list_conversations_query_uses_scope_without_message_content() -> None:
    session = _CapturingSession()
    repository = SqlConversationRepository(session)  # type: ignore[arg-type]

    await repository.list_conversations(owner_id=USER_ID, organization_id=ORG_ID)

    sql = str(session.statements[0])
    assert "FROM conversation" in sql
    assert "max(m.created_at)" in sql
    assert "content" not in sql
    assert "c.title" in sql
    assert "c.pinned_at" in sql
    assert "c.deleted_at IS NULL" in sql
    assert "c.owner_id = " in sql
    assert "c.organization_id = " in sql


def _settings() -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "session_cookie_secure": False,
        "allowed_origins": ORIGIN,
        "csrf_secret": "unit-conversation-list-secret",
    }
    return Settings(**values)


def _context() -> AuthContext:
    return AuthContext(
        user_id=USER_ID,
        organization_id=ORG_ID,
        username="reader",
        is_admin=False,
        session_id=uuid.uuid4(),
        csrf_token="unit-csrf-token",
    )


@pytest.fixture
def conversation_app() -> Any:
    app = create_app(_settings())
    app.dependency_overrides[get_auth_context] = _context
    yield app


def _client(app: Any) -> AsyncClient:
    transport = ASGITransport(app=app, client=("10.20.0.1", 12345))
    return AsyncClient(transport=transport, base_url="http://127.0.0.1")


@pytest.mark.anyio
async def test_list_conversations_returns_owner_scoped_summaries(
    conversation_app: Any,
) -> None:
    repository = FakeConversationRepository(
        [
            _row(
                CONVERSATION_RECENT,
                kb_scope=(KB_A, KB_B),
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                last_message_at=datetime(2026, 1, 5, tzinfo=UTC),
            ),
            _row(
                CONVERSATION_EMPTY,
                kb_scope=(KB_A,),
                created_at=datetime(2026, 1, 2, tzinfo=UTC),
                last_message_at=None,
            ),
        ]
    )
    conversation_app.dependency_overrides[get_conversation_list_repository] = lambda: repository

    async with conversation_app.router.lifespan_context(conversation_app):
        async with _client(conversation_app) as client:
            response = await client.get("/api/v1/conversations")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload.keys()) == {"conversations"}
    first = payload["conversations"][0]
    assert set(first.keys()) == CONVERSATION_KEYS
    assert first["id"] == str(CONVERSATION_RECENT)
    assert first["title"] is None
    assert first["pinned"] is False
    assert first["kbIds"] == [str(KB_A), str(KB_B)]
    assert datetime.fromisoformat(first["createdAt"].replace("Z", "+00:00")) == datetime(
        2026, 1, 1, tzinfo=UTC
    )
    assert datetime.fromisoformat(
        first["lastMessageAt"].replace("Z", "+00:00")
    ) == datetime(2026, 1, 5, tzinfo=UTC)
    assert payload["conversations"][1]["lastMessageAt"] is None
    assert repository.calls == [(USER_ID, ORG_ID)]
