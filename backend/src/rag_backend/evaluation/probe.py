"""Phase 3 真实只读检索探针的纯核心编排（不连数据库、不联网、不读环境）。

职责边界：

- 输入是 ``EvaluationDataset``、资产登记表或显式版本映射、按角色提供的
  ``RetrievalRepository`` 工厂与 ``role_identities`` 身份映射，以及 embedder/analyzer/reranker
  与硬预算计数器。本模块只做编排与确定性派生，**不构造任何真实适配器、不打开连接、不读环境
  变量、不写文件**。
- 每题先按 ``question.scope.role`` 解析该角色自己的 ``ProbeIdentity``；缺角色立即静态失败。
- 每题用生产路径的真实检索事实派生 A/B/C 三组产物：A 仅向量路（复用
  ``resolve_retrieval_scope``/``fetch_vector_candidates``/``release``），B 复用
  ``search_authorized_chunks(reranker=None)``，C 在 B 的 top-10 上加载已授权正文、``release``
  后调用 reranker。候选的 locator 只能来自 ``load_evidence_chunks`` 返回的真实授权行；本模块
  校验证据集合完整、无重复、版本一致，再把 ``EvidenceChunkRow`` 交给注入的 mapper，并核对
  mapper 输出的 candidateId 与逻辑来源完全一致。C 不允许重新伪造 locator，只复用 B 的来源事实
  更新最终名次与重排分。
- 结果全部驻留内存，返回三份 ``AblationArtifact``、calibration 记录与自检报告；调用方负责
  落盘。任何非预期错误统一收敛为静态 :class:`ProbeError`（``RerankUnavailableError`` 是唯一
  允许把 C 降级为 B 的异常）。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from starlette.concurrency import run_in_threadpool

from rag_backend.evaluation.ablation import (
    AblationArtifact,
    AblationQuestion,
    AblationTripletReport,
    AblationVariant,
    DegradedStage,
    validate_ablation_triplet,
)
from rag_backend.evaluation.analysis import build_ranking_questions
from rag_backend.evaluation.calibration import RefusalProbeRecord
from rag_backend.evaluation.dataset import EvaluationDataset, EvaluationQuestion
from rag_backend.evaluation.ranking_metrics import (
    RankingCandidate,
    RankingReport,
    aggregate_ranking_metrics,
)
from rag_backend.evaluation.runner import AssetRegistry, DatasetRunError, LogicalRef
from rag_backend.retrieval.errors import KnowledgeBaseNotAccessible
from rag_backend.retrieval.fusion import FusedCandidate, RankedChunk
from rag_backend.retrieval.query_embedding_client import EmbeddedQuery
from rag_backend.retrieval.repository import EvidenceChunkRow, RetrievalRepository
from rag_backend.retrieval.rerank_client import (
    RerankInput,
    RerankScore,
    RerankUnavailableError,
)
from rag_backend.retrieval.service import (
    RERANK_TOP_K,
    KeywordAnalyzerLike,
    QueryEmbedder,
    Reranker,
    RetrievalResult,
    apply_rerank_scores,
    embed_query_for_scope,
    resolve_retrieval_scope,
    search_authorized_chunks,
)

STAGE_RERANK_UNAVAILABLE: DegradedStage = "rerank_unavailable"


class ProbeError(Exception):
    """探针前置条件或编排失败；消息静态，不回显查询、正文或候选标识。"""


class ProbeBudgetExceeded(ProbeError):
    """embedding/rerank 硬预算耗尽；在发起下一次模型调用前抛出。"""


class BudgetCounter:
    """只增不减的硬预算计数；``spend`` 在越界前抛出 :class:`ProbeBudgetExceeded`。"""

    def __init__(self, limit: int, *, label: str) -> None:
        if limit < 0:
            raise ValueError("预算上限不能为负数")
        self._limit = limit
        self._label = label
        self._spent = 0

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def spent(self) -> int:
        return self._spent

    @property
    def remaining(self) -> int:
        return self._limit - self._spent

    def spend(self) -> None:
        if self._spent + 1 > self._limit:
            raise ProbeBudgetExceeded(f"{self._label}预算耗尽")
        self._spent += 1


class _BudgetedEmbedder:
    """在真正发出编码请求前消耗 embedding 预算；不拥有内层客户端生命周期。"""

    def __init__(self, inner: QueryEmbedder, budget: BudgetCounter) -> None:
        self._inner = inner
        self._budget = budget

    def embed_query(self, text: str, expected_model_revision: str) -> EmbeddedQuery:
        self._budget.spend()
        return self._inner.embed_query(text, expected_model_revision)

    def close(self) -> None:
        return None


class _BudgetedReranker:
    """在真正发出重排请求前消耗 rerank 预算；不拥有内层客户端生命周期。"""

    def __init__(self, inner: Reranker, budget: BudgetCounter) -> None:
        self._inner = inner
        self._budget = budget

    def rerank(self, query: str, candidates: list[RerankInput]) -> list[RerankScore]:
        self._budget.spend()
        return self._inner.rerank(query, candidates)

    def close(self) -> None:
        return None


@dataclass(frozen=True)
class ProbeIdentity:
    """单次授权检索使用的用户/组织身份；按题目角色解析。"""

    user_id: uuid.UUID
    organization_id: uuid.UUID


@dataclass(frozen=True)
class CandidateFacts:
    """一个候选在各检索路上的原始事实；不含逻辑来源与 locator（由 mapper 补齐）。"""

    chunk_id: uuid.UUID
    version_id: uuid.UUID
    rank: int
    vector_rank: int | None = None
    vector_score: float | None = None
    keyword_rank: int | None = None
    keyword_score: float | None = None
    fusion_rank: int | None = None
    fusion_score: float | None = None
    rerank_score: float | None = None


class CandidateMapper(Protocol):
    """把已授权证据行、候选事实与逻辑版本映射为 ``RankingCandidate``。

    locator 必须由真实的 ``EvidenceChunkRow.source_locator`` 构造；映射器不得凭空提供定位。
    """

    def map(
        self,
        row: EvidenceChunkRow,
        facts: CandidateFacts,
        version_ref: LogicalRef,
    ) -> RankingCandidate: ...


class RepositoryFactory(Protocol):
    """按角色与题目提供只读仓储；实现方拥有连接/会话生命周期。"""

    def __call__(self, role: str, question: EvaluationQuestion) -> RetrievalRepository: ...


@dataclass(frozen=True)
class ProbeInputs:
    """一次探针运行的全部显式输入；不读取环境或默认值。"""

    dataset: EvaluationDataset
    registry: AssetRegistry
    repository_factory: RepositoryFactory
    mapper: CandidateMapper
    embedder: QueryEmbedder
    analyzer: KeywordAnalyzerLike
    role_identities: Mapping[str, ProbeIdentity]
    embedding_budget: BudgetCounter
    rerank_budget: BudgetCounter
    model_identities: Mapping[str, str | int | float | bool] = field(default_factory=dict)
    config: Mapping[str, str | int | float | bool] = field(default_factory=dict)
    created_at: str = ""
    reranker: Reranker | None = None


@dataclass(frozen=True)
class ProbeOutcome:
    """内存中的三组消融产物、calibration 记录与确定性自检报告。"""

    a: AblationArtifact
    b: AblationArtifact
    c: AblationArtifact
    calibration: tuple[RefusalProbeRecord, ...]
    triplet: AblationTripletReport
    ranking: RankingReport


@dataclass(frozen=True)
class _Work:
    """单次运行的预算包装与复用上下文。"""

    inputs: ProbeInputs
    embedder: _BudgetedEmbedder
    reranker: _BudgetedReranker


async def run_ablation_probe(inputs: ProbeInputs) -> ProbeOutcome:
    """执行全部题目的 A/B/C 只读探针，返回内存产物并做确定性自检。"""

    if inputs.reranker is None:
        raise ProbeError("未配置重排器")

    work = _Work(
        inputs=inputs,
        embedder=_BudgetedEmbedder(inputs.embedder, inputs.embedding_budget),
        reranker=_BudgetedReranker(inputs.reranker, inputs.rerank_budget),
    )

    a_questions: list[AblationQuestion] = []
    b_questions: list[AblationQuestion] = []
    c_questions: list[AblationQuestion] = []
    calibration: list[RefusalProbeRecord] = []

    for question in inputs.dataset.questions:
        try:
            outcome = await _run_question(work, question)
        except KnowledgeBaseNotAccessible as error:
            if question.category == "no_permission" and question.expected_behavior == "refuse":
                outcome = _empty_question(question)
            else:
                raise ProbeError(f"[{question.id}] 知识库授权失败") from error
        except ProbeError:
            raise
        except DatasetRunError as error:
            raise ProbeError(f"[{question.id}] 资产映射失败") from error
        except Exception as error:  # noqa: BLE001 - 收敛为静态探针错误，不回显底层文本
            raise ProbeError(f"[{question.id}] 探针检索失败") from error
        a_questions.append(outcome[0])
        b_questions.append(outcome[1])
        c_questions.append(outcome[2])
        calibration.append(outcome[3])

    dataset = inputs.dataset
    artifact_a = _artifact(dataset, "A_VECTOR", a_questions, inputs)
    artifact_b = _artifact(dataset, "B_RRF", b_questions, inputs)
    artifact_c = _artifact(dataset, "C_RERANK", c_questions, inputs)
    triplet = validate_ablation_triplet(artifact_a, artifact_b, artifact_c)
    ranking = aggregate_ranking_metrics(build_ranking_questions(dataset, artifact_c))
    return ProbeOutcome(
        a=artifact_a,
        b=artifact_b,
        c=artifact_c,
        calibration=tuple(calibration),
        triplet=triplet,
        ranking=ranking,
    )


async def _run_question(
    work: _Work, question: EvaluationQuestion
) -> tuple[AblationQuestion, AblationQuestion, AblationQuestion, RefusalProbeRecord]:
    inputs = work.inputs
    identity = _identity_for(inputs, question.scope.role)
    requested = [inputs.registry.kb_uuid(kb_id) for kb_id in question.scope.kb_ids]
    query = question.standalone_question or question.question
    scope_id = _scope_id(question)

    started = time.perf_counter()
    a_candidates, a_degraded = await _vector_only(work, question, requested, identity, query)
    a_latency = (time.perf_counter() - started) * 1000.0

    started = time.perf_counter()
    b_raw, b_mapped, b_degraded = await _rrf(work, question, requested, identity, query)
    b_latency = (time.perf_counter() - started) * 1000.0

    started = time.perf_counter()
    c_mapped, c_degraded = await _rerank(work, question, identity, query, b_raw, b_mapped)
    c_extra = (time.perf_counter() - started) * 1000.0
    # C 的延迟是 B 的完整检索延迟加上重排额外耗时；B 为空时没有额外阶段。
    c_latency = b_latency + (c_extra if b_raw else 0.0)

    top_score = b_mapped[0].fusion_score if b_mapped else None
    calibration = RefusalProbeRecord(
        question_id=question.id,
        expected_behavior=question.expected_behavior,
        top_score=top_score,
        candidate_count=len(b_mapped),
    )
    return (
        AblationQuestion(
            question_id=question.id,
            scope_id=scope_id,
            latency_ms=a_latency,
            degraded_stages=list(a_degraded),
            candidates=a_candidates,
        ),
        AblationQuestion(
            question_id=question.id,
            scope_id=scope_id,
            latency_ms=b_latency,
            degraded_stages=list(b_degraded),
            candidates=b_mapped,
        ),
        AblationQuestion(
            question_id=question.id,
            scope_id=scope_id,
            latency_ms=c_latency,
            degraded_stages=list(c_degraded),
            candidates=c_mapped,
        ),
        calibration,
    )


def _empty_question(
    question: EvaluationQuestion,
) -> tuple[AblationQuestion, AblationQuestion, AblationQuestion, RefusalProbeRecord]:
    scope_id = _scope_id(question)

    def empty() -> AblationQuestion:
        return AblationQuestion(
            question_id=question.id, scope_id=scope_id, latency_ms=0.0, candidates=[]
        )

    calibration = RefusalProbeRecord(
        question_id=question.id,
        expected_behavior=question.expected_behavior,
        top_score=None,
        candidate_count=0,
    )
    return empty(), empty(), empty(), calibration


async def _vector_only(
    work: _Work,
    question: EvaluationQuestion,
    requested: Sequence[uuid.UUID],
    identity: ProbeIdentity,
    query: str,
) -> tuple[list[RankingCandidate], tuple[DegradedStage, ...]]:
    inputs = work.inputs
    repository = inputs.repository_factory(question.scope.role, question)
    rows = await repository.load_kb_scope(
        user_id=identity.user_id, organization_id=identity.organization_id, kb_ids=requested
    )
    try:
        scope = resolve_retrieval_scope(
            rows, requested_kb_ids=requested, expected_analyzer_id=inputs.analyzer.analyzer_id
        )
    finally:
        await repository.release()
    if scope is None:
        return [], ()
    embedded = await embed_query_for_scope(work.embedder, query, scope)
    try:
        vector = await repository.fetch_vector_candidates(
            user_id=identity.user_id,
            organization_id=identity.organization_id,
            kb_ids=scope.kb_ids,
            profile_id=scope.profile_id,
            query_vector=embedded.vector,
        )
    finally:
        await repository.release()
    facts = [_ranked_fact(candidate, rank) for rank, candidate in enumerate(vector, start=1)]
    # 向量 SQL 已 release 之后，对全部候选做同一仓储的授权正文加载并立刻释放连接。
    mapped = await _map_facts(work, repository, identity, facts)
    return mapped, ()


async def _rrf(
    work: _Work,
    question: EvaluationQuestion,
    requested: Sequence[uuid.UUID],
    identity: ProbeIdentity,
    query: str,
) -> tuple[tuple[FusedCandidate, ...], list[RankingCandidate], tuple[DegradedStage, ...]]:
    inputs = work.inputs
    repository = inputs.repository_factory(question.scope.role, question)
    result: RetrievalResult = await search_authorized_chunks(
        repository,
        user_id=identity.user_id,
        organization_id=identity.organization_id,
        kb_ids=requested,
        query=query,
        embedder=work.embedder,
        analyzer=inputs.analyzer,
        reranker=None,
    )
    facts = [_fused_fact(candidate) for candidate in result.candidates]
    # search_authorized_chunks 返回前已 release；随后再做一次授权正文加载并释放。
    mapped = await _map_facts(work, repository, identity, facts)
    return tuple(result.candidates), mapped, tuple(result.degraded_stages)


async def _rerank(
    work: _Work,
    question: EvaluationQuestion,
    identity: ProbeIdentity,
    query: str,
    b_raw: tuple[FusedCandidate, ...],
    b_mapped: list[RankingCandidate],
) -> tuple[list[RankingCandidate], tuple[DegradedStage, ...]]:
    inputs = work.inputs
    if not b_raw:
        return list(b_mapped), ()

    top = list(b_raw[:RERANK_TOP_K])
    chunk_ids = [candidate.chunk_id for candidate in top]
    repository = inputs.repository_factory(question.scope.role, question)
    try:
        rows = await repository.load_evidence_chunks(
            user_id=identity.user_id,
            organization_id=identity.organization_id,
            chunk_ids=chunk_ids,
        )
    finally:
        # 关键顺序：正文加载完成后立刻交还连接，再发起模型调用。
        await repository.release()
    expected = {candidate.chunk_id: candidate.version_id for candidate in top}
    row_by_id = _evidence_by_chunk(rows, expected)

    rerank_inputs = [
        RerankInput(candidate_id=str(chunk_id), text=row_by_id[chunk_id].text)
        for chunk_id in chunk_ids
    ]
    try:
        scores = await run_in_threadpool(work.reranker.rerank, query, rerank_inputs)
    except RerankUnavailableError:
        # 只有明确的重排不可用才允许降级回 B。
        return list(b_mapped), (STAGE_RERANK_UNAVAILABLE,)

    ordered = apply_rerank_scores(list(b_raw), scores)
    if ordered is None:
        raise ProbeError("重排分数与候选不一致")

    score_by_id = {score.candidate_id: score.score for score in scores}
    rank_by_id = {
        str(candidate.chunk_id): position
        for position, candidate in enumerate(ordered, start=1)
    }
    mapped_by_id: dict[str, RankingCandidate] = {}
    for candidate in b_mapped:
        if candidate.candidate_id in mapped_by_id:
            raise ProbeError("B 候选标识重复")
        mapped_by_id[candidate.candidate_id] = candidate
    if set(mapped_by_id) != set(rank_by_id):
        raise ProbeError("重排候选集合与 B 不一致")

    # C 复用 B 的真实来源事实，只更新最终名次与重排分，不重新伪造 locator。
    reranked = [
        mapped_by_id[candidate_id].model_copy(
            update={"rank": position, "rerank_score": score_by_id.get(candidate_id)}
        )
        for candidate_id, position in rank_by_id.items()
    ]
    reranked.sort(key=lambda candidate: candidate.rank)
    return reranked, ()


async def _map_facts(
    work: _Work,
    repository: RetrievalRepository,
    identity: ProbeIdentity,
    facts: Sequence[CandidateFacts],
) -> list[RankingCandidate]:
    """加载候选的真实授权正文并映射为 ``RankingCandidate``；保证 release 一定发生。"""

    inputs = work.inputs
    if not facts:
        return []
    expected = {fact.chunk_id: fact.version_id for fact in facts}
    try:
        rows = await repository.load_evidence_chunks(
            user_id=identity.user_id,
            organization_id=identity.organization_id,
            chunk_ids=list(expected),
        )
    finally:
        await repository.release()
    row_by_id = _evidence_by_chunk(rows, expected)

    mapped: list[RankingCandidate] = []
    for fact in facts:
        try:
            version_ref = inputs.registry.version_ref(fact.version_id)
        except DatasetRunError as error:
            raise ProbeError("候选版本未登记") from error
        candidate = inputs.mapper.map(row_by_id[fact.chunk_id], fact, version_ref)
        _validate_mapping(candidate, fact.chunk_id, version_ref)
        mapped.append(candidate)
    if len(mapped) != len(facts):
        raise ProbeError("候选映射数量不一致")
    return mapped


def _evidence_by_chunk(
    rows: Sequence[EvidenceChunkRow],
    expected: Mapping[uuid.UUID, uuid.UUID],
) -> dict[uuid.UUID, EvidenceChunkRow]:
    """校验证据行集合完整、无重复，且每条版本与候选一致。"""

    row_by_id: dict[uuid.UUID, EvidenceChunkRow] = {}
    for row in rows:
        if row.chunk_id in row_by_id:
            raise ProbeError("证据正文重复")
        row_by_id[row.chunk_id] = row
    if set(row_by_id) != set(expected):
        raise ProbeError("证据正文候选集合不一致")
    for chunk_id, version_id in expected.items():
        if row_by_id[chunk_id].version_id != version_id:
            raise ProbeError("证据版本与候选不一致")
    return row_by_id


def _validate_mapping(
    candidate: RankingCandidate, chunk_id: uuid.UUID, version_ref: LogicalRef
) -> None:
    if candidate.candidate_id != str(chunk_id):
        raise ProbeError("候选映射 candidateId 不一致")
    if (candidate.kb_id, candidate.document_id, candidate.version) != (
        version_ref.kb_id,
        version_ref.document_id,
        version_ref.version,
    ):
        raise ProbeError("候选映射来源与逻辑标识不一致")


def _identity_for(inputs: ProbeInputs, role: str) -> ProbeIdentity:
    identity = inputs.role_identities.get(role)
    if identity is None:
        raise ProbeError("缺少角色身份映射")
    return identity


def _ranked_fact(candidate: RankedChunk, rank: int) -> CandidateFacts:
    return CandidateFacts(
        chunk_id=candidate.chunk_id,
        version_id=candidate.version_id,
        rank=rank,
        vector_rank=rank,
        vector_score=candidate.score,
    )


def _fused_fact(candidate: FusedCandidate) -> CandidateFacts:
    return CandidateFacts(
        chunk_id=candidate.chunk_id,
        version_id=candidate.version_id,
        rank=candidate.fusion_rank,
        vector_rank=candidate.vector_rank,
        vector_score=candidate.vector_score,
        keyword_rank=candidate.keyword_rank,
        keyword_score=candidate.keyword_score,
        fusion_rank=candidate.fusion_rank,
        fusion_score=candidate.fusion_score,
    )


def _scope_id(question: EvaluationQuestion) -> str:
    return f"role={question.scope.role};kbIds={','.join(sorted(question.scope.kb_ids))}"


def _artifact(
    dataset: EvaluationDataset,
    variant: AblationVariant,
    questions: Sequence[AblationQuestion],
    inputs: ProbeInputs,
) -> AblationArtifact:
    return AblationArtifact(
        dataset_kind=dataset.dataset_kind,
        dataset_version=dataset.dataset_version,
        variant=variant,
        config=dict(inputs.config),
        model_identities=dict(inputs.model_identities),
        created_at=inputs.created_at,
        questions=list(questions),
    )


__all__ = [
    "BudgetCounter",
    "CandidateFacts",
    "CandidateMapper",
    "ProbeBudgetExceeded",
    "ProbeError",
    "ProbeIdentity",
    "ProbeInputs",
    "ProbeOutcome",
    "RepositoryFactory",
    "run_ablation_probe",
]
