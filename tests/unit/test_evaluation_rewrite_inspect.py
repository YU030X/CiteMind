"""追问改写观测离线检查（``python -m rag_backend.evaluation.rewrite_inspect``）的聚焦单测。

不联网、不读数据库、不调用模型：只用合成题集与产物 JSON 验证结构覆盖、参考重合三类互斥计数、
无参考分母、partial 缺失与未知 id、元数据漂移，以及 CLI 不回显任何题面/改写原文、失败时输出静态
中文错误且不打印 traceback。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from rag_backend.evaluation.dataset import EvaluationDataset
from rag_backend.evaluation.rewrite_artifact import RunnerRewriteArtifact
from rag_backend.evaluation.rewrite_inspect import (
    RewriteInspectError,
    RewriteInspection,
    inspect_rewrite_artifact,
    normalize_standalone,
)
from rag_backend.evaluation.rewrite_inspect import (
    main as rewrite_inspect_main,
)


def _question(
    question_id: str,
    *,
    standalone: str | None = None,
    question: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": question_id,
        "category": "unanswerable",
        "scope": {"role": "staff", "kbIds": ["kb"]},
        "question": question or f"{question_id} 的原始问题",
        "expectedBehavior": "refuse",
    }
    if standalone is not None:
        payload["standaloneQuestion"] = standalone
        payload["history"] = [{"role": "user", "text": "上一轮问题"}]
        payload["tags"] = ["multi_turn"]
    return payload


def _dataset_payload(questions: list[dict[str, object]]) -> dict[str, object]:
    return {
        "datasetKind": "dev",
        "datasetVersion": "v1",
        "corpusManifest": "manifest.json",
        "questions": questions,
    }


def _run(
    question_id: str,
    *,
    turn_index: int,
    is_final: bool,
    standalone: str,
    question: str | None = None,
) -> dict[str, object]:
    return {
        "questionId": question_id,
        "conversationId": str(uuid.uuid4()),
        "queryRunId": str(uuid.uuid4()),
        "turnIndex": turn_index,
        "isFinalQuestion": is_final,
        "question": question or f"{question_id} 的原始问题",
        "standaloneQuestion": standalone,
    }


def _artifact_payload(
    runs: list[dict[str, object]],
    *,
    complete: bool = True,
    dataset_version: str = "v1",
    dataset_kind: str = "dev",
) -> dict[str, object]:
    return {
        "datasetKind": dataset_kind,
        "datasetVersion": dataset_version,
        "generatedFrom": "runner",
        "complete": complete,
        "runs": runs,
    }


def _write(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _inspect(
    dataset_payload: dict[str, object], artifact_payload: dict[str, object]
) -> RewriteInspection:
    dataset = EvaluationDataset.model_validate(dataset_payload)
    artifact = RunnerRewriteArtifact.model_validate(artifact_payload)
    return inspect_rewrite_artifact(dataset, artifact)


def test_normalize_standalone_applies_nfkc_casefold_and_whitespace() -> None:
    assert normalize_standalone("  Expense\u3000 标准 ") == "expense 标准"
    assert normalize_standalone("ＡＢＣ") == "abc"


def test_overlap_counts_exact_normalized_different_and_no_reference() -> None:
    dataset = _dataset_payload(
        [
            _question("q-exact", standalone="独立问题A"),
            _question("q-normalized", standalone="Expense 标准"),
            _question("q-different", standalone="独立问题C"),
            _question("q-noref", question="普通问题"),
        ]
    )
    artifact = _artifact_payload(
        [
            _run("q-exact", turn_index=1, is_final=True, standalone="独立问题A"),
            # 非逐字：大小写与空白折叠后相等。
            _run("q-normalized", turn_index=1, is_final=True, standalone="expense  标准"),
            _run("q-different", turn_index=1, is_final=True, standalone="独立问题D"),
            _run(
                "q-noref",
                turn_index=0,
                is_final=True,
                standalone="普通问题",
                question="普通问题",
            ),
        ]
    )
    report = _inspect(dataset, artifact)
    assert report.partial is False
    assert report.run_count == 4
    assert report.first_turn_count == 1
    assert report.final_observed == 4
    assert report.expected_final == 4
    assert report.missing_final_ids == ()
    assert report.different_question_ids == ("q-different",)
    overlap = report.overlap
    assert (overlap.denominator, overlap.exact_match) == (3, 1)
    assert (overlap.normalized_match, overlap.different) == (1, 1)
    assert overlap.denominator == (
        overlap.exact_match + overlap.normalized_match + overlap.different
    )


def test_no_reference_denominator_outputs_none() -> None:
    dataset = _dataset_payload([_question("q-noref", question="普通问题")])
    artifact = _artifact_payload(
        [
            _run(
                "q-noref",
                turn_index=0,
                is_final=True,
                standalone="普通问题",
                question="普通问题",
            )
        ]
    )
    report = _inspect(dataset, artifact)
    overlap = report.overlap
    assert overlap.denominator == 0
    assert (overlap.exact_match, overlap.normalized_match, overlap.different) == (0, 0, 0)


def test_complete_requires_final_coverage_of_every_question() -> None:
    dataset = _dataset_payload(
        [_question("q1", standalone="独立问题一"), _question("q2", standalone="独立问题二")]
    )
    artifact = _artifact_payload(
        [_run("q1", turn_index=1, is_final=True, standalone="独立问题一")]
    )
    with pytest.raises(RewriteInspectError, match="缺少 final 题目 id：q2"):
        _inspect(dataset, artifact)


def test_duplicate_dataset_question_id_is_rejected() -> None:
    question = _question("q1", standalone="独立问题一")
    dataset = _dataset_payload([question, question])
    artifact = _artifact_payload(
        [_run("q1", turn_index=1, is_final=True, standalone="独立问题一")]
    )
    with pytest.raises(RewriteInspectError, match="题集存在重复题目 id"):
        _inspect(dataset, artifact)


def test_unknown_question_id_is_rejected_even_when_incomplete() -> None:
    dataset = _dataset_payload([_question("q1", standalone="独立问题一")])
    artifact = _artifact_payload(
        [
            _run("q1", turn_index=1, is_final=True, standalone="独立问题一"),
            _run("q-unknown", turn_index=0, is_final=True, standalone="q-unknown 的原始问题"),
        ],
        complete=False,
    )
    with pytest.raises(RewriteInspectError, match="未知题目 id：q-unknown"):
        _inspect(dataset, artifact)


def test_multiple_final_runs_for_one_question_are_rejected() -> None:
    dataset = _dataset_payload([_question("q1", standalone="独立问题一")])
    artifact = _artifact_payload(
        [
            _run("q1", turn_index=0, is_final=True, standalone="q1 的原始问题"),
            _run("q1", turn_index=1, is_final=True, standalone="独立问题一"),
        ],
        complete=False,
    )
    with pytest.raises(RewriteInspectError, match="存在多个 isFinalQuestion"):
        _inspect(dataset, artifact)


def test_partial_reports_missing_final_without_treating_it_as_complete() -> None:
    dataset = _dataset_payload(
        [_question("q1", standalone="独立问题一"), _question("q2", standalone="独立问题二")]
    )
    artifact = _artifact_payload(
        [_run("q1", turn_index=1, is_final=True, standalone="独立问题一")],
        complete=False,
    )
    report = _inspect(dataset, artifact)
    assert report.partial is True
    assert report.final_observed == 1
    assert report.expected_final == 2
    assert report.missing_final_ids == ("q2",)
    assert report.overlap.denominator == 1


def test_dataset_metadata_drift_is_rejected() -> None:
    dataset = _dataset_payload([_question("q1", standalone="独立问题一")])
    artifact = _artifact_payload(
        [_run("q1", turn_index=1, is_final=True, standalone="独立问题一")],
        dataset_version="v2",
    )
    with pytest.raises(RewriteInspectError, match="datasetKind/datasetVersion 与题集不一致"):
        _inspect(dataset, artifact)


def test_cli_success_is_deterministic_and_never_echoes_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "SECRET-QUESTION-TEXT-9F3A"
    dataset = _write(
        tmp_path / "dataset.json",
        _dataset_payload(
            [_question("q1", standalone=f"{secret} 参考"), _question("q-noref", question=secret)]
        ),
    )
    artifact = _write(
        tmp_path / "rewrite.json",
        _artifact_payload(
            [
                _run("q1", turn_index=1, is_final=True, standalone=f"{secret} 参考"),
                _run(
                    "q-noref",
                    turn_index=0,
                    is_final=True,
                    standalone=secret,
                    question=secret,
                ),
            ]
        ),
    )
    code = rewrite_inspect_main(["--dataset", str(dataset), "--rewrite", str(artifact)])
    assert code == 0
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    assert "参考重合观察：denominator=1 exactMatch=1/1" in captured.out
    assert "partial=false" in captured.out
    assert "differentIds=none" in captured.out


def test_cli_schema_error_is_static_and_hides_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "SECRET-STANDALONE-TEXT-7B21"
    dataset = _write(
        tmp_path / "dataset.json",
        _dataset_payload([_question("q1", standalone=f"{secret} 参考")]),
    )
    bad_artifact: dict[str, object] = {
        "datasetKind": "dev",
        "datasetVersion": "v1",
        "complete": True,
        "runs": [
            {
                "questionId": "q1",
                "conversationId": str(uuid.uuid4()),
                "queryRunId": str(uuid.uuid4()),
                "turnIndex": 1,
                "isFinalQuestion": True,
                "question": secret,
                # 缺 standaloneQuestion：schema 失败，且不得回显 question 原文。
            }
        ],
    }
    artifact = _write(tmp_path / "rewrite.json", bad_artifact)
    code = rewrite_inspect_main(["--dataset", str(dataset), "--rewrite", str(artifact)])
    assert code == 1
    captured = capsys.readouterr()
    assert "追问改写检查失败" in captured.err
    assert secret not in captured.err
    assert secret not in captured.out
    assert "Traceback" not in captured.err


def test_cli_coverage_error_is_static(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = _write(
        tmp_path / "dataset.json",
        _dataset_payload([_question("q1", standalone="独立问题一")]),
    )
    artifact = _write(
        tmp_path / "rewrite.json",
        _artifact_payload([], complete=True),
    )
    code = rewrite_inspect_main(["--dataset", str(dataset), "--rewrite", str(artifact)])
    assert code == 1
    captured = capsys.readouterr()
    assert "缺少 final 题目 id：q1" in captured.err
    assert "Traceback" not in captured.err
