"""证据问答主流程在真实 PostgreSQL 上的验收（隔离破坏性测试库 + api 运行角色）。

覆盖：完整 HTTP 流程（创建会话、追问、历史、引用详情）、引用服务端映射与
``llm_usage`` 落账、无证据直接拒答且不调模型、provider 失败落失败事实并返回 502、
所有者隔离、KB 撤权与文档删除后历史隐藏/引用 404、版本变化的重检索一次与持续变化的
静态可重试状态、输入预算映射。生成客户端与查询编码器都是显式假实现，**不**代表真实
provider 或 inference 连通性。缺少守卫 DSN 时按既有契约跳过，绝不触碰开发库。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

import pytest
from alembic import command
from database_guard import DestructiveTestDatabase
from database_roles_guard import RoleTestDatabases, assert_destructive_matches_roles
from fastapi import Depends
from httpx import ASGITransport, AsyncClient
from rag_backend.api.conversations import (
    get_answer_generator,
    get_evidence_repository,
    get_prompt_estimator,
)
from rag_backend.api.errors import (
    CODE_CONVERSATION_NOT_FOUND,
    CODE_GENERATION_FAILED,
    CODE_KNOWLEDGE_BASE_NOT_FOUND,
)
from rag_backend.api.retrieval import get_query_analyzer, get_query_embedder
from rag_backend.app import create_app
from rag_backend.auth.context import AuthContext
from rag_backend.auth.dependencies import get_auth_context
from rag_backend.auth.tokens import CSRF_HEADER_NAME
from rag_backend.config import Settings
from rag_backend.database import get_database_session
from rag_backend.generation.deepseek_client import (
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    GenerationOutcome,
)
from rag_backend.generation.query_rewrite import REWRITE_STAGE, REWRITE_SYSTEM_PROMPT
from rag_backend.retrieval.repository import SqlRetrievalRepository
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession
from test_core_migration import alembic_config, alembic_revision, business_tables
from test_retrieval_flow import (
    FakeAnalyzer,
    FakeEmbedder,
    _vector,
    insert_kb,
    insert_member,
    insert_profile,
    insert_user,
    seed_ready_chain,
)

pytestmark = pytest.mark.integration

SCHEMA_REVISION = "20260927_0009"
ORIGIN = "http://127.0.0.1"
CSRF_TOKEN = "integration-conversation-csrf"

TRUNCATE_SQL = (
    "TRUNCATE citation, query_run, message, conversation, llm_usage, chunk_embedding, chunk, "
    "index_generation, outbox_event, ingest_job, document_version, document, knowledge_base, "
    "index_profile, kb_member, auth_session, user_account CASCADE"
)

LOCATOR = json.dumps({"locator_version": 1, "start_line": 3, "end_line": 4})
CHUNK_TEXT = "hello world 制度原文。"


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, Any]]:
    return ("asyncio", {"loop_factory": asyncio.SelectorEventLoop})


@pytest.fixture(scope="module")
def conversation_schema(
    destructive_test_database: DestructiveTestDatabase,
    role_test_databases: RoleTestDatabases,
) -> Iterator[Engine]:
    assert_destructive_matches_roles(destructive_test_database.url, role_test_databases)
    config = alembic_config(destructive_test_database.url)
    engine = create_engine(destructive_test_database.url, pool_pre_ping=True)
    owns_schema = False
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT current_database()"))
                == destructive_test_database.database_name
            )
            assert alembic_revision(connection) is None
            assert business_tables(connection) == set()
        owns_schema = True
        command.upgrade(config, SCHEMA_REVISION)
        yield engine
    finally:
        try:
            if owns_schema:
                command.downgrade(config, "base")
                with engine.connect() as connection:
                    assert alembic_revision(connection) is None
                    assert business_tables(connection) == set()
        finally:
            engine.dispose()


@pytest.fixture(autouse=True)
def clean_business_rows(conversation_schema: Engine) -> Iterator[None]:
    with conversation_schema.begin() as connection:
        connection.execute(text(TRUNCATE_SQL))
    yield


def make_settings(database_url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "database_url": database_url,
        "allowed_origins": ORIGIN,
        "csrf_secret": CSRF_TOKEN,
        "session_cookie_secure": False,
    }
    values.update(overrides)
    return Settings(**values)


def _context(user_id: uuid.UUID, organization_id: uuid.UUID) -> AuthContext:
    return AuthContext(
        user_id=user_id,
        organization_id=organization_id,
        username="qa-user",
        is_admin=False,
        session_id=uuid.uuid4(),
        csrf_token=CSRF_TOKEN,
    )


class FakeEstimator:
    def estimate_chat_tokens(self, messages: Sequence[Any]) -> int:
        return sum(1 + len(message.content) for message in messages)


class FakeGenerator:
    """按系统提示区分改写与回答两类调用，不联网；分别记录消息与调用次数。"""

    def __init__(
        self,
        outcomes: Sequence[GenerationOutcome],
        *,
        rewrite_outcomes: Sequence[GenerationOutcome] | None = None,
    ) -> None:
        self.outcomes = list(outcomes)
        self.rewrite_outcomes = list(
            rewrite_outcomes
            if rewrite_outcomes is not None
            else [_outcome(content=_rewrite_json())]
        )
        self.calls = 0
        self.answer_calls = 0
        self.rewrite_calls = 0
        # 捕获每次真实调用进入提示的消息，用于断言历史/改写是否实际入参。
        self.messages: list[tuple[Any, ...]] = []
        self.answer_messages: list[tuple[Any, ...]] = []
        self.rewrite_messages: list[tuple[Any, ...]] = []

    def generate(self, messages: Sequence[Any], *, max_output_tokens: int) -> GenerationOutcome:
        captured = tuple(messages)
        self.messages.append(captured)
        self.calls += 1
        if captured[0].content == REWRITE_SYSTEM_PROMPT:
            self.rewrite_calls += 1
            self.rewrite_messages.append(captured)
            pool = self.rewrite_outcomes
            index = min(self.rewrite_calls - 1, len(pool) - 1)
        else:
            self.answer_calls += 1
            self.answer_messages.append(captured)
            pool = self.outcomes
            index = min(self.answer_calls - 1, len(pool) - 1)
        return pool[index]

    def close(self) -> None:
        return None


class PoisoningEvidenceRepository(SqlRetrievalRepository):
    """在指定调用序号上返回“版本已变化”的证据，稳定复现交付前竞态。"""

    def __init__(self, session: Any, poison_calls: set[int]) -> None:
        super().__init__(session)
        self.poison_calls = poison_calls
        self.calls = 0

    async def load_evidence_chunks(
        self, *, user_id: uuid.UUID, organization_id: uuid.UUID, chunk_ids: Sequence[uuid.UUID]
    ) -> Any:
        self.calls += 1
        rows = await super().load_evidence_chunks(
            user_id=user_id, organization_id=organization_id, chunk_ids=chunk_ids
        )
        if self.calls in self.poison_calls:
            return [replace(row, version_id=uuid.uuid4()) for row in rows]
        return rows


def _poisoning_evidence(poison_calls: set[int]) -> Any:
    """构造带 Request Session 的依赖：在指定调用序号上模拟“版本已变化”。"""

    def dependency(
        session: AsyncSession = Depends(get_database_session),
    ) -> SqlRetrievalRepository:
        return PoisoningEvidenceRepository(session, poison_calls)

    return dependency


def _outcome(
    *,
    status: str = STATUS_SUCCEEDED,
    content: str | None = None,
    error_code: str | None = None,
) -> GenerationOutcome:
    succeeded = status == STATUS_SUCCEEDED
    return GenerationOutcome(
        status=status,
        error_code=error_code,
        usage_source="PROVIDER_REPORTED" if succeeded else "UNKNOWN",
        prompt_tokens=11 if succeeded else None,
        completion_tokens=3 if succeeded else None,
        prompt_cache_hit_tokens=1 if succeeded else None,
        prompt_cache_miss_tokens=10 if succeeded else None,
        latency_ms=55,
        content=content,
    )


def _answer_json(citation_ids: Sequence[str] = ("E1",)) -> str:
    return json.dumps(
        {
            "sentences": [{"text": "制度规定。", "citationIds": list(citation_ids)}],
            "insufficientEvidence": False,
            "followUp": None,
        },
        ensure_ascii=False,
    )


REWRITE_STANDALONE = "制度适用范围是什么？"


def _rewrite_json(standalone: str = REWRITE_STANDALONE) -> str:
    return json.dumps({"standaloneQuestion": standalone}, ensure_ascii=False)


class RecordingEmbedder(FakeEmbedder):
    """记录每条被编码的查询文本，用于验证追问用的是改写后的独立问题。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.queries: list[str] = []

    def embed_query(self, text: str, expected_model_revision: str) -> Any:
        self.queries.append(text)
        return super().embed_query(text, expected_model_revision)


