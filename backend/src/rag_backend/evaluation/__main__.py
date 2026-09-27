"""``python -m rag_backend.evaluation``：离线校验开发题集，可选地计算确定性指标。

单行执行（在仓库根目录）::

    uv run python -m rag_backend.evaluation
    uv run python -m rag_backend.evaluation --dataset tests/evaluation/dev-questions.json
    uv run python -m rag_backend.evaluation --results path/to/results.json

不传 ``--results`` 时只校验题集结构、分类、来源与 gold 匹配；该入口不联网、不读环境文件、
不调用任何模型，也不产生质量评分。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from rag_backend.evaluation.dataset import (
    DatasetValidationError,
    load_dataset_bundle,
    validate_dataset,
)
from rag_backend.evaluation.metrics import (
    EvaluationResults,
    MetricsInputError,
    compute_metrics,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_DEFAULT_DATASET = _REPO_ROOT / "tests" / "evaluation" / "dev-questions.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m rag_backend.evaluation",
        description="离线校验开发评估题集，并按需计算确定性指标（不联网、不调用模型）。",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=_DEFAULT_DATASET,
        help="开发题集 JSON 路径（默认 tests/evaluation/dev-questions.json）",
    )
    parser.add_argument(
        "--results",
        type=Path,
        default=None,
        help="可选：真实运行产生的结果文件，用于计算确定性指标",
    )
    args = parser.parse_args(argv)

    try:
        dataset, manifest, corpus_dir = load_dataset_bundle(args.dataset)
        report = validate_dataset(dataset, manifest, corpus_dir)
    except (DatasetValidationError, ValidationError) as error:
        print(f"题集校验失败：{error}", file=sys.stderr)
        return 1

    print(
        f"题集校验通过：kind={report.dataset_kind} version={report.dataset_version} "
        f"total={report.total}"
    )
    categories = "、".join(
        f"{key}={value}" for key, value in sorted(report.category_counts.items())
    )
    print("分类：" + categories)
    if report.tag_counts:
        tags = "、".join(f"{key}={value}" for key, value in sorted(report.tag_counts.items()))
        print("标签：" + tags)

    if args.results is None:
        print("未提供 --results，跳过指标计算（真实指标需由实际运行结果文件产生）。")
        return 0

    try:
        results = EvaluationResults.model_validate_json(args.results.read_text(encoding="utf-8"))
        metrics = compute_metrics(dataset, manifest, corpus_dir, results)
    except (DatasetValidationError, MetricsInputError, ValidationError, OSError) as error:
        print(f"指标计算失败：{error}", file=sys.stderr)
        return 1

    print(
        "确定性指标："
        f"refusalAccuracy={metrics.refusal_accuracy} "
        f"falseRefusalRate={metrics.false_refusal_rate} "
        f"citationSourceValidity={metrics.citation_source_validity} "
        f"goldSourceCoverage={metrics.gold_source_coverage} "
        f"permissionLeakCount={metrics.permission_leak_count} "
        f"(answered={metrics.answered} refused={metrics.refused} "
        f"expectedAnswer={metrics.expected_answer} expectedRefuse={metrics.expected_refuse})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
