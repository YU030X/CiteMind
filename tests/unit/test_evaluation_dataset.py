"""开发评估题集与确定性指标的聚焦单测（不联网、不读 .env、不调用模型）。

验证题集 schema、数量、分类、来源存在、gold 引用匹配、无权限/删除题集不泄漏，以及基于
**合成结果文件**的指标计算逻辑。合成结果只用于验证计算分支，不代表任何真实质量评分。
"""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError
from rag_backend.evaluation.__main__ import main
from rag_backend.evaluation.dataset import (
    CorpusManifest,
    DatasetValidationError,
    EvaluationDataset,
    EvaluationQuestion,
    load_dataset_bundle,
    validate_dataset,
)
from rag_backend.evaluation.metrics import (
    EvaluationResults,
    MetricsInputError,
    QuestionResult,
    ResultCitation,
    compute_metrics,
)

_EVALUATION_DIR = Path(__file__).resolve().parents[1] / "evaluation"
_DATASET_PATH = _EVALUATION_DIR / "dev-questions.json"
_CORPUS_DIR = _EVALUATION_DIR / "corpus"
_PDF_PATH = _CORPUS_DIR / "cafeteria.pdf"
_PDF_BUILDER_PATH = _EVALUATION_DIR / "tools" / "build_corpus.py"
# 生成器 build_corpus.py 的确定性输出；同时钉住仓库内 PDF 样本的字节。
_PDF_SHA256 = "05efca15a7919d1b706eb5ed434879e0287871762a37c54765e8e96e45f47c01"
_EXPECTED_CATEGORY_COUNTS = {
    "single_document": 24,
    "cross_document": 8,
    "unanswerable": 4,
    "no_permission": 4,
}
_ANSWER_QUESTIONS = 32
_REFUSE_QUESTIONS = 8


def _bundle() -> tuple[EvaluationDataset, CorpusManifest, Path]:
    return load_dataset_bundle(_DATASET_PATH)


def _copy_bundle(tmp_path: Path) -> Path:
    """把题集与整个语料目录复制到临时目录，便于注入损坏的语料。"""

    shutil.copy(_DATASET_PATH, tmp_path / "dev-questions.json")
    shutil.copytree(_CORPUS_DIR, tmp_path / "corpus")
    return tmp_path / "dev-questions.json"


def _mutate_question(original: EvaluationQuestion, **overrides: object) -> EvaluationQuestion:
    data = original.model_dump(by_alias=True)
    data.update(overrides)
    return EvaluationQuestion.model_validate(data)


def _dataset_with_question(question: EvaluationQuestion) -> EvaluationDataset:
    dataset, _, _ = _bundle()
    data = dataset.model_dump(by_alias=True)
    data["questions"] = [
        (question if item.id == question.id else item).model_dump(by_alias=True)
        for item in dataset.questions
    ]
    return EvaluationDataset.model_validate(data)


# ---------------------------------------------------------------------------
# 题集结构、分类与来源
# ---------------------------------------------------------------------------


def test_dev_dataset_validates_with_expected_distribution() -> None:
    dataset, manifest, corpus_dir = _bundle()

    report = validate_dataset(dataset, manifest, corpus_dir)

    assert dataset.dataset_kind == "dev"
    assert report.dataset_version == "citemind-eval-dev-2"
    assert report.total == 40 == len(dataset.questions)
    assert report.category_counts == _EXPECTED_CATEGORY_COUNTS


def test_scenario_tags_cover_required_behaviors() -> None:
    dataset, _, _ = _bundle()

    for tag in ("version_update", "deletion", "multi_turn", "pdf_page"):
        assert any(tag in question.tags for question in dataset.questions), tag

    multi_turn = [question for question in dataset.questions if "multi_turn" in question.tags]
    assert multi_turn
    for question in multi_turn:
        assert question.history
        assert question.standalone_question
        assert question.standalone_question != question.question

    cross = [question for question in dataset.questions if question.category == "cross_document"]
    assert cross
    for question in cross:
        distinct = {(span.kb_id, span.document_id) for span in question.gold_source_spans}
        assert len(distinct) >= 2