@asynccontextmanager
async def qa_client(
    settings: Settings,
    *,
    context: AuthContext,
    generator: FakeGenerator,
    evidence_factory: Any = None,
    analyzer_terms: str = "hello",
    analyzer: Any = None,
    embedder: Any = None,
) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    app.dependency_overrides[get_auth_context] = lambda: context
    app.dependency_overrides[get_query_analyzer] = lambda: (
        analyzer if analyzer is not None else FakeAnalyzer(terms=analyzer_terms)
    )
    app.dependency_overrides[get_query_embedder] = lambda: (
        embedder if embedder is not None else FakeEmbedder()
    )
    app.dependency_overrides[get_prompt_estimator] = lambda: FakeEstimator()
    app.dependency_overrides[get_answer_generator] = lambda: generator
    if evidence_factory is not None:
        app.dependency_overrides[get_evidence_repository] = evidence_factory
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, client=("10.20.0.1", 12345))
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            yield client


async def _create_conversation(client: AsyncClient, kb_id: uuid.UUID) -> uuid.UUID:
    return await _create_conversation_with_kbs(client, [kb_id])


async def _create_conversation_with_kbs(
    client: AsyncClient, kb_ids: Sequence[uuid.UUID]
) -> uuid.UUID:
    response = await client.post(
        "/api/v1/conversations",
        json={"kbIds": [str(kb_id) for kb_id in kb_ids]},
        headers={CSRF_HEADER_NAME: CSRF_TOKEN, "Origin": ORIGIN},
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["conversationId"])


