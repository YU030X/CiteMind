"""Phase 3 成本片：固定官方价目快照 + 纯离线 ``Decimal`` 成本复算。

本模块只做离线确定性复算：输入 runner 逐题原始 usage 产物与显式价目快照，按调用方
**显式给出**的 band（``peak``/``offPeak``）对每个 provider attempt 复算费用。它不判断当前
时间属于哪个 band（高峰窗口与中国法定节假日不能离线自动判定），不读环境变量、不读数据库、
不访问网络，也不回写账本或产出真实结果。

- 只有 provider/model 与快照精确匹配、``status=SUCCEEDED`` 且 cacheHit/cacheMiss/completion
  三个 token 都非空的 attempt 才计算费用；失败、超时或任一必需 token 缺失时 ``costAmount=None``
  并给出静态原因，绝不按 0 计。``promptTokens`` 不参与公式，也不假设等于 hit+miss。
- 单次费用公式 ``(hit*rateHit + miss*rateMiss + completion*rateOut) / perTokens``，统一
  ``quantize(0.00000001, ROUND_HALF_UP)``；汇总的 ``knownCostAmount`` 先对可计算 attempt 的
  原始 ``Decimal`` 求和、再统一 quantize，避免逐项舍入误差。
- 所有金额都是 ``Decimal``，JSON 序列化为固定 8 位小数字符串，绝不经过 float。

离线入口 ``python -m rag_backend.evaluation.costs``：不读环境/DB/网络，输入严格校验，
输出拒绝覆盖、唯一临时文件 + ``os.replace``，错误静态且不打印 traceback。价目快照路径
没有默认值，必须显式传入，避免误用旧价。本产物是**估算快照复算，不是账单**。
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    model_validator,
)
from pydantic.alias_generators import to_camel

from rag_backend.evaluation.usage_artifact import (
    RunnerUsageArtifact,
    UsageAttempt,
    UsageStage,
    UsageStatus,
)

EXPECTED_PROVIDER = "deepseek"
EXPECTED_MODEL = "deepseek-flash"
EXPECTED_SOURCE_URL = "https://api-docs.deepseek.com/quick_start/pricing/"

Band = Literal["peak", "offPeak"]
CostReason = Literal[
    "OK",
    "NOT_SUCCEEDED",
    "MISSING_TOKENS",
    "PROVIDER_MISMATCH",
    "MODEL_MISMATCH",
]

_QUANTUM = Decimal("0.00000001")


class _Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


def _reject_float(value: object) -> object:
    """价格只接受 ``Decimal`` 或十进制字符串；``float``/``int``/``bool`` 一律拒绝。"""

    if isinstance(value, bool) or isinstance(value, (float, int)):
        raise ValueError("价格必须是 Decimal 字符串，拒绝 float/int/bool")
    return value


def _finite_non_negative(value: Decimal) -> Decimal:
    if not value.is_finite() or value < 0:
        raise ValueError("价格必须是有限非负 Decimal")
    return value


PriceDecimal = Annotated[
    Decimal, BeforeValidator(_reject_float), AfterValidator(_finite_non_negative)
]

# 价格金额对外 JSON 固定 8 位小数字符串，避免 Decimal 科学计数法与 float。
FixedAmount = Annotated[
    Decimal,
    PlainSerializer(lambda value: format(value, ".8f"), return_type=str, when_used="json"),
]


class BandRates(_Model):
    """单个 band 的每 ``perTokens`` 个 token 的 USD 单价。"""

    input_cache_hit: PriceDecimal
    input_cache_miss: PriceDecimal
    output: PriceDecimal


class PriceBands(_Model):
    off_peak: BandRates
    peak: BandRates


class PeakWindow(_Model):
    """UTC 工作日高峰窗口（``HH:MM`` 开区间语义由官方定义，这里只记录结构）。"""

    start: str = Field(pattern=r"^\d{2}:\d{2}$")
    end: str = Field(pattern=r"^\d{2}:\d{2}$")


class SelectionRule(_Model):
    """官方 band 窗口与节假日例外；operator 仍须显式选择 band。"""

    timezone: Literal["UTC"]
    peak_weekday_windows: list[PeakWindow] = Field(min_length=1)
    excluded_holidays: str = Field(min_length=1)
    default_band: Band
    note: str = Field(min_length=1)


class PriceSnapshot(_Model):
    """固定来源与核对时点的价目快照；不伪造页面未给出的 effective date。"""

    snapshot_version: Literal["citemind-price-1"]
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    observed_at: datetime
    source_url: str = Field(min_length=1)
    currency: Literal["USD"]
    per_tokens: int = Field(gt=0, strict=True)
    bands: PriceBands
    selection_rule: SelectionRule

    @model_validator(mode="after")
    def _check_observed_at(self) -> PriceSnapshot:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observedAt 必须带时区")
        return self


class CostTotals(_Model):
    """一组 attempt 的已知费用汇总；未知 attempt 不按 0 计入已知金额。"""

    known_cost_amount: FixedAmount
    unknown_cost_attempt_count: int = Field(ge=0, strict=True)
    costed_attempt_count: int = Field(ge=0, strict=True)


class QuestionCostTotal(_Model):
    """单题费用汇总；与 ``CostTotals`` 同结构，另带 ``questionId``。"""

    question_id: str = Field(min_length=1)
    known_cost_amount: FixedAmount
    unknown_cost_attempt_count: int = Field(ge=0, strict=True)
    costed_attempt_count: int = Field(ge=0, strict=True)


class AttemptCost(_Model):
    """一次 provider attempt 的 token 事实与复算费用；无法计算时 ``costAmount`` 为空。"""

    usage_id: uuid.UUID
    provider: str = Field(min_length=1)
    stage: UsageStage
    status: UsageStatus
    model: str = Field(min_length=1)
    prompt_tokens: int | None = Field(default=None, ge=0, strict=True)
    completion_tokens: int | None = Field(default=None, ge=0, strict=True)
    prompt_cache_hit_tokens: int | None = Field(default=None, ge=0, strict=True)
    prompt_cache_miss_tokens: int | None = Field(default=None, ge=0, strict=True)
    latency_ms: int | None = Field(default=None, ge=0, strict=True)
    cost_amount: FixedAmount | None
    reason: CostReason


class RunCost(_Model):
    question_id: str = Field(min_length=1)
    query_run_id: uuid.UUID
    turn_index: int = Field(ge=0, strict=True)
    is_final_question: bool
    attempts: list[AttemptCost]


class CostArtifact(_Model):
    """逐题逐 attempt 的离线成本复算产物；含使用量与价目身份，便于复算。"""

    dataset_kind: Literal["dev", "holdout"]
    dataset_version: str = Field(min_length=1)
    usage_complete: bool
    price_snapshot: PriceSnapshot
    selected_band: Band
    currency: Literal["USD"]
    runs: list[RunCost]
    totals: CostTotals
    final_question_only: CostTotals
    per_question: list[QuestionCostTotal]


@dataclass
class _Accumulator:
    """原始（未 quantize）Decimal 求和器；汇总时统一 quantize。"""

    raw: Decimal = field(default_factory=lambda: Decimal(0))
    costed: int = 0
    unknown: int = 0

    def add(self, raw_value: Decimal | None) -> None:
        if raw_value is None:
            self.unknown += 1
        else:
            self.raw += raw_value
            self.costed += 1

    def totals(self) -> CostTotals:
        return CostTotals(
            known_cost_amount=self.raw.quantize(_QUANTUM, rounding=ROUND_HALF_UP),
            unknown_cost_attempt_count=self.unknown,
            costed_attempt_count=self.costed,
        )


def _rates_for(snapshot: PriceSnapshot, band: Band) -> BandRates:
    return snapshot.bands.peak if band == "peak" else snapshot.bands.off_peak


def _raw_cost(
    attempt: UsageAttempt, snapshot: PriceSnapshot, band: Band
) -> tuple[CostReason, Decimal | None]:
    """返回 (静态原因, 未 quantize 的费用)；不可计算时费用为 ``None``。"""

    if attempt.provider != snapshot.provider:
        return "PROVIDER_MISMATCH", None
    if attempt.model != snapshot.model:
        return "MODEL_MISMATCH", None
    if attempt.status != "SUCCEEDED":
        return "NOT_SUCCEEDED", None
    hit = attempt.prompt_cache_hit_tokens
    miss = attempt.prompt_cache_miss_tokens
    completion = attempt.completion_tokens
    if hit is None or miss is None or completion is None:
        return "MISSING_TOKENS", None
    rates = _rates_for(snapshot, band)
    raw = (
        hit * rates.input_cache_hit
        + miss * rates.input_cache_miss
        + completion * rates.output
    ) / Decimal(snapshot.per_tokens)
    return "OK", raw


def _attempt_cost(
    attempt: UsageAttempt, snapshot: PriceSnapshot, band: Band
) -> tuple[AttemptCost, Decimal | None]:
    reason, raw = _raw_cost(attempt, snapshot, band)
    cost = None if raw is None else raw.quantize(_QUANTUM, rounding=ROUND_HALF_UP)
    return (
        AttemptCost(
            usage_id=attempt.usage_id,
            provider=attempt.provider,
            stage=attempt.stage,
            status=attempt.status,
            model=attempt.model,
            prompt_tokens=attempt.prompt_tokens,
            completion_tokens=attempt.completion_tokens,
            prompt_cache_hit_tokens=attempt.prompt_cache_hit_tokens,
            prompt_cache_miss_tokens=attempt.prompt_cache_miss_tokens,
            latency_ms=attempt.latency_ms,
            cost_amount=cost,
            reason=reason,
        ),
        raw,
    )


def build_cost_artifact(
    usage: RunnerUsageArtifact,
    snapshot: PriceSnapshot,
    band: Band,
) -> CostArtifact:
    """按显式 band 对 usage 产物逐 attempt 复算费用，并汇总为逐题/全量/仅最终题。"""

    runs: list[RunCost] = []
    overall = _Accumulator()
    final_only = _Accumulator()
    per_question: dict[str, _Accumulator] = {}
    order: list[str] = []
    for run in usage.runs:
        accumulator = per_question.get(run.question_id)
        if accumulator is None:
            accumulator = _Accumulator()
            per_question[run.question_id] = accumulator
            order.append(run.question_id)
        attempts: list[AttemptCost] = []
        for attempt in run.usage:
            cost, raw = _attempt_cost(attempt, snapshot, band)
            attempts.append(cost)
            overall.add(raw)
            accumulator.add(raw)
            if run.is_final_question:
                final_only.add(raw)
        runs.append(
            RunCost(
                question_id=run.question_id,
                query_run_id=run.query_run_id,
                turn_index=run.turn_index,
                is_final_question=run.is_final_question,
                attempts=attempts,
            )
        )
    per_question_totals: list[QuestionCostTotal] = []
    for question_id in order:
        accumulator = per_question[question_id]
        per_question_totals.append(
            QuestionCostTotal(
                question_id=question_id,
                known_cost_amount=accumulator.raw.quantize(
                    _QUANTUM, rounding=ROUND_HALF_UP
                ),
                unknown_cost_attempt_count=accumulator.unknown,
                costed_attempt_count=accumulator.costed,
            )
        )
    return CostArtifact(
        dataset_kind=usage.dataset_kind,
        dataset_version=usage.dataset_version,
        usage_complete=usage.complete,
        price_snapshot=snapshot,
        selected_band=band,
        currency=snapshot.currency,
        runs=runs,
        totals=overall.totals(),
        final_question_only=final_only.totals(),
        per_question=per_question_totals,
    )


# ---------------------------------------------------------------------------
# 纯离线 CLI


class CostCliError(Exception):
    """CLI 静态错误；消息不回显 token/UUID/DSN，也不打印 traceback。"""


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m rag_backend.evaluation.costs",
        description="离线复算 runner usage 产物的 USD 成本；不联网、不读库、不读环境文件。",
    )
    parser.add_argument("--usage", type=Path, required=True, help="runner usage 产物 JSON")
    parser.add_argument(
        "--price-snapshot",
        type=Path,
        required=True,
        help="显式价目快照 JSON（无默认值，避免误用旧价）",
    )
    parser.add_argument("--band", choices=["peak", "offPeak"], required=True)
    parser.add_argument("--out", type=Path, required=True, help="输出 JSON（拒绝覆盖已存在文件）")
    return parser.parse_args(argv)


def _load_usage(path: Path) -> RunnerUsageArtifact:
    try:
        return RunnerUsageArtifact.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CostCliError("usage 产物不可读或非法") from error


def _load_snapshot(path: Path) -> PriceSnapshot:
    try:
        snapshot = PriceSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CostCliError("价目快照不可读或非法") from error
    if (
        snapshot.provider != EXPECTED_PROVIDER
        or snapshot.model != EXPECTED_MODEL
        or snapshot.source_url != EXPECTED_SOURCE_URL
    ):
        raise CostCliError("价目快照来源与目标 provider/model 不匹配")
    return snapshot


def _write_artifact(path: Path, text: str) -> None:
    if path.exists():
        raise CostCliError("输出已存在同名文件，拒绝覆盖")
    temp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
    except OSError as error:
        try:
            temp.unlink()
        except OSError:
            pass
        raise CostCliError("成本产物写入失败") from error


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        usage = _load_usage(args.usage)
        snapshot = _load_snapshot(args.price_snapshot)
        artifact = build_cost_artifact(usage, snapshot, args.band)
        text = artifact.model_dump_json(by_alias=True, indent=2) + "\n"
        _write_artifact(args.out, text)
    except CostCliError as error:
        print(f"成本复算失败：{error}", file=sys.stderr)
        return 1
    print(f"成本产物已写出：{args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "AttemptCost",
    "Band",
    "BandRates",
    "CostArtifact",
    "CostCliError",
    "CostReason",
    "CostTotals",
    "PeakWindow",
    "PriceBands",
    "PriceSnapshot",
    "QuestionCostTotal",
    "RunCost",
    "SelectionRule",
    "build_cost_artifact",
    "main",
]
