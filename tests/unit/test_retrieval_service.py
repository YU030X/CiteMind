"""授权检索用例与范围解析单测：假仓储 + 假编码器，不连接数据库、不调用模型。"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import pytest
from rag_backend.retrieval.errors import (
    KnowledgeBaseNotAccessible,
    RetrievalAnalyzerMismatch,
    RetrievalEmbeddingError,
    RetrievalProfileConflict,
    RetrievalQueryInvalid,
    RetrievalScopeUnavailable,
)
from rag_backend.retrieval.fusion import RankedChunk
from rag_backend.retrieval.keyword_analyzer import KeywordAnalyzerInputError
from rag_backend.retrieval.query_embedding_client import (
    MAX_QUERY_CHARS,
    EmbeddedQuery,
    QueryEmbeddingBusyError,
    QueryEmbeddingNotReadyError,
)
from rag_backend.retrieval.repository import EvidenceChunkRow, KbScopeRow
from rag_backend.retrieval.rerank_client import (
    RerankInput,
    RerankScore,
    RerankUnavailableError,
)
from rag_backend.retrieval.service import (
    RERANK_TOP_K,
    RetrievalResult,
    resolve_retrieval_scope,
    search_authorized_chunks,
)

PROFILE_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
ORGANIZATION_ID = uuid.uuid4()
QUERY_VECTOR: tuple[float, ...] = (0.0,) * 512
ANALYZER_ID = "test-analyzer"


def scope_row(
    kb_id: uuid.UUID,
    *,
    profile_id: uuid.UUID | None = PROFILE_ID,
    model_revision: str | None = "rev-1",
    dimension: int | None = 512,
    normalize: bool | None = True,
    keyword_analyzer_version: str | None = ANALYZER_ID,
) -> KbScopeRow:
    return KbScopeRow(
        kb_id=kb_id,
        profile_id=profile_id,
        model_revision=model_revision,
        dimension=dimension,
        normalize=normalize,
        keyword_analyzer_version=keyword_analyzer_version,
    )


def ranked(score: float = 1.0) -> RankedChunk:
    return RankedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        kb_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        score=score,
    )


class FakeRepository:
    def __init__(
        self,
        rows: list[KbScopeRow],
        *,
        vector: list[RankedChunk] | None = None,
        keyword: list[RankedChunk] | None = None,
        events: list[str] | None = None,
        vector_error: Exception | None = None,
        release_error: Exception | None = None,
        release_error_call: int = 1,
        texts: dict[uuid.UUID, str] | None = None,
    ) -> None:
        self.rows = rows
        self.vector = vector if vector is not None else []
        self.keyword = keyword if keyword is not None else []
        self.events = events if events is not None else []
        self.vector_error = vector_error
        self.release_error = release_error
        self.release_error_call = release_error_call
        self.texts = texts
        self.released = 0
        self.vector_profile_id: uuid.UUID | None = None
        self.keyword_terms: str | None = None

    async def load_kb_scope(
        self,
        *,
        user_id: uuid.UUID,
        organization_id: uuid.UUID,
        kb_ids: Sequence[uuid.UUID],
    ) -> list[KbScopeRow]:
        self.events.append("load_scope")
        return self.rows

    async def release(self) -> None:
        self.events.append("release")
        self.released += 1
        if self.release_error is not None and self.released == self.release_error_call:
            raise self.release_error

    async def fetch_vector_candidates(
        self,
        *,
        user_id: uuid.UUID,
        organization_id: uuid.UUID,
        kb_ids: Sequence[uuid.UUID],
        profile_id: uuid.UUID,
        query_vector: Sequence[float],
    ) -> list[RankedChunk]:
        self.events.append("vector")
        self.vector_profile_id = profile_id
        if self.vector_error is not None:
            raise self.vector_error
        return self.vector

    async def fetch_keyword_candidates(
        self,
        *,
        user_id: uuid.UUID,
        organization_id: uuid.UUID,
        kb_ids: Sequence[uuid.UUID],
        profile_id: uuid.UUID,
        query_terms: str,
    ) -> list[RankedChunk]:
        self.events.append("keyword")
        self.keyword_terms = query_terms
        return self.keyword

    async def load_evidence_chunks(
        self,
        *,
        user_id: uuid.UUID,
        organization_id: uuid.UUID,
        chunk_ids: Sequence[uuid.UUID],
    ) -> list[EvidenceChunkRow]:
        self.events.append("load_texts")
        return [
            EvidenceChunkRow(
                chunk_id=chunk_id,
                document_id=uuid.uuid4(),
                kb_id=uuid.uuid4(),
                version_id=uuid.uuid4(),
                version_no=1,
                document_title="doc",
                text=self.texts[chunk_id],
                source_locator={},
            )
            for chunk_id in chunk_ids
            if self.texts is not None and chunk_id in self.texts
        ]


class FakeEmbedder:
    def __init__(self, events: list[str], *, vector: tuple[float, ...] = QUERY_VECTOR) -> None:
        self.events = events
        self.vector = vector
        self.revisions: list[str] = []
        self.closed = False

    def embed_query(self, text: str, expected_model_revision: str) -> EmbeddedQuery:
        self.events.append("embed")
        self.revisions.append(expected_model_revision)
        return EmbeddedQuery(
            vector=self.vector, token_count=1, model_revision=expected_model_revision
        )

    def close(self) -> None:
        self.closed = True


class FailingEmbedder:
    def __init__(self, events: list[str], error: Exception) -> None:
        self.events = events
        self.error = error

    def embed_query(self, text: str, expected_model_revision: str) -> EmbeddedQuery:
        self.events.append("embed")
        raise self.error

    def close(self) -> None:
        return None


class FakeAnalyzer:
    def __init__(
        self,
        events: list[str],
        *,
        terms: str = "hello world",
        error: Exception | None = None,
        analyzer_id: str = ANALYZER_ID,
    ) -> None:
        self.events = events
        self.terms = terms
        self.error = error
        self._analyzer_id = analyzer_id

    @property
    def analyzer_id(self) -> str:
        return self._analyzer_id

    def analyze(self, text: str) -> str:
        self.events.append("analyze")
        if self.error is not None:
            raise self.error
        return self.terms


class FakeReranker:
    """确定性重排替身：记录调用、可显式给分或注入可降级故障。"""

    def __init__(
        self,
        events: list[str],
        *,
        scores: Sequence[RerankScore] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.events = events
        self.scores = list(scores) if scores is not None else None
        self.error = error
        self.calls: list[tuple[str, list[RerankInput]]] = []
        self.closed = False

    def rerank(self, query: str, candidates: Sequence[RerankInput]) -> list[RerankScore]:
        self.events.append("rerank")
        self.calls.append((query, list(candidates)))
        if self.error is not None:
            raise self.error
        if self.scores is not None:
            return list(self.scores)
        return [
            RerankScore(candidate_id=candidate.candidate_id, score=float(len(candidate.text)))
            for candidate in candidates
        ]

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# --- 范围解析 ---------------------------------------------------------------


def test_scope_accepts_subset_and_keeps_only_profiled_kbs() -> None:
    first = uuid.uuid4()
    second = uuid.uuid4()
    rows = [scope_row(first), scope_row(second, profile_id=None)]

    scope = resolve_retrieval_scope(
        rows, requested_kb_ids=[first, second], expected_analyzer_id=ANALYZER_ID
    )

    assert scope is not None
    assert scope.kb_ids == (first,)
    assert scope.profile_id == PROFILE_ID
    assert scope.model_revision == "rev-1"
    assert scope.dimension == 512


def test_scope_rejects_kb_outside_authorized_rows() -> None:
    allowed = uuid.uuid4()
    other = uuid.uuid4()

    with pytest.raises(KnowledgeBaseNotAccessible):
        resolve_retrieval_scope(
            [scope_row(allowed)],
            requested_kb_ids=[allowed, other],
            expected_analyzer_id=ANALYZER_ID,
        )


def test_scope_conflicts_when_kbs_use_different_profiles() -> None:
    rows = [
        scope_row(uuid.uuid4(), profile_id=PROFILE_ID),
        scope_row(uuid.uuid4(), profile_id=uuid.uuid4(), model_revision="rev-2"),
    ]

    with pytest.raises(RetrievalProfileConflict):
        resolve_retrieval_scope(
            rows,
            requested_kb_ids=[row.kb_id for row in rows],
            expected_analyzer_id=ANALYZER_ID,
        )


def test_scope_is_none_when_every_kb_has_null_profile() -> None:
    kb_id = uuid.uuid4()

    assert (
        resolve_retrieval_scope(
            [scope_row(kb_id, profile_id=None)],
            requested_kb_ids=[kb_id],
            expected_analyzer_id=ANALYZER_ID,
        )
        is None
    )


def test_scope_fails_when_profile_columns_are_incomplete() -> None:
    kb_id = uuid.uuid4()
    rows = [scope_row(kb_id, model_revision=None, dimension=None)]

    with pytest.raises(RetrievalScopeUnavailable):
        resolve_retrieval_scope(
            rows, requested_kb_ids=[kb_id], expected_analyzer_id=ANALYZER_ID
        )


def test_scope_fails_when_profile_is_not_normalized() -> None:
    kb_id = uuid.uuid4()
    rows = [scope_row(kb_id, normalize=False)]

    with pytest.raises(RetrievalScopeUnavailable):
        resolve_retrieval_scope(
            rows, requested_kb_ids=[kb_id], expected_analyzer_id=ANALYZER_ID
        )


def test_scope_fails_when_keyword_analyzer_identity_differs() -> None:
    kb_id = uuid.uuid4()
    rows = [scope_row(kb_id, keyword_analyzer_version="other-analyzer")]

    with pytest.raises(RetrievalAnalyzerMismatch):
        resolve_retrieval_scope(
            rows, requested_kb_ids=[kb_id], expected_analyzer_id=ANALYZER_ID
        )


# --- 编排 -------------------------------------------------------------------


@pytest.mark.anyio
async def test_model_call_happens_after_connection_release() -> None:
    kb_id = uuid.uuid4()
    events: list[str] = []
    repository = FakeRepository([scope_row(kb_id)], events=events)
    embedder = FakeEmbedder(events)
    analyzer = FakeAnalyzer(events)

    result = await search_authorized_chunks(
        repository,
        user_id=USER_ID,
        organization_id=ORGANIZATION_ID,
        kb_ids=[kb_id],
        query="hello",
        embedder=embedder,
        analyzer=analyzer,
    )

    assert result.kb_ids == (kb_id,)
    assert result.candidates == ()
    assert events == [
        "load_scope",
        "release",
        "analyze",
        "embed",
        "vector",
        "keyword",
        "release",
    ]
    assert repository.released == 2
    assert embedder.revisions == ["rev-1"]
    assert repository.vector_profile_id == PROFILE_ID
    assert repository.keyword_terms == "hello world"


@pytest.mark.anyio
async def test_no_profile_returns_empty_without_embedding() -> None:
    kb_id = uuid.uuid4()
    events: list[str] = []
    repository = FakeRepository([scope_row(kb_id, profile_id=None)], events=events)
    embedder = FakeEmbedder(events)

    result = await search_authorized_chunks(
        repository,
        user_id=USER_ID,
        organization_id=ORGANIZATION_ID,
        kb_ids=[kb_id],
        query="hello",
        embedder=embedder,
        analyzer=FakeAnalyzer(events),
    )

    # NULL active profile 的 KB 不算可检索：实际范围为空。
    assert result.kb_ids == ()
    assert result.candidates == ()
    assert events == ["load_scope", "release"]
    assert repository.released == 1


@pytest.mark.anyio
async def test_success_fuses_both_paths() -> None:
    kb_id = uuid.uuid4()
    events: list[str] = []
    shared = ranked(0.9)
    repository = FakeRepository(
        [scope_row(kb_id)], vector=[shared], keyword=[shared], events=events
    )

    result = await search_authorized_chunks(
        repository,
        user_id=USER_ID,
        organization_id=ORGANIZATION_ID,
        kb_ids=[kb_id],
        query="hello",
        embedder=FakeEmbedder(events),
        analyzer=FakeAnalyzer(events),
    )

    assert result.kb_ids == (kb_id,)
    assert len(result.candidates) == 1
    assert result.candidates[0].chunk_id == shared.chunk_id
    assert result.candidates[0].vector_rank == 1
    assert result.candidates[0].keyword_rank == 1


@pytest.mark.anyio
async def test_analyzer_length_error_maps_to_query_invalid() -> None:
    kb_id = uuid.uuid4()
    events: list[str] = []
    repository = FakeRepository([scope_row(kb_id)], events=events)
    analyzer = FakeAnalyzer(events, error=KeywordAnalyzerInputError("too long"))

    with pytest.raises(RetrievalQueryInvalid):
        await search_authorized_chunks(
            repository,
            user_id=USER_ID,
            organization_id=ORGANIZATION_ID,
            kb_ids=[kb_id],
            query="hello",
            embedder=FakeEmbedder(events),
            analyzer=analyzer,
        )

    # 编码与候选查询都未发生，且连接已交还。
    assert events == ["load_scope", "release", "analyze"]


@pytest.mark.anyio
async def test_busy_embedding_error_is_retryable() -> None:
    kb_id = uuid.uuid4()
    events: list[str] = []
    repository = FakeRepository([scope_row(kb_id)], events=events)
    embedder = FailingEmbedder(events, QueryEmbeddingBusyError("busy", retry_after_seconds=2.0))

    with pytest.raises(RetrievalEmbeddingError) as captured:
        await search_authorized_chunks(
            repository,
            user_id=USER_ID,
            organization_id=ORGANIZATION_ID,
            kb_ids=[kb_id],
            query="hello",
            embedder=embedder,
            analyzer=FakeAnalyzer(events),
        )

    assert captured.value.retryable is True
    assert captured.value.retry_after_seconds == 2.0
    assert "vector" not in events


@pytest.mark.anyio
async def test_not_ready_embedding_error_is_not_retryable_by_default() -> None:
    kb_id = uuid.uuid4()
    events: list[str] = []
    repository = FakeRepository([scope_row(kb_id)], events=events)
    embedder = FailingEmbedder(events, QueryEmbeddingNotReadyError("not ready"))

    with pytest.raises(RetrievalEmbeddingError) as captured:
        await search_authorized_chunks(
            repository,
            user_id=USER_ID,
            organization_id=ORGANIZATION_ID,
            kb_ids=[kb_id],
            query="hello",
            embedder=embedder,
            analyzer=FakeAnalyzer(events),
        )

    assert captured.value.retryable is False


@pytest.mark.anyio
async def test_dimension_mismatch_fails_before_candidate_queries() -> None:
    kb_id = uuid.uuid4()
    events: list[str] = []
    repository = FakeRepository([scope_row(kb_id)], events=events)
    embedder = FakeEmbedder(events, vector=(0.0,) * 8)

    with pytest.raises(RetrievalEmbeddingError):
        await search_authorized_chunks(
            repository,
            user_id=USER_ID,
            organization_id=ORGANIZATION_ID,
            kb_ids=[kb_id],
            query="hello",
            embedder=embedder,
            analyzer=FakeAnalyzer(events),
        )

    assert "vector" not in events


@pytest.mark.anyio
async def test_candidate_query_failure_still_releases_connection() -> None:
    kb_id = uuid.uuid4()
    events: list[str] = []
    original = RuntimeError("候选查询失败")
    repository = FakeRepository([scope_row(kb_id)], events=events, vector_error=original)

    with pytest.raises(RuntimeError) as captured:
        await search_authorized_chunks(
            repository,
            user_id=USER_ID,
            organization_id=ORGANIZATION_ID,
            kb_ids=[kb_id],
            query="hello",
            embedder=FakeEmbedder(events),
            analyzer=FakeAnalyzer(events),
        )

    assert captured.value is original
    # scope 与候选查询各释放一次；异常路径也必须交还连接。
    assert events == ["load_scope", "release", "analyze", "embed", "vector", "release"]
    assert repository.released == 2


@pytest.mark.anyio
async def test_candidate_failure_release_failure_keeps_original_error() -> None:
    kb_id = uuid.uuid4()
    original = RuntimeError("候选查询失败")
    repository = FakeRepository(
        [scope_row(kb_id)],
        vector_error=original,
        release_error=RuntimeError("释放失败"),
        release_error_call=2,
    )

    with pytest.raises(RuntimeError) as captured:
        await search_authorized_chunks(
            repository,
            user_id=USER_ID,
            organization_id=ORGANIZATION_ID,
            kb_ids=[kb_id],
            query="hello",
            embedder=FakeEmbedder([]),
            analyzer=FakeAnalyzer([]),
        )

    assert captured.value is original
    assert any("释放失败" in note for note in captured.value.__notes__)
    assert repository.released == 2


@pytest.mark.anyio
async def test_scope_failure_release_failure_keeps_original_error() -> None:
    kb_id = uuid.uuid4()
    repository = FakeRepository(
        [scope_row(kb_id, keyword_analyzer_version="stale-analyzer")],
        release_error=RuntimeError("释放失败"),
        release_error_call=1,
    )

    with pytest.raises(RetrievalAnalyzerMismatch) as captured:
        await search_authorized_chunks(
            repository,
            user_id=USER_ID,
            organization_id=ORGANIZATION_ID,
            kb_ids=[kb_id],
            query="hello",
            embedder=FakeEmbedder([]),
            analyzer=FakeAnalyzer([]),
        )

    assert any("释放失败" in note for note in captured.value.__notes__)
    assert repository.released == 1


@pytest.mark.anyio
async def test_release_failure_without_in_flight_error_still_raises() -> None:
    kb_id = uuid.uuid4()
    events: list[str] = []
    repository = FakeRepository(
        [scope_row(kb_id)], events=events, release_error=RuntimeError("释放失败")
    )

    with pytest.raises(RuntimeError, match="释放失败"):
        await search_authorized_chunks(
            repository,
            user_id=USER_ID,
            organization_id=ORGANIZATION_ID,
            kb_ids=[kb_id],
            query="hello",
            embedder=FakeEmbedder(events),
            analyzer=FakeAnalyzer(events),
        )

    # 连接释放失败时不得继续分词或调用模型。
    assert events == ["load_scope", "release"]


def test_schema_rejects_blank_query() -> None:
    from pydantic import ValidationError
    from rag_backend.schemas.retrieval import RetrievalSearchRequest

    with pytest.raises(ValidationError):
        RetrievalSearchRequest.model_validate({"query": "   ", "kbIds": [str(uuid.uuid4())]})


def test_schema_rejects_empty_kb_ids() -> None:
    from pydantic import ValidationError
    from rag_backend.schemas.retrieval import RetrievalSearchRequest

    with pytest.raises(ValidationError):
        RetrievalSearchRequest.model_validate({"query": "hello", "kbIds": []})


def test_schema_accepts_valid_request() -> None:
    from rag_backend.schemas.retrieval import RetrievalSearchRequest

    request = RetrievalSearchRequest.model_validate(
        {"query": "hello", "kbIds": [str(uuid.uuid4())]}
    )
    assert isinstance(request.kb_ids, list)


def test_schema_query_length_matches_encoder_limit() -> None:
    from pydantic import ValidationError
    from rag_backend.schemas.retrieval import RetrievalSearchRequest

    assert MAX_QUERY_CHARS == 8000 - 19
    at_limit = RetrievalSearchRequest.model_validate(
        {"query": "x" * MAX_QUERY_CHARS, "kbIds": [str(uuid.uuid4())]}
    )
    assert len(at_limit.query) == MAX_QUERY_CHARS
    with pytest.raises(ValidationError):
        RetrievalSearchRequest.model_validate(
            {"query": "x" * (MAX_QUERY_CHARS + 1), "kbIds": [str(uuid.uuid4())]}
        )


# --- 可降级重排 -------------------------------------------------------------


def rerank_fixture() -> tuple[
    uuid.UUID, list[RankedChunk], dict[uuid.UUID, str]
]:
    kb_id = uuid.uuid4()
    candidates = [ranked(1.0 - index * 0.01) for index in range(RERANK_TOP_K + 2)]
    texts = {candidate.chunk_id: f"text-{index}" for index, candidate in enumerate(candidates)}
    return kb_id, candidates, texts


async def run_search(
    repository: FakeRepository,
    kb_id: uuid.UUID,
    embedder: FakeEmbedder,
    analyzer: FakeAnalyzer,
    *,
    reranker: FakeReranker | None,
) -> RetrievalResult:
    return await search_authorized_chunks(
        repository,
        user_id=USER_ID,
        organization_id=ORGANIZATION_ID,
        kb_ids=[kb_id],
        query="hello",
        embedder=embedder,
        analyzer=analyzer,
        reranker=reranker,
    )


@pytest.mark.anyio
async def test_rerank_reorders_top_k_and_keeps_rest_after_fusion() -> None:
    kb_id, candidates, texts = rerank_fixture()
    baseline_repository = FakeRepository([scope_row(kb_id)], vector=candidates)
    baseline = await run_search(
        baseline_repository, kb_id, FakeEmbedder([]), FakeAnalyzer([]), reranker=None
    )
    baseline_by_id = {
        candidate.chunk_id: (candidate.fusion_rank, candidate.fusion_score)
        for candidate in baseline.candidates
    }

    events: list[str] = []
    repository = FakeRepository([scope_row(kb_id)], vector=candidates, events=events, texts=texts)
    # 分数随融合顺序递增：最后一个候选分数最高，让 top-10 完全倒序，便于断言确定性重排。
    top_ids = [candidate.chunk_id for candidate in candidates[:RERANK_TOP_K]]
    scores = [
        RerankScore(candidate_id=str(chunk_id), score=float(index + 1))
        for index, chunk_id in enumerate(top_ids)
    ]
    reranker = FakeReranker(events, scores=scores)

    result = await run_search(
        repository, kb_id, FakeEmbedder(events), FakeAnalyzer(events), reranker=reranker
    )

    expected_top_ids = list(reversed(top_ids))
    reordered_top_ids = [candidate.chunk_id for candidate in result.candidates[:RERANK_TOP_K]]
    assert reordered_top_ids == expected_top_ids
    assert [candidate.chunk_id for candidate in result.candidates[RERANK_TOP_K:]] == [
        candidate.chunk_id for candidate in candidates[RERANK_TOP_K:]
    ]
    assert result.degraded_stages == ()
    # 既有 fusion_rank/fusion_score 一律不改：每个 chunk 仍带基线值。
    for candidate in result.candidates:
        assert (candidate.fusion_rank, candidate.fusion_score) == baseline_by_id[candidate.chunk_id]
    assert len(reranker.calls) == 1
    assert len(reranker.calls[0][1]) == RERANK_TOP_K


@pytest.mark.anyio
async def test_rerank_failure_degrades_and_keeps_rrf_order() -> None:
    kb_id, candidates, texts = rerank_fixture()
    events: list[str] = []
    repository = FakeRepository([scope_row(kb_id)], vector=candidates, events=events, texts=texts)
    reranker = FakeReranker(events, error=RerankUnavailableError("upstream down"))

    result = await run_search(
        repository, kb_id, FakeEmbedder(events), FakeAnalyzer(events), reranker=reranker
    )

    assert [candidate.chunk_id for candidate in result.candidates] == [
        candidate.chunk_id for candidate in candidates
    ]
    assert result.degraded_stages == ("rerank_unavailable",)
    # 模型调用发生在最后一次 release 之后。
    last_release = max(index for index, event in enumerate(events) if event == "release")
    assert events.index("rerank") > last_release


@pytest.mark.anyio
async def test_rerank_disabled_never_loads_text_or_marks_degrade() -> None:
    kb_id, candidates, texts = rerank_fixture()
    events: list[str] = []
    repository = FakeRepository([scope_row(kb_id)], vector=candidates, events=events, texts=texts)

    result = await run_search(
        repository, kb_id, FakeEmbedder(events), FakeAnalyzer(events), reranker=None
    )

    assert result.degraded_stages == ()
    assert "load_texts" not in events
    assert "rerank" not in events


@pytest.mark.anyio
async def test_rerank_missing_candidate_text_degrades_without_model_call() -> None:
    kb_id, candidates, _texts = rerank_fixture()
    events: list[str] = []
    # 只给部分候选正文，模拟读取正文前候选失效。
    partial = {candidate.chunk_id: "text" for candidate in candidates[:3]}
    repository = FakeRepository(
        [scope_row(kb_id)], vector=candidates, events=events, texts=partial
    )
    reranker = FakeReranker(events)

    result = await run_search(
        repository, kb_id, FakeEmbedder(events), FakeAnalyzer(events), reranker=reranker
    )

    assert [candidate.chunk_id for candidate in result.candidates] == [
        candidate.chunk_id for candidate in candidates
    ]
    assert result.degraded_stages == ("rerank_unavailable",)
    assert reranker.calls == []