async def _ask(client: AsyncClient, conversation_id: uuid.UUID, question: str) -> Any:
    return await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"question": question, "requestId": "req-1"},
        headers={CSRF_HEADER_NAME: CSRF_TOKEN, "Origin": ORIGIN},
    )


def _seed_searchable(
    engine: Engine, *, organization_id: uuid.UUID, analyzer_terms: str = "hello"
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """建一个可检索 KB：READY generation + chunk + embedding，并返回 (kb, user, chunk)。"""

    user_id = insert_user(engine, organization_id=organization_id)
    profile_id = insert_profile(engine, revision="rev-1")
    kb_id = insert_kb(engine, organization_id=organization_id, active_profile_id=profile_id)
    insert_member(engine, kb_id=kb_id, user_id=user_id)
    seeded = seed_ready_chain(
        engine,
        organization_id=organization_id,
        kb_id=kb_id,
        profile_id=profile_id,
        embedding=_vector(1.0, 0.0),
        fts_terms=analyzer_terms,
        source_locator=LOCATOR,
    )
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE chunk SET text = :text WHERE id = :chunk_id"),
            {"text": CHUNK_TEXT, "chunk_id": seeded.chunk_id},
        )
    return kb_id, user_id, seeded.chunk_id


def _active_profile_id(engine: Engine, kb_id: uuid.UUID) -> uuid.UUID:
    with engine.connect() as connection:
        value = connection.scalar(
            text("SELECT active_index_profile_id FROM knowledge_base WHERE id = :id"),
            {"id": kb_id},
        )
    assert value is not None
    return uuid.UUID(str(value))


def _seed_null_profile_kb(
    engine: Engine, *, organization_id: uuid.UUID, user_id: uuid.UUID
) -> uuid.UUID:
    kb_id = insert_kb(engine, organization_id=organization_id, active_profile_id=None)
    insert_member(engine, kb_id=kb_id, user_id=user_id)
    return kb_id


def _llm_usage_count(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(connection.scalar(text("SELECT count(*) FROM llm_usage")))


def _message_role_pairs(engine: Engine) -> list[tuple[str, str]]:
    with engine.connect() as connection:
        return [
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                text("SELECT role, content FROM message ORDER BY sequence")
            )
        ]


# --- 完整 HTTP 流程 ----------------------------------------------------------


