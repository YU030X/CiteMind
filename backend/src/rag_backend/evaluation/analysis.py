"""``python -m rag_backend.evaluation.analysis``：离线读取题集与 A/B/C 消融产物。

它只做题集结构读取、三元组确定性校验与固定 Recall@10/nDCG@10 聚合；不联网、不读环境文件、
不连接数据库、不调用模型，也不产生任何真实质量结论。输出各变体的宏平均指标与微观测分子/分母、
逐题失败 id，以及 C 降级回退到 B 的题号。

可选地接受 ``--calibration`` 消费一份严格 ``CalibrationArtifact``：开发集只用
``scan_refusal_thresholds`` + ``select_dev_threshold`` 选点（禁止传入 ``--refusal-threshold``），
留出集必须显式给出有限 ``--refusal-threshold`` 且只用 ``evaluate_refusal_threshold`` 报告，不得在
留出集上扫描选点。它不写任何新文件，也不改变缺省 `--calibration` 时的输出。

单行示例（在仓库根目录）::

    uv run python -m rag_backend.evaluation.analysis \
        --dataset dev.json --a a.json --b b.json --c c.json
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

from pydantic import ValidationError

from rag_backend.evaluation.ablation import (
    AblationArtifact,
    AblationValidationError,
    validate_ablation_triplet,
)
from rag_backend.evaluation.calibration import (
    CalibrationArtifact,
    CalibrationInputError,
    evaluate_refusal_threshold,
    scan_refusal_thresholds,
    select_dev_threshold,
)
from rag_backend.evaluation.dataset import (
    DatasetValidationError,
    EvaluationDataset,
    GoldSpan,
    load_dataset,
)
from rag_backend.evaluation.ranking_metrics import (
    RankingInputError,
    RankingQuestion,
    aggregate_ranking_metrics,
)


def build_ranking_questions(
    dataset: EvaluationDataset, artifact: AblationArtifact
) -> list[RankingQuestion]:
    """按 questionId 把题集 gold 与产物候选合并；产物题目必须恰好覆盖题集。"""

    if (
        artifact.dataset_kind != dataset.dataset_kind
        or artifact.dataset_version != dataset.dataset_version
    ):
        raise RankingInputError("产物的 datasetKind/datasetVersion 与题集不一致")
    gold_by_id: dict[str, list[GoldSpan]] = {
        question.id: question.gold_source_spans for question in dataset.questions
    }
    artifact_ids = {question.question_id for question in artifact.questions}
    missing = sorted(set(gold_by_id) - artifact_ids)
    unknown = sorted(artifact_ids - set(gold_by_id))
    if missing:
        raise RankingInputError(f"产物缺少题目 id：{', '.join(missing)}")
    if unknown:
        raise RankingInputError(f"产物包含未知题目 id：{', '.join(unknown)}")
    return [
        RankingQuestion(
            question_id=question.question_id,
            gold_spans=gold_by_id[question.question_id],
            candidates=list(question.candidates),
        )
        for question in artifact.questions
    ]


def calibration_summary(
    dataset: EvaluationDataset,
    artifact: CalibrationArtifact,
    refusal_threshold: float | None,
) -> str:
    """按题集 kind 解析拒答标定点并返回一行中文摘要；非法输入抛 ``CalibrationInputError``。"""

    if (
        artifact.dataset_kind != dataset.dataset_kind
        or artifact.dataset_version != dataset.dataset_version
    ):
        raise CalibrationInputError("标定产物的 datasetKind/datasetVersion 与题集不一致")
    dataset_ids = {question.id for question in dataset.questions}
    record_ids = {record.question_id for record in artifact.records}
    missing = sorted(dataset_ids - record_ids)
    unknown = sorted(record_ids - dataset_ids)
    if missing:
        raise CalibrationInputError(f"标定产物缺少题目 id：{', '.join(missing)}")
    if unknown:
        raise CalibrationInputError(f"标定产物包含未知题目 id：{', '.join(unknown)}")

    if dataset.dataset_kind == "dev":
        if refusal_threshold is not None:
            raise CalibrationInputError("开发集标定不得传入 --refusal-threshold，应由扫描选点")
        point = select_dev_threshold(scan_refusal_thresholds(artifact.records))
        if point is None:
            raise CalibrationInputError(
                "开发集标定失败：没有可用的拒答阈值选点（无可观测分数或指标分母为空）"
            )
        label = "dev 选点"
    else:
        if refusal_threshold is None:
            raise CalibrationInputError("留出集标定必须显式传入 --refusal-threshold")
        point = evaluate_refusal_threshold(artifact.records, refusal_threshold)
        label = "holdout 固定点"
    return (
        f"标定（{label}）：threshold={point.threshold} "
        f"refusalAccuracy={point.refusal_accuracy} "
        f"refuseCorrect={point.refuse_correct_numerator}/{point.refuse_denominator} "
        f"falseRefusalRate={point.false_refusal_rate} "
        f"falseRefusal={point.false_refusal_numerator}/{point.answer_denominator} "
        f"balancedAccuracy={point.balanced_accuracy}"
    )


def _parse_refusal_threshold(raw: str | None) -> float | None:
    """把 CLI 字符串阈值解析为有限数；缺省返回 ``None``，非法一律抛 ``CalibrationInputError``。"""

    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError as error:
        raise CalibrationInputError("--refusal-threshold 必须是有限数") from error
    if not math.isfinite(value):
        raise CalibrationInputError("--refusal-threshold 必须是有限数")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m rag_backend.evaluation.analysis",
        description="离线校验 A/B/C 消融产物并聚合 Recall@10/nDCG@10（不联网、不调用模型）。",
    )
    parser.add_argument("--dataset", type=Path, required=True, help="评估题集 JSON 路径")
    parser.add_argument("--a", type=Path, required=True, help="A_VECTOR 产物 JSON 路径")
    parser.add_argument("--b", type=Path, required=True, help="B_RRF 产物 JSON 路径")
    parser.add_argument("--c", type=Path, required=True, help="C_RERANK 产物 JSON 路径")
    parser.add_argument(
        "--calibration",
        type=Path,
        default=None,
        help="可选：拒答标定产物 JSON 路径；给出后按题集 kind 解析标定点",
    )
    parser.add_argument(
        "--refusal-threshold",
        type=str,
        default=None,
        help="可选：留出集已固定的拒答阈值（有限数）；开发集禁止传入",
    )
    args = parser.parse_args(argv)

    if args.refusal_threshold is not None and args.calibration is None:
        print("离线分析失败：--refusal-threshold 只在提供 --calibration 时可用", file=sys.stderr)
        return 1

    try:
        refusal_threshold = _parse_refusal_threshold(args.refusal_threshold)
        dataset = load_dataset(args.dataset)
        artifact_a = AblationArtifact.model_validate_json(args.a.read_text(encoding="utf-8"))
        artifact_b = AblationArtifact.model_validate_json(args.b.read_text(encoding="utf-8"))
        artifact_c = AblationArtifact.model_validate_json(args.c.read_text(encoding="utf-8"))
        triplet = validate_ablation_triplet(artifact_a, artifact_b, artifact_c)
        reports = {
            "A_VECTOR": aggregate_ranking_metrics(build_ranking_questions(dataset, artifact_a)),
            "B_RRF": aggregate_ranking_metrics(build_ranking_questions(dataset, artifact_b)),
            "C_RERANK": aggregate_ranking_metrics(build_ranking_questions(dataset, artifact_c)),
        }
        calibration_line = None
        if args.calibration is not None:
            artifact_calibration = CalibrationArtifact.model_validate_json(
                args.calibration.read_text(encoding="utf-8")
            )
            calibration_line = calibration_summary(
                dataset, artifact_calibration, refusal_threshold
            )
    except (
        DatasetValidationError,
        AblationValidationError,
        RankingInputError,
        CalibrationInputError,
        ValidationError,
        OSError,
        UnicodeError,
    ) as error:
        print(f"离线分析失败：{error}", file=sys.stderr)
        return 1

    print(
        f"三元组校验通过：kind={triplet.dataset_kind} version={triplet.dataset_version} "
        f"questions={triplet.question_count} fallback={len(triplet.fallback_question_ids)}"
    )
    for variant, report in reports.items():
        failed = "、".join(report.failed_question_ids) or "无"
        print(
            f"{variant}：recall@10={report.recall_at_10} "
            f"covered={report.recall_covered_numerator}/{report.recall_gold_denominator} "
            f"nDCG@10={report.ndcg_at_10} "
            f"dcg/idcg={report.ndcg_dcg_numerator}/{report.ndcg_idcg_denominator} "
            f"questions={report.question_count} excluded={len(report.excluded_question_ids)} "
            f"failed={failed}"
        )
    if calibration_line is not None:
        print(calibration_line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
