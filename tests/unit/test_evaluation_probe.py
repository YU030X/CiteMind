"""纯核心探针编排单测：假仓储 + 假 embedder/analyzer/reranker，不连数据库、不联网、不调模型。

覆盖：A 不走关键词、B 走 RRF 融合、C 成功重排与降级回退、no_permission 三组空、
按题解析角色身份、answer 题授权失败即失败、硬预算耗尽、模型调用前已 release、
C 复用 B 的真实来源 locator、C 证据/分数异常静态失败、候选映射不一致失败，以及三元组/排序指标自检。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import replace

import pytest
from rag_backend.evaluation.ablation import validate_ablation_triplet
from rag_backend.evaluation.dataset import (
    ConversationTurn,
    EvaluationDataset,
    EvaluationQuestion,
    GoldLocator,
    GoldSpan,
    QuestionScope,
)
from rag_backend.evaluation.probe import (
    BudgetCounter,
    ProbeError,
    ProbeIdentity,
    ProbeInputs,
    run_ablation_probe,
)
from rag_backend.evaluation.ranking_metrics import RankingCandidate
from rag_backend.evaluation.runner import AssetRegistry, LogicalRef
from rag_backend.retrieval.fusion import RankedChunk
from rag_backend.retrieval.query_embedding_client import EmbeddedQuery
from rag_backend.retrieval.repository import EvidenceChunkRow, KbScopeRow
from rag_backend.retrieval.rerank_client import RerankInput, RerankScore, RerankUnavailableError
from rag_backend.retrieval.service import RERANK_TOP_K

USER_ID = uuid.uuid4()
ORGANIZATION_ID = uuid.uuid4()
KB_UUID = uuid.uuid4()
SECRET_KB_UUID = uuid.uuid4()
PROFILE_ID = uuid.uuid4()
VERSION_A = uuid.uuid4()
VERSION_B = uuid.uuid4()
CHUNK_A = uuid.uuid4()
CHUNK_B = uuid.uuid4()
ANALYZER_ID = "test-analyzer"
QUERY_VECTOR: tuple[float, ...] = (0.0,) * 4
MARKDOWN_LOCATOR = {
    "source_type": "markdown",
    "parser_version": "md-v1",
    "start_line": 3,
    "end_line": 5,
}
LOCATOR = GoldLocator(source_type="markdown", parser_version="md-v1", start_line=3, end_line=5)

READER_IDENTITY = ProbeIdentity(user_id=USER_ID, organization_id=ORGANIZATION_ID)
STAFF_IDENTITY = ProbeIdentity(user_id=uuid.uuid4(), organization_id=uuid.uuid4())
HR_IDENTITY = ProbeIdentity(user_id=uuid.uuid4(), organization_id=uuid.uuid4())


def make_question(
    *,
    question_id: str = "q1",
    category: str = "single_document",
    role: str = "reader",
    kb_ids: list[str] | None = None,
) -> EvaluationQuestion:
    scope = QuestionScope(role=role, kb_ids=kb_ids or ["kb-a"])
    if category == "no_permission":
        return EvaluationQuestion(
            id=question_id,
            category="no_permission",
            scope=scope,
            question="机密内容是什么？",
            expected_behavior="refuse",
            unavailable_document_ids=["doc-secret"],
            unavailable_reason="no_permission",
        )
    return EvaluationQuestion(
        id=question_id,
        category="single_document",
        scope=scope,
        question="示例问题",
        standalone_question="示例问题（独立）",
        history=[ConversationTurn(role="user", text="上一轮")],
        expected_behavior="answer",
        gold_answer_points=["要点"],
        gold_source_spans=[
            GoldSpan(
                kb_id="kb-a",
                document_id="doc-1",
                version=1,
                quote="引文",
                locator=LOCATOR,
            )
        ],
    )


def make_dataset(questions: Sequence[EvaluationQuestion]) -> EvaluationDataset:
    return EvaluationDataset(
        dataset_kind="dev",
        dataset_version="v1",
        corpus_manifest="manifest.json",
        questions=list(questions),
    )


def make_registry(*, include_secret: bool = False) -> AssetRegistry:
    registry = AssetRegistry()
    registry.register_knowledge_base("kb-a", KB_UUID)
    registry.register_version(ref=LogicalRef("kb-a", "doc-1", 1), version_uuid=VERSION_A)
    registry.register_version(ref=LogicalRef("kb-a", "doc-2", 1), version_uuid=VERSION_B)
    if include_secret:
        registry.register_knowledge_base("kb-secret", SECRET_KB_UUID)
    return registry


def scope_row(*, accessible: bool = True) -> KbScopeRow:
    kb_id = KB_UUID if accessible else SECRET_KB_UUID
    return KbScopeRow(
        kb_id=kb_id,
        profile_id=PROFILE_ID if accessible else None,
        model_revision="rev-1" if accessible else None,
        dimension=4 if accessible else None,
        normalize=True if accessible else None,
        keyword_analyzer_version=ANALYZER_ID if accessible else None,
    )


def ranked(chunk_id: uuid.UUID, version_id: uuid.UUID, score: float) -> RankedChunk:
    return RankedChunk(
        chunk_id=chunk_id,
        document_id=uuid.uuid4(),
        kb_id=KB_UUID,
        version_id=version_id,
        score=score,
    )


def evidence_row(
    chunk_id: uuid.UUID,
    version_id: uuid.UUID,
    *,
    text: str = "正文",
    locator: dict[str, object] | None = None,
) -> EvidenceChunkRow:
    return EvidenceChunkRow(
        chunk_id=chunk_id,
        document_id=uuid.uuid4(),
        kb_id=KB_UUID,
        version_id=version_id,
        version_no=1,
        document_title="doc",
        text=text,
        source_locator=dict(locator or MARKDOWN_LOCATOR),
    )


def default_evidence() -> dict[uuid.UUID, EvidenceChunkRow]:
    return {
        CHUNK_A: evidence_row(CHUNK_A, VERSION_A, text="a"),
        CHUNK_B: evidence_row(CHUNK_B, VERSION_B, text="b"),
    }


class FakeRepository:
    def __init__(
        self,
        *,
        events: list[str],
        rows: list[KbScopeRow],
        vector: list[RankedChunk] | None = None,
        keyword: list[RankedChunk] | None = None,
        evidence: dict[uuid.UUID, EvidenceChunkRow] | None = None,
        evidence_fail_at: int | None = None,
        evidence_override: list[EvidenceChunkRow] | None = None,
    ) -> None:
        self.events = events
        self.rows = rows
        self.vector = vector or []
        self.keyword = keyword or []
        self.evidence = evidence or {}
        self.evidence_fail_at = evidence_fail_at
        self.evidence_override = evidence_override
        self.keyword_calls = 0
        self.evidence_calls = 0
        self.identity_calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def load_kb_scope(self, *, user_id, organization_id, kb_ids):
        self.events.append("load_scope")
        self.identity_calls.append((user_id, organization_id))
        return self.rows

    async def release(self) -> None:
        self.events.append("release")

    async def fetch_vector_candidates(self, *, user_id, organization_id, kb_ids, profile_id, query_vector):
        self.events.append("vector")
        return list(self.vector)

    async def fetch_keyword_candidates(self, *, user_id, organization_id, kb_ids, profile_id, query_terms):
        self.events.append("keyword")
        self.keyword_calls += 1
        return list(self.keyword)

    async def load_evidence_chunks(self, *, user_id, organization_id, chunk_ids):
        self.events.append("load_texts")
        self.identity_calls.append((user_id, organization_id))
        self.evidence_calls += 1
        if self.evidence_fail_at is not None and self.evidence_calls == self.evidence_fail_at:
            return list(self.evidence_override or [])
        return [self.evidence[chunk_id] for chunk_id in chunk_ids if chunk_id in self.evidence]


class FakeFactory:
    def __init__(self, repos: dict[str, FakeRepository]) -> None:
        self._repos = repos

    def __call__(self, role: str, question: EvaluationQuestion) -> FakeRepository:
        return self._repos[question.id]


class FakeEmbedder:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def embed_query(self, text: str, expected_model_revision: str) -> EmbeddedQuery:
        self.events.append("embed")
        return EmbeddedQuery(vector=QUERY_VECTOR, token_count=1, model_revision=expected_model_revision)

    def close(self) -> None:
        return None


class FakeAnalyzer:
    analyzer_id = ANALYZER_ID

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def analyze(self, text: str) -> str:
        self.events.append("analyze")
        return "terms"


class FakeReranker:
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

    def rerank(self, query: str, candidates: list[RerankInput]) -> list[RerankScore]:
        self.events.append("rerank")
        if self.error is not None:
            raise self.error
        if self.scores is not None:
            return list(self.scores)
        return [RerankScore(candidate_id=item.candidate_id, score=float(len(item.text))) for item in candidates]

    def close(self) -> None:
        return None


class FakeMapper:
    """从 ``EvidenceChunkRow.source_locator`` 构造真实 locator；来源标识取 LogicalRef。"""

    def map(self, row: EvidenceChunkRow, facts, version_ref: LogicalRef) -> RankingCandidate:
        return RankingCandidate(
            candidate_id=str(facts.chunk_id),
            kb_id=version_ref.kb_id,
            document_id=version_ref.document_id,
            version=version_ref.version,
            locator=GoldLocator.model_validate(row.source_locator),
            rank=facts.rank,
            vector_rank=facts.vector_rank,
            vector_score=facts.vector_score,
            keyword_rank=facts.keyword_rank,
            keyword_score=facts.keyword_score,
            fusion_rank=facts.fusion_rank,
            fusion_score=facts.fusion_score,
            rerank_score=facts.rerank_score,
        )


class WrongSourceMapper(FakeMapper):
    """故意返回与 LogicalRef 不一致的来源，验证探针静态失败。"""

    def map(self, row: EvidenceChunkRow, facts, version_ref: LogicalRef) -> RankingCandidate:
        candidate = super().map(row, facts, version_ref)
        return candidate.model_copy(update={"kb_id": "kb-other"})


def build_inputs(
    question: EvaluationQuestion,
    *,
    events: list[str],
    vector: list[RankedChunk],
    keyword: list[RankedChunk] | None = None,
    evidence: dict[uuid.UUID, EvidenceChunkRow] | None = None,
    evidence_fail_at: int | None = None,
    evidence_override: list[EvidenceChunkRow] | None = None,
    rows: list[KbScopeRow] | None = None,
    reranker: FakeReranker | None = None,
    embedding_limit: int = 10,
    rerank_limit: int = 10,
    registry: AssetRegistry | None = None,
    role_identities: dict[str, ProbeIdentity] | None = None,
    mapper: FakeMapper | None = None,
) -> tuple[ProbeInputs, FakeRepository]:
    repository = FakeRepository(
        events=events,
        rows=rows if rows is not None else [scope_row()],
        vector=vector,
        keyword=keyword,
        evidence=evidence,
        evidence_fail_at=evidence_fail_at,
        evidence_override=evidence_override,
    )
    inputs = ProbeInputs(
        dataset=make_dataset([question]),
        registry=registry or make_registry(),
        repository_factory=FakeFactory({question.id: repository}),
        mapper=mapper or FakeMapper(),
        embedder=FakeEmbedder(events),
        analyzer=FakeAnalyzer(events),
        role_identities=role_identities if role_identities is not None else {"reader": READER_IDENTITY},
        embedding_budget=BudgetCounter(embedding_limit, label="embedding"),
        rerank_budget=BudgetCounter(rerank_limit, label="rerank"),
        model_identities={"embedding": "rev-1", "rerank": "rev-r"},
        config={"stage": "probe"},
        created_at="2026-09-29T00:00:00Z",
        reranker=reranker if reranker is not None else FakeReranker(events),
    )
    return inputs, repository


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_a_does_not_call_keyword_and_b_uses_fusion() -> None:
    events: list[str] = []
    vector = [ranked(CHUNK_A, VERSION_A, 0.9), ranked(CHUNK_B, VERSION_B, 0.8)]
    inputs, repository = build_inputs(
        make_question(), events=events, vector=vector, evidence=default_evidence()
    )

    outcome = await run_ablation_probe(inputs)

    assert repository.keyword_calls == 1  # 只有 B 走关键词路
    a_question = outcome.a.questions[0]
    b_question = outcome.b.questions[0]
    assert all(item.fusion_rank is None and item.keyword_rank is None for item in a_question.candidates)
    assert [item.rank for item in a_question.candidates] == [item.vector_rank for item in a_question.candidates]
    assert all(item.fusion_rank is not None and item.fusion_score is not None for item in b_question.candidates)
    assert [item.rank for item in b_question.candidates] == [item.fusion_rank for item in b_question.candidates]
    assert all(item.rerank_score is None for item in b_question.candidates)
    # locator 只能来自授权行的真实 markdown 定位。
    assert a_question.candidates[0].locator.start_line == MARKDOWN_LOCATOR["start_line"]
    assert a_question.candidates[0].locator.end_line == MARKDOWN_LOCATOR["end_line"]


@pytest.mark.anyio
async def test_c_rerank_success_reuses_b_sources_and_orders() -> None:
    events: list[str] = []
    vector = [ranked(CHUNK_A, VERSION_A, 0.9), ranked(CHUNK_B, VERSION_B, 0.8)]
    reranker = FakeReranker(
        events,
        scores=[RerankScore(candidate_id=str(CHUNK_B), score=0.9), RerankScore(candidate_id=str(CHUNK_A), score=0.1)],
    )
    inputs, _ = build_inputs(
        make_question(),
        events=events,
        vector=vector,
        evidence=default_evidence(),
        reranker=reranker,
    )

    outcome = await run_ablation_probe(inputs)

    c_question = outcome.c.questions[0]
    b_question = outcome.b.questions[0]
    assert [item.candidate_id for item in c_question.candidates] == [str(CHUNK_B), str(CHUNK_A)]
    assert c_question.candidates[0].rerank_score == 0.9
    assert c_question.degraded_stages == []
    # candidateId 集合完整，且 C 复用 B 的真实来源 locator，不重新伪造。
    assert {item.candidate_id for item in c_question.candidates} == {
        item.candidate_id for item in b_question.candidates
    }
    b_by_id = {item.candidate_id: item for item in b_question.candidates}
    for item in c_question.candidates:
        assert item.locator == b_by_id[item.candidate_id].locator
    assert outcome.triplet.question_count == 1
    assert outcome.ranking.question_count == 1


@pytest.mark.anyio
async def test_c_rerank_unavailable_falls_back_to_b() -> None:
    events: list[str] = []
    vector = [ranked(CHUNK_A, VERSION_A, 0.9), ranked(CHUNK_B, VERSION_B, 0.8)]
    reranker = FakeReranker(events, error=RerankUnavailableError("boom"))
    inputs, _ = build_inputs(
        make_question(),
        events=events,
        vector=vector,
        evidence=default_evidence(),
        reranker=reranker,
    )

    outcome = await run_ablation_probe(inputs)

    c_question = outcome.c.questions[0]
    assert "rerank_unavailable" in c_question.degraded_stages
    assert [item.candidate_id for item in c_question.candidates] == [
        item.candidate_id for item in outcome.b.questions[0].candidates
    ]
    assert all(item.rerank_score is None for item in c_question.candidates)
    validate_ablation_triplet(outcome.a, outcome.b, outcome.c)


@pytest.mark.anyio
async def test_c_latency_not_less_than_b() -> None:
    events: list[str] = []
    vector = [ranked(CHUNK_A, VERSION_A, 0.9), ranked(CHUNK_B, VERSION_B, 0.8)]
    inputs, _ = build_inputs(
        make_question(), events=events, vector=vector, evidence=default_evidence()
    )

    outcome = await run_ablation_probe(inputs)

    assert outcome.c.questions[0].latency_ms >= outcome.b.questions[0].latency_ms


@pytest.mark.anyio
async def test_empty_b_keeps_c_latency_equal() -> None:
    events: list[str] = []
    inputs, _ = build_inputs(make_question(), events=events, vector=[], evidence={})

    outcome = await run_ablation_probe(inputs)

    assert outcome.c.questions[0].latency_ms == outcome.b.questions[0].latency_ms
    assert outcome.c.questions[0].candidates == []


@pytest.mark.anyio
async def test_no_permission_yields_three_empty_groups() -> None:
    events: list[str] = []
    question = make_question(category="no_permission", kb_ids=["kb-secret"])
    inputs, repository = build_inputs(
        question,
        events=events,
        vector=[ranked(CHUNK_A, VERSION_A, 0.9)],
        rows=[],
        registry=make_registry(include_secret=True),
    )

    outcome = await run_ablation_probe(inputs)

    for artifact in (outcome.a, outcome.b, outcome.c):
        assert artifact.questions[0].candidates == []
    assert outcome.calibration[0].candidate_count == 0
    assert outcome.calibration[0].top_score is None
    assert repository.keyword_calls == 0
    assert "embed" not in events  # scope 阶段即失败，不消费 embedding 预算
    assert inputs.embedding_budget.spent == 0
    assert inputs.rerank_budget.spent == 0  # no_permission 不消费重排预算


@pytest.mark.anyio
async def test_answer_question_authorization_failure_fails_closed() -> None:
    events: list[str] = []
    inputs, _ = build_inputs(make_question(), events=events, vector=[], rows=[])

    with pytest.raises(ProbeError):
        await run_ablation_probe(inputs)


@pytest.mark.anyio
async def test_missing_role_identity_fails_closed() -> None:
    events: list[str] = []
    inputs, _ = build_inputs(
        make_question(), events=events, vector=[], evidence={}, role_identities={}
    )

    with pytest.raises(ProbeError):
        await run_ablation_probe(inputs)


@pytest.mark.anyio
async def test_role_identity_selected_per_question() -> None:
    staff_question = make_question(question_id="q-staff", role="staff")
    hr_question = make_question(question_id="q-hr", role="hr")
    events: list[str] = []
    staff_repo = FakeRepository(
        events=events, rows=[scope_row()], vector=[ranked(CHUNK_A, VERSION_A, 0.9)], evidence=default_evidence()
    )
    hr_repo = FakeRepository(
        events=events, rows=[scope_row()], vector=[ranked(CHUNK_B, VERSION_B, 0.8)], evidence=default_evidence()
    )
    inputs = ProbeInputs(
        dataset=make_dataset([staff_question, hr_question]),
        registry=make_registry(),
        repository_factory=FakeFactory({"q-staff": staff_repo, "q-hr": hr_repo}),
        mapper=FakeMapper(),
        embedder=FakeEmbedder(events),
        analyzer=FakeAnalyzer(events),
        role_identities={"staff": STAFF_IDENTITY, "hr": HR_IDENTITY},
        embedding_budget=BudgetCounter(10, label="embedding"),
        rerank_budget=BudgetCounter(10, label="rerank"),
        model_identities={},
        config={},
        created_at="2026-09-29T00:00:00Z",
        reranker=FakeReranker(events),
    )

    await run_ablation_probe(inputs)

    staff_expected = (STAFF_IDENTITY.user_id, STAFF_IDENTITY.organization_id)
    hr_expected = (HR_IDENTITY.user_id, HR_IDENTITY.organization_id)
    assert staff_repo.identity_calls and all(call == staff_expected for call in staff_repo.identity_calls)
    assert hr_repo.identity_calls and all(call == hr_expected for call in hr_repo.identity_calls)


@pytest.mark.anyio
async def test_calibration_uses_b_rank1_fusion_score() -> None:
    events: list[str] = []
    vector = [ranked(CHUNK_A, VERSION_A, 0.9), ranked(CHUNK_B, VERSION_B, 0.8)]
    inputs, _ = build_inputs(
        make_question(), events=events, vector=vector, evidence=default_evidence()
    )

    outcome = await run_ablation_probe(inputs)

    b_top = outcome.b.questions[0].candidates[0]
    assert outcome.calibration[0].top_score == b_top.fusion_score
    assert outcome.calibration[0].candidate_count == 2
    assert outcome.calibration[0].expected_behavior == "answer"


@pytest.mark.anyio
async def test_embedding_budget_exhaustion_raises_probe_error() -> None:
    events: list[str] = []
    vector = [ranked(CHUNK_A, VERSION_A, 0.9)]
    inputs, _ = build_inputs(
        make_question(), events=events, vector=vector, evidence=default_evidence(), embedding_limit=1
    )

    with pytest.raises(ProbeError):
        await run_ablation_probe(inputs)


@pytest.mark.anyio
async def test_reranker_none_fails_at_start() -> None:
    events: list[str] = []
    inputs, _ = build_inputs(
        make_question(), events=events, vector=[ranked(CHUNK_A, VERSION_A, 0.9)], evidence=default_evidence()
    )

    with pytest.raises(ProbeError):
        await run_ablation_probe(replace(inputs, reranker=None))


@pytest.mark.anyio
async def test_release_happens_before_rerank_model_call() -> None:
    events: list[str] = []
    vector = [ranked(CHUNK_A, VERSION_A, 0.9)]
    reranker = FakeReranker(events, scores=[RerankScore(candidate_id=str(CHUNK_A), score=0.5)])
    inputs, _ = build_inputs(
        make_question(),
        events=events,
        vector=vector,
        evidence=default_evidence(),
        reranker=reranker,
    )

    await run_ablation_probe(inputs)

    rerank_index = events.index("rerank")
    texts_index = max(index for index, item in enumerate(events) if item == "load_texts")
    assert texts_index < rerank_index
    assert "release" in events[texts_index + 1 : rerank_index]


@pytest.mark.anyio
async def test_c_missing_evidence_body_fails_closed() -> None:
    events: list[str] = []
    vector = [ranked(CHUNK_A, VERSION_A, 0.9), ranked(CHUNK_B, VERSION_B, 0.8)]
    # A 与 B 的映射各消费一次完整证据；C 的第三次加载缺正文。
    inputs, _ = build_inputs(
        make_question(),
        events=events,
        vector=vector,
        evidence=default_evidence(),
        evidence_fail_at=3,
        evidence_override=[evidence_row(CHUNK_A, VERSION_A, text="a")],
    )

    with pytest.raises(ProbeError):
        await run_ablation_probe(inputs)


@pytest.mark.anyio
async def test_c_invalid_rerank_scores_fail_closed() -> None:
    events: list[str] = []
    vector = [ranked(CHUNK_A, VERSION_A, 0.9), ranked(CHUNK_B, VERSION_B, 0.8)]
    reranker = FakeReranker(events, scores=[RerankScore(candidate_id=str(CHUNK_A), score=0.5)])
    inputs, _ = build_inputs(
        make_question(),
        events=events,
        vector=vector,
        evidence=default_evidence(),
        reranker=reranker,
    )

    with pytest.raises(ProbeError):
        await run_ablation_probe(inputs)


@pytest.mark.anyio
async def test_mapper_source_mismatch_fails_closed() -> None:
    events: list[str] = []
    vector = [ranked(CHUNK_A, VERSION_A, 0.9)]
    inputs, _ = build_inputs(
        make_question(),
        events=events,
        vector=vector,
        evidence=default_evidence(),
        mapper=WrongSourceMapper(),
    )

    with pytest.raises(ProbeError):
        await run_ablation_probe(inputs)


@pytest.mark.anyio
async def test_evidence_version_mismatch_fails_closed() -> None:
    events: list[str] = []
    vector = [ranked(CHUNK_A, VERSION_A, 0.9)]
    evidence = {CHUNK_A: evidence_row(CHUNK_A, VERSION_B, text="a")}
    inputs, _ = build_inputs(make_question(), events=events, vector=vector, evidence=evidence)

    with pytest.raises(ProbeError):
        await run_ablation_probe(inputs)


@pytest.mark.anyio
async def test_missing_version_mapping_fails_closed() -> None:
    events: list[str] = []
    unregistered = uuid.uuid4()
    vector = [ranked(CHUNK_A, unregistered, 0.9)]
    evidence = {CHUNK_A: evidence_row(CHUNK_A, unregistered, text="a")}
    inputs, _ = build_inputs(make_question(), events=events, vector=vector, evidence=evidence)

    with pytest.raises(ProbeError):
        await run_ablation_probe(inputs)


@pytest.mark.anyio
async def test_rerank_top_k_boundary_uses_same_evidence() -> None:
    # 构造 12 个候选，只有前 RERANK_TOP_K 个需要正文；证据映射只在 C 阶段读取 top-K。
    events: list[str] = []
    chunk_ids = [uuid.uuid4() for _ in range(RERANK_TOP_K + 2)]
    vector = [ranked(chunk_id, VERSION_A, 1.0 - index / 100) for index, chunk_id in enumerate(chunk_ids)]
    evidence = {chunk_id: evidence_row(chunk_id, VERSION_A, text="x") for chunk_id in chunk_ids}
    inputs, _ = build_inputs(make_question(), events=events, vector=vector, evidence=evidence)

    outcome = await run_ablation_probe(inputs)

    assert len(outcome.c.questions[0].candidates) == len(chunk_ids)
    assert all(item.rerank_score is not None for item in outcome.c.questions[0].candidates[:RERANK_TOP_K])
