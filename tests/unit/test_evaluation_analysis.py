"""离线分析 CLI（``python -m rag_backend.evaluation.analysis``）的合成端到端单测。

只验证 CLI 装配：题集读取、产物 schema、三元组校验与指标输出；不代表真实数据或质量。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rag_backend.evaluation.analysis import (
    LatencySummary,
    VariantObservationSummary,
    format_observation_summary,
    summarize_degraded_stages,
    summarize_latency,
)
from rag_backend.evaluation.analysis import (
    main as analysis_main,
)

_LOCATOR = {
    "sourceType": "markdown",
    "parserVersion": "markdown-it-py-4.2.0-v1",
    "headingPath": ["H"],
    "startLine": 1,
    "endLine": 5,
}


def _dataset() -> dict[str, object]:
    return {
        "datasetKind": "dev",
        "datasetVersion": "v1",
        "corpusManifest": "manifest.json",
        "questions": [
            {
                "id": "q1",
                "category": "single_document",
                "scope": {"role": "staff", "kbIds": ["kb"]},
                "question": "问题一",
                "expectedBehavior": "answer",
                "goldAnswerPoints": ["要点"],
                "goldSourceSpans": [
                    {
                        "kbId": "kb",
                        "documentId": "doc",
                        "version": 1,
                        "quote": "quote",
                        "locator": _LOCATOR,
                    }
                ],
            },
            {
                "id": "q2",
                "category": "unanswerable",
                "scope": {"role": "staff", "kbIds": ["kb"]},
                "question": "问题二",
                "expectedBehavior": "refuse",
            },
        ],
    }


def _artifact(
    variant: str,
    candidates: list[dict[str, object]],
    *,
    degraded: bool = False,
    dataset_kind: str = "dev",
) -> dict[str, object]:
    return {
        "datasetKind": dataset_kind,
        "datasetVersion": "v1",
        "variant": variant,
        "config": {},
        "modelIdentities": {},
        "createdAt": "2026-09-29T00:00:00Z",
        "questions": [
            {
                "questionId": "q1",
                "scopeId": "scope-1",
                "latencyMs": 5.0,
                "degradedStages": ["rerank_unavailable"] if degraded else [],
                "candidates": candidates,
            },
            {
                "questionId": "q2",
                "scopeId": "scope-1",
                "latencyMs": 5.0,
                "degradedStages": [],
                "candidates": [],
            },
        ],
    }


def _question_payload(
    question_id: str,
    latency_ms: float,
    degraded_stages: list[str],
    candidates: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "questionId": question_id,
        "scopeId": "scope-1",
        "latencyMs": latency_ms,
        "degradedStages": degraded_stages,
        "candidates": candidates,
    }


def _artifact_with_questions(
    variant: str, questions: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "datasetKind": "dev",
        "datasetVersion": "v1",
        "variant": variant,
        "config": {},
        "modelIdentities": {},
        "createdAt": "2026-09-29T00:00:00Z",
        "questions": questions,
    }


def _candidate(candidate_id: str, rank: int, **extra: object) -> dict[str, object]:
    return {
        "candidateId": candidate_id,
        "kbId": "kb",
        "documentId": "doc",
        "version": 1,
        "locator": _LOCATOR,
        "rank": rank,
        "vectorRank": rank,
        "vectorScore": 1.0,
        **extra,
    }


def _write(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _calibration(
    *,
    dataset_kind: str = "dev",
    dataset_version: str = "v1",
    records: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    if records is None:
        records = [
            {
                "questionId": "q1",
                "expectedBehavior": "answer",
                "topScore": 0.9,
                "candidateCount": 1,
            },
            {
                "questionId": "q2",
                "expectedBehavior": "refuse",
                "topScore": 0.1,
                "candidateCount": 1,
            },
        ]
    return {
        "datasetKind": dataset_kind,
        "datasetVersion": dataset_version,
        "createdAt": "2026-09-29T00:00:00Z",
        "records": records,
    }


def _write_triplet(tmp_path: Path, dataset_kind: str = "dev") -> tuple[Path, Path, Path]:
    a = _write(
        tmp_path / "a.json",
        _artifact("A_VECTOR", [_candidate("c1", 1)], dataset_kind=dataset_kind),
    )
    b = _write(
        tmp_path / "b.json",
        _artifact(
            "B_RRF",
            [_candidate("c1", 1, fusionRank=1, fusionScore=1.0)],
            dataset_kind=dataset_kind,
        ),
    )
    c = _write(
        tmp_path / "c.json",
        _artifact(
            "C_RERANK",
            [_candidate("c1", 1, fusionRank=1, fusionScore=1.0, rerankScore=0.9)],
            dataset_kind=dataset_kind,
        ),
    )
    return a, b, c


def _holdout_dataset() -> dict[str, object]:
    payload = _dataset()
    payload["datasetKind"] = "holdout"
    return payload


def test_analysis_cli_prints_metrics(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dataset = _write(tmp_path / "dataset.json", _dataset())
    a = _write(tmp_path / "a.json", _artifact("A_VECTOR", [_candidate("c1", 1)]))
    b = _write(
        tmp_path / "b.json",
        _artifact("B_RRF", [_candidate("c1", 1, fusionRank=1, fusionScore=1.0)]),
    )
    c = _write(
        tmp_path / "c.json",
        _artifact(
            "C_RERANK",
            [_candidate("c1", 1, fusionRank=1, fusionScore=1.0, rerankScore=0.9)],
        ),
    )
    code = analysis_main(
        ["--dataset", str(dataset), "--a", str(a), "--b", str(b), "--c", str(c)]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "三元组校验通过" in out
    assert "A_VECTOR" in out and "C_RERANK" in out
    assert "excluded=1" in out
    assert "degraded=none/0" in out
    assert "标定" not in out


def test_summarize_latency_nearest_rank_odd_and_even() -> None:
    even = summarize_latency([40.0, 10.0, 30.0, 20.0])
    assert even == LatencySummary(
        question_count=4, mean_ms=25.0, p50_ms=20.0, p95_ms=40.0, max_ms=40.0
    )
    odd = summarize_latency([30.0, 10.0, 20.0])
    assert odd == LatencySummary(
        question_count=3, mean_ms=20.0, p50_ms=20.0, p95_ms=30.0, max_ms=30.0
    )


def test_summarize_latency_mean_uses_fsum() -> None:
    # 朴素求和在 [1e16, 1.0, -1e16] 上会得到 0.0；math.fsum 保留 1.0。
    summary = summarize_latency([1e16, 1.0, -1e16])
    assert summary.mean_ms == 1.0 / 3.0


def test_summarize_degraded_stages_counts_questions_and_none() -> None:
    counts = summarize_degraded_stages(
        [["rerank_unavailable", "vector_unavailable"], ["rerank_unavailable"], []]
    )
    assert counts == (("rerank_unavailable", 2), ("vector_unavailable", 1))
    assert summarize_degraded_stages([[], []]) == ()


def test_format_observation_summary_is_deterministic() -> None:
    summary = VariantObservationSummary(
        variant="C_RERANK",
        latency=LatencySummary(
            question_count=2, mean_ms=20.0, p50_ms=10.0, p95_ms=30.0, max_ms=30.0
        ),
        degraded_stage_counts=(("rerank_unavailable", 1), ("vector_unavailable", 2)),
    )
    assert format_observation_summary(summary) == (
        "C_RERANK 观察汇总：questions=2 "
        "latencyMs(mean=20.000 p50=10.000 p95=30.000 max=30.000) "
        "degraded=rerank_unavailable/1、vector_unavailable/2"
    )


def test_analysis_cli_prints_latency_and_degraded_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = _write(tmp_path / "dataset.json", _dataset())
    a = _write(
        tmp_path / "a.json",
        _artifact_with_questions(
            "A_VECTOR",
            [
                _question_payload("q1", 10.0, [], [_candidate("c1", 1)]),
                _question_payload("q2", 30.0, [], []),
            ],
        ),
    )
    b = _write(
        tmp_path / "b.json",
        _artifact_with_questions(
            "B_RRF",
            [
                _question_payload(
                    "q1", 10.0, [], [_candidate("c1", 1, fusionRank=1, fusionScore=1.0)]
                ),
                _question_payload("q2", 30.0, [], []),
            ],
        ),
    )
    c = _write(
        tmp_path / "c.json",
        _artifact_with_questions(
            "C_RERANK",
            [
                _question_payload(
                    "q1",
                    10.0,
                    [],
                    [
                        _candidate(
                            "c1", 1, fusionRank=1, fusionScore=1.0, rerankScore=0.9
                        )
                    ],
                ),
                _question_payload("q2", 30.0, ["rerank_unavailable"], []),
            ],
        ),
    )
    code = analysis_main(
        ["--dataset", str(dataset), "--a", str(a), "--b", str(b), "--c", str(c)]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert (
        "A_VECTOR 观察汇总：questions=2 "
        "latencyMs(mean=20.000 p50=10.000 p95=30.000 max=30.000) degraded=none/0"
    ) in out
    assert (
        "C_RERANK 观察汇总：questions=2 "
        "latencyMs(mean=20.000 p50=10.000 p95=30.000 max=30.000) "
        "degraded=rerank_unavailable/1"
    ) in out


def test_analysis_cli_rejects_negative_latency(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = _write(tmp_path / "dataset.json", _dataset())
    a = _write(
        tmp_path / "a.json",
        _artifact_with_questions(
            "A_VECTOR",
            [
                _question_payload("q1", -1.0, [], [_candidate("c1", 1)]),
                _question_payload("q2", 5.0, [], []),
            ],
        ),
    )
    b = _write(
        tmp_path / "b.json",
        _artifact("B_RRF", [_candidate("c1", 1, fusionRank=1, fusionScore=1.0)]),
    )
    c = _write(
        tmp_path / "c.json",
        _artifact(
            "C_RERANK",
            [_candidate("c1", 1, fusionRank=1, fusionScore=1.0, rerankScore=0.9)],
        ),
    )
    code = analysis_main(
        ["--dataset", str(dataset), "--a", str(a), "--b", str(b), "--c", str(c)]
    )
    assert code == 1
    assert "离线分析失败" in capsys.readouterr().err


def test_analysis_cli_rejects_dataset_metadata_drift(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = _dataset()
    payload["datasetVersion"] = "v2"
    dataset = _write(tmp_path / "dataset.json", payload)
    a = _write(tmp_path / "a.json", _artifact("A_VECTOR", [_candidate("c1", 1)]))
    b = _write(
        tmp_path / "b.json",
        _artifact("B_RRF", [_candidate("c1", 1, fusionRank=1, fusionScore=1.0)]),
    )
    c = _write(
        tmp_path / "c.json",
        _artifact(
            "C_RERANK",
            [_candidate("c1", 1, fusionRank=1, fusionScore=1.0, rerankScore=0.9)],
        ),
    )

    code = analysis_main(
        ["--dataset", str(dataset), "--a", str(a), "--b", str(b), "--c", str(c)]
    )
    assert code == 1
    assert "datasetKind/datasetVersion" in capsys.readouterr().err


def test_analysis_cli_rejects_degraded_order_drift(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = _write(tmp_path / "dataset.json", _dataset())
    a = _write(
        tmp_path / "a.json",
        _artifact("A_VECTOR", [_candidate("c1", 1), _candidate("c2", 2)]),
    )
    b = _write(
        tmp_path / "b.json",
        _artifact(
            "B_RRF",
            [
                _candidate("c1", 1, fusionRank=1, fusionScore=1.0),
                _candidate("c2", 2, fusionRank=2, fusionScore=1.0),
            ],
        ),
    )
    c = _write(
        tmp_path / "c.json",
        _artifact(
            "C_RERANK",
            [
                _candidate("c1", 2, fusionRank=2, fusionScore=1.0),
                _candidate("c2", 1, fusionRank=1, fusionScore=1.0),
            ],
            degraded=True,
        ),
    )
    code = analysis_main(
        ["--dataset", str(dataset), "--a", str(a), "--b", str(b), "--c", str(c)]
    )
    assert code == 1
    assert "离线分析失败" in capsys.readouterr().err


def test_analysis_cli_dev_calibration_selects_point(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = _write(tmp_path / "dataset.json", _dataset())
    a, b, c = _write_triplet(tmp_path)
    calibration = _write(tmp_path / "calibration.json", _calibration())
    code = analysis_main(
        [
            "--dataset",
            str(dataset),
            "--a",
            str(a),
            "--b",
            str(b),
            "--c",
            str(c),
            "--calibration",
            str(calibration),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "标定（dev 选点）" in out
    assert "refuseCorrect=" in out and "falseRefusal=" in out
    assert "balancedAccuracy=1.0" in out


def test_analysis_cli_dev_rejects_refusal_threshold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = _write(tmp_path / "dataset.json", _dataset())
    a, b, c = _write_triplet(tmp_path)
    calibration = _write(tmp_path / "calibration.json", _calibration())
    code = analysis_main(
        [
            "--dataset",
            str(dataset),
            "--a",
            str(a),
            "--b",
            str(b),
            "--c",
            str(c),
            "--calibration",
            str(calibration),
            "--refusal-threshold",
            "0.5",
        ]
    )
    assert code == 1
    assert "开发集标定不得传入" in capsys.readouterr().err


def test_analysis_cli_holdout_requires_refusal_threshold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = _write(tmp_path / "dataset.json", _holdout_dataset())
    a, b, c = _write_triplet(tmp_path, dataset_kind="holdout")
    calibration = _write(tmp_path / "calibration.json", _calibration(dataset_kind="holdout"))
    code = analysis_main(
        [
            "--dataset",
            str(dataset),
            "--a",
            str(a),
            "--b",
            str(b),
            "--c",
            str(c),
            "--calibration",
            str(calibration),
        ]
    )
    assert code == 1
    assert "留出集标定必须显式传入" in capsys.readouterr().err


def test_analysis_cli_holdout_reports_fixed_point(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = _write(tmp_path / "dataset.json", _holdout_dataset())
    a, b, c = _write_triplet(tmp_path, dataset_kind="holdout")
    calibration = _write(tmp_path / "calibration.json", _calibration(dataset_kind="holdout"))
    code = analysis_main(
        [
            "--dataset",
            str(dataset),
            "--a",
            str(a),
            "--b",
            str(b),
            "--c",
            str(c),
            "--calibration",
            str(calibration),
            "--refusal-threshold",
            "0.5",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "标定（holdout 固定点）：threshold=0.5" in out
    assert "refuseCorrect=1/1" in out and "falseRefusal=0/1" in out


def test_analysis_cli_rejects_threshold_without_calibration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = _write(tmp_path / "dataset.json", _dataset())
    a, b, c = _write_triplet(tmp_path)
    code = analysis_main(
        [
            "--dataset",
            str(dataset),
            "--a",
            str(a),
            "--b",
            str(b),
            "--c",
            str(c),
            "--refusal-threshold",
            "0.5",
        ]
    )
    assert code == 1
    assert "只在提供 --calibration 时可用" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["abc", "nan", "inf", "-inf"])
def test_analysis_cli_rejects_non_finite_threshold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], bad: str
) -> None:
    dataset = _write(tmp_path / "dataset.json", _holdout_dataset())
    a, b, c = _write_triplet(tmp_path, dataset_kind="holdout")
    calibration = _write(tmp_path / "calibration.json", _calibration(dataset_kind="holdout"))
    code = analysis_main(
        [
            "--dataset",
            str(dataset),
            "--a",
            str(a),
            "--b",
            str(b),
            "--c",
            str(c),
            "--calibration",
            str(calibration),
            f"--refusal-threshold={bad}",
        ]
    )
    assert code == 1
    assert "--refusal-threshold 必须是有限数" in capsys.readouterr().err


def test_analysis_cli_rejects_calibration_coverage_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = _write(tmp_path / "dataset.json", _dataset())
    a, b, c = _write_triplet(tmp_path)
    records: list[dict[str, object]] = [
        {
            "questionId": "q1",
            "expectedBehavior": "answer",
            "topScore": 0.9,
            "candidateCount": 1,
        }
    ]
    calibration = _write(
        tmp_path / "calibration.json", _calibration(records=records)
    )
    code = analysis_main(
        [
            "--dataset",
            str(dataset),
            "--a",
            str(a),
            "--b",
            str(b),
            "--c",
            str(c),
            "--calibration",
            str(calibration),
        ]
    )
    assert code == 1
    assert "标定产物缺少题目 id：q2" in capsys.readouterr().err


def test_analysis_cli_rejects_calibration_metadata_drift(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = _write(tmp_path / "dataset.json", _dataset())
    a, b, c = _write_triplet(tmp_path)
    calibration = _write(
        tmp_path / "calibration.json", _calibration(dataset_version="v2")
    )
    code = analysis_main(
        [
            "--dataset",
            str(dataset),
            "--a",
            str(a),
            "--b",
            str(b),
            "--c",
            str(c),
            "--calibration",
            str(calibration),
        ]
    )
    assert code == 1
    assert "datasetKind/datasetVersion 与题集不一致" in capsys.readouterr().err


def test_analysis_cli_dev_rejects_no_observable_scores(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = _write(tmp_path / "dataset.json", _dataset())
    a, b, c = _write_triplet(tmp_path)
    records: list[dict[str, object]] = [
        {
            "questionId": "q1",
            "expectedBehavior": "answer",
            "topScore": None,
            "candidateCount": 0,
        },
        {
            "questionId": "q2",
            "expectedBehavior": "refuse",
            "topScore": None,
            "candidateCount": 0,
        },
    ]
    calibration = _write(
        tmp_path / "calibration.json", _calibration(records=records)
    )
    code = analysis_main(
        [
            "--dataset",
            str(dataset),
            "--a",
            str(a),
            "--b",
            str(b),
            "--c",
            str(c),
            "--calibration",
            str(calibration),
        ]
    )
    assert code == 1
    assert "没有可用的拒答阈值选点" in capsys.readouterr().err
