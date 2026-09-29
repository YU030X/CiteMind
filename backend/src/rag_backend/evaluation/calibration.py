"""拒答阈值的纯离线标定扫描（不检索、不联网、不调用模型）。

输入是每题观察到的有限 top score 与候选数；本模块只在**由观察到的有限候选 score 构成的阈值
集合**上扫描，不引入模型也不产生“模拟得分”。固定规则：

- 预测：``topScore < t`` 或当日志 ``candidateCount == 0``（``topScore`` 为空）时，预测该题拒答；
  否则预测作答。阈值比较是严格小于，因此 ``topScore == t`` 视为作答。
- 阈值集合：所有观察到的有限 ``topScore`` 去重升序，并各取其两侧确定性边界
  （``nextafter(min, -inf)`` 与 ``nextafter(max, +inf)``），覆盖“全不拒答”与“全拒答”两种极端；
  没有可观察 score 时阈值集合为空。
- 指标只在 ``expectedBehavior`` 上计算：``refusalAccuracy`` 分母是应拒答题数，
  ``falseRefusalRate`` 分母是应作答题数，``balancedAccuracy = (refusalAccuracy + (1 -
  falseRefusalRate)) / 2``；空分母返回 ``None``。``actualBehavior`` 只作人工审计记录，不参与
  阈值指标。
- **只用开发集选点**：``select_dev_threshold`` 按最高 ``balancedAccuracy`` 选点，只应传开发集；
  留出集只能用 ``evaluate_refusal_threshold`` 报告预先固定的点，不得在留出集上挑点。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

Behavior = Literal["answer", "refuse"]
CalibrationVariant = Literal["B_RRF"]
CalibrationScoreField = Literal["fusionScore"]


class CalibrationInputError(Exception):
    """标定输入不满足契约时抛出（重复题目 id 等）。"""


class _Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class RefusalProbeRecord(_Model):
    """一次拒答探针记录；``topScore`` 可空，``candidateCount`` 必须非负。"""

    question_id: str = Field(min_length=1)
    expected_behavior: Behavior
    top_score: float | None = None
    candidate_count: int = Field(ge=0)
    actual_behavior: Behavior | None = None

    @model_validator(mode="after")
    def _check_finite(self) -> RefusalProbeRecord:
        if self.top_score is not None and not math.isfinite(self.top_score):
            raise ValueError(f"[{self.question_id}] topScore 必须是有限数")
        if self.candidate_count == 0 and self.top_score is not None:
            raise ValueError(f"[{self.question_id}] candidateCount=0 时 topScore 必须为空")
        if self.candidate_count > 0 and self.top_score is None:
            raise ValueError(f"[{self.question_id}] candidateCount>0 时必须提供 topScore")
        return self


class CalibrationArtifact(_Model):
    """一次拒答标定探针的落盘产物；来源与计分口径固定为 B 的 RRF 融合分。

    只描述**已记录**的观察值，不生成阈值、不挑选最优阈值，也不产生任何真实指标。重复
    ``questionId`` 直接拒绝，避免同一题被重复计分。
    """

    dataset_kind: Literal["dev", "holdout"]
    dataset_version: str = Field(min_length=1)
    source_variant: CalibrationVariant = "B_RRF"
    score_field: CalibrationScoreField = "fusionScore"
    created_at: str = Field(min_length=1)
    records: list[RefusalProbeRecord] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_unique_records(self) -> CalibrationArtifact:
        ids = [record.question_id for record in self.records]
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        if duplicates:
            raise ValueError(f"重复的题目 id：{', '.join(duplicates)}")
        return self


@dataclass(frozen=True)
class CalibrationPoint:
    """一个阈值下的拒答标定结果，附分子/分母；空分母对应 ``None``。"""

    threshold: float
    refuse_correct_numerator: int
    refuse_denominator: int
    refusal_accuracy: float | None
    false_refusal_numerator: int
    answer_denominator: int
    false_refusal_rate: float | None
    balanced_accuracy: float | None


def threshold_grid(records: Sequence[RefusalProbeRecord]) -> tuple[float, ...]:
    """由观察到的有限 score 构造含边界的升序阈值集合。"""

    _assert_unique_ids(records)
    scores = sorted({record.top_score for record in records if record.top_score is not None})
    if not scores:
        return ()
    return (
        math.nextafter(scores[0], -math.inf),
        *scores,
        math.nextafter(scores[-1], math.inf),
    )


def evaluate_refusal_threshold(
    records: Sequence[RefusalProbeRecord], threshold: float
) -> CalibrationPoint:
    """在固定阈值上报告指标；用于留出集只报告、不挑点。"""

    _assert_unique_ids(records)
    if not math.isfinite(threshold):
        raise CalibrationInputError("threshold 必须是有限数")

    refuse_denominator = sum(1 for record in records if record.expected_behavior == "refuse")
    refuse_correct = sum(
        1
        for record in records
        if record.expected_behavior == "refuse" and _predict_refuse(record, threshold)
    )
    answer_denominator = sum(1 for record in records if record.expected_behavior == "answer")
    false_refusal = sum(
        1
        for record in records
        if record.expected_behavior == "answer" and _predict_refuse(record, threshold)
    )
    refusal_accuracy = _ratio(refuse_correct, refuse_denominator)
    false_refusal_rate = _ratio(false_refusal, answer_denominator)
    balanced_accuracy = _balanced(refusal_accuracy, false_refusal_rate)
    return CalibrationPoint(
        threshold=threshold,
        refuse_correct_numerator=refuse_correct,
        refuse_denominator=refuse_denominator,
        refusal_accuracy=refusal_accuracy,
        false_refusal_numerator=false_refusal,
        answer_denominator=answer_denominator,
        false_refusal_rate=false_refusal_rate,
        balanced_accuracy=balanced_accuracy,
    )


def scan_refusal_thresholds(
    records: Sequence[RefusalProbeRecord],
) -> tuple[CalibrationPoint, ...]:
    """按升序阈值返回全部扫描点；排序规则固定为阈值升序。"""

    return tuple(evaluate_refusal_threshold(records, value) for value in threshold_grid(records))


def select_dev_threshold(
    points: Sequence[CalibrationPoint],
) -> CalibrationPoint | None:
    """在开发集扫描点中按 ``balancedAccuracy`` 最大选点，平局取最低阈值。

    该函数只应传入开发集扫描结果。留出集不得调用它挑点，只能用 ``evaluate_refusal_threshold``
    报告预先固定的阈值。
    """

    usable = [point for point in points if point.balanced_accuracy is not None]
    if not usable:
        return None
    return min(usable, key=lambda point: (-_require(point.balanced_accuracy), point.threshold))


def _require(value: float | None) -> float:
    assert value is not None
    return value


def _predict_refuse(record: RefusalProbeRecord, threshold: float) -> bool:
    return record.top_score is None or record.top_score < threshold


def _assert_unique_ids(records: Sequence[RefusalProbeRecord]) -> None:
    ids = [record.question_id for record in records]
    duplicates = sorted({item for item in ids if ids.count(item) > 1})
    if duplicates:
        raise CalibrationInputError(f"重复的题目 id：{', '.join(duplicates)}")


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def _balanced(refusal_accuracy: float | None, false_refusal_rate: float | None) -> float | None:
    if refusal_accuracy is None or false_refusal_rate is None:
        return None
    return (refusal_accuracy + (1.0 - false_refusal_rate)) / 2.0
