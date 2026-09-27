"""回答结构的严格解析：拒绝额外字段、未知引用 ID 与无引用的回答。

模型只能返回由 :mod:`rag_backend.generation.context_budget` 分配的临时 ``E`` 编号；
本模块只做结构校验，不接触数据库、网络或权限。服务端随后把 ``E`` 编号映射成已保存的
citation（含版本、locator 与短引文），模型永远不能提交来源 URL、页码或数据库 ID。

规则固定：

- 结构必须严格匹配，任何额外字段、缺失字段或类型不符都判为非法响应；
- ``insufficient_evidence=true`` 时不得返回任何句子，否则非法；
- 需要回答时句子必须非空，且每句至少引用一个 allowlist 内的 ``E`` 编号；
- 任何一句引用了 allowlist 之外的编号都判为非法响应，绝不静默丢弃该引用。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass

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


class AnswerSchemaError(RuntimeError):
    """模型回答不满足严格结构或引用了 allowlist 之外的编号。"""


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
    for sentence in payload.sentences:
        ordered = tuple(dict.fromkeys(sentence.citation_ids))
        unknown = [citation_id for citation_id in ordered if citation_id not in allowlist]
        if unknown:
            raise AnswerSchemaError("模型引用了本次证据之外的编号")
        sentences.append(ParsedSentence(text=sentence.text, citation_ids=ordered))

    return ParsedAnswer(
        insufficient_evidence=False,
        sentences=tuple(sentences),
        answer_text="\n".join(sentence.text for sentence in sentences),
        follow_up=payload.follow_up,
    )


__all__ = [
    "MAX_FOLLOW_UP_CHARS",
    "MAX_SENTENCES",
    "MAX_SENTENCE_CHARS",
    "AnswerSchemaError",
    "ParsedAnswer",
    "ParsedSentence",
    "parse_answer",
]
