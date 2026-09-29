"""追问改写观测产物 schema 与纯函数构建器的聚焦单测。

不联网、不读数据库、不调用模型：只用合成 run 记录与 ``query_run`` 行验证严格 camelCase schema、
captured/row id 重复与未知/缺失对齐、重复 turnIndex、complete 的 final 约束、首轮一致性与
standalone 必须已 strip，以及按 (questionId, turnIndex) 稳定排序。
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError
from rag_backend.evaluation.rewrite_artifact import (
    RewriteArtifactError,
    RewriteRun,
    RunnerRewriteArtifact,
    build_runner_rewrite_artifact,
)
from rag_backend.evaluation.runner import AskRunRecord
from rag_backend.evaluation.runner_adapters import QueryRunRewriteRow


def _record(
    question_id: str,
    turn_index: int,
    is_final_question: bool,
    *,
    query_run_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
) -> AskRunRecord:
    return AskRunRecord(
        question_id=question_id,
        conversation_id=conversation_id or uuid.uuid4(),
        query_run_id=query_run_id or uuid.uuid4(),
        turn_index=turn_index,
        is_final_question=is_final_question,
    )


def _row(
    query_run_id: uuid.UUID,
    *,
    question: str = "问题",
    standalone_question: str | None = None,
) -> QueryRunRewriteRow:
    return QueryRunRewriteRow(
        query_run_id=query_run_id,
        question=question,
        standalone_question=question if standalone_question is None else standalone_question,
    )


def _build(
    runs: list[AskRunRecord],
    rows: list[QueryRunRewriteRow],
    *,
    complete: bool = True,
) -> RunnerRewriteArtifact:
    return build_runner_rewrite_artifact(
        runs, rows, dataset_kind="dev", dataset_version="v1", complete=complete
    )


def test_rewrite_run_is_camel_case_and_strict() -> None:
    run = RewriteRun(
        question_id="q1",
        conversation_id=uuid.uuid4(),
        query_run_id=uuid.uuid4(),
        turn_index=0,
        is_final_question=True,
        question="问题",
        standalone_question="问题",
    )
    assert set(run.model_dump(by_alias=True)) == {
        "questionId",
        "conversationId",
        "queryRunId",
        "turnIndex",
        "isFinalQuestion",
        "question",
        "standaloneQuestion",
    }
    base: dict[str, object] = {
        "question_id": "q1",
        "conversation_id": uuid.uuid4(),
        "query_run_id": uuid.uuid4(),
        "turn_index": 0,
        "is_final_question": True,
        "question": "问题",
        "standalone_question": "问题",
    }
    for bad in ({"turn_index": True}, {"turn_index": 1.0}, {"turn_index": -1}):
        kwargs = dict(base)
        kwargs.update(bad)
        with pytest.raises(ValidationError):
            RewriteRun(**kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        RewriteRun(**base, extra="x")  # type: ignore[arg-type,call-arg]


def test_rewrite_run_rejects_blank_and_unstripped_text() -> None:
    conversation_id = uuid.uuid4()
    query_run_id = uuid.uuid4()
    with pytest.raises(ValidationError):
        RewriteRun(
            question_id="q1",
            conversation_id=conversation_id,
            query_run_id=query_run_id,
            turn_index=0,
            is_final_question=True,
            question="",
            standalone_question="",
        )
    with pytest.raises(ValidationError):
        RewriteRun(
            question_id="q1",
            conversation_id=conversation_id,
            query_run_id=query_run_id,
            turn_index=0,
            is_final_question=True,
            question="   ",
            standalone_question="   ",
        )
    # standalone 未 strip：即使 turnIndex>0 也拒绝。
    with pytest.raises(ValidationError):
        RewriteRun(
            question_id="q1",
            conversation_id=conversation_id,
            query_run_id=query_run_id,
            turn_index=1,
            is_final_question=True,
            question="问题",
            standalone_question="改写后 ",
        )


def test_first_turn_requires_standalone_equal_question_but_later_turn_allows() -> None:
    conversation_id = uuid.uuid4()
    query_run_id = uuid.uuid4()
    with pytest.raises(ValidationError):
        RewriteRun(
            question_id="q1",
            conversation_id=conversation_id,
            query_run_id=query_run_id,
            turn_index=0,
            is_final_question=True,
            question="问题",
            standalone_question="改写后",
        )
    # turnIndex>0 允许 standalone == question（模型判定已独立）。
    run = RewriteRun(
        question_id="q1",
        conversation_id=conversation_id,
        query_run_id=query_run_id,
        turn_index=1,
        is_final_question=True,
        question="问题",
        standalone_question="问题",
    )
    assert run.standalone_question == run.question


def test_build_complete_multi_turn_sorts_and_keeps_text() -> None:
    conversation_id = uuid.uuid4()
    history = _record("q-multi", 0, False, conversation_id=conversation_id)
    final = _record("q-multi", 1, True, conversation_id=conversation_id)
    single = _record("q-single", 0, True)
    rows = [
        _row(final.query_run_id, question="追问问题", standalone_question="独立问题"),
        _row(history.query_run_id, question="第一问"),
        _row(single.query_run_id, question="当前问题"),
    ]
    artifact = _build([single, final, history], rows)
    assert artifact.generated_from == "runner"
    assert artifact.complete is True
    assert artifact.dataset_kind == "dev"
    assert artifact.dataset_version == "v1"
    assert [(run.question_id, run.turn_index) for run in artifact.runs] == [
        ("q-multi", 0),
        ("q-multi", 1),
        ("q-single", 0),
    ]
    assert artifact.runs[0].standalone_question == "第一问"
    assert artifact.runs[1].question == "追问问题"
    assert artifact.runs[1].standalone_question == "独立问题"


def test_build_partial_setup_allows_missing_final() -> None:
    history = _record("q-multi", 0, False)
    artifact = _build([history], [_row(history.query_run_id, question="第一问")], complete=False)
    assert artifact.complete is False
    assert [run.is_final_question for run in artifact.runs] == [False]


def test_complete_requires_exactly_one_final_per_question() -> None:
    history = _record("q1", 0, False)
    with pytest.raises(RewriteArtifactError, match="isFinalQuestion"):
        _build([history], [_row(history.query_run_id)], complete=True)


def test_build_rejects_duplicate_captured_query_run_id() -> None:
    shared = uuid.uuid4()
    first = _record("q1", 0, False, query_run_id=shared)
    second = _record("q1", 1, True, query_run_id=shared)
    with pytest.raises(RewriteArtifactError, match="重复 queryRunId"):
        _build([first, second], [_row(shared)])


def test_build_rejects_duplicate_database_query_run_id() -> None:
    record = _record("q1", 0, True)
    with pytest.raises(RewriteArtifactError, match="数据库返回重复 queryRunId"):
        _build([record], [_row(record.query_run_id), _row(record.query_run_id)])


def test_build_rejects_unknown_database_query_run_id() -> None:
    with pytest.raises(RewriteArtifactError, match="未知 queryRunId"):
        _build([_record("q1", 0, True)], [_row(uuid.uuid4())])


def test_build_rejects_missing_row_even_when_incomplete() -> None:
    record = _record("q1", 0, True)
    with pytest.raises(RewriteArtifactError, match="缺少 query_run 权威行"):
        _build([record], [], complete=True)
    with pytest.raises(RewriteArtifactError, match="缺少 query_run 权威行"):
        _build([record], [], complete=False)


def test_build_rejects_duplicate_turn_index_within_question() -> None:
    first = _record("q1", 0, False)
    second = _record("q1", 0, True)
    with pytest.raises(RewriteArtifactError, match="turnIndex"):
        _build([first, second], [_row(first.query_run_id), _row(second.query_run_id)])


def test_artifact_schema_rejects_inconsistent_runs_directly() -> None:
    run = RewriteRun(
        question_id="q1",
        conversation_id=uuid.uuid4(),
        query_run_id=uuid.uuid4(),
        turn_index=0,
        is_final_question=False,
        question="问题",
        standalone_question="问题",
    )
    with pytest.raises(ValidationError):
        RunnerRewriteArtifact(
            dataset_kind="dev",
            dataset_version="v1",
            complete=True,
            runs=[run],
        )
