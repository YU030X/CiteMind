"""Runner 追问改写**观测**产物 schema 与纯函数构建器。

本模块只描述 runner 已经拿到 ``queryRunId`` 的每次 ask 在 ``query_run`` 中落下的权威改写文本，
不做任何语义质量评分，也不按时间窗口猜缺失事实：

- 按已捕获 ``queryRunId`` 从 ``query_run`` 只读回读 ``question`` 与 ``standalone_question``；
  每个捕获的 run 都必须有权威行，缺失或未知一律静态失败，**即使** ``complete=false`` 也不放宽。
- 失败 final ask 的 HTTP 错误响应不返回 ``queryRunId``，因此本就没有 ``AskRunRecord``，不会被
  猜测或按时间补进产物；成功但拒答（``REFUSED``）的响应仍有 ``query_run`` 行，正常记录。
- 首轮（``turnIndex=0``）未发生改写，``standaloneQuestion`` 必须等于 ``question``；更晚轮次允许
  两者相等（模型判定已独立），但 ``standaloneQuestion`` 必须已经 strip。
- 字符串相等只做**结构一致性**检查，不是语义质量结论；本模块不判断改写是否更优。

产物包含用户生成的原始文本，仅允许在隔离评估环境内使用，真实产物默认不提交仓库。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic.alias_generators import to_camel

if TYPE_CHECKING:
    from rag_backend.evaluation.runner import AskRunRecord
    from rag_backend.evaluation.runner_adapters import QueryRunRewriteRow


class RewriteArtifactError(Exception):
    """rewrite 产物构建失败：重复/未知/缺失 queryRunId、重复 turnIndex 或非法文本。"""


class _Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class RewriteRun(_Model):
    """一次 ask 回读到的权威改写观测；文本来自 ``query_run``，不由 runner 编造。"""

    question_id: str = Field(min_length=1)
    conversation_id: uuid.UUID
    query_run_id: uuid.UUID
    turn_index: int = Field(ge=0, strict=True)
    is_final_question: bool
    question: str = Field(min_length=1)
    standalone_question: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check_text(self) -> RewriteRun:
        if not self.question.strip():
            raise ValueError("question 不能为空或纯空白")
        if not self.standalone_question.strip():
            raise ValueError("standaloneQuestion 不能为空或纯空白")
        if self.standalone_question != self.standalone_question.strip():
            raise ValueError("standaloneQuestion 必须已 strip")
        if self.turn_index == 0 and self.standalone_question != self.question:
            raise ValueError("首轮 turnIndex=0 的 standaloneQuestion 必须等于 question")
        return self


class RunnerRewriteArtifact(_Model):
    """逐题追问改写观测产物；``generatedFrom`` 固定标识来源为 runner。"""

    dataset_kind: Literal["dev", "holdout"]
    dataset_version: str = Field(min_length=1)
    generated_from: Literal["runner"] = "runner"
    complete: bool
    runs: list[RewriteRun]

    @model_validator(mode="after")
    def _check_runs(self) -> RunnerRewriteArtifact:
        seen_query: set[uuid.UUID] = set()
        by_question: dict[str, list[RewriteRun]] = {}
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


def build_runner_rewrite_artifact(
    run_records: Sequence[AskRunRecord],
    rewrite_rows: Sequence[QueryRunRewriteRow],
    *,
    dataset_kind: str,
    dataset_version: str,
    complete: bool,
) -> RunnerRewriteArtifact:
    """把捕获的 run 与 ``query_run`` 只读行按 ``queryRunId`` 对齐成产物。

    captured id 与 row id 都不得重复；未知 row、缺失 row、重复 ``turnIndex``、首轮文本不一致或
    非法文本一律静态失败，不静默丢弃、不补默认值。
    """

    runs_by_query: dict[uuid.UUID, AskRunRecord] = {}
    ordered_records: list[AskRunRecord] = []
    for record in run_records:
        if record.query_run_id in runs_by_query:
            raise RewriteArtifactError(f"重复 queryRunId：{record.query_run_id}")
        runs_by_query[record.query_run_id] = record
        ordered_records.append(record)

    rows_by_query: dict[uuid.UUID, QueryRunRewriteRow] = {}
    for row in rewrite_rows:
        if row.query_run_id in rows_by_query:
            raise RewriteArtifactError(f"数据库返回重复 queryRunId：{row.query_run_id}")
        if row.query_run_id not in runs_by_query:
            raise RewriteArtifactError(f"数据库返回未知 queryRunId：{row.query_run_id}")
        rows_by_query[row.query_run_id] = row

    built: list[RewriteRun] = []
    for record in ordered_records:
        authoritative = rows_by_query.get(record.query_run_id)
        if authoritative is None:
            raise RewriteArtifactError(f"缺少 query_run 权威行：queryRunId={record.query_run_id}")
        try:
            built.append(
                RewriteRun(
                    question_id=record.question_id,
                    conversation_id=record.conversation_id,
                    query_run_id=record.query_run_id,
                    turn_index=record.turn_index,
                    is_final_question=record.is_final_question,
                    question=authoritative.question,
                    standalone_question=authoritative.standalone_question,
                )
            )
        except ValidationError as error:
            raise RewriteArtifactError(
                f"query_run 行非法：queryRunId={record.query_run_id}"
            ) from error

    built.sort(key=lambda item: (item.question_id, item.turn_index))
    try:
        return RunnerRewriteArtifact(
            dataset_kind=cast("Literal['dev', 'holdout']", dataset_kind),
            dataset_version=dataset_version,
            complete=complete,
            runs=built,
        )
    except ValidationError as error:
        raise RewriteArtifactError(f"rewrite 产物不满足 schema：{error}") from error


__all__ = [
    "RewriteArtifactError",
    "RewriteRun",
    "RunnerRewriteArtifact",
    "build_runner_rewrite_artifact",
]
