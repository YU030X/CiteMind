"""追问改写的纯逻辑单测：严格结构解析与预算内提示装配，不联网、不连数据库。

覆盖：唯一 ``standaloneQuestion`` 字段、额外字段（含模型试图提交 KB 范围）拒绝、非法类型、
空白与超长结果拒绝、已授权历史问题进入提示而助手回答被排除、最近窗口与预算裁剪，以及独立
问题字符上限与检索侧原始查询上限的一致性。
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import pytest
from rag_backend.generation.context_budget import (
    ContextBudget,
    HistoryTurn,
    MandatoryContextExceedsBudgetError,
)
from rag_backend.generation.deepseek_prompt import NON_THINKING, ChatMessage, ThinkingChoice
from rag_backend.generation.query_rewrite import (
    MAX_STANDALONE_QUESTION_CHARS,
    REWRITE_HISTORY_HEADER,
    REWRITE_QUESTION_HEADER,
    REWRITE_SYSTEM_PROMPT,
    RewriteSchemaError,
    parse_standalone_question,
    plan_rewrite_context,
)
from rag_backend.retrieval.query_embedding_client import MAX_QUERY_CHARS

BUDGET = ContextBudget(input_token_budget=100_000, output_token_budget=800)


class RecordingEstimator:
    """按内容长度估算的假估算器；同时记录参与估算的全部消息内容。"""

    def __init__(self) -> None:
        self.contents: list[str] = []

    def estimate_chat_tokens(
        self, messages: Sequence[ChatMessage], *, thinking: ThinkingChoice = NON_THINKING
    ) -> int:
        self.contents.extend(message.content for message in messages)
        return sum(1 + len(message.content) for message in messages)


def _turn(sequence: int, question: str, answer: str, *, authorized: bool = True) -> HistoryTurn:
    return HistoryTurn(
        sequence=sequence, question=question, answer=answer, authorized=authorized
    )


# --- 结构解析 ---------------------------------------------------------------


def test_parse_returns_single_standalone_question() -> None:
    content = json.dumps({"standaloneQuestion": "制度的适用范围是什么？"}, ensure_ascii=False)
    assert parse_standalone_question(content) == "制度的适用范围是什么？"


def test_parse_accepts_python_field_name() -> None:
    content = json.dumps({"standalone_question": "制度的适用范围是什么？"}, ensure_ascii=False)
    assert parse_standalone_question(content) == "制度的适用范围是什么？"


def test_parse_strips_surrounding_whitespace() -> None:
    content = json.dumps({"standaloneQuestion": "  制度的适用范围  "}, ensure_ascii=False)
    assert parse_standalone_question(content) == "制度的适用范围"


@pytest.mark.parametrize(
    "payload",
    [
        # 模型试图提交 KB 范围 / 过滤条件：额外字段一律拒绝，模型不能提供范围或查询指令。
        {"standaloneQuestion": "问题", "kbIds": ["00000000-0000-0000-0000-000000000001"]},
        {"standaloneQuestion": "问题", "filters": {"source": "x"}},
        # 缺少唯一字段。
        {"question": "问题"},
        # 类型不符。
        {"standaloneQuestion": 42},
    ],
)
def test_parse_rejects_extra_or_invalid_fields(payload: dict[str, object]) -> None:
    with pytest.raises(RewriteSchemaError):
        parse_standalone_question(json.dumps(payload, ensure_ascii=False))


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json",
        "[1, 2, 3]",
        json.dumps({"standaloneQuestion": "   "}, ensure_ascii=False),
    ],
)
def test_parse_rejects_blank_or_non_object(content: str) -> None:
    with pytest.raises(RewriteSchemaError):
        parse_standalone_question(content)


def test_parse_rejects_overlong_standalone_question() -> None:
    content = json.dumps(
        {"standaloneQuestion": "问" * (MAX_STANDALONE_QUESTION_CHARS + 1)}, ensure_ascii=False
    )
    with pytest.raises(RewriteSchemaError):
        parse_standalone_question(content)


def test_standalone_limit_matches_retrieval_query_limit() -> None:
    # 独立问题会被直接送进查询编码；上限必须与检索侧接受的原始查询上限一致。
    assert MAX_STANDALONE_QUESTION_CHARS == MAX_QUERY_CHARS


# --- 提示装配 ---------------------------------------------------------------


def test_plan_includes_only_authorized_history_questions() -> None:
    estimator = RecordingEstimator()
    history = [
        _turn(1, "第一问", "第一答", authorized=True),
        _turn(2, "第二问", "第二答", authorized=False),
        _turn(3, "第三问", "第三答", authorized=True),
    ]

    plan = plan_rewrite_context(
        system_prompt=REWRITE_SYSTEM_PROMPT,
        question="它呢？",
        estimator=estimator,
        history=history,
        budget=BUDGET,
    )

    content = plan.messages[-1].content
    assert REWRITE_HISTORY_HEADER in content
    assert "第一问" in content
    assert "第三问" in content
    # 未授权历史（第二问）整体不进入改写提示。
    assert "第二问" not in content
    # 助手回答绝不进入改写提示，避免把上一轮模型陈述当成事实。
    assert "第一答" not in content
    assert "第三答" not in content
    assert f"{REWRITE_QUESTION_HEADER}它呢？" in content
    assert plan.history_sequences == (1, 3)


def test_plan_keeps_only_recent_window() -> None:
    history = [_turn(sequence, f"第{sequence}问", f"第{sequence}答") for sequence in range(1, 6)]

    plan = plan_rewrite_context(
        system_prompt=REWRITE_SYSTEM_PROMPT,
        question="它呢？",
        estimator=RecordingEstimator(),
        history=history,
        budget=ContextBudget(input_token_budget=100_000, max_history_turns=2),
    )

    # 最近两轮（sequence 4、5）进入提示，装配顺序仍是升序。
    assert plan.history_sequences == (4, 5)


def test_plan_without_history_renders_question_only() -> None:
    plan = plan_rewrite_context(
        system_prompt=REWRITE_SYSTEM_PROMPT,
        question="独立问题",
        estimator=RecordingEstimator(),
        budget=BUDGET,
    )

    assert len(plan.messages) == 2
    assert plan.messages[0].role == "system"
    assert plan.messages[1].role == "user"
    assert plan.messages[1].content == f"{REWRITE_QUESTION_HEADER}独立问题"
    assert plan.history_sequences == ()


def test_plan_drops_history_that_does_not_fit_budget() -> None:
    history = [_turn(1, "很长的历史问题" * 20, "答")]
    question = "当前问题"
    # 只够容纳「改写信令 + 当前问题」，含历史就不够。
    estimator = RecordingEstimator()
    marker_budget = RecordingEstimator().estimate_chat_tokens(
        (
            ChatMessage(role="system", content=REWRITE_SYSTEM_PROMPT),
            ChatMessage(role="user", content=f"{REWRITE_QUESTION_HEADER}{question}"),
        )
    )

    plan = plan_rewrite_context(
        system_prompt=REWRITE_SYSTEM_PROMPT,
        question=question,
        estimator=estimator,
        history=history,
        budget=ContextBudget(input_token_budget=marker_budget),
    )

    assert plan.history_sequences == ()
    assert "很长的历史问题" not in plan.messages[-1].content


def test_plan_raises_when_mandatory_prompt_exceeds_budget() -> None:
    with pytest.raises(MandatoryContextExceedsBudgetError):
        plan_rewrite_context(
            system_prompt=REWRITE_SYSTEM_PROMPT,
            question="当前问题",
            estimator=RecordingEstimator(),
            budget=ContextBudget(input_token_budget=1),
        )


def test_plan_rejects_duplicate_history_sequence() -> None:
    with pytest.raises(ValueError):
        plan_rewrite_context(
            system_prompt=REWRITE_SYSTEM_PROMPT,
            question="当前问题",
            estimator=RecordingEstimator(),
            history=[_turn(1, "问", "答"), _turn(1, "问二", "答二")],
            budget=BUDGET,
        )
