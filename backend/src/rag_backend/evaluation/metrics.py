"""基于已有结果文件的确定性指标计算（不建评估平台、不建数据库、不用 LLM 裁判）。

输入是**真实运行产生的结果文件**：每道题的实际行为、返回的引用来源与回答正文。本模块不做
任何检索或生成，只对已发生的结果做确定性统计，因此它不会产生“模拟模型得分”。测试里用合成
结果文件只验证计算逻辑，不代表任何真实质量结论。

指标定义与 [评估与验收](../../../../docs/evaluation.md) 一致，取可离线复算的子集：

- ``refusalAccuracy``：应拒答题中正确拒答的比例。
- ``falseRefusalRate``：应作答题中被误拒的比例。
- ``citationSourceValidity``：已作答题返回的引用中，(KB, 文档, 版本) 命中本题 gold 的比例。
- ``goldSourceCoverage``：应作答题中，引用覆盖全部 gold (KB, 文档, 版本) 的比例。
- ``permissionLeakCount``：应拒答题的回答正文里出现不可访问文档原文的题数。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from rag_backend.evaluation.dataset import (
    CorpusManifest,
    EvaluationDataset,
    EvaluationQuestion,
    GoldSpan,
    kb_of_document,
    substantive_lines,
)


class MetricsInputError(Exception):
    """结果文件与题集不匹配时抛出。"""


class _Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class ResultCitation(_Model):
    """一次回答返回的引用来源标识；不包含正文，也不含评分。"""

    kb_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    version: int = Field(ge=1)


class QuestionResult(_Model):
    """单题实际结果：实际行为、引用来源与回答正文。"""

    question_id: str = Field(min_length=1)
    behavior: Literal["answer", "refuse"]
    citations: list[ResultCitation] = Field(default_factory=list)
    answer_text: str = ""


class EvaluationResults(_Model):
    results: list[QuestionResult] = Field(default_factory=list)


@dataclass(frozen=True)
class MetricsReport:
    """确定性指标；``None`` 表示该指标没有分母（例如没有应拒答题）。"""

    answered: int
    refused: int
    expected_answer: int
    expected_refuse: int
    refusal_accuracy: float | None
    false_refusal_rate: float | None
    citation_source_validity: float | None
    gold_source_coverage: float | None
    permission_leak_count: int


def _span_key(span: GoldSpan | ResultCitation) -> tuple[str, str, int]:
    return (span.kb_id, span.document_id, span.version)


def compute_metrics(
    dataset: EvaluationDataset,
    manifest: CorpusManifest,
    corpus_dir: Path,
    results: EvaluationResults,
) -> MetricsReport:
    """按固定分母计算确定性指标；结果必须恰好覆盖题集全部 id。"""

    by_id = {question.id: question for question in dataset.questions}
    _assert_results_cover_dataset(by_id, results)
    results_by_id = {result.question_id: result for result in results.results}

    expected_answer = [q for q in dataset.questions if q.expected_behavior == "answer"]
    expected_refuse = [q for q in dataset.questions if q.expected_behavior == "refuse"]
    answered = [r for r in results.results if r.behavior == "answer"]
    refused = [r for r in results.results if r.behavior == "refuse"]

    correct_refusals = sum(1 for r in refused if by_id[r.question_id].expected_behavior == "refuse")
    refusal_accuracy = _ratio(correct_refusals, len(expected_refuse))

    false_refusals = sum(1 for r in refused if by_id[r.question_id].expected_behavior == "answer")
    false_refusal_rate = _ratio(false_refusals, len(expected_answer))

    valid_citations = 0
    total_citations = 0
    for result in answered:
        gold_keys = {_span_key(span) for span in by_id[result.question_id].gold_source_spans}
        for citation in result.citations:
            total_citations += 1
            if _span_key(citation) in gold_keys:
                valid_citations += 1
    citation_source_validity = _ratio(valid_citations, total_citations)

    covered = 0
    for question in expected_answer:
        result = results_by_id[question.id]
        if result.behavior != "answer":
            continue
        gold_keys = {_span_key(span) for span in question.gold_source_spans}
        cited_keys = {_span_key(citation) for citation in result.citations}
        if gold_keys <= cited_keys:
            covered += 1
    gold_source_coverage = _ratio(covered, len(expected_answer))

    leak_count = sum(
        1
        for question in expected_refuse
        if _answer_leaks(question, manifest, corpus_dir, results_by_id[question.id].answer_text)
    )

    return MetricsReport(
        answered=len(answered),
        refused=len(refused),
        expected_answer=len(expected_answer),
        expected_refuse=len(expected_refuse),
        refusal_accuracy=refusal_accuracy,
        false_refusal_rate=false_refusal_rate,
        citation_source_validity=citation_source_validity,
        gold_source_coverage=gold_source_coverage,
        permission_leak_count=leak_count,
    )


def _assert_results_cover_dataset(
    by_id: dict[str, EvaluationQuestion], results: EvaluationResults
) -> None:
    seen: set[str] = set()
    for result in results.results:
        if result.question_id not in by_id:
            raise MetricsInputError(f"结果包含未知题目 id：{result.question_id}")
        if result.question_id in seen:
            raise MetricsInputError(f"结果重复包含题目 id：{result.question_id}")
        seen.add(result.question_id)
    missing = sorted(set(by_id) - seen)
    if missing:
        raise MetricsInputError(f"结果缺少题目 id：{', '.join(missing)}")


def _answer_leaks(
    question: EvaluationQuestion,
    manifest: CorpusManifest,
    corpus_dir: Path,
    answer_text: str,
) -> bool:
    if not answer_text:
        return False
    for document_id in question.unavailable_document_ids:
        kb_id = kb_of_document(manifest, document_id)
        document = manifest.version_entry(kb_id, document_id)
        for entry in document.versions:
            for line in substantive_lines(corpus_dir / entry.file):
                if line in answer_text:
                    return True
    return False


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator
