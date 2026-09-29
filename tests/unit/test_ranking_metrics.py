"""固定 Recall@10 / nDCG@10 纯函数聚焦单测（合成数据，不代表真实质量）。"""

from __future__ import annotations

import math

import pytest
from rag_backend.evaluation.dataset import GoldLocator, GoldSpan
from rag_backend.evaluation.ranking_metrics import (
    RankingCandidate,
    RankingInputError,
    RankingQuestion,
    aggregate_ranking_metrics,
    candidate_covers_span,
    score_question,
)

_MD_PARSER = "markdown-it-py-4.2.0-v1"
_PDF_PARSER = "pypdf-6.19.0+pdfplumber-0.11.10-v1"


def _md_locator(start: int, end: int) -> GoldLocator:
    return GoldLocator(
        source_type="markdown",
        parser_version=_MD_PARSER,
        heading_path=["H"],
        start_line=start,
        end_line=end,
    )


def _pdf_locator(page: int) -> GoldLocator:
    return GoldLocator(source_type="pdf", parser_version=_PDF_PARSER, page=page)


def _span(
    *,
    kb: str = "kb",
    doc: str = "doc",
    version: int = 1,
    locator: GoldLocator | None = None,
) -> GoldSpan:
    return GoldSpan(
        kb_id=kb,
        document_id=doc,
        version=version,
        quote="quote",
        locator=locator or _md_locator(1, 5),
    )


def _candidate(
    candidate_id: str,
    rank: int,
    locator: GoldLocator,
    *,
    kb: str = "kb",
    doc: str = "doc",
    version: int = 1,
) -> RankingCandidate:
    return RankingCandidate(
        candidate_id=candidate_id,
        kb_id=kb,
        document_id=doc,
        version=version,
        locator=locator,
        rank=rank,
        vector_rank=rank,
        vector_score=1.0,
    )


def test_markdown_overlap_is_relevant() -> None:
    span = _span(locator=_md_locator(10, 20))
    candidate = _candidate("c1", 1, _md_locator(15, 25))
    assert candidate_covers_span(candidate, span)
    question = RankingQuestion(question_id="q", gold_spans=[span], candidates=[candidate])
    assert score_question(question).recall_at_10 == 1.0


def test_markdown_non_overlap_is_not_relevant() -> None:
    span = _span(locator=_md_locator(10, 20))
    candidate = _candidate("c1", 1, _md_locator(21, 25))
    assert not candidate_covers_span(candidate, span)
    question = RankingQuestion(question_id="q", gold_spans=[span], candidates=[candidate])
    assert score_question(question).recall_at_10 == 0.0


def test_pdf_page_overlap_and_mismatch() -> None:
    span = _span(locator=_pdf_locator(3))
    assert candidate_covers_span(_candidate("c1", 1, _pdf_locator(3)), span)
    assert not candidate_covers_span(_candidate("c2", 1, _pdf_locator(4)), span)


def test_parser_or_source_type_mismatch_is_not_relevant() -> None:
    span = _span(locator=_md_locator(1, 5))
    other_parser = GoldLocator(
        source_type="markdown",
        parser_version="markdown-other-v9",
        heading_path=["H"],
        start_line=1,
        end_line=5,
    )
    assert not candidate_covers_span(_candidate("c1", 1, other_parser), span)
    assert not candidate_covers_span(_candidate("c2", 1, _pdf_locator(1)), span)


def test_duplicate_chunk_counts_gold_once() -> None:
    span = _span(locator=_md_locator(1, 10))
    candidates = [
        _candidate("c1", 1, _md_locator(2, 3)),
        _candidate("c2", 2, _md_locator(4, 5)),
    ]
    result = score_question(
        RankingQuestion(question_id="q", gold_spans=[span], candidates=candidates)
    )
    assert result.covered_gold_span_count == 1
    assert result.recall_at_10 == 1.0
    assert result.dcg_at_10 == pytest.approx(1.0)  # 只有更高 rank 的 c1 记增益


def test_one_chunk_can_cover_multiple_gold_spans_for_recall() -> None:
    gold = [
        _span(locator=_md_locator(1, 3)),
        _span(locator=_md_locator(4, 6)),
    ]
    result = score_question(
        RankingQuestion(
            question_id="q",
            gold_spans=gold,
            candidates=[_candidate("c1", 1, _md_locator(1, 6))],
        )
    )
    assert result.covered_gold_span_count == 2
    assert result.recall_at_10 == 1.0
    assert result.dcg_at_10 == 1.0