@pytest.mark.anyio
async def test_full_http_flow_maps_citations_and_records_usage(
    conversation_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    organization_id = uuid.uuid4()
    kb_id, user_id, _chunk_id = _seed_searchable(
        conversation_schema, organization_id=organization_id
    )
    generator = FakeGenerator([_outcome(content=_answer_json())])

    async with qa_client(
        make_settings(role_test_databases.api_url),
        context=_context(user_id, organization_id),
        generator=generator,
    ) as client:
        conversation_id = await _create_conversation(client, kb_id)
        answer = await _ask(client, conversation_id, "hello")
        assert answer.status_code == 200, answer.text
        payload = answer.json()

        assert payload["insufficientEvidence"] is False
        assert payload["answer"] == "制度规定。"
        assert len(payload["citations"]) == 1
        citation = payload["citations"][0]
        assert citation["displayLabel"] == "E1"
        assert citation["locator"] == json.loads(LOCATOR)
        assert citation["quote"] == CHUNK_TEXT[:500]
        assert citation["version"] == 1
        assert citation["documentTitle"].startswith("doc-")
        # 本地估算与 provider 实际用量分开呈现。
        assert payload["usage"]["localInputTokens"] is not None
        assert payload["usage"]["providerPromptTokens"] == 11
        assert payload["usage"]["providerCompletionTokens"] == 3
        assert payload["degradedStages"] == []

        history = await client.get(
            f"/api/v1/conversations/{conversation_id}/messages"
        )
        assert history.status_code == 200, history.text
        messages = history.json()["messages"]
        assert [message["role"] for message in messages] == ["user", "assistant"]
        assert messages[0]["content"] == "hello"
        assert messages[1]["citations"][0]["citationId"] == citation["citationId"]

        detail = await client.get(f"/api/v1/citations/{citation['citationId']}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["locator"] == json.loads(LOCATOR)
        assert detail.json()["quote"] == CHUNK_TEXT[:500]

    assert generator.calls == 1
    assert _llm_usage_count(conversation_schema) == 1
    assert _message_role_pairs(conversation_schema) == [
        ("user", "hello"),
        ("assistant", "制度规定。"),
    ]
    with conversation_schema.connect() as connection:
        row = connection.execute(
            text(
                "SELECT status, question, standalone_question, evidence_count, "
                "provider_prompt_tokens FROM query_run"
            )
        ).one()
    assert row[0] == "SUCCEEDED"
    assert row[1] == "hello"
    assert row[2] == "hello"
    assert row[3] == 1
    assert row[4] == 11


@pytest.mark.anyio
async def test_no_evidence_refuses_without_calling_generation(
    conversation_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    organization_id = uuid.uuid4()
    user_id = insert_user(conversation_schema, organization_id=organization_id)
    kb_id = insert_kb(
        conversation_schema, organization_id=organization_id, active_profile_id=None
    )
    insert_member(conversation_schema, kb_id=kb_id, user_id=user_id)
    generator = FakeGenerator([_outcome(content=_answer_json())])

    async with qa_client(
        make_settings(role_test_databases.api_url),
        context=_context(user_id, organization_id),
        generator=generator,
    ) as client:
        conversation_id = await _create_conversation(client, kb_id)
        response = await _ask(client, conversation_id, "hello")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["insufficientEvidence"] is True
    assert payload["citations"] == []
    assert payload["degradedStages"] == []
    assert generator.calls == 0
    assert _llm_usage_count(conversation_schema) == 0
    assert _message_role_pairs(conversation_schema) == [
        ("user", "hello"),
        ("assistant", payload["answer"]),
    ]
    # NULL active profile 的 KB 不入实际检索范围：快照为空、证据数为 0。
    with conversation_schema.connect() as connection:
        row = connection.execute(
            text("SELECT scope_snapshot, evidence_count, status FROM query_run")
        ).one()
    assert row[0] == []
    assert row[1] == 0
    assert row[2] == "REFUSED"


@pytest.mark.anyio
async def test_generation_failure_records_failed_usage_and_returns_502(
    conversation_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    organization_id = uuid.uuid4()
    kb_id, user_id, _chunk_id = _seed_searchable(
        conversation_schema, organization_id=organization_id
    )
    generator = FakeGenerator(
        [_outcome(status=STATUS_FAILED, error_code="HTTP_500")]
    )

    async with qa_client(
        make_settings(role_test_databases.api_url),
        context=_context(user_id, organization_id),
        generator=generator,
    ) as client:
        conversation_id = await _create_conversation(client, kb_id)
        response = await _ask(client, conversation_id, "hello")

    assert response.status_code == 502
    assert response.json()["code"] == CODE_GENERATION_FAILED
    assert _llm_usage_count(conversation_schema) == 1
    with conversation_schema.connect() as connection:
        row = connection.execute(
            text("SELECT status, error_code, prompt_tokens FROM llm_usage")
        ).one()
    assert row[0] == "FAILED"
    assert row[1] == "HTTP_500"
    assert row[2] is None
    # 失败尝试不留下半轮消息或引用。
    assert _message_role_pairs(conversation_schema) == []


@pytest.mark.anyio
async def test_input_budget_is_enforced_before_generation(
    conversation_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    organization_id = uuid.uuid4()
    kb_id, user_id, _chunk_id = _seed_searchable(
        conversation_schema, organization_id=organization_id
    )
    generator = FakeGenerator([_outcome(content=_answer_json())])

    async with qa_client(
        make_settings(role_test_databases.api_url, llm_input_token_budget=1),
        context=_context(user_id, organization_id),
        generator=generator,
    ) as client:
        conversation_id = await _create_conversation(client, kb_id)
        response = await _ask(client, conversation_id, "hello")

    assert response.status_code == 422
    assert response.json()["code"] == "CONVERSATION_QUESTION_TOO_LONG"
    assert generator.calls == 0
    assert _llm_usage_count(conversation_schema) == 0


# --- 所有者隔离与撤权/删除 ----------------------------------------------------


@pytest.mark.anyio
async def test_owner_isolation_hides_conversation_and_citations(
    conversation_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    organization_id = uuid.uuid4()
    kb_id, user_id, _chunk_id = _seed_searchable(
        conversation_schema, organization_id=organization_id
    )
    other_user_id = insert_user(conversation_schema, organization_id=organization_id)
    insert_member(conversation_schema, kb_id=kb_id, user_id=other_user_id)
    generator = FakeGenerator([_outcome(content=_answer_json())])

    async with qa_client(
        make_settings(role_test_databases.api_url),
        context=_context(user_id, organization_id),
        generator=generator,
    ) as client:
        conversation_id = await _create_conversation(client, kb_id)
        answer = await _ask(client, conversation_id, "hello")
        citation_id = answer.json()["citations"][0]["citationId"]

    async with qa_client(
        make_settings(role_test_databases.api_url),
        context=_context(other_user_id, organization_id),
        generator=FakeGenerator([_outcome(content=_answer_json())]),
    ) as other_client:
        history = await other_client.get(
            f"/api/v1/conversations/{conversation_id}/messages"
        )
        assert history.status_code == 404
        assert history.json()["code"] == CODE_CONVERSATION_NOT_FOUND

        detail = await other_client.get(f"/api/v1/citations/{citation_id}")
        assert detail.status_code == 404
        assert detail.json()["code"] == "CITATION_NOT_FOUND"


@pytest.mark.anyio
async def test_revoked_membership_hides_history_and_blocks_question(
    conversation_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    organization_id = uuid.uuid4()
    kb_id, user_id, _chunk_id = _seed_searchable(
        conversation_schema, organization_id=organization_id
    )
    generator = FakeGenerator([_outcome(content=_answer_json())])

    async with qa_client(
        make_settings(role_test_databases.api_url),
        context=_context(user_id, organization_id),
        generator=generator,
    ) as client:
        conversation_id = await _create_conversation(client, kb_id)
        answer = await _ask(client, conversation_id, "hello")
        citation_id = answer.json()["citations"][0]["citationId"]

        with conversation_schema.begin() as connection:
            connection.execute(
                text(
                    "UPDATE kb_member SET revoked_at = now() WHERE kb_id = :kb_id "
                    "AND user_id = :user_id"
                ),
                {"kb_id": kb_id, "user_id": user_id},
            )

        history = await client.get(
            f"/api/v1/conversations/{conversation_id}/messages"
        )
        assert history.status_code == 200
        # 撤权后来源衍生的助手消息整体隐藏，只留下用户问题。
        assert [message["role"] for message in history.json()["messages"]] == ["user"]

        detail = await client.get(f"/api/v1/citations/{citation_id}")
        assert detail.status_code == 404
        assert detail.json()["code"] == "CITATION_NOT_FOUND"

        blocked = await _ask(client, conversation_id, "hello")
        assert blocked.status_code == 404
        assert blocked.json()["code"] == CODE_KNOWLEDGE_BASE_NOT_FOUND

    assert generator.calls == 1


@pytest.mark.anyio
async def test_deleted_document_hides_history_and_citation(
    conversation_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    organization_id = uuid.uuid4()
    kb_id, user_id, _chunk_id = _seed_searchable(
        conversation_schema, organization_id=organization_id
    )
    generator = FakeGenerator([_outcome(content=_answer_json())])

    async with qa_client(
        make_settings(role_test_databases.api_url),
        context=_context(user_id, organization_id),
        generator=generator,
    ) as client:
        conversation_id = await _create_conversation(client, kb_id)
        answer = await _ask(client, conversation_id, "hello")
        citation_id = answer.json()["citations"][0]["citationId"]

        with conversation_schema.begin() as connection:
            connection.execute(
                text(
                    "UPDATE document SET deleted_at = now(), "
                    "lifecycle_status = 'DELETED'"
                )
            )

        history = await client.get(
            f"/api/v1/conversations/{conversation_id}/messages"
        )
        assert [message["role"] for message in history.json()["messages"]] == ["user"]

        detail = await client.get(f"/api/v1/citations/{citation_id}")
        assert detail.status_code == 404


# --- 版本竞态 ---------------------------------------------------------------


@pytest.mark.anyio
async def test_version_change_after_generation_retrieves_once_then_succeeds(
    conversation_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    organization_id = uuid.uuid4()
    kb_id, user_id, _chunk_id = _seed_searchable(
        conversation_schema, organization_id=organization_id
    )
    generator = FakeGenerator(
        [_outcome(content=_answer_json()), _outcome(content=_answer_json())]
    )

    async with qa_client(
        make_settings(role_test_databases.api_url),
        context=_context(user_id, organization_id),
        generator=generator,
        evidence_factory=_poisoning_evidence({2}),
    ) as client:
        conversation_id = await _create_conversation(client, kb_id)
        response = await _ask(client, conversation_id, "hello")

    assert response.status_code == 200, response.text
    assert response.json()["insufficientEvidence"] is False
    assert generator.calls == 2
    # 两次 provider attempt 都落账，最终交付的是重检索后的答案。
    assert _llm_usage_count(conversation_schema) == 2
    assert len(_message_role_pairs(conversation_schema)) == 2
    # 来源变化触发的重检索是真实异常，落静态阶段标识。
    with conversation_schema.connect() as connection:
        stages = connection.scalar(text("SELECT degraded_stages FROM query_run"))
    assert stages == ["source_retry"]


@pytest.mark.anyio
async def test_persistent_version_change_returns_retryable_status(
    conversation_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    organization_id = uuid.uuid4()
    kb_id, user_id, _chunk_id = _seed_searchable(
        conversation_schema, organization_id=organization_id
    )
    generator = FakeGenerator(
        [_outcome(content=_answer_json()), _outcome(content=_answer_json())]
    )

    async with qa_client(
        make_settings(role_test_databases.api_url),
        context=_context(user_id, organization_id),
        generator=generator,
        evidence_factory=_poisoning_evidence({2, 4}),
    ) as client:
        conversation_id = await _create_conversation(client, kb_id)
        response = await _ask(client, conversation_id, "hello")

    assert response.status_code == 409
    assert response.json()["code"] == "CONVERSATION_SOURCES_CHANGED"
    assert generator.calls == 2
    assert _llm_usage_count(conversation_schema) == 2
    # 持续变化不持久化半轮消息。
    assert _message_role_pairs(conversation_schema) == []


# --- 实际范围、历史实际入参、来源变化与旧版本展示 ---------------------------


@pytest.mark.anyio
async def test_query_run_scope_snapshot_records_resolved_searchable_subset(
    conversation_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    """名义范围包含不可检索 KB 时，query_run 快照只记实际可检索子集。"""

    organization_id = uuid.uuid4()
    kb_id, user_id, _chunk_id = _seed_searchable(
        conversation_schema, organization_id=organization_id
    )
    null_kb_id = _seed_null_profile_kb(
        conversation_schema, organization_id=organization_id, user_id=user_id
    )
    generator = FakeGenerator([_outcome(content=_answer_json())])

    async with qa_client(
        make_settings(role_test_databases.api_url),
        context=_context(user_id, organization_id),
        generator=generator,
    ) as client:
        conversation_id = await _create_conversation_with_kbs(client, [kb_id, null_kb_id])
        answer = await _ask(client, conversation_id, "hello")

    assert answer.status_code == 200, answer.text
    with conversation_schema.connect() as connection:
        row = connection.execute(
            text("SELECT scope_snapshot, evidence_count FROM query_run")
        ).one()
    assert row[0] == [str(kb_id)]
    assert row[1] == 1


@pytest.mark.anyio
async def test_second_round_history_enters_prompt_and_records_usage_facts(
    conversation_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    """真实 HTTP + 隔离 PG：第二轮历史实际进提示，逐项校验 query_run/llm_usage 事实。"""

    organization_id = uuid.uuid4()
    kb_id, user_id, _chunk_id = _seed_searchable(
        conversation_schema, organization_id=organization_id
    )
    generator = FakeGenerator(
        [_outcome(content=_answer_json()), _outcome(content=_answer_json())]
    )

    async with qa_client(
        make_settings(role_test_databases.api_url),
        context=_context(user_id, organization_id),
        generator=generator,
    ) as client:
        conversation_id = await _create_conversation(client, kb_id)
        first = await _ask(client, conversation_id, "hello 第一问")
        second = await _ask(client, conversation_id, "hello 第二问")

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    # 第一轮无历史不改写，第二轮改写一次后回答。
    assert generator.rewrite_calls == 1
    assert generator.answer_calls == 2
    second_prompt = "\n".join(
        message.content for message in generator.answer_messages[1]
    )
    assert "hello 第一问" in second_prompt
    assert "制度规定。" in second_prompt

    with conversation_schema.connect() as connection:
        runs = connection.execute(
            text("SELECT scope_snapshot, evidence_count, status FROM query_run")
        ).all()
        assert len(runs) == 2
        for run in runs:
            assert run[0] == [str(kb_id)]
            assert run[1] == 1
            assert run[2] == "SUCCEEDED"
        usage = connection.execute(
            text(
                "SELECT model, stage, status, usage_source, latency_ms, "
                "prompt_tokens, completion_tokens, error_code FROM llm_usage"
            )
        ).all()
    # 第二轮多出一次改写 attempt，每次 attempt 单独一行账本。
    assert len(usage) == 3
    for row in usage:
        assert row[0] == "deepseek-flash"
        assert row[1] in ("qa_answer", REWRITE_STAGE)
        assert row[2] == "SUCCEEDED"
        assert row[3] == "PROVIDER_REPORTED"
        assert row[4] == 55
        assert row[5] == 11
        assert row[6] == 3
        assert row[7] is None
    assert sorted(row[1] for row in usage) == ["qa_answer", "qa_answer", REWRITE_STAGE]


@pytest.mark.anyio
async def test_follow_up_rewrite_uses_standalone_for_retrieval(
    conversation_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    """真实 HTTP + 隔离 PG：第二轮先用独立问题检索，原问题与实际独立问题分开持久化。"""

    organization_id = uuid.uuid4()
    kb_id, user_id, _chunk_id = _seed_searchable(
        conversation_schema, organization_id=organization_id
    )
    generator = FakeGenerator(
        [_outcome(content=_answer_json()), _outcome(content=_answer_json())]
    )
    embedder = RecordingEmbedder()
    analyzer = FakeAnalyzer()

    async with qa_client(
        make_settings(role_test_databases.api_url),
        context=_context(user_id, organization_id),
        generator=generator,
        analyzer=analyzer,
        embedder=embedder,
    ) as client:
        conversation_id = await _create_conversation(client, kb_id)
        first = await _ask(client, conversation_id, "hello 第一问")
        second = await _ask(client, conversation_id, "hello 它的适用范围呢")

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    # 首轮不做额外调用；第二轮改写一次后回答。
    assert generator.rewrite_calls == 1
    assert generator.answer_calls == 2
    # 查询编码与关键词分析都实际用的是改写后的独立问题，而不是原始追问。
    assert embedder.queries == ["hello 第一问", REWRITE_STANDALONE]
    assert analyzer.inputs == ["hello 第一问", REWRITE_STANDALONE]
    # 回答提示仍保留原始问题，独立问题只作为检索上下文。
    second_answer_prompt = "\n".join(
        message.content for message in generator.answer_messages[1]
    )
    assert "hello 它的适用范围呢" in second_answer_prompt
    # 独立问题只用于检索，绝不出现在回答提示里。
    assert REWRITE_STANDALONE not in second_answer_prompt

    with conversation_schema.connect() as connection:
        runs = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                text("SELECT question, standalone_question FROM query_run")
            )
        }
        stages = connection.execute(
            text("SELECT stage FROM llm_usage ORDER BY created_at")
        ).scalars().all()
    assert runs == {
        "hello 第一问": "hello 第一问",
        "hello 它的适用范围呢": REWRITE_STANDALONE,
    }
    # 改写 attempt 单独记账，与两次回答记账分开。
    assert sorted(stages) == ["qa_answer", "qa_answer", REWRITE_STAGE]


@pytest.mark.anyio
async def test_deleted_member_row_hides_history_and_citation(
    conversation_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    """真实删除 kb_member 行：LEFT JOIN 出来 revoked_at 也是 NULL，必须显式判定行存在。"""

    organization_id = uuid.uuid4()
    kb_id, user_id, _chunk_id = _seed_searchable(
        conversation_schema, organization_id=organization_id
    )
    generator = FakeGenerator([_outcome(content=_answer_json())])

    async with qa_client(
        make_settings(role_test_databases.api_url),
        context=_context(user_id, organization_id),
        generator=generator,
    ) as client:
        conversation_id = await _create_conversation(client, kb_id)
        answer = await _ask(client, conversation_id, "hello")
        citation_id = answer.json()["citations"][0]["citationId"]

        with conversation_schema.begin() as connection:
            connection.execute(
                text("DELETE FROM kb_member WHERE kb_id = :kb_id AND user_id = :user_id"),
                {"kb_id": kb_id, "user_id": user_id},
            )

        history = await client.get(f"/api/v1/conversations/{conversation_id}/messages")
        assert history.status_code == 200, history.text
        assert [message["role"] for message in history.json()["messages"]] == ["user"]

        detail = await client.get(f"/api/v1/citations/{citation_id}")
        assert detail.status_code == 404
        assert detail.json()["code"] == "CITATION_NOT_FOUND"


@pytest.mark.anyio
async def test_cross_organization_source_hides_history_and_citation(
    conversation_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    """来源 KB 被移到别的组织：即使成员行还在，也不得显示或交付。"""

    organization_id = uuid.uuid4()
    kb_id, user_id, _chunk_id = _seed_searchable(
        conversation_schema, organization_id=organization_id
    )
    generator = FakeGenerator([_outcome(content=_answer_json())])

    async with qa_client(
        make_settings(role_test_databases.api_url),
        context=_context(user_id, organization_id),
        generator=generator,
    ) as client:
        conversation_id = await _create_conversation(client, kb_id)
        answer = await _ask(client, conversation_id, "hello")
        citation_id = answer.json()["citations"][0]["citationId"]

        with conversation_schema.begin() as connection:
            connection.execute(
                text("UPDATE knowledge_base SET organization_id = :other WHERE id = :kb_id"),
                {"other": uuid.uuid4(), "kb_id": kb_id},
            )

        history = await client.get(f"/api/v1/conversations/{conversation_id}/messages")
        assert [message["role"] for message in history.json()["messages"]] == ["user"]

        detail = await client.get(f"/api/v1/citations/{citation_id}")
        assert detail.status_code == 404


@pytest.mark.anyio
async def test_version_updated_history_keeps_old_answer_with_old_version_marker(
    conversation_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    """文档更新后旧引用仍授权：旧答案作为带旧版本标识的历史展示，不当当前事实。"""

    organization_id = uuid.uuid4()
    kb_id, user_id, chunk_id = _seed_searchable(
        conversation_schema, organization_id=organization_id
    )
    generator = FakeGenerator(
        [_outcome(content=_answer_json()), _outcome(content=_answer_json())]
    )

    async with qa_client(
        make_settings(role_test_databases.api_url),
        context=_context(user_id, organization_id),
        generator=generator,
    ) as client:
        conversation_id = await _create_conversation(client, kb_id)
        first = await _ask(client, conversation_id, "hello 第一问")
        assert first.status_code == 200, first.text
        first_citation = first.json()["citations"][0]
        assert first_citation["version"] == 1

        with conversation_schema.connect() as connection:
            document_id = connection.scalar(
                text("SELECT document_id FROM chunk WHERE id = :chunk_id"),
                {"chunk_id": chunk_id},
            )
        profile_id = _active_profile_id(conversation_schema, kb_id)
        # 发布并切换新版本；旧 chunk 仍在但已不是当前版本。
        seed_ready_chain(
            conversation_schema,
            organization_id=organization_id,
            kb_id=kb_id,
            profile_id=profile_id,
            document_id=document_id,
            version_no=2,
            fts_terms="hello",
            embedding=_vector(1.0, 0.0),
        )

        history = await client.get(f"/api/v1/conversations/{conversation_id}/messages")
        messages = history.json()["messages"]
        assert [message["role"] for message in messages] == ["user", "assistant"]
        assert messages[1]["content"] == "制度规定。"
        assert messages[1]["citations"][0]["version"] == 1

        detail = await client.get(f"/api/v1/citations/{first_citation['citationId']}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["version"] == 1

        second = await _ask(client, conversation_id, "hello 第二问")

    assert second.status_code == 200, second.text
    second_prompt = "\n".join(
        message.content for message in generator.answer_messages[1]
    )
    assert "hello 第一问" in second_prompt
    assert "制度规定。" in second_prompt
    # 当前回答引用的是切换后的新版本，而不是把旧答案当当前事实。
    assert second.json()["citations"][0]["version"] == 2


@pytest.mark.anyio
async def test_partial_citation_revocation_hides_whole_assistant_message(
    conversation_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    """只撤掉部分引用来源时整条助手消息隐藏，且不进入后续模型上下文。"""

    organization_id = uuid.uuid4()
    kb_id, user_id, _chunk_id = _seed_searchable(
        conversation_schema, organization_id=organization_id
    )
    profile_id = _active_profile_id(conversation_schema, kb_id)
    second_document = seed_ready_chain(
        conversation_schema,
        organization_id=organization_id,
        kb_id=kb_id,
        profile_id=profile_id,
        fts_terms="hello",
        embedding=_vector(1.0, 0.0),
    )
    generator = FakeGenerator(
        [
            _outcome(content=_answer_json(("E1", "E2"))),
            _outcome(content=_answer_json()),
        ]
    )

    async with qa_client(
        make_settings(role_test_databases.api_url),
        context=_context(user_id, organization_id),
        generator=generator,
    ) as client:
        conversation_id = await _create_conversation(client, kb_id)
        first = await _ask(client, conversation_id, "hello")
        assert first.status_code == 200, first.text
        assert {citation["displayLabel"] for citation in first.json()["citations"]} == {
            "E1",
            "E2",
        }

        with conversation_schema.begin() as connection:
            connection.execute(
                text(
                    "UPDATE document SET deleted_at = now(), lifecycle_status = 'DELETED' "
                    "WHERE id = :document_id"
                ),
                {"document_id": second_document.document_id},
            )

        history = await client.get(f"/api/v1/conversations/{conversation_id}/messages")
        assert [message["role"] for message in history.json()["messages"]] == ["user"]

        second = await _ask(client, conversation_id, "hello 第二问")

    assert second.status_code == 200, second.text
    assert generator.calls == 2
    second_prompt = "\n".join(
        message.content for message in generator.answer_messages[1]
    )
    assert "制度规定。" not in second_prompt
