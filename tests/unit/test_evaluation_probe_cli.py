"""只读探针 CLI 的聚焦单测：全部注入假运行时/假执行器，不建 engine、不联网、不调模型。

覆盖 dry-run 不读环境/不调 adapter/不写文件、预算与覆盖校验、holdout 护栏、真实开关、
已有产物拒绝，以及四份产物成功落盘与失败零正式文件。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import pytest
import rag_backend.evaluation.probe_cli as probe_cli_module
from rag_backend.evaluation.ablation import (
    AblationArtifact,
    AblationQuestion,
    validate_ablation_triplet,
)
from rag_backend.evaluation.calibration import RefusalProbeRecord
from rag_backend.evaluation.dataset import EvaluationQuestion, GoldLocator
from rag_backend.evaluation.probe import ProbeInputs, ProbeOutcome
from rag_backend.evaluation.probe_adapters import (
    LocatorCandidateMapper,
    ProbeAccountRow,
    ProbeProfileRow,
    ProbeRuntime,
)
from rag_backend.evaluation.probe_cli import ARTIFACT_FILENAMES
from rag_backend.evaluation.probe_cli import main as probe_main
from rag_backend.evaluation.ranking_metrics import RankingCandidate, RankingReport
from rag_backend.retrieval.query_embedding_client import EmbeddedQuery
from rag_backend.retrieval.repository import RetrievalRepository
from rag_backend.retrieval.rerank_client import RerankInput, RerankScore

SCOPE_A = "role=reader;kbIds=kb-a"
SCOPE_B = "role=reader;kbIds=kb-b"
CREATED_AT = "2026-09-29T00:00:00+00:00"
VALID_DSN = "postgresql+psycopg://user:pw@localhost:5432/rag_test"
TEST_ENV = {"EVAL_DATABASE_URL": VALID_DSN, "INFERENCE_TOKEN": "token-value"}


# ---------------------------------------------------------------------------
# 假运行时与产物


class _FakeEmbedder:
    def embed_query(self, text: str, expected_model_revision: str) -> EmbeddedQuery:
        raise AssertionError("dry-run/假执行器不应调用 embedder")

    def close(self) -> None:
        return None


class _FakeAnalyzer:
    @property
    def analyzer_id(self) -> str:
        return "fake-analyzer"

    def analyze(self, text: str) -> str:
        return text


class _FakeReranker:
    def rerank(self, query: str, candidates: list[RerankInput]) -> list[RerankScore]:
        raise AssertionError("dry-run/假执行器不应调用 reranker")

    def close(self) -> None:
        return None


def _unused_factory(role: str, question: EvaluationQuestion) -> RetrievalRepository:
    raise AssertionError("假执行器不应构造仓储")


def _make_runtime() -> ProbeRuntime:
    async def load_kb_organizations(
        kb_ids: Sequence[uuid.UUID],
    ) -> Mapping[uuid.UUID, uuid.UUID]:
        return {kb_id: uuid.uuid4() for kb_id in kb_ids}

    async def load_profiles(kb_ids: Sequence[uuid.UUID]) -> Sequence[ProbeProfileRow]:
        return []

    async def load_accounts(
        organization_id: uuid.UUID, usernames: Sequence[str]
    ) -> Sequence[ProbeAccountRow]:
        return []

    async def aclose() -> None:
        return None

    return ProbeRuntime(
        repository_factory=_unused_factory,
        mapper=LocatorCandidateMapper(),
        embedder=_FakeEmbedder(),
        analyzer=_FakeAnalyzer(),
        reranker=_FakeReranker(),
        load_kb_organizations=load_kb_organizations,
        load_profiles=load_profiles,
        load_accounts=load_accounts,
        aclose=aclose,
    )


def _explode_builder(**kwargs: object) -> ProbeRuntime:
    raise AssertionError("dry-run 不应构造运行时")


def _make_full_runtime() -> ProbeRuntime:
    """可让默认执行器解析身份/profile 的假运行时；不连数据库。"""

    organization = uuid.uuid4()
    user_id = uuid.uuid4()
    profile_id = uuid.uuid4()

    async def load_kb_organizations(
        kb_ids: Sequence[uuid.UUID],
    ) -> Mapping[uuid.UUID, uuid.UUID]:
        return {kb_id: organization for kb_id in kb_ids}

    async def load_profiles(kb_ids: Sequence[uuid.UUID]) -> Sequence[ProbeProfileRow]:
        return [
            ProbeProfileRow(
                kb_id=kb_id,
                profile_id=profile_id,
                embedding_model="bge",
                model_revision="rev-1",
                dimension=512,
                normalize=True,
                keyword_analyzer_version="analyzer-1",
            )
            for kb_id in kb_ids
        ]

    async def load_accounts(
        organization_id: uuid.UUID, usernames: Sequence[str]
    ) -> Sequence[ProbeAccountRow]:
        return [
            ProbeAccountRow(username=username, user_id=user_id)
            for username in usernames
        ]

    async def aclose() -> None:
        return None

    return ProbeRuntime(
        repository_factory=_unused_factory,
        mapper=LocatorCandidateMapper(),
        embedder=_FakeEmbedder(),
        analyzer=_FakeAnalyzer(),
        reranker=_FakeReranker(),
        load_kb_organizations=load_kb_organizations,
        load_profiles=load_profiles,
        load_accounts=load_accounts,
        aclose=aclose,
    )


def _candidate(**overrides: object) -> RankingCandidate:
    payload: dict[str, object] = {
        "candidate_id": "chunk-1",
        "kb_id": "kb-a",
        "document_id": "doc-1",
        "version": 1,
        "locator": GoldLocator(
            source_type="markdown",
            parser_version="md-v1",
            start_line=1,
            end_line=2,
        ),
        "rank": 1,
    }
    payload.update(overrides)
    return RankingCandidate.model_validate(payload)


def _outcome(created_at: str = CREATED_AT) -> ProbeOutcome:
    answer_a = AblationQuestion(
        question_id="q-answer",
        scope_id=SCOPE_A,
        latency_ms=1.0,
        candidates=[
            _candidate(vector_rank=1, vector_score=0.5),
        ],
    )
    answer_b = AblationQuestion(
        question_id="q-answer",
        scope_id=SCOPE_A,
        latency_ms=2.0,
        candidates=[
            _candidate(fusion_rank=1, fusion_score=0.4),
        ],
    )
    answer_c = AblationQuestion(
        question_id="q-answer",
        scope_id=SCOPE_A,
        latency_ms=3.0,
        candidates=[
            _candidate(fusion_rank=1, fusion_score=0.4, rerank_score=0.9),
        ],
    )
    denied_a = AblationQuestion(
        question_id="q-denied", scope_id=SCOPE_B, latency_ms=0.0, candidates=[]
    )
    a = AblationArtifact(
        dataset_kind="dev",
        dataset_version="v1",
        variant="A_VECTOR",
        created_at=created_at,
        questions=[answer_a, denied_a],
    )
    b = AblationArtifact(
        dataset_kind="dev",
        dataset_version="v1",
        variant="B_RRF",
        created_at=created_at,
        questions=[answer_b, denied_a],
    )
    c = AblationArtifact(
        dataset_kind="dev",
        dataset_version="v1",
        variant="C_RERANK",
        created_at=created_at,
        questions=[answer_c, denied_a],
    )
    calibration = (
        RefusalProbeRecord(
            question_id="q-answer",
            expected_behavior="answer",
            top_score=0.4,
            candidate_count=1,
        ),
        RefusalProbeRecord(
            question_id="q-denied",
            expected_behavior="refuse",
            candidate_count=0,
        ),
    )
    ranking = RankingReport(
        question_count=0,
        excluded_question_ids=(),
        recall_at_10=None,
        recall_covered_numerator=0,
        recall_gold_denominator=0,
        ndcg_at_10=None,
        ndcg_dcg_numerator=0.0,
        ndcg_idcg_denominator=0.0,
        failed_question_ids=(),
        questions=(),
    )
    return ProbeOutcome(
        a=a,
        b=b,
        c=c,
        calibration=calibration,
        triplet=validate_ablation_triplet(a, b, c),
        ranking=ranking,
    )


async def _ok_executor(
    runtime: ProbeRuntime, prepared: object, execution: object
) -> ProbeOutcome:
    created_at = getattr(execution, "created_at")
    return _outcome(created_at)


async def _failing_executor(
    runtime: ProbeRuntime, prepared: object, execution: object
) -> ProbeOutcome:
    raise RuntimeError("底层失败")


# ---------------------------------------------------------------------------
# fixture 写入


def _write_dataset(path: Path, *, kind: str = "dev") -> Path:
    payload = {
        "datasetKind": kind,
        "datasetVersion": "v1",
        "corpusManifest": "manifest.json",
        "questions": [
            {
                "id": "q-answer",
                "category": "single_document",
                "scope": {"role": "reader", "kbIds": ["kb-a"]},
                "question": "问题一？",
                "expectedBehavior": "answer",
                "goldAnswerPoints": ["要点"],
                "goldSourceSpans": [
                    {
                        "kbId": "kb-a",
                        "documentId": "doc-1",
                        "version": 1,
                        "quote": "引文",
                        "locator": {
                            "sourceType": "markdown",
                            "parserVersion": "md-v1",
                            "headingPath": [],
                            "startLine": 1,
                            "endLine": 2,
                        },
                    }
                ],
            },
            {
                "id": "q-denied",
                "category": "no_permission",
                "scope": {"role": "reader", "kbIds": ["kb-b"]},
                "question": "机密？",
                "expectedBehavior": "refuse",
                "unavailableDocumentIds": ["doc-secret"],
                "unavailableReason": "no_permission",
            },
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_descriptor(path: Path, *, kbs: Sequence[str] = ("kb-a", "kb-b")) -> Path:
    payload = {
        "knowledgeBases": {kb: str(uuid.uuid4()) for kb in kbs},
        "roles": {"reader": {"username": "reader-user", "password": "pw"}},
        "seedRole": "seed",
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_asset_map(path: Path, *, kbs: Sequence[str] = ("kb-a", "kb-b")) -> Path:
    payload = {
        "knowledgeBases": {kb: str(uuid.uuid4()) for kb in kbs},
        "documents": {
            "doc-1": {
                "kbId": "kb-a",
                "documentId": str(uuid.uuid4()),
                "versions": {"1": str(uuid.uuid4())},
            },
            "doc-secret": {
                "kbId": "kb-b",
                "documentId": str(uuid.uuid4()),
                "versions": {"1": str(uuid.uuid4())},
            },
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _base_args(
    tmp_path: Path,
    *,
    dataset: Path,
    descriptor: Path,
    asset_map: Path,
    out_dir: Path | None = None,
) -> list[str]:
    return [
        "--dataset",
        str(dataset),
        "--descriptor",
        str(descriptor),
        "--asset-map",
        str(asset_map),
        "--out-dir",
        str(out_dir or tmp_path),
        "--max-embedding-requests",
        "4",
        "--max-rerank-requests",
        "1",
    ]


# ---------------------------------------------------------------------------
# dry-run


def test_dry_run_exits_zero_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = _write_dataset(tmp_path / "dataset.json")
    descriptor = _write_descriptor(tmp_path / "descriptor.json")
    asset_map = _write_asset_map(tmp_path / "assets.json")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    rc = probe_main(
        _base_args(
            tmp_path, dataset=dataset, descriptor=descriptor, asset_map=asset_map, out_dir=out_dir
        ),
        environ={"EVAL_DATABASE_URL": "SHOULD_NOT_BE_READ"},
        runtime_builder=_explode_builder,
        executor=_ok_executor,
    )

    assert rc == 0
    assert list(out_dir.iterdir()) == []
    output = capsys.readouterr().out
    assert "dry-run" in output
    assert "knowledgeBases=2" in output


def test_dry_run_rejects_insufficient_budget(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = _write_dataset(tmp_path / "dataset.json")
    descriptor = _write_descriptor(tmp_path / "descriptor.json")
    asset_map = _write_asset_map(tmp_path / "assets.json")

    rc = probe_main(
        [
            "--dataset",
            str(dataset),
            "--descriptor",
            str(descriptor),
            "--asset-map",
            str(asset_map),
            "--out-dir",
            str(tmp_path),
            "--max-embedding-requests",
            "1",
            "--max-rerank-requests",
            "1",
        ]
    )

    assert rc == 1
    assert "预算" in capsys.readouterr().err


def test_dry_run_rejects_missing_role(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dataset = _write_dataset(tmp_path / "dataset.json")
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text(
        json.dumps(
            {
                "knowledgeBases": {"kb-a": str(uuid.uuid4()), "kb-b": str(uuid.uuid4())},
                "roles": {"other": {"username": "other", "password": "pw"}},
                "seedRole": "seed",
            }
        ),
        encoding="utf-8",
    )
    asset_map = _write_asset_map(tmp_path / "assets.json")

    rc = probe_main(
        _base_args(tmp_path, dataset=dataset, descriptor=descriptor, asset_map=asset_map)
    )

    assert rc == 1
    assert "角色" in capsys.readouterr().err


def test_dry_run_rejects_asset_map_missing_scope_kb(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = _write_dataset(tmp_path / "dataset.json")
    descriptor = _write_descriptor(tmp_path / "descriptor.json")
    asset_map = _write_asset_map(tmp_path / "assets.json", kbs=("kb-a",))

    rc = probe_main(
        _base_args(tmp_path, dataset=dataset, descriptor=descriptor, asset_map=asset_map)
    )

    assert rc == 1
    assert "资产映射" in capsys.readouterr().err


def test_dry_run_rejects_descriptor_missing_scope_kb(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = _write_dataset(tmp_path / "dataset.json")
    descriptor = _write_descriptor(tmp_path / "descriptor.json", kbs=("kb-a",))
    asset_map = _write_asset_map(tmp_path / "assets.json")

    rc = probe_main(
        _base_args(tmp_path, dataset=dataset, descriptor=descriptor, asset_map=asset_map)
    )

    assert rc == 1
    assert "环境描述" in capsys.readouterr().err


def test_dry_run_rejects_non_finite_timeout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = _write_dataset(tmp_path / "dataset.json")
    descriptor = _write_descriptor(tmp_path / "descriptor.json")
    asset_map = _write_asset_map(tmp_path / "assets.json")

    rc = probe_main(
        [
            *_base_args(
                tmp_path, dataset=dataset, descriptor=descriptor, asset_map=asset_map
            ),
            "--embedding-timeout-seconds",
            "nan",
        ]
    )

    assert rc == 1
    assert "有限正数" in capsys.readouterr().err


def test_dry_run_holdout_needs_no_confirm(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = _write_dataset(tmp_path / "holdout.json", kind="holdout")
    descriptor = _write_descriptor(tmp_path / "descriptor.json")
    asset_map = _write_asset_map(tmp_path / "assets.json")

    rc = probe_main(
        _base_args(tmp_path, dataset=dataset, descriptor=descriptor, asset_map=asset_map)
    )

    assert rc == 0
    assert "holdout" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 真实运行护栏


def _real_args(
    tmp_path: Path,
    *,
    dataset: Path,
    descriptor: Path,
    asset_map: Path,
    extra: Sequence[str] = (),
) -> list[str]:
    return [
        *_base_args(
            tmp_path, dataset=dataset, descriptor=descriptor, asset_map=asset_map
        ),
        "--allow-real-probe",
        "--inference-base-url",
        "http://127.0.0.1:9000",
        *extra,
    ]


def test_real_run_requires_rerank_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = _write_dataset(tmp_path / "dataset.json")
    descriptor = _write_descriptor(tmp_path / "descriptor.json")
    asset_map = _write_asset_map(tmp_path / "assets.json")

    rc = probe_main(
        _real_args(tmp_path, dataset=dataset, descriptor=descriptor, asset_map=asset_map),
        environ=TEST_ENV,
    )

    assert rc == 1
    assert "allow-real-rerank" in capsys.readouterr().err


def test_real_holdout_requires_confirmation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = _write_dataset(tmp_path / "holdout.json", kind="holdout")
    descriptor = _write_descriptor(tmp_path / "descriptor.json")
    asset_map = _write_asset_map(tmp_path / "assets.json")

    rc = probe_main(
        _real_args(
            tmp_path,
            dataset=dataset,
            descriptor=descriptor,
            asset_map=asset_map,
            extra=("--allow-real-rerank",),
        ),
        environ=TEST_ENV,
    )

    assert rc == 1
    assert "confirm-holdout" in capsys.readouterr().err


def test_real_run_requires_environment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = _write_dataset(tmp_path / "dataset.json")
    descriptor = _write_descriptor(tmp_path / "descriptor.json")
    asset_map = _write_asset_map(tmp_path / "assets.json")

    rc = probe_main(
        _real_args(
            tmp_path,
            dataset=dataset,
            descriptor=descriptor,
            asset_map=asset_map,
            extra=("--allow-real-rerank",),
        ),
        environ={},
    )

    assert rc == 1
    assert "环境变量" in capsys.readouterr().err


def test_real_run_rejects_existing_artifact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = _write_dataset(tmp_path / "dataset.json")
    descriptor = _write_descriptor(tmp_path / "descriptor.json")
    asset_map = _write_asset_map(tmp_path / "assets.json")
    (tmp_path / "a-vector.json").write_text("{}", encoding="utf-8")

    rc = probe_main(
        _real_args(
            tmp_path,
            dataset=dataset,
            descriptor=descriptor,
            asset_map=asset_map,
            extra=("--allow-real-rerank",),
        ),
        environ={},
    )

    assert rc == 1
    assert "拒绝覆盖" in capsys.readouterr().err


def test_real_run_persists_four_aliased_artifacts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = _write_dataset(tmp_path / "dataset.json")
    descriptor = _write_descriptor(tmp_path / "descriptor.json")
    asset_map = _write_asset_map(tmp_path / "assets.json")

    rc = probe_main(
        _real_args(
            tmp_path,
            dataset=dataset,
            descriptor=descriptor,
            asset_map=asset_map,
            extra=("--allow-real-rerank",),
        ),
        environ=TEST_ENV,
        runtime_builder=lambda **_: _make_runtime(),
        executor=_ok_executor,
        clock=lambda: CREATED_AT,
    )

    assert rc == 0
    for name in ARTIFACT_FILENAMES:
        assert (tmp_path / name).is_file()
    assert not list(tmp_path.glob("*.tmp")) and not list(tmp_path.glob(".*.tmp"))
    a_payload = json.loads((tmp_path / "a-vector.json").read_text(encoding="utf-8"))
    assert a_payload["datasetKind"] == "dev"
    assert a_payload["variant"] == "A_VECTOR"
    assert a_payload["questions"][0]["questionId"] == "q-answer"
    calibration = json.loads(
        (tmp_path / "calibration.json").read_text(encoding="utf-8")
    )
    assert calibration["sourceVariant"] == "B_RRF"
    assert calibration["scoreField"] == "fusionScore"
    assert calibration["createdAt"] == CREATED_AT
    assert len(calibration["records"]) == 2


def test_partial_publish_failure_removes_formal_files(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = _write_dataset(tmp_path / "dataset.json")
    descriptor = _write_descriptor(tmp_path / "descriptor.json")
    asset_map = _write_asset_map(tmp_path / "assets.json")
    real_replace = probe_cli_module.os.replace
    calls = 0

    def fail_second_replace(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic publish failure")
        real_replace(source, target)

    monkeypatch.setattr(probe_cli_module.os, "replace", fail_second_replace)
    rc = probe_main(
        _real_args(
            tmp_path,
            dataset=dataset,
            descriptor=descriptor,
            asset_map=asset_map,
            extra=("--allow-real-rerank",),
        ),
        environ=TEST_ENV,
        runtime_builder=lambda **_: _make_runtime(),
        executor=_ok_executor,
        clock=lambda: CREATED_AT,
    )

    assert rc == 1
    assert "产物写入失败" in capsys.readouterr().err
    for name in ARTIFACT_FILENAMES:
        assert not (tmp_path / name).exists()
    assert list(tmp_path.glob(".*.tmp")) == []


def test_real_run_failure_creates_no_formal_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = _write_dataset(tmp_path / "dataset.json")
    descriptor = _write_descriptor(tmp_path / "descriptor.json")
    asset_map = _write_asset_map(tmp_path / "assets.json")

    rc = probe_main(
        _real_args(
            tmp_path,
            dataset=dataset,
            descriptor=descriptor,
            asset_map=asset_map,
            extra=("--allow-real-rerank",),
        ),
        environ=TEST_ENV,
        runtime_builder=lambda **_: _make_runtime(),
        executor=_failing_executor,
    )

    assert rc == 1
    assert "运行失败" in capsys.readouterr().err
    for name in ARTIFACT_FILENAMES:
        assert not (tmp_path / name).exists()
    assert list(tmp_path.glob(".*.tmp")) == []


def test_real_run_default_executor_builds_explicit_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = _write_dataset(tmp_path / "dataset.json")
    descriptor = _write_descriptor(tmp_path / "descriptor.json")
    asset_map = _write_asset_map(tmp_path / "assets.json")
    captured: dict[str, object] = {}

    async def fake_run(inputs: ProbeInputs) -> ProbeOutcome:
        captured["inputs"] = inputs
        return _outcome(CREATED_AT)

    monkeypatch.setattr(probe_cli_module, "run_ablation_probe", fake_run)

    rc = probe_main(
        _real_args(
            tmp_path,
            dataset=dataset,
            descriptor=descriptor,
            asset_map=asset_map,
            extra=("--allow-real-rerank",),
        ),
        environ=TEST_ENV,
        runtime_builder=lambda **_: _make_full_runtime(),
        clock=lambda: CREATED_AT,
    )

    assert rc == 0
    inputs = cast(ProbeInputs, captured["inputs"])
    assert inputs.created_at == CREATED_AT
    assert inputs.config["calibrationSource"] == "B_RRF"
    assert set(inputs.role_identities) == {"reader"}
    assert inputs.model_identities["modelRevision"] == "rev-1"
    assert inputs.model_identities["queryContract"] == "bge-zh-query-v1"