def test_every_question_id_is_unique() -> None:
    dataset, _, _ = _bundle()
    ids = [question.id for question in dataset.questions]
    assert len(ids) == len(set(ids))


def test_every_referenced_corpus_file_exists() -> None:
    _, manifest, corpus_dir = _bundle()
    for knowledge_base in manifest.knowledge_bases.values():
        for document in knowledge_base.documents.values():
            for entry in document.versions:
                assert (corpus_dir / entry.file).is_file(), entry.file


def test_gold_spans_bind_source_version_and_are_verified_against_active() -> None:
    dataset, manifest, _ = _bundle()
    for question in dataset.questions:
        for span in question.gold_source_spans:
            entry = manifest.resolve(span.kb_id, span.document_id, span.version)
            assert entry.status == "active"
            assert span.locator.parser_version == entry.parser_version
        for span in question.distractors:
            entry = manifest.resolve(span.kb_id, span.document_id, span.version)
            assert entry.status == "superseded"


def test_pdf_sample_is_reproducible_by_builder() -> None:
    assert hashlib.sha256(_PDF_PATH.read_bytes()).hexdigest() == _PDF_SHA256

    spec = importlib.util.spec_from_file_location("evaluation_build_corpus", _PDF_BUILDER_PATH)
    assert spec is not None and spec.loader is not None
    module = cast(Any, importlib.util.module_from_spec(spec))
    spec.loader.exec_module(module)
    assert hashlib.sha256(module.build_pdf()).hexdigest() == _PDF_SHA256


# ---------------------------------------------------------------------------
# 负例：schema、来源、gold 匹配与不泄漏
# ---------------------------------------------------------------------------


def test_gold_quote_outside_declared_span_is_rejected() -> None:
    dataset, manifest, corpus_dir = _bundle()
    original = next(q for q in dataset.questions if q.id == "dev-single-001")
    span = original.gold_source_spans[0].model_copy(
        update={"quote": "这句原文不存在于任何样本文件。"}
    )
    broken = _mutate_question(original, goldSourceSpans=[span.model_dump(by_alias=True)])

    with pytest.raises(DatasetValidationError):
        validate_dataset(_dataset_with_question(broken), manifest, corpus_dir)


def test_gold_quote_with_wrong_line_range_is_rejected() -> None:
    dataset, manifest, corpus_dir = _bundle()
    original = next(q for q in dataset.questions if q.id == "dev-single-001")
    locator = original.gold_source_spans[0].locator.model_copy(
        update={"start_line": 999, "end_line": 999}
    )
    span = original.gold_source_spans[0].model_copy(update={"locator": locator})
    broken = _mutate_question(original, goldSourceSpans=[span.model_dump(by_alias=True)])

    with pytest.raises(DatasetValidationError):
        validate_dataset(_dataset_with_question(broken), manifest, corpus_dir)


def test_no_permission_question_leaking_forbidden_line_is_rejected() -> None:
    dataset, manifest, corpus_dir = _bundle()
    original = next(q for q in dataset.questions if q.id == "dev-no-permission-001")
    broken = _mutate_question(original, question="职级P5的年度薪酬带宽为25万元至35万元。")

    with pytest.raises(DatasetValidationError):
        validate_dataset(_dataset_with_question(broken), manifest, corpus_dir)


def test_no_permission_question_with_accessible_role_is_rejected() -> None:
    dataset, manifest, corpus_dir = _bundle()
    original = next(q for q in dataset.questions if q.id == "dev-no-permission-001")
    scope = original.scope.model_copy(update={"role": "hr"})
    broken = _mutate_question(original, scope=scope.model_dump(by_alias=True))

    with pytest.raises(DatasetValidationError):
        validate_dataset(_dataset_with_question(broken), manifest, corpus_dir)


def test_no_permission_question_with_gold_span_fails_schema() -> None:
    dataset, _, _ = _bundle()
    original = next(q for q in dataset.questions if q.id == "dev-no-permission-001")
    gold = next(q for q in dataset.questions if q.id == "dev-single-001").gold_source_spans[0]

    with pytest.raises(ValidationError):
        _mutate_question(original, goldSourceSpans=[gold.model_dump(by_alias=True)])


