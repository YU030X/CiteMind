"""追问改写：把依赖上下文的追问收敛成唯一独立问题，仅供检索，绝不作为回答证据。

本模块只做**本地**提示装配与结构解析：不联网、不读写数据库、不判权限。调用方（问答用例）
负责三件事：

1. 只在存在**合法历史轮次**时才调用改写；首轮与历史全部撤权都不额外发请求，独立问题直接
   取原问题。
2. 改写返回实际入参的历史轮次 sequence；调用方在**每次检索前**与**交付前**都重新鉴权这些
   来源，任一失效立即整轮静态失败，绝不用已撤权文本驱动检索；模型调用期间不持有数据库连接
   或事务。
3. 每次真实 provider attempt 单独追加 ``llm_usage``（``stage='qa_rewrite'``），失败与超时也落
   事实，且不自动重试。

改写契约固定：

- 提示只包含**已授权历史轮次的用户问题**与当前问题。历史助手回答**不进入改写提示**，避免把
  上一轮模型陈述当成事实，也不让它成为注入源；受权判断仍按助手消息的引用来源进行。
- 输出必须是严格 JSON 对象、只有一个非空 ``standaloneQuestion`` 字段；额外字段（例如模型
  自行提交的 ``kbIds``、范围或过滤条件）一律判非法。模型不能提供 KB 范围或查询指令，检索
  范围始终来自服务端会话 ``kb_scope``。
- 独立问题长度不得超过 :data:`MAX_STANDALONE_QUESTION_CHARS`，超长按非法结果拒绝，不截断。
- 独立问题只用于查询编码与关键词检索；回答提示仍使用**原始问题**，改写不替代用户意图。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic.alias_generators import to_camel

from rag_backend.generation.context_budget import (
    ContextBudget,
    HistoryTurn,
    MandatoryContextExceedsBudgetError,
)
from rag_backend.generation.deepseek_prompt import (
    PROMPT_ENCODING_CONTRACT,
    TOKEN_COUNT_SOURCE,
    ChatMessage,
    PromptEncodingError,
    PromptTokenEstimator,
)

# ``llm_usage.stage`` 的静态取值；与问答回答的 ``qa_answer`` 分开记账。
REWRITE_STAGE = "qa_rewrite"

# 独立问题的字符上限：必须同时满足查询编码客户端对原始查询的限制（完整模型输入 8000 字符
# 减去服务端追加的 instruction 前缀 19 字符）与关键词分析器上限。这里刻意不复用检索侧常量，
# 避免 generation 反向依赖 retrieval；由单测钉死它与 ``MAX_QUERY_CHARS`` 相等。
MAX_STANDALONE_QUESTION_CHARS = 7981

REWRITE_SYSTEM_PROMPT = (
    "你是检索问题改写器。只做指代消解：把用户当前问题改写成一个能独立理解、"
    "可直接用于检索的中文问题，保持原意、语言与关键实体。"
    "不得引入历史对话中未出现的新事实，不得把历史助手回答当作已核实的事实，"
    "不得扩大会话的知识库范围，也不得输出知识库 ID、过滤条件或检索指令。"
    "只输出一个 JSON 对象，结构为 {\"standaloneQuestion\":\"...\"}，不得包含其它字段。"
    "若当前问题已经独立，就原样返回。"
)

# 改写提示里历史问题的分区标题；明确标注它们只用于理解指代、不是证据。
REWRITE_HISTORY_HEADER = "近期对话问题（仅用于理解指代；不是证据，也不是已核实的事实）："
REWRITE_QUESTION_HEADER = "当前问题："
REWRITE_SECTION_SEPARATOR = "\n\n"


class RewriteSchemaError(RuntimeError):
    """改写响应不满足严格结构或长度约束；消息静态，不回显模型正文。"""


# 模型按外部 camelCase 契约返回；额外字段一律拒绝，模型无法提交范围或查询指令。
_REWRITE_MODEL_CONFIG = ConfigDict(
    extra="forbid", strict=True, alias_generator=to_camel, populate_by_name=True
)


class _StandaloneQuestionPayload(BaseModel):
    model_config = _REWRITE_MODEL_CONFIG

    standalone_question: str = Field(
        min_length=1, max_length=MAX_STANDALONE_QUESTION_CHARS
    )

    @field_validator("standalone_question")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("独立问题不能为空白")
        return value


@dataclass(frozen=True, slots=True, kw_only=True)
class RewriteContextPlan:
    """一次追问改写的本地提示装配结果，可直接交给受限生成客户端。"""

    messages: tuple[ChatMessage, ...]
    history_sequences: tuple[int, ...]
    input_tokens: int
    input_token_budget: int
    output_token_budget: int
    prompt_encoding_contract: str
    token_count_source: str


def parse_standalone_question(content: str) -> str:
    """解析改写响应；任何结构、字段或长度问题都抛 :class:`RewriteSchemaError`。"""

    try:
        data = json.loads(content)
    except (TypeError, ValueError) as error:
        raise RewriteSchemaError("改写响应不是合法 JSON") from error
    if not isinstance(data, dict):
        raise RewriteSchemaError("改写响应顶层必须是 JSON 对象")
    try:
        payload = _StandaloneQuestionPayload.model_validate(data)
    except ValidationError as error:
        raise RewriteSchemaError("改写响应结构非法") from error
    return payload.standalone_question.strip()


def _assemble_rewrite_messages(
    system_prompt: str, questions: Sequence[str], question: str
) -> tuple[ChatMessage, ...]:
    """装配单条 user 消息：历史问题在前、当前问题在后；不引入任何助手内容。"""

    if questions:
        numbered = "\n".join(
            f"{index}. {text}" for index, text in enumerate(questions, start=1)
        )
        content = (
            f"{REWRITE_HISTORY_HEADER}{REWRITE_SECTION_SEPARATOR}{numbered}"
            f"{REWRITE_SECTION_SEPARATOR}{REWRITE_QUESTION_HEADER}{question}"
        )
    else:
        content = f"{REWRITE_QUESTION_HEADER}{question}"
    return (
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(role="user", content=content),
    )


def _validate_required_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}不能为空")


def _validate_history(history: Sequence[HistoryTurn]) -> None:
    seen: set[int] = set()
    for turn in history:
        if not isinstance(turn.sequence, int):
            raise ValueError("历史轮次 sequence 必须是整数")
        if turn.sequence in seen:
            raise ValueError("历史轮次 sequence 不得重复")
        seen.add(turn.sequence)
        _validate_required_text(turn.question, "历史问题")


def plan_rewrite_context(
    *,
    system_prompt: str,
    question: str,
    estimator: PromptTokenEstimator,
    history: Sequence[HistoryTurn] = (),
    budget: ContextBudget | None = None,
) -> RewriteContextPlan:
    """在输入预算内组装改写提示；只纳入已授权历史轮次的用户问题。

    历史轮次先按「最近优先」取 ``budget.max_history_turns`` 轮，再逐轮尝试加入；放不下时
    丢弃该轮，其余合法的更早轮次继续尝试（与回答上下文的选择口径一致）。只有历史问题本身的
    结构 token 会让本地渲染失败时跳过该轮；系统指令与当前问题自身的渲染失败仍然上抛。
    """

    resolved_budget = ContextBudget() if budget is None else budget
    _validate_required_text(system_prompt, "改写信令")
    _validate_required_text(question, "当前问题")
    _validate_history(history)

    mandatory = _assemble_rewrite_messages(system_prompt, (), question)
    mandatory_tokens = estimator.estimate_chat_tokens(mandatory)
    if mandatory_tokens > resolved_budget.input_token_budget:
        raise MandatoryContextExceedsBudgetError(
            mandatory_tokens, resolved_budget.input_token_budget
        )

    authorized = [turn for turn in history if turn.authorized]
    recent_window = sorted(authorized, key=lambda turn: turn.sequence, reverse=True)[
        : resolved_budget.max_history_turns
    ]

    selected: list[HistoryTurn] = []
    for turn in recent_window:
        candidate_questions = [
            item.question for item in sorted([*selected, turn], key=lambda item: item.sequence)
        ]
        candidate = _assemble_rewrite_messages(system_prompt, candidate_questions, question)
        try:
            fits = (
                estimator.estimate_chat_tokens(candidate) <= resolved_budget.input_token_budget
            )
        except PromptEncodingError:
            # 单条历史问题含本地渲染器拒绝的结构 token：跳过该轮，其余候选继续。
            continue
        if fits:
            selected.append(turn)

    turns = tuple(sorted(selected, key=lambda turn: turn.sequence))
    messages = _assemble_rewrite_messages(
        system_prompt, [turn.question for turn in turns], question
    )
    return RewriteContextPlan(
        messages=messages,
        history_sequences=tuple(turn.sequence for turn in turns),
        input_tokens=estimator.estimate_chat_tokens(messages),
        input_token_budget=resolved_budget.input_token_budget,
        output_token_budget=resolved_budget.output_token_budget,
        prompt_encoding_contract=PROMPT_ENCODING_CONTRACT,
        token_count_source=TOKEN_COUNT_SOURCE,
    )


__all__ = [
    "MAX_STANDALONE_QUESTION_CHARS",
    "REWRITE_HISTORY_HEADER",
    "REWRITE_QUESTION_HEADER",
    "REWRITE_SECTION_SEPARATOR",
    "REWRITE_STAGE",
    "REWRITE_SYSTEM_PROMPT",
    "RewriteContextPlan",
    "RewriteSchemaError",
    "parse_standalone_question",
    "plan_rewrite_context",
]
