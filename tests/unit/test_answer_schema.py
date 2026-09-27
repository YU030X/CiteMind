"""回答结构严格校验单测：额外字段、类型、allowlist 与无引用回答的拒绝。"""

from __future__ import annotations

import json

import pytest
from rag_backend.generation.answer_schema import (
    AnswerSchemaError,
    citation_display_label,
    escape_literal_citation_markers,
    parse_answer,
)

ALLOWED = frozenset({"E1", "E2"})


def _payload(**overrides: object) -> str:
    base: dict[str, object] = {
        "sentences": [{"text": "制度规定。", "citationIds": ["E1"]}],
        "insufficientEvidence": False,
        "followUp": None,
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


def test_valid_answer_maps_sentences_and_citations() -> None:
    parsed = parse_answer(_payload(), allowed_citation_ids=ALLOWED)

    assert parsed.insufficient_evidence is False
    assert parsed.citation_ids == ("E1",)
    # 服务端在句末追加与 display_label 一致的 [n]，正文本身不改写。
    assert parsed.answer_text == "制度规定。[1]"
    assert parsed.sentences[0].text == "制度规定。"


def test_duplicate_citation_ids_are_deduplicated_in_order() -> None:
    content = _payload(
        sentences=[
            {"text": "第一句。", "citationIds": ["E2", "E1", "E2"]},
            {"text": "第二句。", "citationIds": ["E1"]},
        ]
    )
    parsed = parse_answer(content, allowed_citation_ids=ALLOWED)

    assert parsed.sentences[0].citation_ids == ("E2", "E1")
    assert parsed.citation_ids == ("E2", "E1")
    # 同一来源跨句标号一致；重复编号在同句内只出现一次。
    assert parsed.answer_text == "第一句。[2] [1]\n第二句。[1]"


def test_citation_display_label_maps_evidence_number() -> None:
    assert citation_display_label("E1") == "1"
    assert citation_display_label("E12") == "12"
    assert citation_display_label("legacy") == "legacy"


def test_literal_marker_in_model_text_is_escaped() -> None:
    # 模型自己写出的 [2] 不能变成已授权引用入口；只有服务端从结构化 citationIds
    # 派生的标记才是真正的引用。
    assert escape_literal_citation_markers("伪造[2]证据。") == "伪造\\[2\\]证据。"


def test_literal_marker_escaping_keeps_code_and_links() -> None:
    # 链接语法与其他非标记括号保持原样。
    assert escape_literal_citation_markers("见 [1](https://example.invalid)。") == (
        "见 [1](https://example.invalid)。"
    )
    # 代码区原样保留，反斜杠不得进入代码。
    assert escape_literal_citation_markers("`a[1]`") == "`a[1]`"
    assert escape_literal_citation_markers("```\na[1]\n```") == "```\na[1]\n```"
    # 非代码文本仍被转义。
    assert escape_literal_citation_markers("a[1] b") == "a\\[1\\] b"


def test_literal_marker_escaping_matches_markdown_semantics() -> None:
    # P1 路径 A：长反引号串不是合法围栏/代码区，markdown-it 当正文处理，必须转义。
    assert escape_literal_citation_markers("```X[9]Y``") == "```X\\[9\\]Y``"
    # P1 路径 B：相邻标记不能因 lookahead 跳过而留下未转义的第一个标记。
    assert escape_literal_citation_markers("adj[9][1]end") == (
        "adj\\[9\\]\\[1\\]end"
    )
    # 合法代码区（缩进、tilde 围栏、未闭合围栏、多反引号行内）原样保留。
    assert escape_literal_citation_markers("    [1]") == "    [1]"
    assert escape_literal_citation_markers("~~~\n[1]\n~~~") == "~~~\n[1]\n~~~"
    assert escape_literal_citation_markers("```\n[1]") == "```\n[1]"
    assert escape_literal_citation_markers("`` [1] ``") == "`` [1] ``"


def test_p1_server_output_has_no_undeclared_marker_buttons() -> None:
    # 与 frontend/tests/markdown.test.ts 的 P1 用例使用同一字符串。
    content = _payload(
        sentences=[
            {"text": "```X[9]Y``", "citationIds": ["E1"]},
            {"text": "adj[9][1]end", "citationIds": ["E1"]},
            {"text": "B", "citationIds": ["E9"]},
        ]
    )
    parsed = parse_answer(content, allowed_citation_ids=frozenset({"E1", "E9"}))

    assert parsed.answer_text == (
        "```X\\[9\\]Y``[1]\nadj\\[9\\]\\[1\\]end[1]\nB[9]"
    )


def test_trailing_backslash_does_not_swallow_server_marker() -> None:
    content = _payload(sentences=[{"text": "path C:\\", "citationIds": ["E1"]}])
    parsed = parse_answer(content, allowed_citation_ids=ALLOWED)

    # 句末孤立反斜杠被补成成对反斜杠，服务端追加的 [1] 仍是可识别的标记。
    assert parsed.answer_text == "path C:\\\\[1]"


def test_marker_parity_uses_actual_insertion_point() -> None:
    # 反斜杠后跟空白/换行时，标记插在反斜杠正后方；奇偶判定必须看插入点前缀，
    # 否则 ``a\ `` 会变成 ``a\[1]``（被转义、不可点）。偶数反斜杠不得多补。
    cases = {
        "a\\ ": "a\\\\[1] ",
        "a\\\t": "a\\\\[1]\t",
        "a\\\n": "a\\\\[1]\n",
        "a\\\\": "a\\\\[1]",
    }
    for source, expected in cases.items():
        content = _payload(sentences=[{"text": source, "citationIds": ["E1"]}])
        parsed = parse_answer(content, allowed_citation_ids=ALLOWED)
        assert parsed.answer_text == expected


def test_multi_sentence_blocks_keep_code_markers_and_append_after_block() -> None:
    content = _payload(
        sentences=[
            {
                "text": "示例：\n```python\nprint(1)  # [9]\n```",
                "citationIds": ["E1"],
            },
            {"text": "后续说明 [1]。", "citationIds": ["E1"]},
        ]
    )
    parsed = parse_answer(content, allowed_citation_ids=ALLOWED)

    # 围栏内 [9] 保真；句末标记另起一行；第二句自写的 [1] 被转义。
    assert parsed.answer_text == (
        "示例：\n```python\nprint(1)  # [9]\n```\n[1]\n后续说明 \\[1\\]。[1]"
    )


def test_cross_sentence_fence_escapes_marker_after_closing_fence() -> None:
    content = _payload(
        sentences=[
            {"text": "```\ncode [9]", "citationIds": ["E1"]},
            {"text": "```\ntext [8]", "citationIds": ["E2"]},
        ]
    )
    parsed = parse_answer(content, allowed_citation_ids=ALLOWED)

    # 整段解析才能知道第一句的围栏在第二句被关闭，关闭后的 [8] 必须转义。
    assert parsed.answer_text == "```\ncode [9]\n[1]\n```\ntext \\[8\\]\n[2]"


def test_refusal_without_sentences_is_accepted() -> None:
    parsed = parse_answer(
        _payload(sentences=[], insufficientEvidence=True),
        allowed_citation_ids=ALLOWED,
    )

    assert parsed.insufficient_evidence is True
    assert parsed.sentences == ()
    assert parsed.citation_ids == ()


def test_refusal_with_sentences_is_rejected() -> None:
    with pytest.raises(AnswerSchemaError):
        parse_answer(
            _payload(insufficientEvidence=True), allowed_citation_ids=ALLOWED
        )


def test_non_refusal_without_sentences_is_rejected() -> None:
    with pytest.raises(AnswerSchemaError):
        parse_answer(_payload(sentences=[]), allowed_citation_ids=ALLOWED)


def test_unknown_citation_id_is_rejected() -> None:
    with pytest.raises(AnswerSchemaError):
        parse_answer(
            _payload(sentences=[{"text": "句子。", "citationIds": ["E9"]}]),
            allowed_citation_ids=ALLOWED,
        )


def test_sentence_without_citation_is_rejected() -> None:
    with pytest.raises(AnswerSchemaError):
        parse_answer(
            _payload(sentences=[{"text": "句子。", "citationIds": []}]),
            allowed_citation_ids=ALLOWED,
        )


def test_extra_fields_are_rejected() -> None:
    content = json.dumps(
        {
            "sentences": [{"text": "句子。", "citationIds": ["E1"]}],
            "insufficientEvidence": False,
            "followUp": None,
            "url": "https://example.invalid",
        },
        ensure_ascii=False,
    )
    with pytest.raises(AnswerSchemaError):
        parse_answer(content, allowed_citation_ids=ALLOWED)

    nested_extra = json.dumps(
        {
            "sentences": [
                {"text": "句子。", "citationIds": ["E1"], "page": 3}
            ],
            "insufficientEvidence": False,
            "followUp": None,
        },
        ensure_ascii=False,
    )
    with pytest.raises(AnswerSchemaError):
        parse_answer(nested_extra, allowed_citation_ids=ALLOWED)


def test_wrong_types_are_rejected() -> None:
    for content in (
        "{not json",
        "[]",
        json.dumps({"sentences": "x", "insufficientEvidence": False}),
        json.dumps(
            {
                "sentences": [{"text": 1, "citationIds": ["E1"]}],
                "insufficientEvidence": False,
            }
        ),
        json.dumps(
            {
                "sentences": [{"text": "句。", "citationIds": [1]}],
                "insufficientEvidence": False,
            }
        ),
        json.dumps(
            {
                "sentences": [{"text": "句。", "citationIds": ["E1"]}],
                "insufficientEvidence": "false",
            }
        ),
    ):
        with pytest.raises(AnswerSchemaError):
            parse_answer(content, allowed_citation_ids=ALLOWED)


def test_blank_sentence_is_rejected() -> None:
    with pytest.raises(AnswerSchemaError):
        parse_answer(
            _payload(sentences=[{"text": "   ", "citationIds": ["E1"]}]),
            allowed_citation_ids=ALLOWED,
        )