def test_cross_document_partial_coverage() -> None:
    gold = [
        _span(doc="a", locator=_md_locator(1, 5)),
        _span(doc="b", locator=_md_locator(1, 5)),
    ]
    candidates = [_candidate("c1", 1, _md_locator(1, 5), doc="a")]
    result = score_question(
        RankingQuestion(question_id="q", gold_spans=gold, candidates=candidates)
    )
    assert result.covered_gold_span_count == 1
    assert result.recall_at_10 == 0.5
    idcg = 1.0 / math.log2(2) + 1.0 / math.log2(3)
    assert result.ndcg_at_10 == pytest.approx((1.0 / math.log2(2)) / idcg)


def test_top10_truncation_ignores_rank_11() -> None:
    span = _span(locator=_md_locator(1, 5))
    candidates = [
        _candidate(f"c{rank}", rank, _md_locator(100, 200)) for rank in range(1, 11)
    ]
    candidates.append(_candidate("c11", 11, _md_locator(1, 5)))
    result = score_question(
        RankingQuestion(question_id="q", gold_spans=[span], candidates=candidates)
    )
    assert result.recall_at_10 == 0.0
    assert result.dcg_at_10 == 0.0


def test_ideal_ranking_has_ndcg_one() -> None:
    gold = [_span(doc=f"d{i}", locator=_md_locator(1, 5)) for i in range(3)]
    candidates = [
        _candidate(f"c{i}", i + 1, _md_locator(1, 5), doc=f"d{i}") for i in range(3)
    ]
    result = score_question(
        RankingQuestion(question_id="q", gold_spans=gold, candidates=candidates)
    )
    assert result.recall_at_10 == 1.0
    assert result.ndcg_at_10 == pytest.approx(1.0)


def test_irrelevant_first_lowers_ndcg() -> None:
    gold = [_span(doc="a", locator=_md_locator(1, 5)), _span(doc="b", locator=_md_locator(1, 5))]
    candidates = [
        _candidate("c1", 1, _md_locator(50, 60)),
        _candidate("c2", 2, _md_locator(1, 5), doc="a"),
        _candidate("c3", 3, _md_locator(1, 5), doc="b"),
    ]
    result = score_question(
        RankingQuestion(question_id="q", gold_spans=gold, candidates=candidates)
    )
    assert result.recall_at_10 == 1.0
    assert result.ndcg_at_10 is not None and result.ndcg_at_10 < 1.0


def test_no_gold_is_excluded_from_denominator() -> None:
    answer = RankingQuestion(
        question_id="a",
        gold_spans=[_span()],
        candidates=[_candidate("c1", 1, _md_locator(1, 5))],
    )
    refusal = RankingQuestion(
        question_id="r",
        gold_spans=[],
        candidates=[_candidate("c9", 1, _md_locator(1, 5))],
    )
    report = aggregate_ranking_metrics([answer, refusal])
    assert report.question_count == 1
    assert report.excluded_question_ids == ("r",)
    assert report.recall_at_10 == 1.0
    assert report.failed_question_ids == ()


def test_aggregate_micro_denominators_and_failures() -> None:
    perfect = RankingQuestion(
        question_id="ok",
        gold_spans=[_span(doc="a", locator=_md_locator(1, 5))],
        candidates=[_candidate("c1", 1, _md_locator(1, 5), doc="a")],
    )
    partial = RankingQuestion(
        question_id="miss",
        gold_spans=[
            _span(doc="a", locator=_md_locator(1, 5)),
            _span(doc="b", locator=_md_locator(1, 5)),
        ],
        candidates=[_candidate("c2", 1, _md_locator(1, 5), doc="a")],
    )
    report = aggregate_ranking_metrics([perfect, partial])
    assert report.question_count == 2
    assert report.recall_covered_numerator == 2
    assert report.recall_gold_denominator == 3
    assert report.recall_at_10 == pytest.approx(0.75)
    assert report.failed_question_ids == ("miss",)


def test_ranking_question_rejects_duplicate_rank() -> None:
    with pytest.raises(ValueError):
        RankingQuestion(
            question_id="q",
            candidates=[
                _candidate("c1", 1, _md_locator(1, 2)),
                _candidate("c2", 1, _md_locator(3, 4)),
            ],
        )


def test_aggregate_rejects_duplicate_question_ids() -> None:
    question = RankingQuestion(question_id="dup", gold_spans=[_span()], candidates=[])
    with pytest.raises(RankingInputError):
        aggregate_ranking_metrics([question, question])
