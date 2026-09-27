"""问答上下文的确定性预算选择：纯函数，不调用模型、不访问数据库或网络。

输入目标与输出上限来自进程配置（默认输入 4,000、输出 800）。选择规则的优先级固定为：

1. 系统提示与当前问题**必然保留**；两者单独就超过输入预算时抛
   :class:`MandatoryContextExceedsBudgetError`，绝不静默截断或丢证据以外的东西。
2. 历史只接受调用者显式标记为已授权的轮次：先按"最近优先"取最新的
   :data:`DEFAULT_MAX_HISTORY_TURNS` 轮，再按（调用者给出的相关性降序、轮次序号降序）逐个尝试
   加入；相关性完全由调用者提供，本模块不额外调用任何模型打分，也不读上一轮模型回答当事实。
3. 证据按调用者给出的检索顺序（融合名次）依次尝试加入，同一文档最多
   :data:`DEFAULT_MAX_EVIDENCE_PER_DOCUMENT` 段、总计最多 :data:`DEFAULT_MAX_EVIDENCE` 段；
   每接受一项后，**完整提示**的本地估算都必须不超过输入预算，超出的项被跳过并记录原因。
4. `4～6 段`是可用性目标而不是门槛：可用或被预算容纳的证据少于 4 段时，返回实际纳入的数量，
   既不伪造证据，也不把目标当硬约束报错。

单个**候选**的正文里出现本地渲染器拒绝的结构 token 时，该候选被跳过并记静态原因码
:data:`REASON_UNSUPPORTED_TEXT`，其余合法候选继续入选；系统提示与当前问题自身的这类文本仍然
是无法吞咽的具名 :class:`~rag_backend.generation.deepseek_prompt.PromptEncodingError`，留给
后续 API 层映射，本模块不替调用者静默吞咽必需内容的渲染失败。

本模块只做选择，不作回答决策：**:attr:`ChatContextPlan.evidence_ids` 为空（无入选证据）时，调用方
必须拒答**，不得在没有证据的情况下继续调用模型；`insufficientEvidence` 之类的回答语义由生成层负责。

计数一律走 :class:`~rag_backend.generation.deepseek_prompt.PromptTokenEstimator`（本地
tokenizer + 本地 chat 包装），因此 :attr:`ChatContextPlan.input_tokens` 是**估算**值而不是
provider 精确用量；真实用量必须由生成切片按 provider 响应 ``usage`` 另行记账。
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from rag_backend.config import Settings
from rag_backend.generation.deepseek_prompt import (
    PROMPT_ENCODING_CONTRACT,
    TOKEN_COUNT_SOURCE,
    ChatMessage,
    PromptEncodingError,
    PromptTokenEstimator,
)

# 与 Settings 同名配置字段的默认值保持一致；配置是运行期权威值，这里的常量只服务直接调用与测试。
DEFAULT_INPUT_TOKEN_BUDGET = 4000
DEFAULT_OUTPUT_TOKEN_BUDGET = 800
DEFAULT_MAX_HISTORY_TURNS = 3
DEFAULT_MAX_EVIDENCE = 6
DEFAULT_MAX_EVIDENCE_PER_DOCUMENT = 3

# 证据区放在当前问题之前、同一条 user 消息内，与官方"连续 user 消息以空行合并"的行为一致。
EVIDENCE_SECTION_HEADER = "证据片段（回答只能引用下列 E 编号）："
EVIDENCE_BLOCK_TEMPLATE = "{evidence_id}: {text}"
EVIDENCE_SECTION_SEPARATOR = "\n\n"

# 排除原因码；都是静态字符串，便于单测断言与运维诊断。
REASON_UNAUTHORIZED = "unauthorized"
REASON_OUTSIDE_RECENT_WINDOW = "outside_recent_window"
REASON_BUDGET = "budget"
REASON_MAX_EVIDENCE = "max_evidence"
REASON_MAX_PER_DOCUMENT = "max_per_document"
# 候选正文含本地渲染器拒绝的结构 token：只跳过该候选，不中断整次装配。
REASON_UNSUPPORTED_TEXT = "unsupported_text"

# 历史轮次的最终装配顺序始终按 sequence 升序；这里只影响"先尝试谁"。
_LOWEST_RELEVANCE = float("-inf")


class MandatoryContextExceedsBudgetError(RuntimeError):
    """系统提示与当前问题单独超出输入预算；调用方必须显式处理，不得截断后继续。"""

    def __init__(self, mandatory_tokens: int, input_token_budget: int) -> None:
        super().__init__(
            f"系统提示与当前问题合计 {mandatory_tokens} tokens，"
            f"超过输入预算 {input_token_budget}；不做静默截断"
        )
        self.mandatory_tokens = mandatory_tokens
        self.input_token_budget = input_token_budget


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextBudget:
    """单次问答的预算参数；输入预算由本地估算执行，输出上限交给 provider 强制。"""

    input_token_budget: int = DEFAULT_INPUT_TOKEN_BUDGET
    output_token_budget: int = DEFAULT_OUTPUT_TOKEN_BUDGET
    max_history_turns: int = DEFAULT_MAX_HISTORY_TURNS
    max_evidence: int = DEFAULT_MAX_EVIDENCE
    max_evidence_per_document: int = DEFAULT_MAX_EVIDENCE_PER_DOCUMENT

    def __post_init__(self) -> None:
        for name in (
            "input_token_budget",
            "output_token_budget",
            "max_history_turns",
            "max_evidence",
            "max_evidence_per_document",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} 必须为正数")

    @classmethod
    def from_settings(cls, settings: Settings) -> ContextBudget:
        """用进程配置的两个预算字段显式构造；历史与证据上限保持模块默认。

        正数校验由 :class:`~rag_backend.config.Settings` 与 :meth:`__post_init__` 各自执行，
        这里不静默夹取非法值。
        """

        return cls(
            input_token_budget=settings.llm_input_token_budget,
            output_token_budget=settings.llm_output_token_budget,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceCandidate:
    """一条已由调用方按授权链取出、按检索顺序排列的候选证据。

    ``evidence_id`` 是调用者分配的临时引用 ID（如 ``E1``）：模型只能回这些 ID，服务端再映射成
    citation；``text`` 是未归一化的原文片段，属于低信任数据。
    """

    evidence_id: str
    document_id: uuid.UUID
    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoryTurn:
    """一轮合法历史；``authorized`` 必须由调用者显式声明，未授权轮次绝不进入上下文。"""

    sequence: int
    question: str
    answer: str
    authorized: bool
    relevance: float | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ExcludedItem:
    """被排除的候选项与静态原因码；``key`` 是证据临时 ID 或历史 ``sequence``。"""

    kind: Literal["evidence", "history"]
    key: str
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatContextPlan:
    """一次问答的最终提示装配结果，可直接交给后续 LLM 客户端序列化。"""

    messages: tuple[ChatMessage, ...]
    evidence_ids: tuple[str, ...]
    history_sequences: tuple[int, ...]
    excluded: tuple[ExcludedItem, ...]
    input_tokens: int
    input_token_budget: int
    output_token_budget: int
    prompt_encoding_contract: str
    token_count_source: str


def render_evidence_block(evidence: Sequence[EvidenceCandidate]) -> str:
    """渲染证据区文本；证据正文原样保留，不做归一化或截断。"""

    blocks = EVIDENCE_SECTION_SEPARATOR.join(
        EVIDENCE_BLOCK_TEMPLATE.format(evidence_id=item.evidence_id, text=item.text)
        for item in evidence
    )
    return f"{EVIDENCE_SECTION_HEADER}\n{blocks}"


def _validate_required_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}不能为空")


def _validate_history(history: Sequence[HistoryTurn]) -> None:
    seen: set[int] = set()
    for turn in history:
        if not isinstance(turn.sequence, int):
            raise ValueError("历史轮次 sequence 必须是整数")
        if turn.sequence in seen:
            # 重复序号会让“最近窗口”与装配顺序有歧义，必须显式拒绝而不是任选其一。
            raise ValueError("历史轮次 sequence 不得重复")
        seen.add(turn.sequence)
        _validate_required_text(turn.question, "历史问题")
        _validate_required_text(turn.answer, "历史回答")


def _validate_evidence(evidence: Sequence[EvidenceCandidate]) -> None:
    seen: set[str] = set()
    for item in evidence:
        if not isinstance(item.evidence_id, str) or not item.evidence_id.strip():
            raise ValueError("证据临时 ID 不能为空")
        if item.evidence_id in seen:
            raise ValueError("证据临时 ID 不得重复")
        seen.add(item.evidence_id)
        _validate_required_text(item.text, "证据正文")


def _assemble_messages(
    system_prompt: str,
    history: Sequence[HistoryTurn],
    question: str,
    evidence: Sequence[EvidenceCandidate],
) -> tuple[ChatMessage, ...]:
    """按固定顺序装配：system → 历史轮次（按时序）→ 当前问题（证据区前置）。"""

    messages = [ChatMessage(role="system", content=system_prompt)]
    for turn in history:
        messages.append(ChatMessage(role="user", content=turn.question))
        messages.append(ChatMessage(role="assistant", content=turn.answer))
    user_content = question
    if evidence:
        user_content = f"{render_evidence_block(evidence)}{EVIDENCE_SECTION_SEPARATOR}{question}"
    messages.append(ChatMessage(role="user", content=user_content))
    return tuple(messages)


def _relevance_order(turn: HistoryTurn) -> tuple[float, int]:
    """历史尝试顺序：相关性优先，其次更新者优先。"""

    return (-(turn.relevance if turn.relevance is not None else _LOWEST_RELEVANCE), -turn.sequence)


def plan_chat_context(
    *,
    system_prompt: str,
    question: str,
    estimator: PromptTokenEstimator,
    evidence: Sequence[EvidenceCandidate] = (),
    history: Sequence[HistoryTurn] = (),
    budget: ContextBudget | None = None,
) -> ChatContextPlan:
    """选择证据与历史并装配最终提示；系统提示与当前问题超预算时抛预算错误。

    输入顺序即优先级顺序：``evidence`` 必须是检索（融合）顺序，``history`` 的顺序不影响结果
    （轮次序号与相关性决定取舍，装配始终按时序）。
    """

    resolved_budget = ContextBudget() if budget is None else budget
    _validate_required_text(system_prompt, "系统提示")
    _validate_required_text(question, "当前问题")
    _validate_history(history)
    _validate_evidence(evidence)

    mandatory = _assemble_messages(system_prompt, (), question, ())
    mandatory_tokens = estimator.estimate_chat_tokens(mandatory)
    if mandatory_tokens > resolved_budget.input_token_budget:
        raise MandatoryContextExceedsBudgetError(
            mandatory_tokens, resolved_budget.input_token_budget
        )

    excluded: list[ExcludedItem] = []
    selected_turns: list[HistoryTurn] = []

    authorized = [turn for turn in history if turn.authorized]
    for turn in history:
        if not turn.authorized:
            excluded.append(
                ExcludedItem(kind="history", key=str(turn.sequence), reason=REASON_UNAUTHORIZED)
            )

    # 最近优先：最新 N 轮才有资格，窗口之外的轮次直接排除。
    recent_window = sorted(authorized, key=lambda turn: turn.sequence, reverse=True)[
        : resolved_budget.max_history_turns
    ]
    eligible_sequences = {turn.sequence for turn in recent_window}
    for turn in authorized:
        if turn.sequence not in eligible_sequences:
            excluded.append(
                ExcludedItem(
                    kind="history",
                    key=str(turn.sequence),
                    reason=REASON_OUTSIDE_RECENT_WINDOW,
                )
            )

    for turn in sorted(recent_window, key=_relevance_order):
        candidate = _assemble_messages(
            system_prompt,
            sorted([*selected_turns, turn], key=lambda item: item.sequence),
            question,
            (),
        )
        try:
            fits = estimator.estimate_chat_tokens(candidate) <= resolved_budget.input_token_budget
        except PromptEncodingError:
            excluded.append(
                ExcludedItem(kind="history", key=str(turn.sequence), reason=REASON_UNSUPPORTED_TEXT)
            )
            continue
        if fits:
            selected_turns.append(turn)
        else:
            excluded.append(
                ExcludedItem(kind="history", key=str(turn.sequence), reason=REASON_BUDGET)
            )

    selected_evidence: list[EvidenceCandidate] = []
    per_document: Counter[uuid.UUID] = Counter()
    for item in evidence:
        if len(selected_evidence) >= resolved_budget.max_evidence:
            excluded.append(
                ExcludedItem(kind="evidence", key=item.evidence_id, reason=REASON_MAX_EVIDENCE)
            )
            continue
        if per_document[item.document_id] >= resolved_budget.max_evidence_per_document:
            excluded.append(
                ExcludedItem(kind="evidence", key=item.evidence_id, reason=REASON_MAX_PER_DOCUMENT)
            )
            continue
        candidate = _assemble_messages(
            system_prompt, selected_turns, question, [*selected_evidence, item]
        )
        try:
            fits = estimator.estimate_chat_tokens(candidate) <= resolved_budget.input_token_budget
        except PromptEncodingError:
            # 低信任证据含本地结构 token：只跳过该候选，其余合法证据继续按顺序加入。
            excluded.append(
                ExcludedItem(kind="evidence", key=item.evidence_id, reason=REASON_UNSUPPORTED_TEXT)
            )
            continue
        if fits:
            selected_evidence.append(item)
            per_document[item.document_id] += 1
        else:
            excluded.append(
                ExcludedItem(kind="evidence", key=item.evidence_id, reason=REASON_BUDGET)
            )

    turns = tuple(sorted(selected_turns, key=lambda turn: turn.sequence))
    messages = _assemble_messages(system_prompt, turns, question, selected_evidence)
    return ChatContextPlan(
        messages=messages,
        evidence_ids=tuple(item.evidence_id for item in selected_evidence),
        history_sequences=tuple(turn.sequence for turn in turns),
        excluded=tuple(excluded),
        input_tokens=estimator.estimate_chat_tokens(messages),
        input_token_budget=resolved_budget.input_token_budget,
        output_token_budget=resolved_budget.output_token_budget,
        prompt_encoding_contract=PROMPT_ENCODING_CONTRACT,
        token_count_source=TOKEN_COUNT_SOURCE,
    )


__all__ = [
    "DEFAULT_INPUT_TOKEN_BUDGET",
    "DEFAULT_MAX_EVIDENCE",
    "DEFAULT_MAX_EVIDENCE_PER_DOCUMENT",
    "DEFAULT_MAX_HISTORY_TURNS",
    "DEFAULT_OUTPUT_TOKEN_BUDGET",
    "EVIDENCE_SECTION_HEADER",
    "EVIDENCE_SECTION_SEPARATOR",
    "REASON_BUDGET",
    "REASON_MAX_EVIDENCE",
    "REASON_MAX_PER_DOCUMENT",
    "REASON_OUTSIDE_RECENT_WINDOW",
    "REASON_UNAUTHORIZED",
    "REASON_UNSUPPORTED_TEXT",
    "ChatContextPlan",
    "ContextBudget",
    "EvidenceCandidate",
    "ExcludedItem",
    "HistoryTurn",
    "MandatoryContextExceedsBudgetError",
    "plan_chat_context",
    "render_evidence_block",
]
