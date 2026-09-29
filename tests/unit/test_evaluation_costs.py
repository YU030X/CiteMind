"""Phase 3 成本片纯离线复算的聚焦单测：不联网、不读库、不调用模型。

覆盖严格 ``Decimal`` 价目 schema（拒 float/额外字段）、provider/model 匹配、两 band 官方已知
单价、混合 token 公式与 8 位 ``ROUND_HALF_UP``、失败/缺 token 记未知而非 0、汇总对抗逐项
舍入、仅最终题与逐题汇总，以及 ``python -m rag_backend.evaluation.costs`` 的原子落盘/拒覆盖/
非法输入零输出。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from rag_backend.evaluation.costs import (
    CostArtifact,
    PriceSnapshot,
    build_cost_artifact,
)
from rag_backend.evaluation.costs import main as costs_main
from rag_backend.evaluation.runner import AskRunRecord
from rag_backend.evaluation.runner_adapters import UsageAttemptRow
from rag_backend.evaluation.usage_artifact import (
    RunnerUsageArtifact,
    build_runner_usage_artifact,
)

PRICE_PATH = (
    Path(__file__).resolve().parents[1]
    / "evaluation"
    / "pricing"
    / "deepseek-flash-usd-2026-09-29.json"
)


def _snapshot_payload() -> dict[str, object]:
    return json.loads(PRICE_PATH.read_text(encoding="utf-8"))


def _snapshot() -> PriceSnapshot:
    return PriceSnapshot.model_validate_json(PRICE_PATH.read_text(encoding="utf-8"))


def _record(
    question_id: str,
    turn_index: int,
    is_final_question: bool,
    *,
    query_run_id: uuid.UUID | None = None,
) -> AskRunRecord:
    return AskRunRecord(
        question_id=question_id,
        conversation_id=uuid.uuid4(),
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
        "provider": "deepseek",
        "model": "deepseek-flash",
        "attempt": 1,
        "error_code": None,
        "prompt_tokens": 1,
        "completion_tokens": 0,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 0,
        "latency_ms": 10,
    }
    payload.update(overrides)
    return UsageAttemptRow(query_run_id=query_run_id, **payload)  # type: ignore[arg-type]


def _artifact(
    records: list[AskRunRecord],
    rows: list[UsageAttemptRow],
    *,
    complete: bool = True,
) -> RunnerUsageArtifact:
    return build_runner_usage_artifact(
        records, rows, dataset_kind="dev", dataset_version="citemind-eval-dev-2", complete=complete
    )


def _costs(records: list[AskRunRecord], rows: list[UsageAttemptRow]) -> CostArtifact:
    return build_cost_artifact(_artifact(records, rows), _snapshot(), "offPeak")


# ---------------------------------------------------------------------------
# 价目 schema


def test_committed_snapshot_matches_official_facts() -> None:
    snapshot = _snapshot()
    assert snapshot.snapshot_version == "citemind-price-1"
    assert (snapshot.provider, snapshot.model, snapshot.model_version) == (
        "deepseek",
        "deepseek-flash",
        "DeepSeek-V4.1-Flash",
    )
    assert snapshot.source_url == "https://api-docs.deepseek.com/quick_start/pricing/"
    assert snapshot.currency == "USD"
    assert snapshot.per_tokens == 1000000
    assert snapshot.bands.off_peak == snapshot.bands.off_peak.model_validate(
        {"inputCacheHit": "0.003", "inputCacheMiss": "0.15", "output": "0.6"}
    )
    assert snapshot.bands.peak == snapshot.bands.peak.model_validate(
        {"inputCacheHit": "0.006", "inputCacheMiss": "0.3", "output": "1.2"}
    )
    assert snapshot.selection_rule.timezone == "UTC"
    assert [
        (window.start, window.end) for window in snapshot.selection_rule.peak_weekday_windows
    ] == [
        ("01:00", "04:00"),
        ("06:00", "10:00"),
    ]
    assert snapshot.selection_rule.default_band == "offPeak"
    # 页面未给出 effective date，快照不得伪造；observedAt 只表示项目核对时点。
    assert snapshot.observed_at == datetime(2026, 9, 29, 17, 39, 3, tzinfo=UTC)


def test_price_snapshot_rejects_float_and_extra_fields() -> None:
    payload = _snapshot_payload()
    bands = payload["bands"]
    assert isinstance(bands, dict)
    off_peak = bands["offPeak"]
    assert isinstance(off_peak, dict)
    off_peak["inputCacheHit"] = 0.003
    with pytest.raises(ValidationError):
        PriceSnapshot.model_validate(payload)

    payload = _snapshot_payload()
    payload["extraField"] = "x"
    with pytest.raises(ValidationError):
        PriceSnapshot.model_validate(payload)

    payload = _snapshot_payload()
    bands = payload["bands"]
    assert isinstance(bands, dict)
    off_peak = bands["offPeak"]
    assert isinstance(off_peak, dict)
    off_peak["inputCacheHit"] = "not-a-decimal"
    with pytest.raises(ValidationError):
        PriceSnapshot.model_validate(payload)


# ---------------------------------------------------------------------------
# 官方已知单价与公式


@pytest.mark.parametrize(
    ("band", "expected"),
    [
        ("offPeak", ("0.003", "0.15", "0.6")),
        ("peak", ("0.006", "0.3", "1.2")),
    ],
)
def test_official_rates_for_one_million_tokens(
    band: str, expected: tuple[str, str, str]
) -> None:
    record = _record("q1", 0, True)
    rows = [
        _row(
            record.query_run_id,
            created_at=datetime(2026, 9, 29, 0, 0, tzinfo=UTC),
            prompt_cache_hit_tokens=1_000_000,
            prompt_tokens=999_999,
        ),
        _row(
            record.query_run_id,
            created_at=datetime(2026, 9, 29, 0, 1, tzinfo=UTC),
            prompt_cache_miss_tokens=1_000_000,
            prompt_tokens=1,
        ),
        _row(
            record.query_run_id,
            created_at=datetime(2026, 9, 29, 0, 2, tzinfo=UTC),
            completion_tokens=1_000_000,
            prompt_tokens=123,
        ),
    ]
    artifact = build_cost_artifact(_artifact([record], rows), _snapshot(), band)  # type: ignore[arg-type]
    amounts = [attempt.cost_amount for attempt in artifact.runs[0].attempts]
    assert amounts == [Decimal(value) for value in expected]
    # promptTokens 不参与公式。
    assert artifact.runs[0].attempts[0].prompt_tokens == 999_999


def test_mixed_tokens_use_eight_digit_round_half_up() -> None:
    record = _record("q1", 0, True)
    down = _row(
        record.query_run_id,
        created_at=datetime(2026, 9, 29, 0, 0, tzinfo=UTC),
        prompt_cache_hit_tokens=1,
    )
    half_up = _row(
        record.query_run_id,
        created_at=datetime(2026, 9, 29, 0, 1, tzinfo=UTC),
        prompt_cache_hit_tokens=5,
    )
    mixed = _row(
        record.query_run_id,
        created_at=datetime(2026, 9, 29, 0, 2, tzinfo=UTC),
        prompt_cache_hit_tokens=1,
        prompt_cache_miss_tokens=1,
        completion_tokens=1,
    )
    artifact = build_cost_artifact(
        _artifact([record], [down, half_up, mixed]), _snapshot(), "offPeak"
    )
    reasons = [attempt.reason for attempt in artifact.runs[0].attempts]
    assert reasons == ["OK", "OK", "OK"]
    amounts = [attempt.cost_amount for attempt in artifact.runs[0].attempts]
    # 3e-9 向下；1.5e-8 恰好半值向上；0.000000753 正常 8 位舍入。
    assert amounts == [
        Decimal("0.00000000"),
        Decimal("0.00000002"),
        Decimal("0.00000075"),
    ]


# ---------------------------------------------------------------------------
# 未知费用


def test_failed_or_missing_tokens_are_unknown_not_zero() -> None:
    record = _record("q1", 0, True)
    cases = {
        "failed": (
            _row(record.query_run_id, status="FAILED", error_code="HTTP_500"),
            "NOT_SUCCEEDED",
        ),
        "timeout": (
            _row(record.query_run_id, status="TIMEOUT", error_code="TIMEOUT"),
            "NOT_SUCCEEDED",
        ),
        "missing_hit": (
            _row(record.query_run_id, prompt_cache_hit_tokens=None),
            "MISSING_TOKENS",
        ),
        "missing_miss": (
            _row(record.query_run_id, prompt_cache_miss_tokens=None),
            "MISSING_TOKENS",
        ),
        "missing_completion": (
            _row(record.query_run_id, completion_tokens=None),
            "MISSING_TOKENS",
        ),
        "wrong_provider": (_row(record.query_run_id, provider="other"), "PROVIDER_MISMATCH"),
        "wrong_model": (_row(record.query_run_id, model="deepseek-v4-pro"), "MODEL_MISMATCH"),
    }
    rows = [row for row, _ in cases.values()]
    artifact = build_cost_artifact(_artifact([record], rows), _snapshot(), "offPeak")
    attempts = artifact.runs[0].attempts
    by_usage = {attempt.usage_id: attempt for attempt in attempts}
    for row, expected_reason in cases.values():
        assert by_usage[row.usage_id].reason == expected_reason
        assert by_usage[row.usage_id].cost_amount is None
    assert artifact.totals.known_cost_amount == Decimal("0.00000000")
    assert artifact.totals.costed_attempt_count == 0
    assert artifact.totals.unknown_cost_attempt_count == 7


def test_totals_sum_raw_decimals_before_quantizing() -> None:
    record = _record("q1", 0, True)
    # 每次 5 个 hit token：raw = 15e-9，单项 ROUND_HALF_UP 到 2e-8；10 项逐项相加 = 2e-7，
    # 但原始和 150e-9 = 1.5e-7 才是正确结果。
    rows = [_row(record.query_run_id, prompt_cache_hit_tokens=5) for _ in range(10)]
    artifact = build_cost_artifact(_artifact([record], rows), _snapshot(), "offPeak")
    per_item = sum(
        (
            attempt.cost_amount
            for attempt in artifact.runs[0].attempts
            if attempt.cost_amount is not None
        ),
        Decimal(0),
    )
    assert per_item == Decimal("0.00000020")
    assert artifact.totals.known_cost_amount == Decimal("0.00000015")
    assert artifact.totals.costed_attempt_count == 10
    assert artifact.totals.unknown_cost_attempt_count == 0


# ---------------------------------------------------------------------------
# 仅最终题与逐题


def test_final_only_and_per_question_totals() -> None:
    history = _record("q1", 0, False)
    final = _record("q1", 1, True)
    other = _record("q2", 0, True)
    rows = [
        _row(history.query_run_id, prompt_cache_hit_tokens=1_000_000),
        _row(final.query_run_id, prompt_cache_miss_tokens=1_000_000),
        _row(other.query_run_id, completion_tokens=1_000_000),
    ]
    artifact = build_cost_artifact(_artifact([history, final, other], rows), _snapshot(), "offPeak")
    assert artifact.totals.known_cost_amount == Decimal("0.75300000")
    assert artifact.final_question_only.known_cost_amount == Decimal("0.75000000")
    assert artifact.final_question_only.costed_attempt_count == 2
    assert [
        (item.question_id, item.known_cost_amount) for item in artifact.per_question
    ] == [
        ("q1", Decimal("0.15300000")),
        ("q2", Decimal("0.60000000")),
    ]
    assert artifact.usage_complete is True
    assert artifact.selected_band == "offPeak"
    assert artifact.currency == "USD"


def test_usage_provider_is_passed_through_to_cost_attempts() -> None:
    record = _record("q1", 0, True)
    artifact = _costs([record], [_row(record.query_run_id)])
    assert artifact.runs[0].attempts[0].provider == "deepseek"
    assert artifact.price_snapshot.provider == "deepseek"


# ---------------------------------------------------------------------------
# CLI


def _write_usage(tmp_path: Path, rows: list[UsageAttemptRow], records: list[AskRunRecord]) -> Path:
    path = tmp_path / "usage.json"
    path.write_text(
        _artifact(records, rows).model_dump_json(by_alias=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def test_cli_writes_fixed_eight_digit_strings(tmp_path: Path) -> None:
    record = _record("q1", 0, True)
    usage_path = _write_usage(
        tmp_path, [_row(record.query_run_id, prompt_cache_hit_tokens=5)], [record]
    )
    out = tmp_path / "costs.json"
    rc = costs_main(
        [
            "--usage",
            str(usage_path),
            "--price-snapshot",
            str(PRICE_PATH),
            "--band",
            "offPeak",
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    attempt = payload["runs"][0]["attempts"][0]
    assert attempt["costAmount"] == "0.00000002"
    assert attempt["reason"] == "OK"
    assert payload["totals"]["knownCostAmount"] == "0.00000002"
    assert payload["selectedBand"] == "offPeak"
    assert payload["priceSnapshot"]["sourceUrl"] == (
        "https://api-docs.deepseek.com/quick_start/pricing/"
    )


def test_cli_refuses_to_overwrite_existing_output(tmp_path: Path) -> None:
    record = _record("q1", 0, True)
    usage_path = _write_usage(tmp_path, [_row(record.query_run_id)], [record])
    out = tmp_path / "costs.json"
    out.write_text("existing", encoding="utf-8")
    rc = costs_main(
        [
            "--usage",
            str(usage_path),
            "--price-snapshot",
            str(PRICE_PATH),
            "--band",
            "peak",
            "--out",
            str(out),
        ]
    )
    assert rc == 1
    assert out.read_text(encoding="utf-8") == "existing"


def test_cli_invalid_input_writes_no_output(tmp_path: Path) -> None:
    usage_path = tmp_path / "usage.json"
    usage_path.write_text("{ not json", encoding="utf-8")
    out = tmp_path / "costs.json"
    rc = costs_main(
        [
            "--usage",
            str(usage_path),
            "--price-snapshot",
            str(PRICE_PATH),
            "--band",
            "offPeak",
            "--out",
            str(out),
        ]
    )
    assert rc == 1
    assert not out.exists()


def test_cli_rejects_snapshot_with_unknown_source(tmp_path: Path) -> None:
    snapshot_payload = _snapshot_payload()
    snapshot_payload["sourceUrl"] = "https://example.invalid/pricing"
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot_payload), encoding="utf-8")
    record = _record("q1", 0, True)
    usage_path = _write_usage(tmp_path, [_row(record.query_run_id)], [record])
    out = tmp_path / "costs.json"
    rc = costs_main(
        [
            "--usage",
            str(usage_path),
            "--price-snapshot",
            str(snapshot_path),
            "--band",
            "offPeak",
            "--out",
            str(out),
        ]
    )
    assert rc == 1
    assert not out.exists()


def test_cli_requires_explicit_snapshot_path() -> None:
    with pytest.raises(SystemExit):
        costs_main(["--usage", "u.json", "--band", "offPeak", "--out", "o.json"])


def test_cost_artifact_round_trips_as_strings() -> None:
    record = _record("q1", 0, True)
    artifact = _costs([record], [_row(record.query_run_id)])
    dumped = artifact.model_dump_json(by_alias=True)
    assert isinstance(CostArtifact.model_validate_json(dumped), CostArtifact)
    payload = json.loads(dumped)
    assert isinstance(payload["runs"][0]["attempts"][0]["costAmount"], str)
