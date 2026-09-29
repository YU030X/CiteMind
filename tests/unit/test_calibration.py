"""拒答阈值标定纯函数聚焦单测（合成记录，不代表真实标定结果）。"""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError
from rag_backend.evaluation.calibration import (
    Behavior,
    CalibrationInputError,
    RefusalProbeRecord,
    evaluate_refusal_threshold,
    scan_refusal_thresholds,
    select_dev_threshold,
    threshold_grid,
)


def _record(
    question_id: str,
    expected: Behavior,
    top_score: float | None,
    candidate_count: int = 1,
) -> RefusalProbeRecord:
    return RefusalProbeRecord(
        question_id=question_id,
        expected_behavior=expected,
        top_score=top_score,
        candidate_count=candidate_count,
    )


def test_no_candidates_always_refuses() -> None:
    record = _record("r", "refuse", None, candidate_count=0)
    point = evaluate_refusal_threshold([record], threshold=0.0)
    assert point.refuse_correct_numerator == 1
    assert point.refusal_accuracy == 1.0


def test_score_equal_to_threshold_is_answer() -> None:
    record = _record("a", "answer", 0.5)
    point = evaluate_refusal_threshold([record], threshold=0.5)
    assert point.false_refusal_numerator == 0
    assert point.false_refusal_rate == 0.0
    below = evaluate_refusal_threshold([record], threshold=0.6)
    assert below.false_refusal_numerator == 1


def test_empty_denominator_is_none() -> None:
    records = [_record("a", "answer", 0.9)]
    point = evaluate_refusal_threshold(records, threshold=0.1)
    assert point.refuse_denominator == 0
    assert point.refusal_accuracy is None
    assert point.balanced_accuracy is None


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_top_score_rejected(bad: float) -> None:
    with pytest.raises(ValidationError):
        _record("a", "answer", bad)


def test_negative_candidate_count_rejected() -> None:
    with pytest.raises(ValidationError):
        _record("a", "answer", 0.5, candidate_count=-1)


def test_zero_candidates_with_score_rejected() -> None:
    with pytest.raises(ValidationError):
        _record("a", "answer", 0.5, candidate_count=0)


def test_candidates_without_top_score_rejected() -> None:
    with pytest.raises(ValidationError):
        _record("a", "answer", None, candidate_count=1)


def test_duplicate_id_rejected() -> None:
    records = [_record("dup", "answer", 0.5), _record("dup", "refuse", None, 0)]
    with pytest.raises(CalibrationInputError):
        threshold_grid(records)
    with pytest.raises(CalibrationInputError):
        evaluate_refusal_threshold(records, threshold=0.1)


def test_threshold_grid_includes_boundaries_in_order() -> None:
    records = [_record("a", "answer", 0.2), _record("b", "answer", 0.8)]
    grid = threshold_grid(records)
    assert len(grid) == 4
    assert grid[0] < grid[1] == 0.2 < grid[2] == 0.8 < grid[3]
    assert list(grid) == sorted(grid)


def test_empty_grid_without_scores() -> None:
    records = [_record("r", "refuse", None, candidate_count=0)]
    assert threshold_grid(records) == ()
    assert scan_refusal_thresholds(records) == ()


def test_scan_is_ascending_and_select_is_deterministic() -> None:
    records = [
        _record("a1", "answer", 0.2),
        _record("a2", "answer", 0.6),
        _record("r1", "refuse", 0.0),
        _record("r2", "refuse", 0.1),
    ]
    points = scan_refusal_thresholds(records)
    thresholds = [point.threshold for point in points]
    assert thresholds == sorted(thresholds)
    chosen = select_dev_threshold(points)
    assert chosen is not None
    # 平衡准确率最高且平局取最低阈值
    assert chosen.balanced_accuracy == max(
        point.balanced_accuracy for point in points if point.balanced_accuracy is not None
    )
    first_best = next(
        point
        for point in points
        if point.balanced_accuracy == chosen.balanced_accuracy
    )
    assert chosen.threshold == first_best.threshold


def test_select_returns_none_without_usable_points() -> None:
    records = [_record("a", "answer", 0.9)]
    assert select_dev_threshold(scan_refusal_thresholds(records)) is None


def test_non_finite_threshold_rejected() -> None:
    with pytest.raises(CalibrationInputError):
        evaluate_refusal_threshold([], threshold=math.inf)
