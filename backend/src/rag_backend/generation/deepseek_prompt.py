"""DeepSeek V4.1 文本 chat 提示的本地渲染契约（纯函数，不导入 ``tokenizers``）。

本模块把 ``system`` / ``user`` / ``assistant`` 三种角色的文本消息渲染成 DeepSeek V4.1 的
**chat（非思考）** 提示文本，作为本地 token 估算的唯一输入。它只做字符串拼接，不联网、不读配置、
不接触数据库，也不导入任何 tokenizer 库，因此可以在 API 进程安全导入。

渲染子集逐字取自 **recipe 源码**（生产提示模板契约）：
``deepseek-ai/deepseek-recipe``，commit ``8cadfede7063c896b944e7bae05daa3549ae97ea``（``main``，
2026-09-10），文件 ``deepseek-recipe-encoding/src/v4/mod.rs``（``render_message`` /
``render_conversation``）与 ``deepseek-recipe-encoding/src/v4/dsv41.rs``（V4.1 变体：
``system_token() == "<｜System｜>"``）。可追溯 URL 见
:data:`PROMPT_ENCODING_REFERENCE_URL` 与 :data:`PROMPT_ENCODING_REFERENCE_VARIANT_URL`，许可 MIT。

该 commit 下 ``thinking_mode=false``（非思考）、无工具、无图片、无 reasoning content 的实际语义是：

1. 提示以 ``<｜begin▁of▁sentence｜>`` 开头。
2. ``system``：``<｜System｜>`` + 正文（V4.1 有 system 专用特殊 token；同仓库另一份 V4 参考
   ``encoding/encoding_dsv4.py`` 没有它，那份不是本契约的依据）。
3. ``user``：``<｜User｜>`` + 正文。recipe 会把连续 user/tool 消息用空行合并；本模块不支持连续
   同角色消息，遇到即拒绝，绝不猜测合并语义。
4. ``assistant``：``<｜Assistant｜></think>`` + 正文 + ``<｜end▁of▁sentence｜>``；非思考模式的
   ``</think>`` 前缀属于 assistant 轮次自身，不是接在 user 正文之后。
5. 最后一条消息之后追加生成前缀 ``<｜Assistant｜></think>``。

同一 commit 下 ``thinking_mode=true``（思考）的差异只有三处，均由同两份源码逐字推出：

1. 首条消息之前插入 ``<｜System｜>`` 加一行强度说明
   ``Reasoning Effort: {score} (range 1-100, the higher the value, the more thorough the``
   `` reasoning)`` 与空行；``score`` 按 ``dsv41.rs`` 的 ``reasoning_effort_template`` 取
   ``low=50``、``high=75``（未指定强度也按 75）、``max=100``。
2. 历史 ``assistant`` 轮次渲染空思考块：``<｜Assistant｜><think></think>`` + 正文 + EOS
   （无 ``reasoning_content`` 时 recipe 仍补 ``<think>`` 与 ``</think>``）。
3. 末尾生成前缀为 ``<｜Assistant｜><think>``，等待模型先输出思考段。

强度取值只接受本项目已验证的 ``low``/``high``/``max``；关闭思考由
:class:`ThinkingChoice` 的 ``enabled``（请求侧 ``thinking.type=disabled``）表达。recipe 适配器
或公开文档里出现的其它 ``reasoning_effort`` 取值不能据此推断为云 API 支持。

这是**本地复刻**而不是官方实现：官方 ``deepseek-recipe`` 是原生扩展（无 Windows wheel），HF
参考脚本也不在本仓库依赖内；改动渲染规则必须提升 :data:`PROMPT_ENCODING_CONTRACT`，不能靠 revision
静默漂移。因此本地计数**只是估算**——它不含 provider 服务端模板的内部细节，官方也明文以响应
``usage`` 为准；:data:`TOKEN_COUNT_SOURCE` 固定标出这一点，调用方不得把估算值当 provider 精确用量。

tokenizer 产物是另一个独立来源（HF 模型仓库及其 revision），其身份钉死在
:mod:`rag_backend.generation.deepseek_token_counting`，与这里的 recipe 源码 revision 互不替代。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

# 官方编码常量；拼写必须逐字一致，否则会改变提示结构。BOS/EOS/`</think>` 与 recipe 的
# BOS_TOKEN/EOS_TOKEN/THINKING_END_TOKEN 相同；SYSTEM 只存在于 V4.1 变体。
BOS_TOKEN = "<｜begin▁of▁sentence｜>"
EOS_TOKEN = "<｜end▁of▁sentence｜>"
SYSTEM_SP_TOKEN = "<｜System｜>"
USER_SP_TOKEN = "<｜User｜>"
ASSISTANT_SP_TOKEN = "<｜Assistant｜>"
# 非思考模式（``thinking_mode=false``）在 assistant 轮次开头立即关闭思考段。
THINKING_END_TOKEN = "</think>"
# 思考模式（``thinking_mode=true``）在 assistant 轮次与末尾生成前缀处开启思考段。
THINKING_START_TOKEN = "<think>"

# 本项目已验证并暴露的强度取值；关闭思考由 ``ThinkingChoice.enabled`` 表达。
ReasoningEffort = Literal["low", "high", "max"]
SUPPORTED_REASONING_EFFORTS: tuple[ReasoningEffort, ...] = ("low", "high", "max")
DEFAULT_REASONING_EFFORT: ReasoningEffort = "high"
# recipe ``dsv41.rs`` 的 ``reasoning_effort_template``：low=50、high/None=75、max=100。
_REASONING_EFFORT_SCORES: dict[ReasoningEffort, int] = {"low": 50, "high": 75, "max": 100}
_REASONING_EFFORT_PREAMBLE = (
    "Reasoning Effort: {score} "
    "(range 1-100, the higher the value, the more thorough the reasoning)\n\n"
)

# 消息正文中不得出现的结构 token：出现即拒绝，避免本地渲染出结构有歧义、计数无意义的提示。
SCAFFOLD_SENTINEL_TOKENS = (
    BOS_TOKEN,
    EOS_TOKEN,
    SYSTEM_SP_TOKEN,
    USER_SP_TOKEN,
    ASSISTANT_SP_TOKEN,
)

# 本地渲染契约的具名版本；改变拼接规则、间距或特殊 token 必须提升它。
# v2：改用 recipe 源码（``deepseek-recipe``）作为唯一渲染参考，补上 V4.1 的 ``<｜System｜>``，
# 并把 ``<｜Assistant｜></think>`` 归还给 assistant 轮次和末尾生成前缀。
# v3：增加 recipe ``thinking_mode=true`` 变体（强度说明前缀、空思考块与 ``<think>`` 生成前缀）；
# 非思考输出与 v2 逐字一致。
PROMPT_ENCODING_CONTRACT = "deepseek-v41-chat-v3"
# 渲染参考是 recipe 源码；它的 revision 与 tokenizer 产物的 HF 仓库 revision 是两个独立事实。
PROMPT_ENCODING_REFERENCE_REPOSITORY = "deepseek-ai/deepseek-recipe"
PROMPT_ENCODING_REFERENCE_REVISION = "8cadfede7063c896b944e7bae05daa3549ae97ea"
PROMPT_ENCODING_REFERENCE_PATH = "deepseek-recipe-encoding/src/v4/mod.rs"
PROMPT_ENCODING_REFERENCE_VARIANT_PATH = "deepseek-recipe-encoding/src/v4/dsv41.rs"
PROMPT_ENCODING_REFERENCE_URL = (
    f"https://github.com/{PROMPT_ENCODING_REFERENCE_REPOSITORY}/blob/"
    f"{PROMPT_ENCODING_REFERENCE_REVISION}/{PROMPT_ENCODING_REFERENCE_PATH}"
)
PROMPT_ENCODING_REFERENCE_VARIANT_URL = (
    f"https://github.com/{PROMPT_ENCODING_REFERENCE_REPOSITORY}/blob/"
    f"{PROMPT_ENCODING_REFERENCE_REVISION}/{PROMPT_ENCODING_REFERENCE_VARIANT_PATH}"
)
PROMPT_ENCODING_REFERENCE_ENTRYPOINT = (
    "EncodingV4::render_conversation(thinking_mode=false)"
)

# 计数口径标记：本地 tokenizer + 本地 chat 包装的估算值，绝不是 provider 精确用量。
TOKEN_COUNT_SOURCE = "LOCAL_TOKENIZER_ESTIMATE"

# 支持的输出角色；当前切片只渲染纯文本，不渲染工具调用、图片、reasoning content 或 latest_reminder。
ChatRole = Literal["system", "user", "assistant"]

_ROLE_TEMPLATE_HINT = "纯文本 chat（无工具、无图片、无 reasoning content）"


class PromptEncodingError(RuntimeError):
    """消息序列不满足本地渲染契约；消息静态脱敏，不回显正文。"""


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatMessage:
    """一条待渲染的 chat 消息；``role`` 决定使用的特殊 token 包装。"""

    role: ChatRole
    content: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ThinkingChoice:
    """一次请求的思考开关与强度；``effort`` 只在开启思考时有意义。

    ``effort=None`` 且开启表示沿用官方默认强度 ``high``（recipe 对 ``None`` 也按 75 计），
    因此本地渲染无需调用方先补默认值。关闭思考时不得携带强度。
    """

    enabled: bool = False
    effort: ReasoningEffort | None = None

    def __post_init__(self) -> None:
        if self.effort is not None and self.effort not in SUPPORTED_REASONING_EFFORTS:
            raise ValueError("reasoning_effort 必须是 low/high/max")
        if not self.enabled and self.effort is not None:
            raise ValueError("thinking 关闭时不得指定 reasoning_effort")

    @property
    def effective_effort(self) -> ReasoningEffort | None:
        """实际生效强度；关闭时为 None，开启且未指定时为官方默认 ``high``。"""

        if not self.enabled:
            return None
        return self.effort if self.effort is not None else DEFAULT_REASONING_EFFORT


# 默认选择：沿用既有行为，显式关闭 thinking（不依赖供应商默认）。
NON_THINKING = ThinkingChoice()


class PromptTokenEstimator(Protocol):
    """提示 token 估算接口；本模块只依赖它，不依赖具体 tokenizer 实现。"""

    def estimate_chat_tokens(
        self, messages: Sequence[ChatMessage], *, thinking: ThinkingChoice = NON_THINKING
    ) -> int: ...


def _validate_content(role: ChatRole, content: str) -> None:
    """拒绝正文里的结构 token；错误消息静态，不回显正文片段。"""

    for token in SCAFFOLD_SENTINEL_TOKENS:
        if token in content:
            raise PromptEncodingError(f"{role} 消息正文包含提示结构 token，本地渲染拒绝")


def _validate_messages(messages: Sequence[ChatMessage]) -> None:
    """校验消息序列满足 :data:`_ROLE_TEMPLATE_HINT` 的子集约束。"""

    if not messages:
        raise PromptEncodingError("提示至少需要一条消息")
    roles = [message.role for message in messages]
    if roles[0] == "assistant":
        raise PromptEncodingError("提示不得以 assistant 消息开头")
    if roles[-1] != "user":
        # 末条必须是待回答的 user 消息；末尾 assistant 属另一种提示契约，本片不支持。
        raise PromptEncodingError("提示必须以 user 消息结尾")
    if roles.count("system") > 1:
        raise PromptEncodingError("system 消息最多一条")
    if "system" in roles and roles[0] != "system":
        raise PromptEncodingError("system 消息必须在最前")
    for previous, current in zip(roles, roles[1:]):
        if previous == current:
            raise PromptEncodingError("相邻消息不得同角色；连续 user 必须先合并为一条")
        if current == "system":
            raise PromptEncodingError("system 消息只能出现在最前")
    for message in messages:
        _validate_content(message.role, message.content)


def reasoning_effort_preamble(thinking: ThinkingChoice) -> str:
    """返回 recipe ``dsv41.rs`` 在思考模式首条消息前插入的强度说明文本。"""

    if not thinking.enabled:
        raise ValueError("非思考模式没有 reasoning_effort 前缀")
    effort = thinking.effective_effort
    assert effort is not None  # enabled 时 effective_effort 必不为 None
    return _REASONING_EFFORT_PREAMBLE.format(score=_REASONING_EFFORT_SCORES[effort])


def render_chat_prompt(
    messages: Sequence[ChatMessage], *, thinking: ThinkingChoice = NON_THINKING
) -> str:
    """把消息序列渲染为 DeepSeek V4.1 提示文本（非思考或思考变体）；不合法时抛异常。"""

    _validate_messages(messages)
    parts: list[str] = [BOS_TOKEN]
    if thinking.enabled:
        # recipe：思考模式在首条消息之前先补 ``<｜System｜>`` 与强度说明，无论首条是否 system。
        parts.append(SYSTEM_SP_TOKEN)
        parts.append(reasoning_effort_preamble(thinking))
    for message in messages:
        if message.role == "system":
            if not thinking.enabled:
                parts.append(SYSTEM_SP_TOKEN)
            parts.append(message.content)
        elif message.role == "user":
            parts.append(USER_SP_TOKEN)
            parts.append(message.content)
        else:
            # recipe 把 ``<｜Assistant｜>`` 与思考段标记都算在 assistant 轮次里；无
            # ``reasoning_content`` 的历史轮次渲染空思考块 ``<think></think>``。
            parts.append(ASSISTANT_SP_TOKEN)
            if thinking.enabled:
                parts.append(THINKING_START_TOKEN)
            parts.append(THINKING_END_TOKEN)
            parts.append(message.content)
            parts.append(EOS_TOKEN)
    # 末条 user 之后的生成前缀；思考模式等待模型先输出思考段，因此补 ``<think>``。
    parts.append(ASSISTANT_SP_TOKEN)
    parts.append(THINKING_START_TOKEN if thinking.enabled else THINKING_END_TOKEN)
    return "".join(parts)


__all__ = [
    "ASSISTANT_SP_TOKEN",
    "BOS_TOKEN",
    "ChatMessage",
    "ChatRole",
    "DEFAULT_REASONING_EFFORT",
    "EOS_TOKEN",
    "NON_THINKING",
    "PROMPT_ENCODING_CONTRACT",
    "PROMPT_ENCODING_REFERENCE_ENTRYPOINT",
    "PROMPT_ENCODING_REFERENCE_PATH",
    "PROMPT_ENCODING_REFERENCE_REPOSITORY",
    "PROMPT_ENCODING_REFERENCE_REVISION",
    "PROMPT_ENCODING_REFERENCE_URL",
    "PROMPT_ENCODING_REFERENCE_VARIANT_PATH",
    "PROMPT_ENCODING_REFERENCE_VARIANT_URL",
    "PromptEncodingError",
    "PromptTokenEstimator",
    "ReasoningEffort",
    "SCAFFOLD_SENTINEL_TOKENS",
    "SUPPORTED_REASONING_EFFORTS",
    "SYSTEM_SP_TOKEN",
    "THINKING_END_TOKEN",
    "THINKING_START_TOKEN",
    "TOKEN_COUNT_SOURCE",
    "ThinkingChoice",
    "USER_SP_TOKEN",
    "reasoning_effort_preamble",
    "render_chat_prompt",
]
