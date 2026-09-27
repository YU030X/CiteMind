"""RRF 融合纯函数单测：排名权重、去重、两路命中差异、上限与确定性。

不连接数据库、不调用模型；只验证 :mod:`rag_backend.retrieval.fusion` 的本地计算契约。
"""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest
from rag_backend.retrieval.fusion import (
    MAX_FUSED_CANDIDATES,
    RRF_K,
    FusedCandidate,
    RankedChunk,
    reciprocal_rank_fusion,
)


def chunk(score: float = 1.0) -> RankedChunk:
    return RankedChunk(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        kb_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        score=score,
    )


def test_contract_constants_are_frozen() -> None:
    assert RRF_K == 60
    assert MAX_FUSED_CANDIDATES == 40


def test_single_path_scores_match_rrf_formula() -> None:
    first = chunk()
    second = chunk()

    fused = reciprocal_rank_fusion([first, second], [])

    assert [candidate.chunk_id for candidate in fused] == [first.chunk_id, second.chunk_id]
    assert fused[0].fusion_score == pytest.approx(1 / (RRF_K + 1))
    assert fused[1].fusion_score == pytest.approx(1 / (RRF_K + 2))
    assert fused[0].vector_rank == 1
    assert fused[0].vector_score == first.score
    assert fused[0].keyword_rank is None
    assert fused[0].keyword_score is None


def test_two_path_hits_sum_and_keep_both_ranks() -> None:
    shared = chunk(score=0.9)
    vector_only = chunk(score=0.8)

    fused = reciprocal_rank_fusion([shared, vector_only], [shared])

    assert len(fused) == 2
    top: FusedCandidate = fused[0]
    assert top.chunk_id == shared.chunk_id
    assert top.vector_rank == 1
    assert top.keyword_rank == 1
    assert top.vector_score == 0.9
    assert top.keyword_score == shared.score
    assert top.fusion_score == pytest.approx(2 / (RRF_K + 1))
    assert fused[1].chunk_id == vector_only.chunk_id
    assert fused[1].vector_rank == 2
    assert fused[1].keyword_rank is None


def test_chunk_appearing_in_both_paths_is_deduplicated_once() -> None:
    shared = chunk()

    fused = reciprocal_rank_fusion([shared], [shared])

    assert len(fused) == 1
    assert fused[0].chunk_id == shared.chunk_id


def test_duplicate_within_one_path_counts_first_position_only() -> None:
    shared = chunk()
    later = chunk()

    fused = reciprocal_rank_fusion([shared, shared, later], [])

    assert len(fused) == 2
    assert fused[0].chunk_id == shared.chunk_id
    assert fused[0].vector_rank == 1
    assert fused[0].fusion_score == pytest.approx(1 / (RRF_K + 1))


def test_anchor_metadata_follows_the_path_that_has_the_chunk() -> None:
    keyword_only = chunk()

    fused = reciprocal_rank_fusion([], [keyword_only])

    assert fused[0].document_id == keyword_only.document_id
    assert fused[0].kb_id == keyword_only.kb_id
    assert fused[0].version_id == keyword_only.version_id


def test_max_candidates_and_deterministic_tie_break() -> None:
    candidates = [chunk() for _ in range(MAX_FUSED_CANDIDATES + 5)]

    fused = reciprocal_rank_fusion(candidates, [], max_candidates=3)

    assert len(fused) == 3
    assert [candidate.fusion_rank for candidate in fused] == [1, 2, 3]
    # 并列时按 chunkId 整数值升序；分数本身单调下降，因此只断言前缀稳定。
    assert fused[0].fusion_score == pytest.approx(1 / (RRF_K + 1))


def test_tie_break_uses_chunk_id_order() -> None:
    low = replace(chunk(), chunk_id=uuid.UUID(int=1))
    high = replace(chunk(), chunk_id=uuid.UUID(int=2))
    # 两路各 rank 1 -> 融合分数相同，只能由 chunkId 决定顺序。
    fused = reciprocal_rank_fusion([low], [high])

    assert fused[0].chunk_id == low.chunk_id
    assert fused[1].chunk_id == high.chunk_id


def test_empty_inputs_yield_empty_output() -> None:
    assert reciprocal_rank_fusion([], []) == []


def test_rejects_non_positive_parameters() -> None:
    with pytest.raises(ValueError):
        reciprocal_rank_fusion([], [], rrf_k=0)
    with pytest.raises(ValueError):
        reciprocal_rank_fusion([], [], max_candidates=0)
