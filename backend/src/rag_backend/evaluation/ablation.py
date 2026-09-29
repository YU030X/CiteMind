"""A/B/C 三组消融产物的严格 schema 与三元组校验（纯离线，不检索、不联网、不伪造数据）。

产物只描述**已记录**的候选、时延与降级阶段，本模块不生成候选也不补默认值。三组定义：

- ``A_VECTOR``：仅向量路。候选只允许 ``vectorRank``/``vectorScore``，最终 ``rank`` 必须等于
  ``vectorRank``；不得出现关键词、融合或重排字段。
- ``B_RRF``：向量 + 关键词 + RRF。候选必须有 ``fusionRank``/``fusionScore`` 且无 ``rerankScore``；
  最终 ``rank`` 必须等于 ``fusionRank``。
- ``C_RERANK``：B 之上可加重排。候选必须有融合字段；``rerankScore`` 可选。若本题
  ``degradedStages`` 含 ``rerank_unavailable``，则 ``rerankScore`` 必须为空且候选最终顺序必须与
  B 基线完全相同。

``validate_ablation_triplet`` 只做确定性检查：三组题目集合一致、dataset 元数据一致、每题授权
scope 标识一致、C 降级题的最终顺序等于 B。它不判定候选是否真的由模型产生，也不测量真实质量。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from rag_backend.evaluation.ranking_metrics import RankingCandidate

AblationVariant = Literal["A_VECTOR", "B_RRF", "C_RERANK"]
DegradedStage = Literal["rerank_unavailable", "vector_unavailable", "keyword_unavailable"]
RERANK_UNAVAILABLE: DegradedStage = "rerank_unavailable"


class AblationValidationError(Exception):
    """三元组不满足一致性契约时抛出。"""


class _Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class AblationQuestion(_Model):
    """一组消融中的一道题：授权 scope 标识、时延、降级阶段与候选。"""

    question_id: str = Field(min_length=1)
    scope_id: str = Field(min_length=1)
    latency_ms: float = Field(ge=0)
    degraded_stages: list[DegradedStage] = Field(default_factory=list)
    candidates: list[RankingCandidate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_candidates(self) -> AblationQuestion:
        if not math.isfinite(self.latency_ms):
            raise ValueError(f"[{self.question_id}] latencyMs 必须是有限数")
        ids = [candidate.candidate_id for candidate in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError(f"[{self.question_id}] candidateId 不能重复")
        ranks = [candidate.rank for candidate in self.candidates]
        if len(ranks) != len(set(ranks)):
            raise ValueError(f"[{self.question_id}] 候选 rank 不能重复")
        return self


class AblationArtifact(_Model):
    """一组消融产物；``variant`` 决定候选字段约束。"""

    dataset_kind: Literal["dev", "holdout"]
    dataset_version: str = Field(min_length=1)
    variant: AblationVariant
    config: dict[str, str | int | float | bool] = Field(default_factory=dict)
    model_identities: dict[str, str | int | float | bool] = Field(default_factory=dict)
    created_at: str = Field(min_length=1)
    questions: list[AblationQuestion] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_variant(self) -> AblationArtifact:
        ids = [question.question_id for question in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("artifact 内 questionId 不能重复")
        for question in self.questions:
            for candidate in question.candidates:
                _assert_candidate_matches_variant(self.variant, question, candidate)
        return self


@dataclass(frozen=True)
class AblationTripletReport:
    """三元组校验通过后的汇总；``fallback_question_ids`` 是 C 降级回退到 B 的题。"""

    dataset_kind: str
    dataset_version: str
    question_count: int
    fallback_question_ids: tuple[str, ...]


def validate_ablation_triplet(
    a: AblationArtifact,
    b: AblationArtifact,
    c: AblationArtifact,
) -> AblationTripletReport:
    """校验 A/B/C 三组产物的一致性；不满足时抛 ``AblationValidationError``。"""

    if (a.variant, b.variant, c.variant) != ("A_VECTOR", "B_RRF", "C_RERANK"):
        raise AblationValidationError("三组产物必须依次为 A_VECTOR、B_RRF、C_RERANK")

    a_meta = (a.dataset_kind, a.dataset_version)
    if (b.dataset_kind, b.dataset_version) != a_meta:
        raise AblationValidationError("A 与 B 的 datasetKind/datasetVersion 不一致")
    if (c.dataset_kind, c.dataset_version) != a_meta:
        raise AblationValidationError("A 与 C 的 datasetKind/datasetVersion 不一致")

    a_questions = {question.question_id: question for question in a.questions}
    b_questions = {question.question_id: question for question in b.questions}
    c_questions = {question.question_id: question for question in c.questions}
    if set(a_questions) != set(b_questions) or set(a_questions) != set(c_questions):
        raise AblationValidationError("A/B/C 的题目集合不一致")

    fallback: list[str] = []
    for question_id, a_question in a_questions.items():
        b_question = b_questions[question_id]
        c_question = c_questions[question_id]
        if not (a_question.scope_id == b_question.scope_id == c_question.scope_id):
            raise AblationValidationError(f"[{question_id}] A/B/C 的授权 scope 标识不一致")
        if RERANK_UNAVAILABLE in c_question.degraded_stages:
            if _ordered_candidates(c_question) != _ordered_candidates(b_question):
                raise AblationValidationError(
                    f"[{question_id}] C 声明 rerank_unavailable，候选事实与顺序必须与 B 相同"
                )
            fallback.append(question_id)

    return AblationTripletReport(
        dataset_kind=a.dataset_kind,
        dataset_version=a.dataset_version,
        question_count=len(a_questions),
        fallback_question_ids=tuple(sorted(fallback)),
    )


def _ordered_candidates(question: AblationQuestion) -> tuple[RankingCandidate, ...]:
    return tuple(sorted(question.candidates, key=lambda item: item.rank))


def _assert_candidate_matches_variant(
    variant: AblationVariant, question: AblationQuestion, candidate: RankingCandidate
) -> None:
    label = f"[{question.question_id}/{candidate.candidate_id}]"
    if variant == "A_VECTOR":
        if candidate.vector_rank is None or candidate.vector_score is None:
            raise ValueError(f"{label} A_VECTOR 候选必须带 vectorRank/vectorScore")
        if (
            candidate.keyword_rank is not None
            or candidate.keyword_score is not None
            or candidate.fusion_rank is not None
            or candidate.fusion_score is not None
            or candidate.rerank_score is not None
        ):
            raise ValueError(f"{label} A_VECTOR 候选不得含关键词/融合/重排字段")
        if candidate.rank != candidate.vector_rank:
            raise ValueError(f"{label} A_VECTOR 最终 rank 必须等于 vectorRank")
        return
    if candidate.fusion_rank is None or candidate.fusion_score is None:
        raise ValueError(f"{label} {variant} 候选必须带 fusionRank/fusionScore")
    if variant == "B_RRF":
        if candidate.rerank_score is not None:
            raise ValueError(f"{label} B_RRF 候选不得含 rerankScore")
        if candidate.rank != candidate.fusion_rank:
            raise ValueError(f"{label} B_RRF 最终 rank 必须等于 fusionRank")
        return
    if RERANK_UNAVAILABLE in question.degraded_stages:
        if candidate.rerank_score is not None:
            raise ValueError(f"{label} C_RERANK 降级时不得带 rerankScore")
    if candidate.rerank_score is None and candidate.rank != candidate.fusion_rank:
        raise ValueError(f"{label} C_RERANK 无重排分数时最终 rank 必须等于 fusionRank")
