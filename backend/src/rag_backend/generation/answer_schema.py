"""回答结构的严格解析：拒绝额外字段、未知引用 ID 与无引用的回答，并渲染服务端引用标记。

模型只能返回由 :mod:`rag_backend.generation.context_budget` 分配的临时 ``E`` 编号；
本模块只做结构校验与纯文本渲染，不接触数据库、网络或权限。服务端随后把 ``E`` 编号映射成
已保存的 citation（含版本、locator 与短引文），模型永远不能提交来源 URL、页码或数据库 ID。

规则固定：

- 结构必须严格匹配，任何额外字段、缺失字段或类型不符都判为非法响应；
- ``insufficient_evidence=true`` 时不得返回任何句子，否则非法；
- 需要回答时句子必须非空，且每句至少引用一个 allowlist 内的 ``E`` 编号；
- 任何一句引用了 allowlist 之外的编号都判为非法响应，绝不静默丢弃该引用；
- 持久化的 ``answer_text`` 在每句末尾追加服务端生成的 ``[n]`` 标记，``n`` 即该来源的
  ``display_label``；模型正文自带的裸 ``[n]`` 会被转义成字面文本，不能伪造引用入口。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass

from markdown_it import MarkdownIt
from markdown_it.rules_inline import StateInline
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)
from pydantic.alias_generators import to_camel

# 单句与整次回答的边界；超出即非法响应，不做静默截断。
MAX_SENTENCE_CHARS = 2000
MAX_SENTENCES = 64
MAX_FOLLOW_UP_CHARS = 500

# 服务端在句末追加的引用标记形如 ``[1]``；紧邻链接语法（``[1](``、``[1][``、``[1]:``）的不算。
# 这里只做形态匹配，是否属于正文、是否位于代码区由 markdown-it 的实际 token 流判定。
_CITATION_MARKER = re.compile(r"\[(\d{1,3})\]")
_LINK_FOLLOW = frozenset({"(", "[", ":"})

# 与前端渲染器对齐的 markdown-it 配置（关闭原始 HTML 与 linkify、开启 breaks），
# 用它判断代码区/链接/正文，避免用正则近似 Markdown 语义而漏判未声明的正文标记。
_MARKDOWN = MarkdownIt("commonmark", {"html": False, "linkify": False, "breaks": True})
_LITERAL_MARKER_POSITIONS_KEY = "literal_citation_marker_positions"


class AnswerSchemaError(RuntimeError):
    """模型回答不满足严格结构或引用了 allowlist 之外的编号。"""


def citation_display_label(evidence_id: str) -> str:
    """把临时证据编号 ``E1`` 映射为持久化、可点击的引用标号 ``1``。

    标号与 citation 的 ``display_label``（以及前端 ``[n]`` 映射键）保持一一对应；
    非 ``E`` 前缀的编号原样返回，便于历史数据或未来编号方案兼容。
    """

    return evidence_id[1:] if evidence_id.startswith("E") else evidence_id


def _capture_literal_marker(state: StateInline, silent: bool) -> bool:
    """记录 markdown-it 当正文处理、形态像 ``[n]`` 的位置；链接语法交给 link 规则。"""

    if state.src[state.pos] != "[":
        return False
    match = _CITATION_MARKER.match(state.src, state.pos)
    if match is None:
        return False
    if state.src[match.end() : match.end() + 1] in _LINK_FOLLOW:
        return False
    if not silent:
        positions = state.env.setdefault(_LITERAL_MARKER_POSITIONS_KEY, [])
        positions.append(state.pos)
    return False


_MARKDOWN.inline.ruler.before("link", "capture_literal_marker", _capture_literal_marker)


def _literal_marker_positions(inline_source: str) -> tuple[int, ...]:
    env: dict[str, object] = {}
    _MARKDOWN.parseInline(inline_source, env)
    positions = env.get(_LITERAL_MARKER_POSITIONS_KEY)
    if not isinstance(positions, list):
        return ()
    return tuple(position for position in positions if isinstance(position, int))


def _escape_inline_source(inline_source: str) -> str:
    """转义一段内联源码里的正文 ``[n]``，直到再次解析不再产生候选。"""

    current = inline_source
    for _ in range(8):
        positions = _literal_marker_positions(current)
        if not positions:
            return current
        parts: list[str] = []
        cursor = 0
        for position in positions:
            match = _CITATION_MARKER.match(current, position)
            if match is None:  # pragma: no cover - 位置来自同一解析器，不会失配
                continue
            parts.append(current[cursor:position])
            parts.append(f"\\[{match.group(1)}\\]")
            cursor = match.end()
        parts.append(current[cursor:])
        current = "".join(parts)
    return current


def _escape_all_literal_markers(text: str) -> str:
    """回填失败时的保守兜底：宁可多转义，也不留下未声明的正文标记。"""

    return _CITATION_MARKER.sub(lambda match: f"\\[{match.group(1)}\\]", text)


def _escape_document(text: str) -> str:
    """按 markdown-it 的实际块/内联划分，只转义正文里的 ``[n]``。"""

    lines = text.split("\n")
    new_lines = list(lines)
    for token in _MARKDOWN.parse(text):
        if token.type != "inline" or token.map is None:
            continue
        escaped = _escape_inline_source(token.content)
        if escaped == token.content:
            continue
        content_lines = token.content.split("\n")
        escaped_lines = escaped.split("\n")
        start, end = token.map
        if len(content_lines) != end - start or len(escaped_lines) != len(content_lines):
            return _escape_all_literal_markers(text)
        for offset, content_line in enumerate(content_lines):
            index = start + offset
            original = new_lines[index]
            stripped = original.rstrip(" \t")
            if not stripped.endswith(content_line):
                return _escape_all_literal_markers(text)
            prefix = stripped[: len(stripped) - len(content_line)]
            suffix = original[len(stripped) :]
            new_lines[index] = f"{prefix}{escaped_lines[offset]}{suffix}"
    return "\n".join(new_lines)


def escape_literal_citation_markers(text: str) -> str:
    """转义模型正文里自带的裸 ``[n]``，避免被当成本次授权之外的引用标记。

    判定与前端 markdown-it 一致：代码区（围栏、缩进、行内代码）原样保留，链接语法
    （``[n](``、``[n][``、``[n]:``）不被破坏，其余正文 ``[n]`` 一律转义成字面文本。
    转义会改变相邻标记的上下文，因此迭代到不再产生新候选为止。
    """

    current = text
    for _ in range(8):
        escaped = _escape_document(current)
        if escaped == current:
            return current
        current = escaped
    return current


def _count_trailing_backslashes(text: str) -> int:
    count = 0
    for character in reversed(text):
        if character != "\\":
            break
        count += 1
    return count


def _insert_markers_at_last_inline(text: str, markers: str) -> str:
    tokens = _MARKDOWN.parse(text)
    if not tokens:
        return f"{text}{markers}"
    if tokens[-1].type in {"fence", "code_block", "hr"}:
        # 正文以块级代码或分隔线结束时，标记必须另起一行，否则关闭围栏会失效。
        return f"{text}{markers}" if text.endswith("\n") else f"{text}\n{markers}"
    inline_tokens = [token for token in tokens if token.type == "inline"]
    if not inline_tokens or inline_tokens[-1].map is None:
        return f"{text}{markers}"
    line_index = inline_tokens[-1].map[1] - 1
    lines = text.split("\n")
    if line_index < 0 or line_index >= len(lines):
        return f"{text}{markers}"
    stripped = lines[line_index].rstrip(" \t")
    offset = sum(len(line) + 1 for line in lines[:line_index]) + len(stripped)
    # 判定基准必须是真实插入点前缀，而不是整段 text 的尾部：句末反斜杠后跟空格/制表符/换行时，
    # 标记插在反斜杠正后方，只有 ``text[:offset]`` 的奇偶才对是否转义 ``[`` 有意义。
    prefix = text[:offset]
    if _count_trailing_backslashes(prefix) % 2 == 1:
        # 插入点前的孤立反斜杠会转义紧随其后的 ``[``，补一个让它先成对消费。
        prefix = f"{prefix}\\"
    return f"{prefix}{markers}{text[offset:]}"


def _append_citation_markers(text: str, markers: str) -> str:
    """在句子末尾追加服务端合法标记；避免句末反斜杠或块级代码吞掉标记。"""

    if not markers:
        return text
    # 末尾反斜杠的奇偶保护由 ``_insert_markers_at_last_inline`` 在真实插入点完成，这里不再重复。
    return _insert_markers_at_last_inline(text, markers)


# 模型按外部 camelCase 契约返回；这里接受 camelCase，也允许直接按 Python 字段名构造。
_ANSWER_MODEL_CONFIG = ConfigDict(
    extra="forbid", strict=True, alias_generator=to_camel, populate_by_name=True
)


class _AnswerSentence(BaseModel):
    model_config = _ANSWER_MODEL_CONFIG

    text: str = Field(min_length=1, max_length=MAX_SENTENCE_CHARS)
    citation_ids: list[str] = Field(min_length=1, max_length=MAX_SENTENCES)

    @field_validator("text")
    @classmethod
    def _reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("句子正文不能为空白")
        return value


class _AnswerPayload(BaseModel):
    model_config = _ANSWER_MODEL_CONFIG

    sentences: list[_AnswerSentence] = Field(max_length=MAX_SENTENCES)
    insufficient_evidence: bool
    follow_up: str | None = Field(default=None, max_length=MAX_FOLLOW_UP_CHARS)


@dataclass(frozen=True, slots=True)
class ParsedSentence:
    """一条通过校验的句子与它引用的临时证据编号（保序去重）。"""

    text: str
    citation_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ParsedAnswer:
    """一次通过校验的模型回答；拒绝回答时 ``sentences`` 为空。"""

    insufficient_evidence: bool
    sentences: tuple[ParsedSentence, ...]
    answer_text: str
    follow_up: str | None

    @property
    def citation_ids(self) -> tuple[str, ...]:
        """按出现顺序去重后的全部引用编号。"""

        seen: dict[str, None] = {}
        for sentence in self.sentences:
            for citation_id in sentence.citation_ids:
                seen.setdefault(citation_id, None)
        return tuple(seen)


def parse_answer(content: str, *, allowed_citation_ids: Iterable[str]) -> ParsedAnswer:
    """解析模型回答文本；任何结构或引用问题都抛 :class:`AnswerSchemaError`。"""

    allowlist = frozenset(allowed_citation_ids)
    try:
        data = json.loads(content)
    except (TypeError, ValueError) as error:
        raise AnswerSchemaError("模型响应不是合法 JSON") from error
    if not isinstance(data, dict):
        raise AnswerSchemaError("模型响应顶层必须是 JSON 对象")
    try:
        payload = _AnswerPayload.model_validate(data)
    except ValidationError as error:
        raise AnswerSchemaError("模型响应结构非法") from error

    if payload.insufficient_evidence:
        if payload.sentences:
            # 拒绝回答时还带句子属于自相矛盾；不静默只取其一。
            raise AnswerSchemaError("拒绝回答时不得返回句子")
        return ParsedAnswer(
            insufficient_evidence=True,
            sentences=(),
            answer_text="",
            follow_up=payload.follow_up,
        )

    if not payload.sentences:
        # 没有拒答却没有任何句子＝无引用的静默回答，必须显式失败。
        raise AnswerSchemaError("非拒答回答必须包含至少一句引用句")

    sentences: list[ParsedSentence] = []
    source_texts: list[str] = []
    marker_groups: list[str] = []
    for sentence in payload.sentences:
        ordered = tuple(dict.fromkeys(sentence.citation_ids))
        unknown = [citation_id for citation_id in ordered if citation_id not in allowlist]
        if unknown:
            raise AnswerSchemaError("模型引用了本次证据之外的编号")
        sentences.append(ParsedSentence(text=sentence.text, citation_ids=ordered))
        source_texts.append(sentence.text)
        # 标号之间用空格分隔：`[2][1]` 会被 Markdown 当成引用式链接语法，空格保证逐个可识别。
        marker_groups.append(" ".join(f"[{citation_display_label(cid)}]" for cid in ordered))

    # 按整次回答统一解析 markdown：跨句的代码区/链接语义必须与前端整段渲染一致。
    escaped_joined = escape_literal_citation_markers("\n".join(source_texts))
    escaped_lines = escaped_joined.split("\n")
    rendered: list[str] = []
    cursor = 0
    for parsed_sentence, markers in zip(sentences, marker_groups, strict=True):
        line_count = parsed_sentence.text.count("\n") + 1
        segment = "\n".join(escaped_lines[cursor : cursor + line_count])
        cursor += line_count
        rendered.append(_append_citation_markers(segment, markers))

    return ParsedAnswer(
        insufficient_evidence=False,
        sentences=tuple(sentences),
        answer_text="\n".join(rendered),
        follow_up=payload.follow_up,
    )


__all__ = [
    "MAX_FOLLOW_UP_CHARS",
    "MAX_SENTENCES",
    "MAX_SENTENCE_CHARS",
    "AnswerSchemaError",
    "ParsedAnswer",
    "ParsedSentence",
    "citation_display_label",
    "escape_literal_citation_markers",
    "parse_answer",
]
