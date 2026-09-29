"""固定 Recall@10 与 nDCG@10 的纯离线计算（不检索、不联网、不调用模型）。

输入是每题已记录好的 gold 来源 span 与候选 chunk 列表；本模块只做确定性数学，不产生任何
“模拟得分”。相关性与增益定义固定如下：

- **候选相关**：候选的 (KB, 文档, 版本, 解析器版本, 来源类型) 与某个 gold span 一致，且 locator
  相交。Markdown 用 1-based 行闭区间相交（``max(start) <= min(end)``）；PDF 用 1-based 页号相等。
  解析器版本或来源类型不一致一律不相关。
- **每个 gold span 最多贡献一次**：同一 span 被多个 chunk 命中只算覆盖一次，nDCG 也只在前一个
  更高 rank 的候选已经消耗该 span 后不再给增益。cross-document 覆盖按 gold span 计数。
- **二值增益**：候选若覆盖了至少一个尚未被更高 rank 候选覆盖的 gold span，则 ``rel_i = 1``，
  否则为 0；``DCG@10 = Σ rel_i / log2(i + 1)``（i 为 1-based rank），
  ``IDCG@10 = Σ_{i=1..min(gold span 数, 10)} 1 / log2(i + 1)``。
- **拒答题不进入排序分母**：没有 gold span 的题（拒答题）Recall/nDCG 为 ``None``，只留给
  ``calibration`` 处理；聚合时也不计入宏平均。

本定义只覆盖离线数学；它不等于句子级引用支持率，也不代表真实检索质量。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from rag_backend.evaluation.dataset import GoldLocator, GoldSpan

TOP_K = 10


class RankingInputError(Exception):
    """排序输入不满足契约时抛出（重复题目 id 等）。"""


class _Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class RankingCandidate(_Model):
    """一个候选 chunk；``rank`` 是最终名次，其余 rank/score 为各路检索事实。"""

    candidate_id: str = Field(min_length=1)
    kb_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    locator: GoldLocator
    rank: int = Field(ge=1)
    vector_rank: int | None = Field(default=None, ge=1)
    vector_score: float | None = None
    keyword_rank: int | None = Field(default=None, ge=1)
    keyword_score: float | None = None
    fusion_rank: int | None = Field(default=None, ge=1)
    fusion_score: float | None = None
    rerank_score: float | None = None

    @model_validator(mode="after")
    def _check_scores(self) -> RankingCandidate:
        for name in ("vector_score", "keyword_score", "fusion_score", "rerank_score"):
            value = getattr(self, name)
            if value is not None and not math.isfinite(value):
                raise ValueError(f"{name} 必须是有限数")
        return self


class RankingQuestion(_Model):
    """一道题的 gold span 与已记录候选；无 gold 即拒答题。"""

    question_id: str = Field(min_length=1)
    gold_spans: list[GoldSpan] = Field(default_factory=list)
    candidates: list[RankingCandidate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_candidates(self) -> RankingQuestion:
        ids = [candidate.candidate_id for candidate in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError(f"[{self.question_id}] candidateId 不能重复")
        ranks = [candidate.rank for candidate in self.candidates]
        if len(ranks) != len(set(ranks)):
            raise ValueError(f"[{self.question_id}] rank 不能重复")
        return self


@dataclass(frozen=True)
class QuestionRanking:
    """单题排序结果；无 gold 时两个比率都为 ``None``。"""

    question_id: str
    gold_span_count: int
    covered_gold_span_count: int
    recall_at_10: float | None
    dcg_at_10: float
    idcg_at_10: float
    ndcg_at_10: float | None


@dataclass(frozen=True)
class RankingReport:
    """按题宏平均的固定 @10 指标，附微观测分子/分母与逐题失败 id。"""

    question_count: int
    excluded_question_ids: tuple[str, ...]
    recall_at_10: float | None
    recall_covered_numerator: int
    recall_gold_denominator: int
    ndcg_at_10: float | None
    ndcg_dcg_numerator: float
    ndcg_idcg_denominator: float
    failed_question_ids: tuple[str, ...]
    questions: tuple[QuestionRanking, ...]


def _locator_overlaps(left: GoldLocator, right: GoldLocator) -> bool:
    if left.source_type != right.source_type or left.parser_version != right.parser_version:
        return False
    if left.source_type == "markdown":
        assert left.start_line is not None and left.end_line is not None
        assert right.start_line is not None and right.end_line is not None
        return not (left.end_line < right.start_line or right.end_line < left.start_line)
    assert left.page is not None and right.page is not None
    return left.page == right.page


def candidate_covers_span(candidate: RankingCandidate, span: GoldSpan) -> bool:
    """候选来源版本与 locator 都命中该 gold span 才算相关。"""

    if (candidate.kb_id, candidate.document_id, candidate.version) != (
        span.kb_id,
        span.document_id,
        span.version,
    ):
        return False
    return _locator_overlaps(candidate.locator, span.locator)


def score_question(question: RankingQuestion) -> QuestionRanking:
    """按固定 @10 定义给单题打分；无 gold 返回全 ``None`` 比率。"""

    gold_spans = question.gold_spans
    top = sorted(question.candidates, key=lambda item: (item.rank, item.candidate_id))[:TOP_K]
    if not gold_spans:
        return QuestionRanking(
            question_id=question.question_id,
            gold_span_count=0,
            covered_gold_span_count=0,
            recall_at_10=None,
            dcg_at_10=0.0,
            idcg_at_10=0.0,
            ndcg_at_10=None,
        )

    covered: set[int] = set()
    dcg = 0.0
    for position, candidate in enumerate(top, start=1):
        newly_covered = {
            index
            for index, span in enumerate(gold_spans)
            if index not in covered and candidate_covers_span(candidate, span)
        }
        covered.update(newly_covered)
        # nDCG 使用候选级二值增益；单个 chunk 即使覆盖多个 span，本位置也只贡献 1。
        gain = 1.0 if newly_covered else 0.0
        dcg += gain / math.log2(position + 1)

    idcg = sum(
        1.0 / math.log2(position + 1) for position in range(1, min(len(gold_spans), TOP_K) + 1)
    )
    recall = len(covered) / len(gold_spans)
    ndcg = dcg / idcg if idcg > 0 else None
    return QuestionRanking(
        question_id=question.question_id,
        gold_span_count=len(gold_spans),
        covered_gold_span_count=len(covered),
        recall_at_10=recall,
        dcg_at_10=dcg,
        idcg_at_10=idcg,
        ndcg_at_10=ndcg,
    )


def aggregate_ranking_metrics(questions: Sequence[RankingQuestion]) -> RankingReport:
    """按题聚合 Recall@10/nDCG@10；拒答题排除在分母外并单独列出。"""

    ids = [question.question_id for question in questions]
    duplicates = sorted({item for item in ids if ids.count(item) > 1})
    if duplicates:
        raise RankingInputError(f"重复的题目 id：{', '.join(duplicates)}")

    scored = [score_question(question) for question in questions]
    ranked = [item for item in scored if item.gold_span_count > 0]
    excluded = tuple(sorted(item.question_id for item in scored if item.gold_span_count == 0))

    recall_at_10 = _mean([item.recall_at_10 for item in ranked])
    ndcg_at_10 = _mean([item.ndcg_at_10 for item in ranked])
    failed = tuple(
        sorted(
            item.question_id
            for item in ranked
            if (item.recall_at_10 is not None and item.recall_at_10 < 1.0)
            or (item.ndcg_at_10 is not None and item.ndcg_at_10 < 1.0)
        )
    )
    return RankingReport(
        question_count=len(ranked),
        excluded_question_ids=excluded,
        recall_at_10=recall_at_10,
        recall_covered_numerator=sum(item.covered_gold_span_count for item in ranked),
        recall_gold_denominator=sum(item.gold_span_count for item in ranked),
        ndcg_at_10=ndcg_at_10,
        ndcg_dcg_numerator=sum(item.dcg_at_10 for item in ranked),
        ndcg_idcg_denominator=sum(item.idcg_at_10 for item in ranked),
        failed_question_ids=failed,
        questions=tuple(scored),
    )


def _mean(values: Sequence[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present) / len(present)
