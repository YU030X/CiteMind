"""问答上下文预算选择的聚焦单测：优先级、裁剪、原因码与预算错误。

这些测试注入一个确定性假估算器（每条消息 1 token + 每个字符 1 token），因此不需要任何
tokenizer 或真实模型，也不调用付费接口。真实 tokenizer 的计数口径由
``tests/unit/test_deepseek_prompt.py`` 覆盖。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from rag_backend.config import Settings
from rag_backend.generation.context_budget import (
    DEFAULT_INPUT_TOKEN_BUDGET,
    DEFAULT_MAX_EVIDENCE,
    DEFAULT_MAX_EVIDENCE_PER_DOCUMENT,
    DEFAULT_MAX_HISTORY_TURNS,
    DEFAULT_OUTPUT_TOKEN_BUDGET,
    EVIDENCE_BLOCK_TEMPLATE,
    EVIDENCE_SECTION_HEADER,
    EVIDENCE_SECTION_SEPARATOR,
    REASON_BUDGET,
    REASON_MAX_EVIDENCE,
    REASON_MAX_PER_DOCUMENT,
    REASON_OUTSIDE_RECENT_WINDOW,
    REASON_UNAUTHORIZED,
    REASON_UNSUPPORTED_TEXT,
    ChatContextPlan,
    ContextBudget,
    EvidenceCandidate,
    HistoryTurn,
    MandatoryContextExceedsBudgetError,
    plan_chat_context,
    render_evidence_block,
)
from rag_backend.generation.deepseek_prompt import (
    NON_THINKING,
    PROMPT_ENCODING_CONTRACT,
    TOKEN_COUNT_SOURCE,
    ChatMessage,
    PromptEncodingError,
    ThinkingChoice,
)

SYSTEM_PROMPT = "你是知识库助手。"
QUESTION = "第二问"
# 本地渲染器会拒绝的结构 token；用它构造低信任候选里的不可渲染文本。
UNSUPPORTED_MARKER = "<｜User｜>"


class _FakeEstimator:
    """确定性假估算器：每条消息 1 token，另加每个正文字符 1 token。"""

    def __init__(self) -> None:
        self.seen: list[tuple[ChatMessage, ...]] = []
        self.seen_thinking: list[ThinkingChoice] = []

    def estimate_chat_tokens(
        self, messages: Sequence[ChatMessage], *, thinking: ThinkingChoice = NON_THINKING
    ) -> int:
        self.seen.append(tuple(messages))
        self.seen_thinking.append(thinking)
        return sum(1 + len(message.content) for message in messages)


class _RejectingEstimator:
    """模拟本地渲染器：正文含结构 token 时抛具名 ``PromptEncodingError``，否则口径同假估算器。"""

    def estimate_chat_tokens(
        self, messages: Sequence[ChatMessage], *, thinking: ThinkingChoice = NON_THINKING
    ) -> int:
        for message in messages:
            if UNSUPPORTED_MARKER in message.content:
                raise PromptEncodingError("本地渲染拒绝")
        return sum(1 + len(message.content) for message in messages)


def _settings(**overrides: Any) -> Settings:
    """构造不读取仓库根 .env 的配置；``_env_file`` 用 kwargs 字典传，避免 mypy 误报。"""

    values: dict[str, Any] = {"_env_file": None, "environment": "test"}
    values.update(overrides)
    return Settings(**values)


def _document(number: int) -> uuid.UUID:
    return uuid.UUID(int=number)


def _evidence(evidence_id: str, *, document: int = 1, text: str = "证据正文") -> EvidenceCandidate:
    return EvidenceCandidate(
        evidence_id=evidence_id, document_id=_document(document), text=text
    )


def _user_content(evidence: Sequence[EvidenceCandidate], question: str) -> str:
    """测试侧独立复算的 user 消息内容：证据区 + 空行 + 当前问题。"""

    if not evidence:
        return question
    return f"{render_evidence_block(evidence)}{EVIDENCE_SECTION_SEPARATOR}{question}"


def _tokens(
    *,
    system: str = SYSTEM_PROMPT,
    question: str = QUESTION,
    turns: Sequence[HistoryTurn] = (),
    evidence: Sequence[EvidenceCandidate] = (),
) -> int:
    """假估算器口径下、按文档化装配规则算出的 token 数，用作预算阈值。"""

    messages = [1 + len(system)]
    for turn in turns:
        messages.append(1 + len(turn.question))
        messages.append(1 + len(turn.answer))
    messages.append(1 + len(_user_content(evidence, question)))
    return sum(messages)


def _reason(plan: ChatContextPlan, kind: str, key: str) -> str:
    matches = [item.reason for item in plan.excluded if item.kind == kind and item.key == key]
    assert len(matches) == 1, f"{kind} {key} 应恰好有一条排除记录，实际 {matches}"
    return matches[0]


# ---------------------------------------------------------------------------
# 系统提示与当前问题：必然保留，超预算显式报错
# ---------------------------------------------------------------------------


def test_mandatory_context_over_budget_raises_instead_of_truncating() -> None:
    estimator = _FakeEstimator()
    mandatory = _tokens()
    budget = ContextBudget(input_token_budget=mandatory - 1)

    with pytest.raises(MandatoryContextExceedsBudgetError) as info:
        plan_chat_context(
            system_prompt=SYSTEM_PROMPT,
            question=QUESTION,
            estimator=estimator,
            evidence=(_evidence("E1"),),
            history=(HistoryTurn(sequence=1, question="问", answer="答", authorized=True),),
            budget=budget,
        )

    assert info.value.mandatory_tokens == mandatory
    assert info.value.input_token_budget == mandatory - 1
    assert SYSTEM_PROMPT not in str(info.value)
    assert estimator.seen, "必须先测量再判定是否超预算"


def test_system_and_current_question_are_kept_when_budget_is_tight() -> None:
    estimator = _FakeEstimator()
    budget = ContextBudget(input_token_budget=_tokens())
    evidence = tuple(_evidence(f"E{index}", document=index) for index in range(1, 4))
    history = (
        HistoryTurn(sequence=1, question="旧问", answer="旧答", authorized=True),
        HistoryTurn(sequence=2, question="新问", answer="新答", authorized=True),
    )

    plan = plan_chat_context(
        system_prompt=SYSTEM_PROMPT,
        question=QUESTION,
        estimator=estimator,
        evidence=evidence,
        history=history,
        budget=budget,
    )

    assert plan.messages[0] == ChatMessage(role="system", content=SYSTEM_PROMPT)
    assert plan.messages[-1].role == "user"
    assert plan.messages[-1].content == QUESTION
    assert plan.evidence_ids == ()
    assert plan.history_sequences == ()
    assert plan.input_tokens == budget.input_token_budget
    assert _reason(plan, "evidence", "E1") == REASON_BUDGET
    assert _reason(plan, "history", "2") == REASON_BUDGET


# ---------------------------------------------------------------------------
# 证据：按检索顺序加入、上限与同文档上限、缺少 4 段不报错
# ---------------------------------------------------------------------------


def test_evidence_follows_retrieval_order_until_budget() -> None:
    estimator = _FakeEstimator()
    evidence = tuple(_evidence(f"E{index}", document=index) for index in range(1, 4))
    budget = ContextBudget(input_token_budget=_tokens(evidence=evidence[:2]))

    plan = plan_chat_context(
        system_prompt=SYSTEM_PROMPT,
        question=QUESTION,
        estimator=estimator,
        evidence=evidence,
        budget=budget,
    )

    assert plan.evidence_ids == ("E1", "E2")
    assert plan.input_tokens == budget.input_token_budget
    assert _reason(plan, "evidence", "E3") == REASON_BUDGET
    assert _user_content(evidence[:2], QUESTION) in plan.messages[-1].content


def test_evidence_skips_oversized_item_and_continues_in_rank_order() -> None:
    estimator = _FakeEstimator()
    oversized = _evidence("E1", document=1, text="x" * 400)
    small = (_evidence("E2", document=2), _evidence("E3", document=3))
    budget = ContextBudget(input_token_budget=_tokens(evidence=small))

    plan = plan_chat_context(
        system_prompt=SYSTEM_PROMPT,
        question=QUESTION,
        estimator=estimator,
        evidence=(oversized, *small),
        budget=budget,
    )

    assert plan.evidence_ids == ("E2", "E3")
    assert _reason(plan, "evidence", "E1") == REASON_BUDGET


def test_evidence_is_capped_per_document_without_blocking_other_documents() -> None:
    estimator = _FakeEstimator()
    evidence = (
        _evidence("E1", document=1),
        _evidence("E2", document=1),
        _evidence("E3", document=1),
        _evidence("E4", document=1),
        _evidence("E5", document=2),
    )
    budget = ContextBudget(input_token_budget=10_000)

    plan = plan_chat_context(
        system_prompt=SYSTEM_PROMPT,
        question=QUESTION,
        estimator=estimator,
        evidence=evidence,
        budget=budget,
    )

    assert DEFAULT_MAX_EVIDENCE_PER_DOCUMENT == 3
    assert plan.evidence_ids == ("E1", "E2", "E3", "E5")
    assert _reason(plan, "evidence", "E4") == REASON_MAX_PER_DOCUMENT


def test_evidence_total_cap_is_six() -> None:
    estimator = _FakeEstimator()
    evidence = tuple(_evidence(f"E{index}", document=index) for index in range(1, 9))

    plan = plan_chat_context(
        system_prompt=SYSTEM_PROMPT,
        question=QUESTION,
        estimator=estimator,
        evidence=evidence,
        budget=ContextBudget(input_token_budget=10_000),
    )

    assert DEFAULT_MAX_EVIDENCE == 6
    assert plan.evidence_ids == ("E1", "E2", "E3", "E4", "E5", "E6")
    assert _reason(plan, "evidence", "E7") == REASON_MAX_EVIDENCE
    assert _reason(plan, "evidence", "E8") == REASON_MAX_EVIDENCE


def test_fewer_than_four_evidence_is_not_an_error() -> None:
    estimator = _FakeEstimator()
    evidence = (_evidence("E1", document=1), _evidence("E2", document=2))

    plan = plan_chat_context(
        system_prompt=SYSTEM_PROMPT,
        question=QUESTION,
        estimator=estimator,
        evidence=evidence,
        budget=ContextBudget(input_token_budget=10_000),
    )

    # 4～6 段是可用性目标而不是门槛：可用证据不足时返回实际数量，不报错、不伪造。
    assert plan.evidence_ids == ("E1", "E2")
    assert plan.excluded == ()


def test_no_selected_evidence_is_reported_for_caller_refusal() -> None:
    """边界：无任何入选证据时必须可由调用方识别并拒答，而不是静默返回空证据继续生成。"""

    estimator = _FakeEstimator()
    oversized = _evidence("E1", document=1, text="x" * 400)

    plan = plan_chat_context(
        system_prompt=SYSTEM_PROMPT,
        question=QUESTION,
        estimator=estimator,
        evidence=(oversized,),
        budget=ContextBudget(input_token_budget=_tokens()),
    )

    assert plan.evidence_ids == ()
    assert _reason(plan, "evidence", "E1") == REASON_BUDGET
    assert plan.messages[-1].content == QUESTION


def test_evidence_with_unsupported_text_is_skipped_and_other_evidence_continues() -> None:
    """低信任证据含本地结构 token 时只跳过该候选，不中断整次装配。"""

    unsupported = _evidence("E1", document=1, text=f"前置{UNSUPPORTED_MARKER}尾部")
    good = (_evidence("E2", document=2), _evidence("E3", document=3))

    plan = plan_chat_context(
        system_prompt=SYSTEM_PROMPT,
        question=QUESTION,
        estimator=_RejectingEstimator(),
        evidence=(unsupported, *good),
        budget=ContextBudget(input_token_budget=10_000),
    )

    assert plan.evidence_ids == ("E2", "E3")
    assert _reason(plan, "evidence", "E1") == REASON_UNSUPPORTED_TEXT
    assert "尾部" not in "".join(message.content for message in plan.messages)


def test_history_with_unsupported_text_is_skipped_without_aborting() -> None:
    """历史轮次与证据同样处理：不可渲染的那一轮被跳过，其余轮次继续。"""

    unsupported = HistoryTurn(
        sequence=1,
        question="旧问",
        answer=f"答案{UNSUPPORTED_MARKER}",
        authorized=True,
    )
    good = HistoryTurn(sequence=2, question="新问", answer="新答", authorized=True)

    plan = plan_chat_context(
        system_prompt=SYSTEM_PROMPT,
        question=QUESTION,
        estimator=_RejectingEstimator(),
        history=(unsupported, good),
        budget=ContextBudget(input_token_budget=10_000),
    )

    assert plan.history_sequences == (2,)
    assert _reason(plan, "history", "1") == REASON_UNSUPPORTED_TEXT


def test_unsupported_current_question_still_raises_encoding_error() -> None:
    """系统提示与当前问题自身的不可渲染文本不得被吞掉，必须保留具名错误给 API 层映射。"""

    with pytest.raises(PromptEncodingError):
        plan_chat_context(
            system_prompt=SYSTEM_PROMPT,
            question=f"问题{UNSUPPORTED_MARKER}",
            estimator=_RejectingEstimator(),
            evidence=(_evidence("E1", document=1),),
            budget=ContextBudget(input_token_budget=10_000),
        )


def test_unsupported_system_prompt_still_raises_encoding_error() -> None:
    with pytest.raises(PromptEncodingError):
        plan_chat_context(
            system_prompt=f"系统{UNSUPPORTED_MARKER}",
            question=QUESTION,
            estimator=_RejectingEstimator(),
        )


def test_evidence_ids_are_validated_before_planning() -> None:
    estimator = _FakeEstimator()
    with pytest.raises(ValueError, match="不得重复"):
        plan_chat_context(
            system_prompt=SYSTEM_PROMPT,
            question=QUESTION,
            estimator=estimator,
            evidence=(_evidence("E1"), _evidence("E1", document=2)),
        )
    with pytest.raises(ValueError, match="不能为空"):
        plan_chat_context(
            system_prompt=SYSTEM_PROMPT,
            question=QUESTION,
            estimator=estimator,
            evidence=(_evidence("  "),),
        )
    with pytest.raises(ValueError, match="证据正文不能为空"):
        plan_chat_context(
            system_prompt=SYSTEM_PROMPT,
            question=QUESTION,
            estimator=estimator,
            evidence=(_evidence("E1", text="   "),),
        )


# ---------------------------------------------------------------------------
# 历史：只接受已授权轮次、最近 3 轮窗口、相关性优先
# ---------------------------------------------------------------------------


def test_unauthorized_history_never_enters_context() -> None:
    estimator = _FakeEstimator()
    history = (
        HistoryTurn(sequence=1, question="已撤权问", answer="已撤权答", authorized=False),
        HistoryTurn(sequence=2, question="合法问", answer="合法答", authorized=True),
    )

    plan = plan_chat_context(
        system_prompt=SYSTEM_PROMPT,
        question=QUESTION,
        estimator=estimator,
        history=history,
        budget=ContextBudget(input_token_budget=10_000),
    )

    assert plan.history_sequences == (2,)
    assert "已撤权" not in "".join(message.content for message in plan.messages)
    assert _reason(plan, "history", "1") == REASON_UNAUTHORIZED


def test_history_window_keeps_only_three_most_recent_turns() -> None:
    estimator = _FakeEstimator()
    history = tuple(
        HistoryTurn(
            sequence=sequence,
            question=f"问{sequence}",
            answer=f"答{sequence}",
            authorized=True,
        )
        for sequence in range(1, 6)
    )

    plan = plan_chat_context(
        system_prompt=SYSTEM_PROMPT,
        question=QUESTION,
        estimator=estimator,
        history=history,
        budget=ContextBudget(input_token_budget=10_000),
    )

    assert DEFAULT_MAX_HISTORY_TURNS == 3
    assert plan.history_sequences == (3, 4, 5)
    assert [message.content for message in plan.messages] == [
        SYSTEM_PROMPT,
        "问3",
        "答3",
        "问4",
        "答4",
        "问5",
        "答5",
        QUESTION,
    ]
    assert _reason(plan, "history", "1") == REASON_OUTSIDE_RECENT_WINDOW
    assert _reason(plan, "history", "2") == REASON_OUTSIDE_RECENT_WINDOW


def test_history_priority_uses_relevance_then_recency() -> None:
    estimator = _FakeEstimator()
    relevant_old = HistoryTurn(
        sequence=1, question="相关旧问", answer="相关旧答", authorized=True, relevance=0.9
    )
    recent_low = HistoryTurn(
        sequence=3, question="较新问", answer="较新答", authorized=True, relevance=0.5
    )
    recent_lowest = HistoryTurn(
        sequence=2, question="最新但无关问", answer="最新但无关答", authorized=True, relevance=0.1
    )
    two_turns = (relevant_old, recent_low)
    budget = ContextBudget(input_token_budget=_tokens(turns=two_turns))

    plan = plan_chat_context(
        system_prompt=SYSTEM_PROMPT,
        question=QUESTION,
        estimator=estimator,
        history=(relevant_old, recent_low, recent_lowest),
        budget=budget,
    )

    assert plan.history_sequences == (1, 3), "相关性决定取舍，装配仍按时序"
    assert _reason(plan, "history", "2") == REASON_BUDGET


def test_history_turns_without_relevance_are_treated_as_lowest() -> None:
    estimator = _FakeEstimator()
    scored = HistoryTurn(
        sequence=2, question="有分问", answer="有分答", authorized=True, relevance=0.4
    )
    unscored = HistoryTurn(sequence=3, question="无分问", answer="无分答", authorized=True)
    budget = ContextBudget(input_token_budget=_tokens(turns=(scored,)))

    plan = plan_chat_context(
        system_prompt=SYSTEM_PROMPT,
        question=QUESTION,
        estimator=estimator,
        history=(unscored, scored),
        budget=budget,
    )

    assert plan.history_sequences == (2,)
    assert _reason(plan, "history", "3") == REASON_BUDGET


def test_history_content_is_validated() -> None:
    estimator = _FakeEstimator()
    with pytest.raises(ValueError, match="历史回答不能为空"):
        plan_chat_context(
            system_prompt=SYSTEM_PROMPT,
            question=QUESTION,
            estimator=estimator,
            history=(
                HistoryTurn(sequence=1, question="问", answer="  ", authorized=True),
            ),
        )


def test_duplicate_history_sequence_is_rejected() -> None:
    with pytest.raises(ValueError, match="不得重复"):
        plan_chat_context(
            system_prompt=SYSTEM_PROMPT,
            question=QUESTION,
            estimator=_FakeEstimator(),
            history=(
                HistoryTurn(sequence=1, question="问一", answer="答一", authorized=True),
                HistoryTurn(sequence=1, question="问二", answer="答二", authorized=True),
            ),
        )


# ---------------------------------------------------------------------------
# 装配形状、估算标记与配置默认值
# ---------------------------------------------------------------------------


def test_plan_places_evidence_in_user_region_and_never_in_system_region() -> None:
    estimator = _FakeEstimator()
    evidence = (_evidence("E1", document=1, text="唯一哨兵证据E1"),)

    plan = plan_chat_context(
        system_prompt=SYSTEM_PROMPT,
        question=QUESTION,
        estimator=estimator,
        evidence=evidence,
        budget=ContextBudget(input_token_budget=10_000),
    )

    assert plan.messages[0].content == SYSTEM_PROMPT
    assert "唯一哨兵证据E1" not in plan.messages[0].content
    assert plan.messages[-1].content == (
        f"{EVIDENCE_SECTION_HEADER}\n"
        f"{EVIDENCE_BLOCK_TEMPLATE.format(evidence_id='E1', text='唯一哨兵证据E1')}"
        f"{EVIDENCE_SECTION_SEPARATOR}{QUESTION}"
    )


def test_plan_is_deterministic_and_marks_counts_as_local_estimate() -> None:
    evidence = (_evidence("E1", document=1),)
    history = (
        HistoryTurn(sequence=1, question="问", answer="答", authorized=True, relevance=0.2),
    )
    budget = ContextBudget(input_token_budget=10_000, output_token_budget=777)
    first = plan_chat_context(
        system_prompt=SYSTEM_PROMPT,
        question=QUESTION,
        estimator=_FakeEstimator(),
        evidence=evidence,
        history=history,
        budget=budget,
    )
    second = plan_chat_context(
        system_prompt=SYSTEM_PROMPT,
        question=QUESTION,
        estimator=_FakeEstimator(),
        evidence=evidence,
        history=history,
        budget=budget,
    )

    assert first == second
    assert first.token_count_source == TOKEN_COUNT_SOURCE
    assert first.prompt_encoding_contract == PROMPT_ENCODING_CONTRACT
    assert first.output_token_budget == 777
    assert first.input_token_budget == 10_000
    assert first.input_tokens == _tokens(turns=history, evidence=evidence)
    assert first.input_tokens <= first.input_token_budget


def test_plan_input_tokens_equal_recount_of_returned_messages() -> None:
    estimator = _FakeEstimator()
    plan = plan_chat_context(
        system_prompt=SYSTEM_PROMPT,
        question=QUESTION,
        estimator=estimator,
        evidence=(_evidence("E1", document=1),),
        budget=ContextBudget(input_token_budget=10_000),
    )

    assert plan.input_tokens == estimator.estimate_chat_tokens(plan.messages)


@pytest.mark.parametrize(
    ("system_prompt", "question", "fragment"),
    [
        ("  ", QUESTION, "系统提示不能为空"),
        (SYSTEM_PROMPT, "", "当前问题不能为空"),
    ],
)
def test_blank_required_text_is_rejected(
    system_prompt: str, question: str, fragment: str
) -> None:
    with pytest.raises(ValueError, match=fragment):
        plan_chat_context(
            system_prompt=system_prompt,
            question=question,
            estimator=_FakeEstimator(),
        )


@pytest.mark.parametrize(
    "field",
    [
        "input_token_budget",
        "output_token_budget",
        "max_history_turns",
        "max_evidence",
        "max_evidence_per_document",
    ],
)
def test_non_positive_budget_parameters_are_rejected(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        ContextBudget(**{field: 0})


def test_settings_defaults_match_budget_constants() -> None:
    assert Settings.model_fields["llm_input_token_budget"].default == DEFAULT_INPUT_TOKEN_BUDGET
    assert Settings.model_fields["llm_output_token_budget"].default == DEFAULT_OUTPUT_TOKEN_BUDGET
    assert DEFAULT_INPUT_TOKEN_BUDGET == 4000
    assert DEFAULT_OUTPUT_TOKEN_BUDGET == 800


def test_budget_from_settings_uses_configured_budgets() -> None:
    settings = _settings(llm_input_token_budget=1234, llm_output_token_budget=321)

    budget = ContextBudget.from_settings(settings)

    assert budget.input_token_budget == 1234
    assert budget.output_token_budget == 321
    # 历史与证据上限不在配置里，保持模块默认。
    assert budget.max_history_turns == DEFAULT_MAX_HISTORY_TURNS
    assert budget.max_evidence == DEFAULT_MAX_EVIDENCE
    assert budget.max_evidence_per_document == DEFAULT_MAX_EVIDENCE_PER_DOCUMENT


def test_thinking_choice_is_threaded_to_every_estimate() -> None:
    """思考选项必须传入每次估算：思考模式多出强度说明与 ``<think>``，不能按非思考口径估算。"""

    estimator = _FakeEstimator()
    history = [HistoryTurn(sequence=1, question="旧问", answer="旧答", authorized=True)]
    plan_chat_context(
        system_prompt=SYSTEM_PROMPT,
        question=QUESTION,
        estimator=estimator,
        evidence=(_evidence("E1"),),
        history=history,
        thinking=ThinkingChoice(enabled=True, effort="max"),
    )

    assert estimator.seen_thinking
    assert all(
        choice.enabled and choice.effort == "max" for choice in estimator.seen_thinking
    )


def test_budget_from_settings_rejects_non_positive_configuration() -> None:
    with pytest.raises(ValueError, match="llm_input_token_budget"):
        _settings(llm_input_token_budget=0)