def test_deletion_question_requires_deleted_document() -> None:
    dataset, manifest, corpus_dir = _bundle()
    data = manifest.model_dump(by_alias=True)
    legacy = data["knowledgeBases"]["kb-handbook"]["documents"]["legacy-bonus"]
    legacy["currentVersion"] = 1
    legacy["versions"][0]["status"] = "active"
    broken_manifest = CorpusManifest.model_validate(data)

    with pytest.raises(DatasetValidationError):
        validate_dataset(dataset, broken_manifest, corpus_dir)


def test_deletion_document_without_deleted_status_is_rejected() -> None:
    dataset, manifest, corpus_dir = _bundle()
    data = manifest.model_dump(by_alias=True)
    versions = data["knowledgeBases"]["kb-handbook"]["documents"]["legacy-bonus"]["versions"]
    versions[0]["status"] = "superseded"
    broken_manifest = CorpusManifest.model_validate(data)

    with pytest.raises(DatasetValidationError):
        validate_dataset(dataset, broken_manifest, corpus_dir)


def test_gold_span_outside_question_scope_is_rejected() -> None:
    dataset, manifest, corpus_dir = _bundle()
    original = next(q for q in dataset.questions if q.id == "dev-single-001")
    scope = original.scope.model_copy(update={"role": "hr", "kb_ids": ["kb-restricted"]})
    broken = _mutate_question(original, scope=scope.model_dump(by_alias=True))

    with pytest.raises(DatasetValidationError):
        validate_dataset(_dataset_with_question(broken), manifest, corpus_dir)


def test_distractor_outside_question_scope_is_rejected() -> None:
    dataset, manifest, corpus_dir = _bundle()
    original = next(q for q in dataset.questions if q.id == "dev-unanswerable-001")
    distractor = next(q for q in dataset.questions if q.id == "dev-single-001").distractors[0]
    scope = original.scope.model_copy(update={"role": "hr", "kb_ids": ["kb-restricted"]})
    broken = _mutate_question(
        original,
        scope=scope.model_dump(by_alias=True),
        distractors=[distractor.model_dump(by_alias=True)],
    )

    with pytest.raises(DatasetValidationError):
        validate_dataset(_dataset_with_question(broken), manifest, corpus_dir)


def test_answer_question_referencing_missing_file_is_rejected() -> None:
    dataset, manifest, corpus_dir = _bundle()
    data = manifest.model_dump(by_alias=True)
    versions = data["knowledgeBases"]["kb-handbook"]["documents"]["handbook"]["versions"]
    versions[1]["file"] = "handbook-missing.md"
    broken_manifest = CorpusManifest.model_validate(data)

    with pytest.raises(DatasetValidationError):
        validate_dataset(dataset, broken_manifest, corpus_dir)


def test_corpus_read_failure_is_reported_as_static_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset, manifest, corpus_dir = _bundle()
    real_read_bytes = Path.read_bytes

    def failing_read_bytes(self: Path) -> bytes:
        if self.name == "travel.md":
            raise OSError("simulated read failure")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", failing_read_bytes)
    with pytest.raises(DatasetValidationError):
        validate_dataset(dataset, manifest, corpus_dir)


def test_invalid_pdf_is_reported_as_static_validation_error(tmp_path: Path) -> None:
    dataset_path = _copy_bundle(tmp_path)
    (tmp_path / "corpus" / "cafeteria.pdf").write_bytes(b"%PDF- not a real pdf")
    dataset, manifest, corpus_dir = load_dataset_bundle(dataset_path)

    with pytest.raises(DatasetValidationError):
        validate_dataset(dataset, manifest, corpus_dir)


def test_invalid_utf8_markdown_is_reported_as_static_validation_error(tmp_path: Path) -> None:
    dataset_path = _copy_bundle(tmp_path)
    (tmp_path / "corpus" / "travel.md").write_bytes(b"\xff\xfe\x00 not utf-8")
    dataset, manifest, corpus_dir = load_dataset_bundle(dataset_path)

    with pytest.raises(DatasetValidationError):
        validate_dataset(dataset, manifest, corpus_dir)


