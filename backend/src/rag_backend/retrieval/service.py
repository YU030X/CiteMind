"""授权混合检索用例：范围解析、查询编码、双路召回与 RRF 融合。

编排顺序固定，以保证模型调用不持有数据库连接：

1. 用调用方 ``AsyncSession`` 读出请求 KB 的授权范围与 active index profile；
2. 立刻 ``release`` 结束只读事务并交还连接；
3. 在无数据库连接的状态下做本地关键词分析与查询编码（编码同步客户端在线程池执行）；
4. 用新的短事务跑向量路与关键词路 SQL，取完 DTO 后再 ``release``；
5. 纯本地 RRF 融合。

授权与版本谓词全部在两条 SQL 内，检索候选只能是当前组织、未撤销成员、未删除文档的
active version、``READY`` generation 且 profile 与 KB active profile 一致的 chunk。
作用域跨越多个不同 profile 时无法用同一次编码检索，显式拒绝而不是猜测。
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from starlette.concurrency import run_in_threadpool

from rag_backend.retrieval.errors import (
    KnowledgeBaseNotAccessible,
    RetrievalAnalyzerMismatch,
    RetrievalEmbeddingError,
    RetrievalProfileConflict,
    RetrievalQueryInvalid,
    RetrievalScopeUnavailable,
)
from rag_backend.retrieval.fusion import (
    FusedCandidate,
    reciprocal_rank_fusion,
)
from rag_backend.retrieval.keyword_analyzer import KeywordAnalyzerInputError
from rag_backend.retrieval.query_embedding_client import (
    EmbeddedQuery,
    QueryEmbeddingError,
    QueryEmbeddingInputError,
)
from rag_backend.retrieval.repository import KbScopeRow, RetrievalRepository
from rag_backend.retrieval.rerank_client import (
    RerankInput,
    RerankScore,
    RerankUnavailableError,
)


class QueryEmbedder(Protocol):
    """单条查询编码接口；``QueryEmbeddingClient`` 在结构上满足它。"""

    def embed_query(self, text: str, expected_model_revision: str) -> EmbeddedQuery: ...

    def close(self) -> None: ...


class Reranker(Protocol):
    """可选重排接口；``RerankClient`` 在结构上满足它。"""

    def rerank(self, query: str, candidates: list[RerankInput]) -> list[RerankScore]: ...

    def close(self) -> None: ...


class KeywordAnalyzerLike(Protocol):
    """查询侧关键词分析器；只产出可交给 ``to_tsvector('simple', :param)`` 的词流。

    ``analyzer_id`` 是分析器身份（jieba 版本 + 词典摘要 + 归一化规则），用于与 active
    index profile 的 ``keyword_analyzer_version`` 比对。
    """

    @property
    def analyzer_id(self) -> str: ...

    def analyze(self, text: str) -> str: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class RetrievalScope:
    """单次检索的有效范围：可检索 KB 与唯一的 active index profile。"""

    kb_ids: tuple[uuid.UUID, ...]
    profile_id: uuid.UUID
    model_revision: str
    dimension: int


@dataclass(frozen=True, slots=True, kw_only=True)
class RetrievalResult:
    """一次授权检索的完整结果：

    ``kb_ids`` 是**实际解析后**的可检索 KB 子集：``active_index_profile_id`` 为 NULL 的 KB
    已被静默排除，没有任何可检索 KB 时为空元组。它可能小于请求的名义范围，调用方必须用它
    记录真实检索范围，而不是复用请求范围或二次查询（那会与本次检索竞态）。
    ``candidates`` 是按融合名次排列的候选。
    ``degraded_stages`` 只记录真实异常造成的降级阶段（当前仅 ``rerank_unavailable``）；
    重排未启用或成功时不标记降级。
    """

    kb_ids: tuple[uuid.UUID, ...]
    candidates: tuple[FusedCandidate, ...]
    degraded_stages: tuple[str, ...] = ()


# 只对融合后的前 10 个候选调用 reranker；其余保持原融合顺序跟在其后。
RERANK_TOP_K = 10
STAGE_RERANK_UNAVAILABLE = "rerank_unavailable"


def resolve_retrieval_scope(
    rows: Sequence[KbScopeRow],
    *,
    requested_kb_ids: Sequence[uuid.UUID],
    expected_analyzer_id: str,
) -> RetrievalScope | None:
    """把授权行收敛为检索范围；纯函数，便于独立单测。

    - 请求的每个 KB 都必须在授权行内，否则统一抛 :class:`KnowledgeBaseNotAccessible`；
    - ``active_index_profile_id`` 为 NULL 的 KB 不可检索，静默排除（它们仍是有效授权行）；
    - 剩余 KB 必须共享同一个 profile，跨越多个 profile 时抛
      :class:`RetrievalProfileConflict`；
    - profile 行的 revision/dimension/normalize 不完整或不满足契约时抛
      :class:`RetrievalScopeUnavailable`；
    - profile 的 ``keyword_analyzer_version`` 必须严格等于当前分析器身份，否则抛
      :class:`RetrievalAnalyzerMismatch`（fail closed，避免用新词项流查旧索引）；
    - 没有任何可检索 KB 时返回 ``None``（调用方返回空候选，不调用模型）。
    """

    accessible = {row.kb_id for row in rows}
    if any(kb_id not in accessible for kb_id in requested_kb_ids):
        raise KnowledgeBaseNotAccessible("知识库不存在或无权访问")

    # 同一 profile 行的字段在各 KB 上报值相同，保留首个代表行即可。
    profiles: dict[uuid.UUID, KbScopeRow] = {}
    searchable: list[uuid.UUID] = []
    for row in rows:
        if row.profile_id is None:
            continue
        searchable.append(row.kb_id)
        profiles.setdefault(row.profile_id, row)

    if len(profiles) > 1:
        raise RetrievalProfileConflict("所选知识库使用了不同的索引 profile")

    if not profiles:
        return None

    profile_id, representative = next(iter(profiles.items()))
    if (
        representative.model_revision is None
        or representative.dimension is None
        or representative.normalize is not True
    ):
        raise RetrievalScopeUnavailable("知识库索引 profile 不完整")
    if representative.keyword_analyzer_version != expected_analyzer_id:
        raise RetrievalAnalyzerMismatch("知识库索引的分析器身份与当前服务不一致")
    return RetrievalScope(
        kb_ids=tuple(searchable),
        profile_id=profile_id,
        model_revision=representative.model_revision,
        dimension=representative.dimension,
    )


async def _release_read_transaction(repository: RetrievalRepository) -> None:
    """结束只读事务并交还连接，且不替换正在传播的原始错误。

    清理本身若失败，只在已有异常在传播时作为附注挂到该异常上；没有异常时照常上抛，
    避免掩盖真正的领域错误（连接最终仍由请求级 Session 依赖关闭兜底）。
    """

    in_flight = sys.exc_info()[1]
    try:
        await repository.release()
    except Exception as release_error:
        if in_flight is None:
            raise
        in_flight.add_note(f"检索只读事务释放失败: {release_error!r}")


async def search_authorized_chunks(
    repository: RetrievalRepository,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    kb_ids: Sequence[uuid.UUID],
    query: str,
    embedder: QueryEmbedder,
    analyzer: KeywordAnalyzerLike,
    reranker: Reranker | None = None,
) -> RetrievalResult:
    """执行一次授权混合检索；失败时抛 :mod:`rag_backend.retrieval.errors` 内的领域错误。

    ``reranker`` 为 ``None`` 时完全不调用重排、不标记降级。提供时，融合与数据库 release 之后
    才加载 top-10 候选正文并调用重排；重排失败整体保持原 RRF 顺序并返回
    ``degraded_stages=('rerank_unavailable',)``。
    """

    requested = tuple(dict.fromkeys(kb_ids))
    rows = await repository.load_kb_scope(
        user_id=user_id, organization_id=organization_id, kb_ids=requested
    )
    try:
        scope = resolve_retrieval_scope(
            rows, requested_kb_ids=requested, expected_analyzer_id=analyzer.analyzer_id
        )
    finally:
        # 关键顺序：无论范围解析成功与否都交还连接，后续本地分析与模型调用都不持有连接。
        await _release_read_transaction(repository)
    if scope is None:
        return RetrievalResult(kb_ids=(), candidates=())

    try:
        # 分词是同步 CPU 调用；放进线程池，避免阻塞事件循环。
        query_terms = await run_in_threadpool(analyzer.analyze, query)
    except KeywordAnalyzerInputError as error:
        raise RetrievalQueryInvalid("查询超出可处理长度") from error

    embedded = await _embed_query(embedder, query, scope)
    if len(embedded.vector) != scope.dimension:
        raise RetrievalEmbeddingError("查询向量维度与索引 profile 不一致")

    try:
        vector_candidates = await repository.fetch_vector_candidates(
            user_id=user_id,
            organization_id=organization_id,
            kb_ids=scope.kb_ids,
            profile_id=scope.profile_id,
            query_vector=embedded.vector,
        )
        keyword_candidates = await repository.fetch_keyword_candidates(
            user_id=user_id,
            organization_id=organization_id,
            kb_ids=scope.kb_ids,
            profile_id=scope.profile_id,
            query_terms=query_terms,
        )
    finally:
        # 候选查询无论成功与否都结束只读事务；后续融合是纯本地计算，不持有连接。
        await _release_read_transaction(repository)
    fused = reciprocal_rank_fusion(vector_candidates, keyword_candidates)
    degraded_stages: tuple[str, ...] = ()
    if reranker is not None and fused:
        fused, degraded_stages = await _rerank_top_candidates(
            repository,
            reranker,
            user_id=user_id,
            organization_id=organization_id,
            query=query,
            fused=fused,
        )
    return RetrievalResult(
        kb_ids=scope.kb_ids,
        candidates=tuple(fused),
        degraded_stages=degraded_stages,
    )


async def _rerank_top_candidates(
    repository: RetrievalRepository,
    reranker: Reranker,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    query: str,
    fused: list[FusedCandidate],
) -> tuple[list[FusedCandidate], tuple[str, ...]]:
    """对 RRF top-10 调用重排；失败整体回退，绝不伪造分数或降级为部分重排。

    关键顺序：先加载候选正文并 release 数据库连接，再发起模型 HTTP；模型调用期间不持有连接。
    成功时按 score 降序、同分 chunkId 升序重排这 10 个，其余保持原融合顺序跟在其后；
    ``fusion_rank``/``fusion_score`` 等既有字段一律不改。
    """

    top = fused[:RERANK_TOP_K]
    rest = fused[RERANK_TOP_K:]
    chunk_ids = [candidate.chunk_id for candidate in top]
    try:
        rows = await repository.load_evidence_chunks(
            user_id=user_id, organization_id=organization_id, chunk_ids=chunk_ids
        )
    finally:
        await _release_read_transaction(repository)
    text_by_id = {row.chunk_id: row.text for row in rows}
    inputs: list[RerankInput] = []
    for candidate in top:
        text = text_by_id.get(candidate.chunk_id)
        if text is None:
            # 候选在读取正文前失效（撤权/删文档/换版本）：整体降级，不做部分重排。
            return fused, (STAGE_RERANK_UNAVAILABLE,)
        inputs.append(RerankInput(candidate_id=str(candidate.chunk_id), text=text))
    try:
        scores = await run_in_threadpool(reranker.rerank, query, inputs)
    except RerankUnavailableError:
        return fused, (STAGE_RERANK_UNAVAILABLE,)
    score_by_id = {score.candidate_id: score.score for score in scores}
    if set(score_by_id) != {candidate.candidate_id for candidate in inputs}:
        return fused, (STAGE_RERANK_UNAVAILABLE,)
    ordered = sorted(
        top,
        key=lambda candidate: (-score_by_id[str(candidate.chunk_id)], candidate.chunk_id.int),
    )
    return [*ordered, *rest], ()


async def _embed_query(embedder: QueryEmbedder, query: str, scope: RetrievalScope) -> EmbeddedQuery:
    """在线程池内执行同步编码客户端，并把失败收敛为静态领域错误。"""

    try:
        return await run_in_threadpool(embedder.embed_query, query, scope.model_revision)
    except QueryEmbeddingInputError as error:
        raise RetrievalQueryInvalid("查询无法编码") from error
    except QueryEmbeddingError as error:
        raise RetrievalEmbeddingError(
            "查询编码服务暂时不可用",
            retryable=error.retryable,
            retry_after_seconds=error.retry_after_seconds,
        ) from error


__all__ = [
    "KeywordAnalyzerLike",
    "QueryEmbedder",
    "RERANK_TOP_K",
    "Reranker",
    "RetrievalResult",
    "RetrievalScope",
    "resolve_retrieval_scope",
    "search_authorized_chunks",
]
