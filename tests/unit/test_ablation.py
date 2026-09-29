"""A/B/C 消融产物 schema 与三元组校验聚焦单测（合成产物，不代表真实数据）。"""

from __future__ import annotations

from typing import Literal

import pytest
from pydantic import ValidationError
from rag_backend.config import Settings
from rag_backend.evaluation.ablation import (
    AblationArtifact,
    AblationQuestion,
    AblationValidationError,
    AblationVariant,
    DegradedStage,
    validate_ablation_triplet,
)
from rag_backend.evaluation.dataset import GoldLocator
from rag_backend.evaluation.ranking_metrics import RankingCandidate

_MD_PARSER = "markdown-it-py-4.2.0-v1"


def _locator() -> GoldLocator:
    return GoldLocator(
        source_type="markdown",
        parser_version=_MD_PARSER,
        heading_path=["H"],
        start_line=1,
        end_line=5,
    )


def _candidate(
    candidate_id: str,
    rank: int,
    *,
    vector_rank: int | None = None,
    fusion_rank: int | None = None,
    fusion_score: float | None = None,
    rerank_score: float | None = None,
) -> RankingCandidate:
    return RankingCandidate(
        candidate_id=candidate_id,
        kb_id="kb",
        document_id="doc",
        version=1,
        locator=_locator(),
        rank=rank,
        vector_rank=vector_rank if vector_rank is not None else rank,
        vector_score=1.0,
        fusion_rank=fusion_rank,
        fusion_score=fusion_score,
        rerank_score=rerank_score,
    )


def _a_candidate(candidate_id: str, rank: int) -> RankingCandidate:
    return _candidate(candidate_id, rank)


def _b_candidate(candidate_id: str, rank: int) -> RankingCandidate:
    return _candidate(candidate_id, rank, fusion_rank=rank, fusion_score=1.0)


def _c_candidate(
    candidate_id: str,
    rank: int,
    *,
    fusion_rank: int | None = None,
    rerank_score: float | None = None,
) -> RankingCandidate:
    return _candidate(
        candidate_id,
        rank,
        fusion_rank=fusion_rank if fusion_rank is not None else rank,
        fusion_score=1.0,
        rerank_score=rerank_score,
    )


def _question(
    variant_candidates: list[RankingCandidate],
    *,
    question_id: str = "q1",
    scope_id: str = "scope-1",
    degraded: list[DegradedStage] | None = None,
) -> AblationQuestion:
    return AblationQuestion(
        question_id=question_id,
        scope_id=scope_id,
        latency_ms=12.0,
        degraded_stages=degraded or [],
        candidates=variant_candidates,
    )


def _artifact(
    variant: AblationVariant,
    questions: list[AblationQuestion],
    *,
    dataset_kind: Literal["dev", "holdout"] = "dev",
    dataset_version: str = "v1",
) -> AblationArtifact:
    return AblationArtifact(
        dataset_kind=dataset_kind,
        dataset_version=dataset_version,
        variant=variant,
        config={},
        model_identities={},
        created_at="2026-09-29T00:00:00Z",
        questions=questions,
    )


def _triplet(
    *,
    a_questions: list[AblationQuestion] | None = None,
    b_questions: list[AblationQuestion] | None = None,
    c_questions: list[AblationQuestion] | None = None,
    b_version: str = "v1",
    c_version: str = "v1",
) -> tuple[AblationArtifact, AblationArtifact, AblationArtifact]:
    a = _artifact("A_VECTOR", a_questions or [_question([_a_candidate("c1", 1)])])
    b = _artifact(
        "B_RRF",
        b_questions or [_question([_b_candidate("c1", 1)])],
        dataset_version=b_version,
    )
    c = _artifact(
        "C_RERANK",
        c_questions or [_question([_c_candidate("c1", 1)])],
        dataset_version=c_version,
    )
    return a, b, c


def test_a_rejects_fusion_fields() -> None:
    with pytest.raises(ValidationError):
        _artifact(
            "A_VECTOR",
            [_question([_candidate("c1", 1, fusion_rank=1, fusion_score=1.0)])],
        )


def test_a_requires_final_rank_equal_vector_rank() -> None:
    with pytest.raises(ValidationError):
        _artifact(
            "A_VECTOR",
            [_question([_candidate("c1", 2, vector_rank=1)])],
        )