def test_cli_reports_corpus_failure_without_traceback(tmp_path: Path) -> None:
    dataset_path = _copy_bundle(tmp_path)
    (tmp_path / "corpus" / "cafeteria.pdf").write_bytes(b"%PDF- not a real pdf")

    assert main(["--dataset", str(dataset_path)]) == 1


def test_dataset_with_fewer_than_minimum_questions_is_rejected() -> None:
    dataset, manifest, corpus_dir = _bundle()
    data = dataset.model_dump(by_alias=True)
    data["questions"] = data["questions"][:10]
    small = EvaluationDataset.model_validate(data)

    with pytest.raises(DatasetValidationError):
        validate_dataset(small, manifest, corpus_dir)


def test_duplicate_question_ids_are_rejected() -> None:
    dataset, manifest, corpus_dir = _bundle()
    data = dataset.model_dump(by_alias=True)
    data["questions"].append(data["questions"][0])
    duplicated = EvaluationDataset.model_validate(data)

    with pytest.raises(DatasetValidationError):
        validate_dataset(duplicated, manifest, corpus_dir)


# ---------------------------------------------------------------------------
# 确定性指标（合成结果只验证计算逻辑）
# ---------------------------------------------------------------------------


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
                    question_id=question.id,
                    behavior="answer",
                    citations=citations,
                )
            )
        else:
            results.append(QuestionResult(question_id=question.id, behavior="refuse"))
    return EvaluationResults(results=results)


def test_metrics_on_perfect_synthetic_results() -> None:
    dataset, manifest, corpus_dir = _bundle()
    metrics = compute_metrics(dataset, manifest, corpus_dir, _perfect_results(dataset))

    assert metrics.expected_answer == _ANSWER_QUESTIONS
    assert metrics.expected_refuse == _REFUSE_QUESTIONS
    assert metrics.refusal_accuracy == 1.0
    assert metrics.false_refusal_rate == 0.0
    assert metrics.citation_source_validity == 1.0
    assert metrics.gold_source_coverage == 1.0
    assert metrics.permission_leak_count == 0


def test_metrics_count_false_refusal_and_permission_leak() -> None:
    dataset, manifest, corpus_dir = _bundle()
    degraded: list[QuestionResult] = []
    for result in _perfect_results(dataset).results:
        if result.question_id == "dev-unanswerable-003":
            degraded.append(
                result.model_copy(
                    update={
                        "behavior": "answer",
                        "answer_text": "旧办法按当年基本工资的2倍发放年终奖。",
                    }
                )
            )
        elif result.question_id == "dev-single-003":
            degraded.append(result.model_copy(update={"behavior": "refuse", "citations": []}))
        else:
            degraded.append(result)

    metrics = compute_metrics(
        dataset, manifest, corpus_dir, EvaluationResults(results=degraded)
    )

    assert metrics.permission_leak_count == 1
    assert metrics.refusal_accuracy == pytest.approx(7 / 8)
    assert metrics.false_refusal_rate == pytest.approx(1 / 32)
    assert metrics.gold_source_coverage == pytest.approx(31 / 32)
    assert metrics.citation_source_validity == 1.0


def test_metrics_reject_incomplete_results() -> None:
    dataset, manifest, corpus_dir = _bundle()
    results = _perfect_results(dataset)

    with pytest.raises(MetricsInputError):
        compute_metrics(
            dataset,
            manifest,
            corpus_dir,
            EvaluationResults(results=results.results[:-1]),
        )


# ---------------------------------------------------------------------------
# 离线入口
# ---------------------------------------------------------------------------


def test_cli_validates_default_dataset() -> None:
    assert main(["--dataset", str(_DATASET_PATH)]) == 0


def test_cli_reports_metrics_with_results_file(tmp_path: Path) -> None:
    dataset, _, _ = _bundle()
    results_path = tmp_path / "results.json"
    results_path.write_text(
        _perfect_results(dataset).model_dump_json(by_alias=True, indent=2), encoding="utf-8"
    )

    assert main(["--dataset", str(_DATASET_PATH), "--results", str(results_path)]) == 0


def test_cli_fails_on_missing_dataset(tmp_path: Path) -> None:
    assert main(["--dataset", str(tmp_path / "missing.json")]) == 1
