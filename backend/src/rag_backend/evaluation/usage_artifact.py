"""Runner 逐题**原始** provider usage 产物 schema 与纯函数构建器。

本模块只描述 runner 已经拿到的账本事实，不做价格、费用或汇率换算，也不读失败提问的
时间窗口去猜 provider usage：

- 只保留 runner 通过 HTTP 成功响应拿到 ``queryRunId`` 的 ask；设计上每次 ask 都保留，
  即使账本里暂时没有对应 attempt（``usage`` 为空列表）。
- HTTP 错误响应不返回 ``queryRunId``，因此**最终 ask 失败**时该次 provider attempt 无法
  无歧义归因；产物用 ``complete=false`` 表示运行不完整，绝不按时间窗口把失败行猜进某次 run。
- 账本行必须落在已知 ``queryRunId`` 上，``usageId`` 不得重复；同一 run 内允许多条
  ``(stage, attempt)`` 相同的真实行，因为当前每次独立 HTTP 调用的 ``attempt`` 都记录为 1。
- token 缺失时保持空值并计入 ``missingCount``，不填 0；``knownSum`` 只累加已报告值。

金额/单价快照、`Decimal` 成本与汇率都不在本模块范围。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic.alias_generators import to_camel

if TYPE_CHECKING:
    from rag_backend.evaluation.runner import AskRunRecord
    from rag_backend.evaluation.runner_adapters import UsageAttemptRow


class UsageArtifactError(Exception):
    """usage 产物构建失败：未知 run、重复 usageId、缺少 latency 或非法账本事实。"""


class _Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


UsageStage = Literal["qa_rewrite", "qa_answer"]
UsageStatus = Literal["SUCCEEDED", "FAILED", "TIMEOUT"]

# usage 内固定阶段顺序：改写在前，回答在后；同阶段内按 attempt 升序。
_STAGE_ORDER: dict[str, int] = {"qa_rewrite": 0, "qa_answer": 1}


class UsageAttempt(_Model):
    """一次 provider attempt 的原始账本事实；失败/超时的 token 保持空，不填 0。"""

    usage_id: uuid.UUID
    created_at: datetime
    stage: UsageStage
    status: UsageStatus
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    attempt: int = Field(ge=1, strict=True)
    error_code: str | None = None
    prompt_tokens: int | None = Field(default=None, ge=0, strict=True)
    completion_tokens: int | None = Field(default=None, ge=0, strict=True)
    prompt_cache_hit_tokens: int | None = Field(default=None, ge=0, strict=True)
    prompt_cache_miss_tokens: int | None = Field(default=None, ge=0, strict=True)
    latency_ms: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def _check_created_at(self) -> UsageAttempt:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("createdAt 必须带时区")
        return self


class UsageRun(_Model):
    """一次 ask 对应的 run 及其全部已捕获 attempt。"""

    question_id: str = Field(min_length=1)
    conversation_id: uuid.UUID
    query_run_id: uuid.UUID
    turn_index: int = Field(ge=0, strict=True)
    is_final_question: bool
    usage: list[UsageAttempt] = Field(default_factory=list)


class KnownTotal(_Model):
    """已报告值之和与缺失计数；缺失不当作 0 计入 ``knownSum``。"""

    known_sum: int = Field(ge=0, strict=True)
    missing_count: int = Field(ge=0, strict=True)


class UsageBreakdown(_Model):
    """一组 run 的 provider 尝试与 token/latency 汇总。"""

    provider_attempts: int = Field(ge=0, strict=True)
    succeeded: int = Field(ge=0, strict=True)
    failed: int = Field(ge=0, strict=True)
    timed_out: int = Field(ge=0, strict=True)
    prompt_tokens: KnownTotal
    completion_tokens: KnownTotal
    prompt_cache_hit_tokens: KnownTotal
    prompt_cache_miss_tokens: KnownTotal
    latency_ms: KnownTotal


class UsageTotals(UsageBreakdown):
    """全量汇总与仅最终问题子集汇总（同结构）。"""

    final_question_only: UsageBreakdown


class RunnerUsageArtifact(_Model):
    """逐题原始 usage 产物；``generatedFrom`` 固定标识来源为 runner。"""

    dataset_kind: Literal["dev", "holdout"]
    dataset_version: str = Field(min_length=1)
    generated_from: Literal["runner"] = "runner"
    complete: bool
    runs: list[UsageRun]
    totals: UsageTotals

    @model_validator(mode="after")
    def _check_runs(self) -> RunnerUsageArtifact:
        seen_query: set[uuid.UUID] = set()
        by_question: dict[str, list[UsageRun]] = {}
        for run in self.runs:
            if run.query_run_id in seen_query:
                raise ValueError(f"queryRunId 重复：{run.query_run_id}")
            seen_query.add(run.query_run_id)
            by_question.setdefault(run.question_id, []).append(run)
        for question_id, question_runs in by_question.items():
            indexes = [run.turn_index for run in question_runs]
            if len(indexes) != len(set(indexes)):
                raise ValueError(f"[{question_id}] turnIndex 不能重复")
            finals = [run for run in question_runs if run.is_final_question]
            if self.complete and len(finals) != 1:
                raise ValueError(
                    f"[{question_id}] 完整运行每题必须恰有一个 isFinalQuestion"
                )
        return self


@dataclass
class _Sum:
    """可变的已知和/缺失计数累加器。"""

    known_sum: int = 0
    missing_count: int = 0

    def add(self, value: int | None) -> None:
        if value is None:
            self.missing_count += 1
        else:
            self.known_sum += value

    def total(self) -> KnownTotal:
        return KnownTotal(known_sum=self.known_sum, missing_count=self.missing_count)


def build_runner_usage_artifact(
    run_records: Sequence[AskRunRecord],
    usage_rows: Sequence[UsageAttemptRow],
    *,
    dataset_kind: str,
    dataset_version: str,
    complete: bool,
) -> RunnerUsageArtifact:
    """把 runner 捕获的 run 与只读账本行构建成产物；不补默认值、不猜缺失事实。"""

    runs_by_query: dict[uuid.UUID, AskRunRecord] = {}
    ordered_records: list[AskRunRecord] = []
    for record in run_records:
        if record.query_run_id in runs_by_query:
            raise UsageArtifactError(f"重复 queryRunId：{record.query_run_id}")
        runs_by_query[record.query_run_id] = record
        ordered_records.append(record)

    attempts: dict[uuid.UUID, list[UsageAttempt]] = {}
    seen_usage_ids: set[uuid.UUID] = set()
    for row in usage_rows:
        if row.query_run_id not in runs_by_query:
            raise UsageArtifactError(f"数据库返回未知 queryRunId：{row.query_run_id}")
        if row.usage_id in seen_usage_ids:
            raise UsageArtifactError(f"数据库返回重复 usageId：{row.usage_id}")
        seen_usage_ids.add(row.usage_id)
        attempts.setdefault(row.query_run_id, []).append(_to_attempt(row))

    built: list[UsageRun] = []
    for record in ordered_records:
        usage = sorted(
            attempts.get(record.query_run_id, []),
            key=lambda item: (
                _STAGE_ORDER[item.stage],
                item.created_at,
                item.attempt,
                item.usage_id.int,
            ),
        )
        built.append(
            UsageRun(
                question_id=record.question_id,
                conversation_id=record.conversation_id,
                query_run_id=record.query_run_id,
                turn_index=record.turn_index,
                is_final_question=record.is_final_question,
                usage=usage,
            )
        )
    built.sort(key=lambda item: (item.question_id, item.turn_index))

    overall = _aggregate(built)
    final_only = _aggregate([run for run in built if run.is_final_question])
    try:
        return RunnerUsageArtifact(
            dataset_kind=cast("Literal['dev', 'holdout']", dataset_kind),
            dataset_version=dataset_version,
            complete=complete,
            runs=built,
            totals=UsageTotals(**overall.model_dump(), final_question_only=final_only),
        )
    except ValidationError as error:
        raise UsageArtifactError(f"usage 产物不满足 schema：{error}") from error


def _to_attempt(row: UsageAttemptRow) -> UsageAttempt:
    if row.latency_ms is None:
        raise UsageArtifactError(f"账本行缺少 latencyMs：queryRunId={row.query_run_id}")
    try:
        return UsageAttempt(
            usage_id=row.usage_id,
            created_at=row.created_at,
            stage=cast(UsageStage, row.stage),
            status=cast(UsageStatus, row.status),
            provider=row.provider,
            model=row.model,
            attempt=row.attempt,
            error_code=row.error_code,
            prompt_tokens=row.prompt_tokens,
            completion_tokens=row.completion_tokens,
            prompt_cache_hit_tokens=row.prompt_cache_hit_tokens,
            prompt_cache_miss_tokens=row.prompt_cache_miss_tokens,
            latency_ms=row.latency_ms,
        )
    except ValidationError as error:
        raise UsageArtifactError(f"账本行非法：queryRunId={row.query_run_id}") from error


def _aggregate(runs: Sequence[UsageRun]) -> UsageBreakdown:
    provider = succeeded = failed = timed_out = 0
    prompt = _Sum()
    completion = _Sum()
    cache_hit = _Sum()
    cache_miss = _Sum()
    latency = _Sum()
    for run in runs:
        for attempt in run.usage:
            provider += 1
            if attempt.status == "SUCCEEDED":
                succeeded += 1
            elif attempt.status == "FAILED":
                failed += 1
            else:
                timed_out += 1
            prompt.add(attempt.prompt_tokens)
            completion.add(attempt.completion_tokens)
            cache_hit.add(attempt.prompt_cache_hit_tokens)
            cache_miss.add(attempt.prompt_cache_miss_tokens)
            latency.add(attempt.latency_ms)
    return UsageBreakdown(
        provider_attempts=provider,
        succeeded=succeeded,
        failed=failed,
        timed_out=timed_out,
        prompt_tokens=prompt.total(),
        completion_tokens=completion.total(),
        prompt_cache_hit_tokens=cache_hit.total(),
        prompt_cache_miss_tokens=cache_miss.total(),
        latency_ms=latency.total(),
    )


__all__ = [
    "KnownTotal",
    "RunnerUsageArtifact",
    "UsageArtifactError",
    "UsageAttempt",
    "UsageBreakdown",
    "UsageRun",
    "UsageTotals",
    "build_runner_usage_artifact",
]
