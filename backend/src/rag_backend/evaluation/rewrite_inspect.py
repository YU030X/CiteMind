"""``python -m rag_backend.evaluation.rewrite_inspect``：离线检查追问改写观测产物的结构覆盖。

本入口只读题集与 ``RunnerRewriteArtifact``，不联网、不读环境文件、不连接数据库、不调用模型，
也不写任何文件。它做两件事：

1. **结构覆盖。** ``complete=true`` 时，产物中 final run 的 ``questionId`` 集合必须恰好覆盖题集；
   缺失、未知或同一题多个 final 一律静态失败。``complete=false`` 时允许 final 缺失，但未知
   ``questionId`` 仍然禁止；报告 ``observedFinal``/``expectedFinal`` 与缺失 id，绝不把 partial
   当完整。
2. **单一参考文本重合观察。** 只对题集中带 ``standaloneQuestion`` 且已观察到 final 的题，比较
   final run 的 ``standaloneQuestion`` 与该参考，分 ``exactMatch``（逐字相等）、``normalizedMatch``
   （非逐字但 NFKC + casefold + 连续空白折叠为单空格 + strip 后相等）、``different`` 三类，
   三类互斥。
   分母明确是“有参考且已观察到 final”的题数；没有分母时输出 ``None/0``。

它是**结构观察，不是质量分**：题集只有一个 gold ``standaloneQuestion``，不是唯一正确表达；重合计数
不衡量改写语义是否更优，也不做 token 相似度、编辑距离、阈值、pass/fail 或质量等级。真实语义需要人工
或另立授权成本的裁判。输出不打印任何 ``question``/``standaloneQuestion`` 原文，只打印题 id。

查看命令帮助（在仓库根目录运行）::

    uv run python -m rag_backend.evaluation.rewrite_inspect --help
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from rag_backend.evaluation.dataset import (
    DatasetValidationError,
    EvaluationDataset,
    load_dataset,
)
from rag_backend.evaluation.rewrite_artifact import RewriteRun, RunnerRewriteArtifact


class RewriteInspectError(Exception):
    """结构覆盖不满足契约时抛出；消息只含静态原因与题 id，不含任何题面或改写文本。"""


@dataclass(frozen=True)
class OverlapCounts:
    """单一参考文本重合的三类互斥计数；分母是“有参考且已观察到 final”的题数。"""

    denominator: int
    exact_match: int
    normalized_match: int
    different: int


@dataclass(frozen=True)
class RewriteInspection:
    """一次结构覆盖与参考重合观察的确定性结果；不含题面或改写原文。"""

    dataset_kind: str
    dataset_version: str
    partial: bool
    run_count: int
    first_turn_count: int
    final_observed: int
    expected_final: int
    missing_final_ids: tuple[str, ...]
    overlap: OverlapCounts
    different_question_ids: tuple[str, ...]


def normalize_standalone(text: str) -> str:
    """参考重合归一化：Unicode NFKC + casefold + 连续空白折叠为单空格 + strip。"""

    normalized = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", " ", normalized).strip()


def inspect_rewrite_artifact(
    dataset: EvaluationDataset, artifact: RunnerRewriteArtifact
) -> RewriteInspection:
    """校验元数据与结构覆盖，并统计带参考 final 题的重合计数；失败抛 ``RewriteInspectError``。"""

    if (
        artifact.dataset_kind != dataset.dataset_kind
        or artifact.dataset_version != dataset.dataset_version
    ):
        raise RewriteInspectError("产物的 datasetKind/datasetVersion 与题集不一致")

    dataset_ids = {question.id for question in dataset.questions}
    if len(dataset_ids) != len(dataset.questions):
        raise RewriteInspectError("题集存在重复题目 id")
    unknown = sorted({run.question_id for run in artifact.runs} - dataset_ids)
    if unknown:
        raise RewriteInspectError(f"产物包含未知题目 id：{', '.join(unknown)}")

    finalized: dict[str, RewriteRun] = {}
    for run in artifact.runs:
        if not run.is_final_question:
            continue
        if run.question_id in finalized:
            raise RewriteInspectError(f"[{run.question_id}] 存在多个 isFinalQuestion")
        finalized[run.question_id] = run

    missing = tuple(sorted(dataset_ids - set(finalized)))
    if artifact.complete and missing:
        raise RewriteInspectError(
            f"完整运行的产物缺少 final 题目 id：{', '.join(missing)}"
        )

    references = {
        question.id: question.standalone_question
        for question in dataset.questions
        if question.standalone_question is not None
    }
    exact_match = 0
    normalized_match = 0
    different = 0
    different_ids: list[str] = []
    for question_id in sorted(references):
        observed = finalized.get(question_id)
        if observed is None:
            continue
        reference = references[question_id]
        if observed.standalone_question == reference:
            exact_match += 1
        elif normalize_standalone(observed.standalone_question) == normalize_standalone(reference):
            normalized_match += 1
        else:
            different += 1
            different_ids.append(question_id)

    return RewriteInspection(
        dataset_kind=dataset.dataset_kind,
        dataset_version=dataset.dataset_version,
        partial=not artifact.complete,
        run_count=len(artifact.runs),
        first_turn_count=sum(1 for run in artifact.runs if run.turn_index == 0),
        final_observed=len(finalized),
        expected_final=len(dataset.questions),
        missing_final_ids=missing,
        overlap=OverlapCounts(
            denominator=exact_match + normalized_match + different,
            exact_match=exact_match,
            normalized_match=normalized_match,
            different=different,
        ),
        different_question_ids=tuple(different_ids),
    )


def _fraction(count: int, denominator: int) -> str:
    """有分母时输出 ``count/denominator``；无分母时输出 ``None/0``。"""

    if denominator == 0:
        return "None/0"
    return f"{count}/{denominator}"


def format_inspection(report: RewriteInspection) -> str:
    """把检查结果格式化为确定性多行中文文本；只含题 id，不含题面或改写原文。"""

    missing = ",".join(report.missing_final_ids) or "none"
    different = ",".join(report.different_question_ids) or "none"
    overlap = report.overlap
    return (
        f"追问改写观测检查通过：kind={report.dataset_kind} version={report.dataset_version} "
        f"partial={'true' if report.partial else 'false'} runs={report.run_count} "
        f"firstTurn={report.first_turn_count} finalObserved={report.final_observed} "
        f"expectedFinal={report.expected_final} missingFinal={missing}\n"
        f"参考重合观察：denominator={overlap.denominator} "
        f"exactMatch={_fraction(overlap.exact_match, overlap.denominator)} "
        f"normalizedMatch={_fraction(overlap.normalized_match, overlap.denominator)} "
        f"different={_fraction(overlap.different, overlap.denominator)}\n"
        f"differentIds={different}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m rag_backend.evaluation.rewrite_inspect",
        description="离线检查追问改写观测产物的结构覆盖与单一参考重合（不联网、不调用模型）。",
    )
    parser.add_argument("--dataset", type=Path, required=True, help="评估题集 JSON 路径")
    parser.add_argument("--rewrite", type=Path, required=True, help="追问改写观测产物 JSON 路径")
    args = parser.parse_args(argv)

    try:
        dataset = load_dataset(args.dataset)
    except DatasetValidationError as error:
        print(f"追问改写检查失败：{error}", file=sys.stderr)
        return 1
    except (ValidationError, OSError, UnicodeError):
        print("追问改写检查失败：题集不可读或不满足 schema", file=sys.stderr)
        return 1

    try:
        artifact = RunnerRewriteArtifact.model_validate_json(
            args.rewrite.read_text(encoding="utf-8")
        )
    except (ValidationError, OSError, UnicodeError):
        print("追问改写检查失败：改写产物不可读或不满足 schema", file=sys.stderr)
        return 1

    try:
        report = inspect_rewrite_artifact(dataset, artifact)
    except RewriteInspectError as error:
        print(f"追问改写检查失败：{error}", file=sys.stderr)
        return 1

    print(format_inspection(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
