"""RRF 融合：向量路与关键词路候选按 ``chunkId`` 去重的纯函数实现。

本模块只做本地计算：不连接数据库、不调用模型、不读环境变量。输入是两路各自按相关性
降序排列的候选（位置即原排名，从 1 开始），输出带两路原排名/分数与融合名次的候选。
融合公式与参数由 [检索契约](../../../../docs/retrieval.md) 冻结：

    RRF(d) = Σ 1 / (60 + rank_i(d))

一路未命中时不贡献分数；按 ``chunkId`` 去重；最多保留 ``MAX_FUSED_CANDIDATES`` 个。
同一路内重复出现的 ``chunkId`` 只按首次出现计一次，避免重复累加。融合名次相同的候选
按 ``chunkId`` 的整数值升序排列，保证输出确定。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

# 融合参数与每路 top-k 是已冻结的初值，需在开发集调优；本切片不引入新阈值。
RRF_K: Final = 60
PATH_TOP_K: Final = 20
MAX_FUSED_CANDIDATES: Final = 40


@dataclass(frozen=True, slots=True)
class RankedChunk:
    """单路候选：排名由在序列中的位置决定，``score`` 是该路原始分数。"""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    kb_id: uuid.UUID
    version_id: uuid.UUID
    score: float


@dataclass(frozen=True, slots=True)
class FusedCandidate:
    """融合结果：两路可能未命中的排名与分数用 ``None`` 表示。"""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    kb_id: uuid.UUID
    version_id: uuid.UUID
    vector_rank: int | None
    vector_score: float | None
    keyword_rank: int | None
    keyword_score: float | None
    fusion_rank: int
    fusion_score: float


def _positions(
    candidates: Sequence[RankedChunk],
) -> dict[uuid.UUID, tuple[int, RankedChunk]]:
    """按首次出现记录每个 ``chunkId`` 的 1-based 排名与代表行。"""

    positions: dict[uuid.UUID, tuple[int, RankedChunk]] = {}
    for rank, candidate in enumerate(candidates, start=1):
        positions.setdefault(candidate.chunk_id, (rank, candidate))
    return positions


def reciprocal_rank_fusion(
    vector_candidates: Sequence[RankedChunk],
    keyword_candidates: Sequence[RankedChunk],
    *,
    rrf_k: int = RRF_K,
    max_candidates: int = MAX_FUSED_CANDIDATES,
) -> list[FusedCandidate]:
    """融合两路候选；返回按融合分数降序、最多 ``max_candidates`` 个的结果。"""

    if rrf_k <= 0:
        raise ValueError("rrf_k 必须为正整数")
    if max_candidates <= 0:
        raise ValueError("max_candidates 必须为正整数")

    vector_positions = _positions(vector_candidates)
    keyword_positions = _positions(keyword_candidates)
    chunk_ids = list(dict.fromkeys([*vector_positions, *keyword_positions]))

    scored: list[tuple[float, uuid.UUID, RankedChunk]] = []
    for chunk_id in chunk_ids:
        score = 0.0
        if chunk_id in vector_positions:
            score += 1.0 / (rrf_k + vector_positions[chunk_id][0])
        if chunk_id in keyword_positions:
            score += 1.0 / (rrf_k + keyword_positions[chunk_id][0])
        anchor = (
            vector_positions[chunk_id][1]
            if chunk_id in vector_positions
            else keyword_positions[chunk_id][1]
        )
        scored.append((score, chunk_id, anchor))

    scored.sort(key=lambda item: (-item[0], item[1].int))

    fused: list[FusedCandidate] = []
    for fusion_rank, (score, chunk_id, anchor) in enumerate(scored[:max_candidates], start=1):
        vector_hit = vector_positions.get(chunk_id)
        keyword_hit = keyword_positions.get(chunk_id)
        fused.append(
            FusedCandidate(
                chunk_id=chunk_id,
                document_id=anchor.document_id,
                kb_id=anchor.kb_id,
                version_id=anchor.version_id,
                vector_rank=vector_hit[0] if vector_hit is not None else None,
                vector_score=vector_hit[1].score if vector_hit is not None else None,
                keyword_rank=keyword_hit[0] if keyword_hit is not None else None,
                keyword_score=keyword_hit[1].score if keyword_hit is not None else None,
                fusion_rank=fusion_rank,
                fusion_score=score,
            )
        )
    return fused


__all__ = [
    "MAX_FUSED_CANDIDATES",
    "PATH_TOP_K",
    "RRF_K",
    "FusedCandidate",
    "RankedChunk",
    "reciprocal_rank_fusion",
]
