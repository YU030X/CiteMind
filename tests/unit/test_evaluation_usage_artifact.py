"""Runner 逐题原始 usage 产物 schema 与纯函数构建器的聚焦单测。

不联网、不读数据库、不调用模型：只用合成 run 记录与账本行验证严格 camelCase schema、
空 usage 保留、稳定排序、unknown/duplicate 拒绝、complete 的 final 约束与
knownSum/missingCount（含 finalQuestionOnly）汇总。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from rag_backend.evaluation.runner import AskRunRecord
from rag_backend.evaluation.runner_adapters import UsageAttemptRow
from rag_backend.evaluation.usage_artifact import (
    KnownTotal,
    RunnerUsageArtifact,
    UsageArtifactError,
    UsageAttempt,
    UsageBreakdown,
    UsageRun,
    UsageTotals,
    build_runner_usage_artifact,
)


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


def _row(query_run_id: uuid.UUID, **overrides: object) -> UsageAttemptRow:
    payload: dict[str, object] = {
        "usage_id": uuid.uuid4(),
        "created_at": datetime(2026, 9, 29, tzinfo=UTC),
        "stage": "qa_answer",
        "status": "SUCCEEDED",
        "model": "deepseek-flash",
        "attempt": 1,
        "error_code": None,
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 1,
        "latency_ms": 10,
    }
    payload.update(overrides)
    return UsageAttemptRow(query_run_id=query_run_id, **payload)  # type: ignore[arg-type]


def _build(
    runs: list[AskRunRecord],
    rows: list[UsageAttemptRow],
    *,
    complete: bool = True,
) -> RunnerUsageArtifact:
    return build_runner_usage_artifact(
        runs, rows, dataset_kind="dev", dataset_version="v1", complete=complete
    )


def test_usage_attempt_is_camel_case_and_strict() -> None:
    attempt = UsageAttempt(
        usage_id=uuid.uuid4(),
        created_at=datetime(2026, 9, 29, tzinfo=UTC),
        stage="qa_answer",
        status="SUCCEEDED",
        model="m",
        attempt=1,
        latency_ms=0,
    )
    assert set(attempt.model_dump(by_alias=True)) == {
        "usageId",
        "createdAt",
        "stage",
        "status",
        "model",
        "attempt",
        "errorCode",
        "promptTokens",
        "completionTokens",
        "promptCacheHitTokens",
        "promptCacheMissTokens",
        "latencyMs",
    }
    # bool / 浮点 / 字符串不得被当作 int；阶段与状态必须是受限字面量；额外字段拒绝。
    for bad in (
        {"attempt": True},
        {"attempt": 1.0},
        {"prompt_tokens": True},
        {"latency_ms": -1},
        {"stage": "llm_probe"},
        {"status": "UNKNOWN"},
    ):
        kwargs: dict[str, object] = {
            "usage_id": uuid.uuid4(),
            "created_at": datetime(2026, 9, 29, tzinfo=UTC),
            "stage": "qa_answer",
            "status": "SUCCEEDED",
            "model": "m",
            "attempt": 1,
            "latency_ms": 0,
        }
        kwargs.update(bad)
        with pytest.raises(ValidationError):
            UsageAttempt(**kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        UsageAttempt(
            usage_id=uuid.uuid4(),
            created_at=datetime(2026, 9, 29, tzinfo=UTC),
            stage="qa_answer",
            status="SUCCEEDED",
            model="m",
            attempt=1,
            latency_ms=0,
            extra="x",  # type: ignore[call-arg]
        )


def test_build_keeps_run_with_empty_usage() -> None:
    record = _record("q1", 0, True)
    artifact = _build([record], [])
    assert len(artifact.runs) == 1
    assert artifact.runs[0].usage == []
    assert artifact.totals.provider_attempts == 0


def test_build_sorts_runs_and_usage_stably() -> None:
    conversation_a = uuid.uuid4()
    q2 = _record("q2", 0, True, conversation_id=conversation_a)
    q1_final = _record("q1", 1, True, conversation_id=conversation_a)
    q1_history = _record("q1", 0, False, conversation_id=conversation_a)
    rows = [
        _row(q1_final.query_run_id, stage="qa_answer", attempt=2, latency_ms=22),
        _row(q1_final.query_run_id, stage="qa_answer", attempt=1, latency_ms=21),
        _row(q1_final.query_run_id, stage="qa_rewrite", attempt=1, latency_ms=20),
        _row(q1_history.query_run_id),
        _row(q2.query_run_id),
    ]
    artifact = _build([q2, q1_final, q1_history], rows)
    assert [(run.question_id, run.turn_index) for run in artifact.runs] == [
        ("q1", 0),
        ("q1", 1),
        ("q2", 0),
    ]
    final_usage = artifact.runs[1].usage
    assert [(item.stage, item.attempt) for item in final_usage] == [
        ("qa_rewrite", 1),
        ("qa_answer", 1),
        ("qa_answer", 2),
    ]


def test_totals_known_sum_and_missing_count_with_final_only() -> None:
    history = _record("q1", 0, False)
    final = _record("q1", 1, True)
    rows = [
        # history：成功，token 已报告。
        _row(history.query_run_id, prompt_tokens=10, completion_tokens=2, latency_ms=30),
        # final：失败，provider 未报告 token（保持 NULL，不计 0）。
        _row(
            final.query_run_id,
            status="FAILED",
            error_code="HTTP_500",
            prompt_tokens=None,
            completion_tokens=None,
            prompt_cache_hit_tokens=None,
            prompt_cache_miss_tokens=None,
            latency_ms=40,
        ),
    ]
    artifact = _build([history, final], rows)
    totals = artifact.totals
    assert totals.provider_attempts == 2
    assert (totals.succeeded, totals.failed, totals.timed_out) == (1, 1, 0)
    assert totals.prompt_tokens.known_sum == 10
    assert totals.prompt_tokens.missing_count == 1
    assert totals.completion_tokens.known_sum == 2
    assert totals.latency_ms.known_sum == 70
    assert totals.latency_ms.missing_count == 0
    final_only = totals.final_question_only
    assert final_only.provider_attempts == 1
    assert final_only.failed == 1
    assert final_only.prompt_tokens.known_sum == 0
    assert final_only.prompt_tokens.missing_count == 1


def test_build_rejects_unknown_query_run_id() -> None:
    with pytest.raises(UsageArtifactError, match="未知"):
        _build([_record("q1", 0, True)], [_row(uuid.uuid4())])


def test_build_keeps_distinct_source_retry_rows_with_same_stage_and_attempt() -> None:
    record = _record("q1", 0, True)
    artifact = _build([record], [_row(record.query_run_id), _row(record.query_run_id)])
    assert len(artifact.runs[0].usage) == 2


def test_build_rejects_duplicate_usage_id() -> None:
    record = _record("q1", 0, True)
    usage_id = uuid.uuid4()
    with pytest.raises(UsageArtifactError, match="重复 usageId"):
        _build(
            [record],
            [
                _row(record.query_run_id, usage_id=usage_id),
                _row(record.query_run_id, usage_id=usage_id),
            ],
        )


def test_build_rejects_duplicate_query_run_id() -> None:
    shared = uuid.uuid4()
    record = _record("q1", 0, True, query_run_id=shared)
    duplicate = _record("q1", 1, False, query_run_id=shared)
    with pytest.raises(UsageArtifactError, match="重复 queryRunId"):
        _build([record, duplicate], [])


def test_build_rejects_duplicate_turn_index_within_question() -> None:
    first = _record("q1", 0, False)
    second = _record("q1", 0, True)
    with pytest.raises(UsageArtifactError, match="turnIndex"):
        _build([first, second], [])


def test_complete_requires_exactly_one_final_per_question() -> None:
    # 完整运行：只有 setup 轮、没有 final，必须拒绝。
    with pytest.raises(UsageArtifactError, match="isFinalQuestion"):
        _build([_record("q1", 0, False)], [], complete=True)
    # 失败 partial：允许 0 final。
    artifact = _build([_record("q1", 0, False)], [], complete=False)
    assert artifact.complete is False


def test_build_rejects_null_latency_row() -> None:
    record = _record("q1", 0, True)
    with pytest.raises(UsageArtifactError, match="latencyMs"):
        _build([record], [_row(record.query_run_id, latency_ms=None)])


def test_artifact_schema_rejects_inconsistent_runs_directly() -> None:
    conversation_id = uuid.uuid4()
    run = UsageRun(
        question_id="q1",
        conversation_id=conversation_id,
        query_run_id=uuid.uuid4(),
        turn_index=0,
        is_final_question=False,
        usage=[],
    )
    zero = KnownTotal(known_sum=0, missing_count=0)
    breakdown = UsageBreakdown(
        provider_attempts=0,
        succeeded=0,
        failed=0,
        timed_out=0,
        prompt_tokens=zero,
        completion_tokens=zero,
        prompt_cache_hit_tokens=zero,
        prompt_cache_miss_tokens=zero,
        latency_ms=zero,
    )
    with pytest.raises(ValidationError):
        RunnerUsageArtifact(
            dataset_kind="dev",
            dataset_version="v1",
            complete=True,
            runs=[run],
            totals=UsageTotals(**breakdown.model_dump(), final_question_only=breakdown),
        )
