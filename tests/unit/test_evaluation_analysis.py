"""离线分析 CLI（``python -m rag_backend.evaluation.analysis``）的合成端到端单测。

只验证 CLI 装配：题集读取、产物 schema、三元组校验与指标输出；不代表真实数据或质量。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rag_backend.evaluation.analysis import main as analysis_main

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
) -> dict[str, object]:
    return {
        "datasetKind": "dev",
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