def test_b_requires_fusion_and_forbids_rerank() -> None:
    with pytest.raises(ValidationError):
        _artifact("B_RRF", [_question([_a_candidate("c1", 1)])])
    with pytest.raises(ValidationError):
        _artifact(
            "B_RRF",
            [_question([_candidate("c1", 1, fusion_rank=1, fusion_score=1.0, rerank_score=0.5)])],
        )


def test_c_allows_rerank_and_degraded_forbids_score() -> None:
    _artifact("C_RERANK", [_question([_c_candidate("c1", 1, rerank_score=0.5)])])
    with pytest.raises(ValidationError):
        _artifact(
            "C_RERANK",
            [
                _question(
                    [_c_candidate("c1", 1, rerank_score=0.5)],
                    degraded=["rerank_unavailable"],
                )
            ],
        )


def test_triplet_success_with_rerank_and_report() -> None:
    a, b, c = _triplet()
    c_degraded = _artifact(
        "C_RERANK",
        [_question([_c_candidate("c1", 1)], degraded=["rerank_unavailable"])],
    )
    report = validate_ablation_triplet(a, b, c)
    assert report.question_count == 1
    assert report.fallback_question_ids == ()
    fallback_report = validate_ablation_triplet(a, b, c_degraded)
    assert fallback_report.fallback_question_ids == ("q1",)


def test_triplet_successful_rerank_may_reorder() -> None:
    a = _artifact("A_VECTOR", [_question([_a_candidate("c1", 1), _a_candidate("c2", 2)])])
    b = _artifact("B_RRF", [_question([_b_candidate("c1", 1), _b_candidate("c2", 2)])])
    c = _artifact(
        "C_RERANK",
        [
            _question(
                [
                    _c_candidate("c1", 2, fusion_rank=1, rerank_score=0.1),
                    _c_candidate("c2", 1, fusion_rank=2, rerank_score=0.9),
                ]
            )
        ],
    )
    assert validate_ablation_triplet(a, b, c).fallback_question_ids == ()


def test_triplet_question_set_drift_rejected() -> None:
    a, b, c = _triplet(
        c_questions=[_question([_c_candidate("c1", 1)], question_id="other")]
    )
    with pytest.raises(AblationValidationError):
        validate_ablation_triplet(a, b, c)


def test_triplet_dataset_metadata_mismatch_rejected() -> None:
    a, b, c = _triplet(c_version="v2")
    with pytest.raises(AblationValidationError):
        validate_ablation_triplet(a, b, c)


def test_triplet_scope_mismatch_rejected() -> None:
    a, b, c = _triplet(
        c_questions=[_question([_c_candidate("c1", 1)], scope_id="scope-2")]
    )
    with pytest.raises(AblationValidationError):
        validate_ablation_triplet(a, b, c)


def test_triplet_wrong_variant_order_rejected() -> None:
    a, b, c = _triplet()
    with pytest.raises(AblationValidationError):
        validate_ablation_triplet(b, a, c)


def test_triplet_degraded_must_match_all_b_candidate_facts() -> None:
    a, b, _ = _triplet()
    changed = _c_candidate("c1", 1).model_copy(update={"document_id": "other"})
    c = _artifact(
        "C_RERANK",
        [_question([changed], degraded=["rerank_unavailable"])],
    )
    with pytest.raises(AblationValidationError):
        validate_ablation_triplet(a, b, c)


def test_triplet_degraded_must_match_b_order() -> None:
    a = _artifact("A_VECTOR", [_question([_a_candidate("c1", 1), _a_candidate("c2", 2)])])
    b = _artifact("B_RRF", [_question([_b_candidate("c1", 1), _b_candidate("c2", 2)])])
    c = _artifact(
        "C_RERANK",
        [
            _question(
                [
                    _c_candidate("c1", 2, fusion_rank=2),
                    _c_candidate("c2", 1, fusion_rank=1),
                ],
                degraded=["rerank_unavailable"],
            )
        ],
    )
    with pytest.raises(AblationValidationError):
        validate_ablation_triplet(a, b, c)


def test_production_rerank_default_stays_disabled() -> None:
    assert Settings.model_fields["rerank_enabled"].default is False
