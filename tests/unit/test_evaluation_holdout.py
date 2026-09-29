"""Phase 3 固定留出集与跨集、冲突/注入字段、新旧指标的聚焦单测（纯离线）。

验证：留出集 60 题与分类矩阵、场景/角色覆盖、跨集 id 唯一与近重复拒绝、
conflictingSpans / injectionCanary 正负例、旧归档 5 指标回归、新冲突/注入指标合成结果。
所有结果均为合成或归档数据，不代表真实模型质量。
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError
from rag_backend.evaluation.dataset import (
    CorpusManifest,
    DatasetValidationError,
    EvaluationDataset,
    EvaluationQuestion,
    load_dataset_bundle,
    validate_dataset,
    validate_dataset_pair,
)
from rag_backend.evaluation.metrics import (
    EvaluationResults,
    MetricsInputError,
    QuestionResult,
    ResultCitation,
    assess_questions,
    compute_metrics,
)

_EVALUATION_DIR = Path(__file__).resolve().parents[1] / "evaluation"
_DEV_PATH = _EVALUATION_DIR / "dev-questions.json"
_HOLDOUT_PATH = _EVALUATION_DIR / "holdout-questions.json"
_ARCHIVE_RESULTS_PATH = _EVALUATION_DIR / "results" / "2026-09-28" / "results.json"


def _dev_bundle() -> tuple[EvaluationDataset, CorpusManifest, Path]:
    return load_dataset_bundle(_DEV_PATH)


def _holdout_bundle() -> tuple[EvaluationDataset, CorpusManifest, Path]:
    return load_dataset_bundle(_HOLDOUT_PATH)


def _mutate_question(original: EvaluationQuestion, **overrides: object) -> EvaluationQuestion:
    data = original.model_dump(by_alias=True)
    data.update(overrides)
    return EvaluationQuestion.model_validate(data)


def _holdout_with_question(
    question: EvaluationQuestion, original_id: str | None = None
) -> EvaluationDataset:
    dataset, _, _ = _holdout_bundle()
    target_id = original_id or question.id
    data = dataset.model_dump(by_alias=True)
    data["questions"] = [
        (question if item.id == target_id else item).model_dump(by_alias=True)
        for item in dataset.questions
    ]
    return EvaluationDataset.model_validate(data)


def _perfect_results(dataset: EvaluationDataset) -> EvaluationResults:
    results: list[QuestionResult] = []
    for question in dataset.questions:
        if question.expected_behavior == "answer":
            citations = [
                ResultCitation(
                    kb_id=span.kb_id,
                    document_id=span.document_id,
                    version=span.version,
                )
                for span in question.gold_source_spans
            ]
            results.append(
                QuestionResult(
                    question_id=question.id, behavior="answer", citations=citations
                )
            )
        else:
            results.append(QuestionResult(question_id=question.id, behavior="refuse"))
    return EvaluationResults(results=results)


# ---------------------------------------------------------------------------
# 60 题契约与跨集校验


def test_holdout_dataset_validates_with_expected_distribution() -> None:
    dataset, manifest, corpus_dir = _holdout_bundle()

    report = validate_dataset(dataset, manifest, corpus_dir)

    assert dataset.dataset_kind == "holdout"
    assert report.total == 60 == len(dataset.questions)
    assert report.category_counts == {
        "single_document": 26,
        "cross_document": 12,
        "unanswerable": 11,
        "no_permission": 11,
    }


def test_holdout_covers_required_scenarios_and_roles() -> None:
    dataset, _, _ = _holdout_bundle()
    tag_counts = Counter(tag for question in dataset.questions for tag in question.tags)

    assert tag_counts["multi_turn"] >= 8
    assert tag_counts["version_update"] >= 7
    assert tag_counts["deletion"] >= 6
    assert tag_counts["pdf_page"] >= 6
    assert tag_counts["evidence_conflict"] >= 5
    assert tag_counts["prompt_injection"] >= 4
    assert tag_counts["permission"] == 11
    assert tag_counts["insufficient_evidence"] == 11
    assert sum(1 for question in dataset.questions if question.scope.role == "hr") >= 4

    for question in dataset.questions:
        if question.category == "no_permission":
            assert "permission" in question.tags
        if question.category == "unanswerable":
            assert "insufficient_evidence" in question.tags
        if "multi_turn" in question.tags:
            assert question.history
            assert question.standalone_question


def test_validate_dataset_pair_accepts_fixed_dev_and_holdout() -> None:
    dev, _, _ = _dev_bundle()
    holdout, manifest, corpus_dir = _holdout_bundle()

    report = validate_dataset_pair(dev, holdout, manifest, corpus_dir)

    assert report.total == 100
    assert report.combined_category_counts == {
        "single_document": 50,
        "cross_document": 20,
        "unanswerable": 15,
        "no_permission": 15,
    }


def test_validate_dataset_pair_rejects_different_manifests() -> None:
    dev, manifest, corpus_dir = _dev_bundle()
    holdout, _, _ = _holdout_bundle()
    mismatched = holdout.model_copy(update={"corpus_manifest": "corpus/other.json"})

    with pytest.raises(DatasetValidationError, match="同一个 corpusManifest"):
        validate_dataset_pair(dev, mismatched, manifest, corpus_dir)


def test_validate_dataset_pair_rejects_id_overlap() -> None:
    dev, manifest, corpus_dir = _dev_bundle()
    holdout, _, _ = _holdout_bundle()
    original = next(q for q in holdout.questions if q.id == "holdout-single-001")
    renamed = _mutate_question(original, id="dev-single-001")

    with pytest.raises(DatasetValidationError):
        validate_dataset_pair(
            dev, _holdout_with_question(renamed, "holdout-single-001"), manifest, corpus_dir
        )


def test_validate_dataset_pair_rejects_identical_question_text() -> None:
    dev, manifest, corpus_dir = _dev_bundle()
    holdout, _, _ = _holdout_bundle()
    dev_text = next(q for q in dev.questions if q.id == "dev-single-001").question
    original = next(q for q in holdout.questions if q.id == "holdout-single-001")
    duplicated = _mutate_question(original, question=dev_text)

    with pytest.raises(DatasetValidationError):
        validate_dataset_pair(dev, _holdout_with_question(duplicated), manifest, corpus_dir)


def test_validate_dataset_pair_rejects_edit_distance_one() -> None:
    dev, manifest, corpus_dir = _dev_bundle()
    holdout, _, _ = _holdout_bundle()
    dev_text = next(q for q in dev.questions if q.id == "dev-single-001").question
    original = next(q for q in holdout.questions if q.id == "holdout-single-001")
    one_edit = _mutate_question(original, question=dev_text[:-1])

    with pytest.raises(DatasetValidationError):
        validate_dataset_pair(dev, _holdout_with_question(one_edit), manifest, corpus_dir)


# ---------------------------------------------------------------------------
# conflictingSpans / injectionCanary 正负例


def test_conflicting_span_must_use_distinct_source_version() -> None:
    dataset, manifest, corpus_dir = _holdout_bundle()
    original = next(q for q in dataset.questions if q.id == "holdout-single-009")
    gold = original.gold_source_spans[0]
    conflict = original.conflicting_spans[0].model_copy(
        update={"kb_id": gold.kb_id, "document_id": gold.document_id, "version": gold.version}
    )
    mutated = _mutate_question(
        original,
        conflictingSpans=[conflict.model_dump(by_alias=True)],
    )

    with pytest.raises(DatasetValidationError, match="不同文档版本"):
        validate_dataset(
            _holdout_with_question(mutated), manifest, corpus_dir
        )


def test_evidence_conflict_without_conflicting_spans_is_rejected() -> None:
    dataset, _, _ = _holdout_bundle()
    original = next(q for q in dataset.questions if q.id == "holdout-single-009")

    with pytest.raises(ValidationError):
        _mutate_question(original, conflictingSpans=[])


def test_conflicting_span_duplicating_gold_is_rejected() -> None:
    dataset, manifest, corpus_dir = _holdout_bundle()
    original = next(q for q in dataset.questions if q.id == "holdout-single-009")
    broken = _mutate_question(
        original, conflictingSpans=[original.gold_source_spans[0].model_dump(by_alias=True)]
    )

    with pytest.raises(DatasetValidationError):
        validate_dataset(_holdout_with_question(broken), manifest, corpus_dir)


def test_prompt_injection_without_canary_is_rejected() -> None:
    dataset, _, _ = _holdout_bundle()
    original = next(q for q in dataset.questions if q.id == "holdout-single-005")

    with pytest.raises(ValidationError):
        _mutate_question(original, injectionCanary=None)


def test_canary_absent_from_corpus_is_rejected() -> None:
    dataset, manifest, corpus_dir = _holdout_bundle()
    original = next(q for q in dataset.questions if q.id == "holdout-single-005")
    broken = _mutate_question(original, injectionCanary="CANARY-NOT-IN-CORPUS")

    with pytest.raises(DatasetValidationError):
        validate_dataset(_holdout_with_question(broken), manifest, corpus_dir)


def test_non_injection_question_cannot_carry_canary() -> None:
    dataset, _, _ = _holdout_bundle()
    original = next(q for q in dataset.questions if q.id == "holdout-single-002")

    with pytest.raises(ValidationError):
        _mutate_question(original, injectionCanary="CANARY-7F3A9D2B")


# ---------------------------------------------------------------------------
# 旧归档 5 指标回归与新指标


def test_archived_dev_results_recompute_original_metrics() -> None:
    dataset, manifest, corpus_dir = _dev_bundle()
    results = EvaluationResults.model_validate_json(
        _ARCHIVE_RESULTS_PATH.read_text(encoding="utf-8")
    )

    metrics = compute_metrics(dataset, manifest, corpus_dir, results)

    assert metrics.refusal_accuracy == 1.0
    assert metrics.false_refusal_rate == 0.03125
    assert metrics.citation_source_validity == 1.0
    assert metrics.gold_source_coverage == 0.9375
    assert metrics.permission_leak_count == 0
    assert metrics.conflict_resolution_rate is None
    assert metrics.injection_leak_count == 0
    assert metrics.injection_resistance_rate is None


def test_old_results_without_metadata_still_parse() -> None:
    results = EvaluationResults.model_validate_json(
        _ARCHIVE_RESULTS_PATH.read_text(encoding="utf-8")
    )

    assert results.dataset_kind is None
    assert results.dataset_version is None


def test_results_metadata_mismatch_is_rejected() -> None:
    dataset, manifest, corpus_dir = _dev_bundle()
    results = _perfect_results(dataset)
    mismatched = EvaluationResults(
        dataset_kind="holdout",
        dataset_version="citemind-eval-holdout-1",
        results=results.results,
    )

    with pytest.raises(MetricsInputError):
        compute_metrics(dataset, manifest, corpus_dir, mismatched)


def test_new_metrics_on_synthetic_holdout_results() -> None:
    dataset, manifest, corpus_dir = _holdout_bundle()
    results = EvaluationResults(
        dataset_kind="holdout",
        dataset_version="citemind-eval-holdout-1",
        results=_perfect_results(dataset).results,
    )

    metrics = compute_metrics(dataset, manifest, corpus_dir, results)

    assert metrics.conflict_resolution_rate == 1.0
    assert metrics.injection_leak_count == 0
    assert metrics.injection_resistance_rate == 1.0

    degraded: list[QuestionResult] = []
    for result in results.results:
        if result.question_id == "holdout-single-009":
            conflict_question = next(
                q for q in dataset.questions if q.id == "holdout-single-009"
            )
            conflicting = conflict_question.conflicting_spans[0]
            degraded.append(
                result.model_copy(
                    update={
                        "citations": [
                            ResultCitation(
                                kb_id=conflicting.kb_id,
                                document_id=conflicting.document_id,
                                version=conflicting.version,
                            )
                        ]
                    }
                )
            )
        elif result.question_id == "holdout-single-005":
            degraded.append(result.model_copy(update={"answer_text": "CANARY-7F3A9D2B"}))
        elif result.question_id == "holdout-single-006":
            degraded.append(
                result.model_copy(update={"behavior": "refuse", "citations": []})
            )
        else:
            degraded.append(result)

    degraded_metrics = compute_metrics(
        dataset,
        manifest,
        corpus_dir,
        EvaluationResults(
            dataset_kind="holdout",
            dataset_version="citemind-eval-holdout-1",
            results=degraded,
        ),
    )

    assert degraded_metrics.conflict_resolution_rate == pytest.approx(5 / 6)
    assert degraded_metrics.injection_leak_count == 1
    assert degraded_metrics.injection_resistance_rate == pytest.approx(2 / 4)


def test_assess_questions_reports_conflict_and_injection_reasons() -> None:
    dataset, manifest, corpus_dir = _holdout_bundle()
    degraded: list[QuestionResult] = []
    for result in _perfect_results(dataset).results:
        if result.question_id == "holdout-single-009":
            conflict_question = next(
                q for q in dataset.questions if q.id == "holdout-single-009"
            )
            conflicting = conflict_question.conflicting_spans[0]
            degraded.append(
                result.model_copy(
                    update={
                        "citations": [
                            ResultCitation(
                                kb_id=conflicting.kb_id,
                                document_id=conflicting.document_id,
                                version=conflicting.version,
                            )
                        ]
                    }
                )
            )
        elif result.question_id == "holdout-single-005":
            degraded.append(result.model_copy(update={"answer_text": "CANARY-7F3A9D2B"}))
        else:
            degraded.append(result)

    results = EvaluationResults(
        dataset_kind="holdout",
        dataset_version="citemind-eval-holdout-1",
        results=degraded,
    )
    assessments = {
        assessment.question_id: assessment
        for assessment in assess_questions(dataset, manifest, corpus_dir, results)
    }

    assert assessments["holdout-single-009"].failure_reasons == (
        "citation_outside_gold",
        "incomplete_gold_coverage",
        "conflict_unresolved",
    )
    assert assessments["holdout-single-005"].failure_reasons == (
        "injection_leak",
        "injection_unresisted",
    )
    # 合成结果只证明分支；正确拒答的无权限题不是失败。
    assert assessments["holdout-no-permission-001"].failure_reasons == ()
