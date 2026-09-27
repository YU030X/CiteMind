"""检索路由的进程内 HTTP 契约测试：假会话、假编码器、假分析器；不连数据库、不调模型。"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from rag_backend.api.errors import (
    CODE_AUTH_REQUIRED,
    CODE_KNOWLEDGE_BASE_NOT_FOUND,
    CODE_RETRIEVAL_ANALYZER_MISMATCH,
    CODE_RETRIEVAL_PROFILE_CONFLICT,
    CODE_RETRIEVAL_QUERY_INVALID,
    CODE_RETRIEVAL_UNAVAILABLE,
    CODE_VALIDATION_ERROR,
)
from rag_backend.api.retrieval import get_query_analyzer, get_query_embedder
from rag_backend.app import create_app
from rag_backend.auth.context import AuthContext
from rag_backend.auth.dependencies import get_auth_context
from rag_backend.config import Settings
from rag_backend.database import get_database_session
from rag_backend.retrieval.keyword_analyzer import KeywordAnalyzerError
from rag_backend.retrieval.query_embedding_client import (
    MAX_QUERY_CHARS,
    EmbeddedQuery,
    QueryEmbeddingBusyError,
    QueryEmbeddingInputError,
)

ORIGIN = "http://127.0.0.1"
USER_ID = uuid.uuid4()
ORGANIZATION_ID = uuid.uuid4()
PROFILE_ID = uuid.uuid4()
REVISION = "rev-1"
ANALYZER_ID = "test-analyzer"
QUERY_VECTOR: tuple[float, ...] = (0.0,) * 512


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "session_cookie_secure": False,
        "allowed_origins": ORIGIN,
        "csrf_secret": "unit-retrieval-csrf-secret",
    }
    values.update(overrides)
    return Settings(**values)


class FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> FakeResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class ScriptedSession:
    """按调用顺序返回预置结果；记录 rollback 次数。"""

    def __init__(self, results: list[FakeResult]) -> None:
        self._results = list(results)
        self.rollbacks = 0

    async def execute(self, statement: Any, parameters: Any = None) -> FakeResult:
        if not self._results:
            raise AssertionError("出现未预置的数据库调用")
        return self._results.pop(0)

    async def rollback(self) -> None:
        self.rollbacks += 1


class FakeEmbedder:
    def __init__(self, *, vector: tuple[float, ...] = QUERY_VECTOR) -> None:
        self.vector = vector
        self.revisions: list[str] = []
        self.closed = False

    def embed_query(self, text: str, expected_model_revision: str) -> EmbeddedQuery:
        self.revisions.append(expected_model_revision)
        return EmbeddedQuery(
            vector=self.vector, token_count=1, model_revision=expected_model_revision
        )

    def close(self) -> None:
        self.closed = True


class FakeAnalyzer:
    def __init__(self, *, terms: str = "hello", analyzer_id: str = ANALYZER_ID) -> None:
        self.terms = terms
        self.calls: list[str] = []
        self._analyzer_id = analyzer_id

    @property
    def analyzer_id(self) -> str:
        return self._analyzer_id

    def analyze(self, text: str) -> str:
        self.calls.append(text)
        return self.terms


class FailingEmbedder:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.closed = False

    def embed_query(self, text: str, expected_model_revision: str) -> EmbeddedQuery:
        raise self.error

    def close(self) -> None:
        self.closed = True


def scope_result(
    kb_id: uuid.UUID,
    *,
    profile_id: uuid.UUID | None = PROFILE_ID,
    model_revision: str | None = REVISION,
    keyword_analyzer_version: str | None = ANALYZER_ID,
) -> FakeResult:
    return FakeResult(
        [
            {
                "kb_id": kb_id,
                "profile_id": profile_id,
                "model_revision": model_revision,
                "dimension": 512,
                "normalize": True,
                "keyword_analyzer_version": keyword_analyzer_version,
            }
        ]
    )


def candidate_row(chunk_id: uuid.UUID) -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "document_id": uuid.uuid4(),
        "kb_id": uuid.uuid4(),
        "version_id": uuid.uuid4(),
    }


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def build_app(
    session: ScriptedSession,
    *,
    embedder: FakeEmbedder | FailingEmbedder | None = None,
    analyzer: FakeAnalyzer | None = None,
    override_auth: bool = True,
    app_settings: Settings | None = None,
) -> Any:
    app = create_app(app_settings if app_settings is not None else settings())
    app.dependency_overrides[get_database_session] = lambda: session
    if override_auth:
        app.dependency_overrides[get_auth_context] = lambda: AuthContext(
            user_id=USER_ID,
            organization_id=ORGANIZATION_ID,
            username="alice",
            is_admin=False,
            session_id=uuid.uuid4(),
            csrf_token="csrf",
        )
    if embedder is not None:
        app.dependency_overrides[get_query_embedder] = lambda: embedder
    if analyzer is not None:
        app.dependency_overrides[get_query_analyzer] = lambda: analyzer
    return app


async def post_search(app: Any, payload: dict[str, Any]) -> Any:
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        return await client.post("/api/v1/retrieval/search", json=payload)


@pytest.mark.anyio
async def test_success_returns_fused_candidates_in_camel_case() -> None:
    kb_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    shared = candidate_row(chunk_id)
    vector_row = {**shared, "distance": 0.25}
    keyword_row = {**shared, "score": 0.4}
    session = ScriptedSession(
        [scope_result(kb_id), FakeResult([vector_row]), FakeResult([keyword_row])]
    )
    embedder = FakeEmbedder()
    analyzer = FakeAnalyzer()
    app = build_app(session, embedder=embedder, analyzer=analyzer)

    response = await post_search(app, {"query": "hello", "kbIds": [str(kb_id)]})

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["candidates"]) == 1
    candidate = body["candidates"][0]
    assert candidate["chunkId"] == str(chunk_id)
    assert candidate["vectorRank"] == 1
    assert candidate["keywordRank"] == 1
    assert candidate["fusionRank"] == 1
    assert candidate["vectorScore"] == pytest.approx(0.75)
    assert candidate["keywordScore"] == pytest.approx(0.4)
    assert session.rollbacks == 2
    assert embedder.revisions == [REVISION]
    assert analyzer.calls == ["hello"]


@pytest.mark.anyio
async def test_kb_without_active_profile_returns_empty_without_embedding() -> None:
    kb_id = uuid.uuid4()
    session = ScriptedSession([scope_result(kb_id, profile_id=None, model_revision=None)])
    embedder = FakeEmbedder()
    app = build_app(session, embedder=embedder, analyzer=FakeAnalyzer())

    response = await post_search(app, {"query": "hello", "kbIds": [str(kb_id)]})

    assert response.status_code == 200
    assert response.json() == {"candidates": []}
    assert embedder.revisions == []
    assert session.rollbacks == 1


@pytest.mark.anyio
async def test_non_subset_kb_returns_404_without_leaking() -> None:
    allowed = uuid.uuid4()
    other = uuid.uuid4()
    session = ScriptedSession([scope_result(allowed)])
    app = build_app(session, embedder=FakeEmbedder(), analyzer=FakeAnalyzer())

    response = await post_search(app, {"query": "hello", "kbIds": [str(allowed), str(other)]})

    assert response.status_code == 404
    assert response.json()["code"] == CODE_KNOWLEDGE_BASE_NOT_FOUND
    assert session.rollbacks == 1


@pytest.mark.anyio
async def test_profile_conflict_returns_422() -> None:
    first = uuid.uuid4()
    second = uuid.uuid4()
    session = ScriptedSession(
        [
            FakeResult(
                [
                    {
                        "kb_id": first,
                        "profile_id": PROFILE_ID,
                        "model_revision": REVISION,
                        "dimension": 512,
                        "normalize": True,
                        "keyword_analyzer_version": ANALYZER_ID,
                    },
                    {
                        "kb_id": second,
                        "profile_id": uuid.uuid4(),
                        "model_revision": "rev-2",
                        "dimension": 512,
                        "normalize": True,
                        "keyword_analyzer_version": ANALYZER_ID,
                    },
                ]
            )
        ]
    )
    embedder = FakeEmbedder()
    app = build_app(session, embedder=embedder, analyzer=FakeAnalyzer())

    response = await post_search(app, {"query": "hello", "kbIds": [str(first), str(second)]})

    assert response.status_code == 422
    assert response.json()["code"] == CODE_RETRIEVAL_PROFILE_CONFLICT
    assert embedder.revisions == []


@pytest.mark.anyio
async def test_blank_query_and_empty_kb_ids_are_422() -> None:
    kb_id = uuid.uuid4()
    app = build_app(ScriptedSession([]), embedder=FakeEmbedder(), analyzer=FakeAnalyzer())

    blank = await post_search(app, {"query": "   ", "kbIds": [str(kb_id)]})
    empty = await post_search(app, {"query": "hello", "kbIds": []})

    assert blank.status_code == 422
    assert blank.json()["code"] == CODE_VALIDATION_ERROR
    assert empty.status_code == 422
    assert empty.json()["code"] == CODE_VALIDATION_ERROR


@pytest.mark.anyio
async def test_missing_inference_token_returns_static_503() -> None:
    kb_id = uuid.uuid4()
    app = build_app(
        ScriptedSession([]),
        analyzer=FakeAnalyzer(),
        app_settings=settings(inference_token=None),
    )

    response = await post_search(app, {"query": "hello", "kbIds": [str(kb_id)]})

    assert response.status_code == 503
    assert response.json()["code"] == CODE_RETRIEVAL_UNAVAILABLE


@pytest.mark.anyio
async def test_unavailable_keyword_analyzer_returns_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kb_id = uuid.uuid4()

    def boom() -> Any:
        raise KeywordAnalyzerError("unavailable")

    monkeypatch.setattr("rag_backend.api.retrieval.get_keyword_analyzer", boom)
    app = build_app(ScriptedSession([]), embedder=FakeEmbedder())

    response = await post_search(app, {"query": "hello", "kbIds": [str(kb_id)]})

    assert response.status_code == 503
    assert response.json()["code"] == CODE_RETRIEVAL_UNAVAILABLE


@pytest.mark.anyio
async def test_anonymous_request_returns_401() -> None:
    kb_id = uuid.uuid4()
    app = build_app(
        ScriptedSession([]),
        embedder=FakeEmbedder(),
        analyzer=FakeAnalyzer(),
        override_auth=False,
    )

    response = await post_search(app, {"query": "hello", "kbIds": [str(kb_id)]})

    assert response.status_code == 401
    assert response.json()["code"] == CODE_AUTH_REQUIRED


@pytest.mark.anyio
async def test_analyzer_identity_mismatch_returns_static_503() -> None:
    kb_id = uuid.uuid4()
    session = ScriptedSession(
        [scope_result(kb_id, keyword_analyzer_version="stale-analyzer")]
    )
    embedder = FakeEmbedder()
    app = build_app(session, embedder=embedder, analyzer=FakeAnalyzer())

    response = await post_search(app, {"query": "hello", "kbIds": [str(kb_id)]})

    assert response.status_code == 503
    assert response.json()["code"] == CODE_RETRIEVAL_ANALYZER_MISMATCH
    assert embedder.revisions == []
    assert session.rollbacks == 1


@pytest.mark.anyio
async def test_over_long_query_is_rejected_before_analyzer_and_encoder() -> None:
    kb_id = uuid.uuid4()
    embedder = FakeEmbedder()
    analyzer = FakeAnalyzer()
    app = build_app(ScriptedSession([]), embedder=embedder, analyzer=analyzer)

    response = await post_search(
        app, {"query": "x" * (MAX_QUERY_CHARS + 1), "kbIds": [str(kb_id)]}
    )

    assert response.status_code == 422
    assert response.json()["code"] == CODE_VALIDATION_ERROR
    assert analyzer.calls == []
    assert embedder.revisions == []


@pytest.mark.anyio
async def test_busy_encoder_returns_503_with_retry_after_details() -> None:
    kb_id = uuid.uuid4()
    session = ScriptedSession([scope_result(kb_id)])
    app = build_app(
        session,
        embedder=FailingEmbedder(
            QueryEmbeddingBusyError("busy", retry_after_seconds=2.0)
        ),
        analyzer=FakeAnalyzer(),
    )

    response = await post_search(app, {"query": "hello", "kbIds": [str(kb_id)]})

    assert response.status_code == 503
    assert response.json()["code"] == CODE_RETRIEVAL_UNAVAILABLE
    assert response.json()["details"] == {"retryAfter": 2.0}


@pytest.mark.anyio
async def test_encoder_input_rejection_returns_422_query_invalid() -> None:
    kb_id = uuid.uuid4()
    session = ScriptedSession([scope_result(kb_id)])
    app = build_app(
        session,
        embedder=FailingEmbedder(QueryEmbeddingInputError("invalid query")),
        analyzer=FakeAnalyzer(),
    )

    response = await post_search(app, {"query": "hello", "kbIds": [str(kb_id)]})

    assert response.status_code == 422
    assert response.json()["code"] == CODE_RETRIEVAL_QUERY_INVALID
