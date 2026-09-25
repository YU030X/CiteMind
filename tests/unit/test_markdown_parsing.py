"""Markdown 解析与切分的聚焦单测：来源位置、预算与无静默截断。

这些测试只使用显式假计数器（1 字符 = 1 token），不加载任何真实 tokenizer，也不连接
数据库、Redis 或模型；因此这里通过不等于“已验收真实模型的 512 token 上限”。
"""

from __future__ import annotations

import hashlib
import json
from typing import cast

import pytest
from rag_backend.ingestion import parsing
from rag_backend.ingestion.chunking import (
    CHUNKER_VERSION,
    Chunk,
    ChunkBudget,
    ChunkBudgetExceeded,
    ChunkingError,
    NoChunkableContent,
    build_model_input,
    chunk_markdown,
)
from rag_backend.ingestion.parsing import ParsedDocument, parse_markdown


class CharacterCounter:
    """显式测试假计数器：把每个字符当成一个 token，不做真实分词。"""

    def count_tokens(self, text: str) -> int:
        return len(text)


class SeparatorDiscountCounter:
    """显式测试假计数器：``len(text) - 10 * 双换行数``。

    它刻意非严格单调：拼接时插入的 ``\n\n`` 会把计数压低，用于复现“同一块的两个片段
    被打包进同一 chunk”的非法拼接路径。它不是真实 tokenizer。
    """

    def count_tokens(self, text: str) -> int:
        return len(text) - 10 * text.count("\n\n")


def _document(source: str) -> ParsedDocument:
    return parse_markdown(source.encode("utf-8"))


def _segments(chunk: Chunk) -> list[dict[str, int]]:
    return cast("list[dict[str, int]]", chunk.source_locator["segments"])


# ---------------------------------------------------------------------------
# 解析：字节来源、行号映射、heading_path、列表与代码块
# ---------------------------------------------------------------------------


def test_parser_version_pins_implementation_not_legacy_placeholder() -> None:
    """解析器版本是真实实现版本，不再是上传阶段的占位值。"""

    assert parsing.MARKDOWN_PARSER_VERSION == "markdown-it-py-4.2.0-v1"
    assert parsing.MARKDOWN_PARSER_VERSION != "markdown-v1"


def test_source_hash_follows_raw_bytes_not_decoded_text() -> None:
    lf = b"# T\n\npara\n"
    crlf = b"# T\r\n\r\npara\r\n"

    lf_doc = parse_markdown(lf)
    crlf_doc = parse_markdown(crlf)

    assert lf_doc.source_sha256 == hashlib.sha256(lf).hexdigest()
    assert crlf_doc.source_sha256 == hashlib.sha256(crlf).hexdigest()
    assert lf_doc.source_sha256 != crlf_doc.source_sha256
    # 逻辑正文一致，但来源 hash 不同：说明哈希只依赖原始字节。
    assert [block.text for block in lf_doc.blocks] == [
        block.text for block in crlf_doc.blocks
    ]


def test_line_map_is_converted_from_0based_halfopen_to_1based_inclusive() -> None:
    source = "# T\n\nfirst\nsecond line\n\n```sh\nx=1\n```\n"
    document = parse_markdown(source.encode("utf-8"))

    paragraph = document.blocks[0]
    assert paragraph.kind == "paragraph"
    assert (paragraph.start_line, paragraph.end_line) == (3, 4)

    code = document.blocks[1]
    assert code.kind == "code_block"
    assert code.code_info == "sh"
    assert (code.start_line, code.end_line) == (6, 8)


def test_heading_path_tracks_nesting_and_sibling_reset() -> None:
    source = (
        "# Top\n"
        "\n"
        "intro\n"
        "\n"
        "## Sub A\n"
        "\n"
        "alpha\n"
        "\n"
        "## Sub B\n"
        "\n"
        "beta\n"
    )
    document = _document(source)

    paths = [(block.text, block.heading_path) for block in document.blocks]
    assert paths == [
        ("intro", ("Top",)),
        ("alpha", ("Top", "Sub A")),
        ("beta", ("Top", "Sub B")),
    ]


def test_nested_list_items_keep_order_depth_and_lines() -> None:
    source = "# Doc\n\n- item one\n  - nested a\n  - nested b\n- item two\n"
    document = _document(source)

    items = [block for block in document.blocks if block.kind == "list_item"]
    assert [(block.text, block.list_depth, block.start_line) for block in items] == [
        ("item one", 1, 3),
        ("nested a", 2, 4),
        ("nested b", 2, 5),
        ("item two", 1, 6),
    ]
    assert all(block.heading_path == ("Doc",) for block in items)


def test_empty_heading_resets_deeper_heading_path() -> None:
    source = "# A\n\n### C\n\nunder c\n\n#\n\nbody\n"
    document = _document(source)

    assert [(block.text, block.heading_path) for block in document.blocks] == [
        ("under c", ("A", "C")),
        ("body", ()),
    ]


def test_blockquote_heading_does_not_leak_to_outer_content() -> None:
    source = "# Real\n\n> ## Fake\n\n### Sub\n\nbody\n"
    document = _document(source)

    assert [(block.text, block.heading_path) for block in document.blocks] == [
        ("body", ("Real", "Sub")),
    ]


def test_blockquote_heading_applies_only_inside_the_quote() -> None:
    source = "# Real\n\n> ## Fake\n>\n> quoted body\n\nafter\n"
    document = _document(source)

    assert [(block.text, block.heading_path) for block in document.blocks] == [
        ("quoted body", ("Real", "Fake")),
        ("after", ("Real",)),
    ]


def test_list_internal_heading_does_not_leak_to_outer_content() -> None:
    source = "# Top\n\n- intro\n\n  ## InList\n\n  body in list\n\nouter tail\n"
    document = _document(source)

    assert [(block.text, block.heading_path) for block in document.blocks] == [
        ("intro", ("Top",)),
        ("body in list", ("Top", "InList")),
        ("outer tail", ("Top",)),
    ]


def test_leading_bom_is_ignored_but_hash_uses_raw_bytes() -> None:
    raw = b"\xef\xbb\xbf# H\r\n\r\nbody\r\n"
    document = parse_markdown(raw)

    assert document.source_sha256 == hashlib.sha256(raw).hexdigest()
    assert [(block.text, block.heading_path) for block in document.blocks] == [
        ("body", ("H",)),
    ]
    # CRLF 与 BOM 不改变 1-based 行号。
    assert document.blocks[0].start_line == 3
    assert document.blocks[0].end_line == 3


def test_fenced_code_block_keeps_body_and_language() -> None:
    source = "```python\nprint('hi')\nprint(2)\n```\n"
    document = _document(source)

    assert len(document.blocks) == 1
    code = document.blocks[0]
    assert code.kind == "code_block"
    assert code.text == "print('hi')\nprint(2)"
    assert code.code_info == "python"
    assert (code.start_line, code.end_line) == (1, 4)


def test_inline_html_tags_and_image_urls_are_not_emitted() -> None:
    source = (
        'before <script>alert(1)</script> <a href="javascript:go()">link</a> '
        "![alt *em*](http://evil.example/x.png) after"
    )
    document = _document(source)

    body = document.blocks[0].text
    assert "<script>" not in body
    assert "</script>" not in body
    assert "javascript:" not in body
    assert "http://evil.example" not in body
    # 标签被丢弃、图片只保留 alt 文本；标签内的文本保持为普通文本，不可执行。
    assert body == "before alert(1) link alt em after"


def test_block_level_html_is_excluded_from_blocks() -> None:
    source = "text before\n\n<div onclick='x()'>raw</div>\n\ntext after\n"
    document = _document(source)

    texts = [block.text for block in document.blocks]
    assert texts == ["text before", "text after"]


def test_unicode_content_is_preserved() -> None:
    source = "# 标题\n\n中文段落，包含 emoji 🙂 与 CJK。\n"
    document = _document(source)

    assert document.blocks[0].text == "中文段落，包含 emoji 🙂 与 CJK。"
    assert document.blocks[0].heading_path == ("标题",)


# ---------------------------------------------------------------------------
# 切分：预算、重叠、来源 locator 与显式错误
# ---------------------------------------------------------------------------


def test_parser_version_matches_installed_library() -> None:
    import markdown_it

    assert parsing.MARKDOWN_PARSER_VERSION == f"markdown-it-py-{markdown_it.__version__}-v1"


def test_minimal_budget_accepts_single_character_candidate() -> None:
    document = _document("body\n")
    budget = ChunkBudget(target_tokens=1, overlap_tokens=0, max_tokens=1)

    chunks = chunk_markdown(document, CharacterCounter(), budget)

    assert [chunk.text for chunk in chunks] == list("body")
    assert all(chunk.token_count <= 1 for chunk in chunks)


@pytest.mark.parametrize("length", [513, 1025, 1537])
def test_default_budget_splits_single_long_segment(length: int) -> None:
    body = "x" * length
    document = _document(body + "\n")
    budget = ChunkBudget(target_tokens=360, overlap_tokens=0, max_tokens=512)

    chunks = chunk_markdown(document, CharacterCounter(), budget)

    assert "".join(chunk.text for chunk in chunks) == body
    assert all(chunk.token_count <= 512 for chunk in chunks)


def test_same_block_pieces_are_not_joined_with_fabricated_newlines() -> None:
    body = "x" * 22
    document = _document(body + "\n")
    budget = ChunkBudget(target_tokens=20, overlap_tokens=0, max_tokens=20)

    chunks = chunk_markdown(document, SeparatorDiscountCounter(), budget)

    assert chunks
    # 同一块被拆开后不得再被拼回同一 chunk，否则会伪造原文不存在的空行。
    assert all("\n\n" not in chunk.text for chunk in chunks)
    assert "".join(chunk.text for chunk in chunks) == body
    for chunk in chunks:
        assert chunk.text_hash == hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()


def test_short_standalone_paragraph_is_not_fully_repeated_as_overlap() -> None:
    document = _document("short\n\n" + "B" * 45 + "\n")
    budget = ChunkBudget(target_tokens=45, overlap_tokens=8, max_tokens=60)

    chunks = chunk_markdown(document, CharacterCounter(), budget)

    assert [chunk.text for chunk in chunks] == ["short", "B" * 45]


def test_non_monotonic_counter_over_long_block_terminates_and_covers() -> None:
    body = "ab" * 50
    document = _document(body + "\n")
    budget = ChunkBudget(target_tokens=7, overlap_tokens=3, max_tokens=7)

    chunks = chunk_markdown(document, SeparatorDiscountCounter(), budget)

    assert chunks
    assert all(chunk.token_count <= 7 for chunk in chunks)
    intervals = sorted(
        (segment["block_char_start"], segment["block_char_end"])
        for chunk in chunks
        for segment in _segments(chunk)
    )
    assert intervals[0][0] == 0
    assert intervals[-1][1] == len(body)
    for (_, previous_end), (next_start, _) in zip(intervals, intervals[1:]):
        assert next_start <= previous_end


def test_budget_rejects_invalid_combinations() -> None:
    with pytest.raises(ValueError):
        ChunkBudget(target_tokens=0)
    with pytest.raises(ValueError):
        ChunkBudget(max_tokens=0)
    with pytest.raises(ValueError):
        ChunkBudget(target_tokens=600, max_tokens=512)
    with pytest.raises(ValueError):
        ChunkBudget(target_tokens=100, overlap_tokens=100)


def test_token_count_covers_heading_prefix_and_body() -> None:
    document = _document("# Section\n\nshort body\n")
    counter = CharacterCounter()
    chunks = chunk_markdown(document, counter)

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.heading_path == ("Section",)
    assert chunk.token_count == len(build_model_input(("Section",), "short body"))
    assert chunk.token_count == counter.count_tokens(build_model_input(("Section",), chunk.text))


def test_packing_flushes_at_target_and_carries_bounded_overlap() -> None:
    first = "A" * 40
    second = "B" * 40
    document = _document(f"{first}\n\n{second}\n")
    budget = ChunkBudget(target_tokens=45, overlap_tokens=8, max_tokens=60)

    chunks = chunk_markdown(document, CharacterCounter(), budget)

    assert [chunk.chunk_index for chunk in chunks] == [0, 1]
    assert chunks[0].text == first
    # 第二个 chunk 以第一个段落的最多 overlap 个字符开头。
    assert chunks[1].text.startswith("A" * 8)
    assert chunks[1].text.endswith(second)
    assert chunks[1].token_count <= budget.max_tokens


def test_heading_change_forces_new_chunk_even_within_target() -> None:
    document = _document("# A\n\naaa\n\n# B\n\nbbb\n")
    budget = ChunkBudget(target_tokens=1000, overlap_tokens=10, max_tokens=1000)

    chunks = chunk_markdown(document, CharacterCounter(), budget)

    assert [chunk.heading_path for chunk in chunks] == [("A",), ("B",)]
    assert [chunk.text for chunk in chunks] == ["aaa", "bbb"]


def test_long_paragraph_is_split_deterministically_without_truncation() -> None:
    body = "字" * 200
    document = _document(body + "\n")
    budget = ChunkBudget(target_tokens=40, overlap_tokens=6, max_tokens=48)

    first = chunk_markdown(document, CharacterCounter(), budget)
    second = chunk_markdown(document, CharacterCounter(), budget)

    assert len(first) > 1
    assert [chunk.text for chunk in first] == [chunk.text for chunk in second]
    for chunk in first:
        assert chunk.token_count <= budget.max_tokens

    # 覆盖检查：所有分片在该块正文中的区间连续覆盖 [0, len)。
    intervals = sorted(
        (segment["block_char_start"], segment["block_char_end"])
        for chunk in first
        for segment in _segments(chunk)
    )
    assert intervals[0][0] == 0
    assert intervals[-1][1] == len(body)
    for (_, previous_end), (next_start, _) in zip(intervals, intervals[1:]):
        assert next_start <= previous_end  # 允许重叠，但绝不能有缺口


def test_locator_round_trips_back_to_original_block_text() -> None:
    body = "abcdefghij" * 20
    document = _document(f"# H\n\n{body}\n")
    budget = ChunkBudget(target_tokens=30, overlap_tokens=5, max_tokens=40)
    blocks = {block.ordinal: block for block in document.blocks}

    chunks = chunk_markdown(document, CharacterCounter(), budget)

    assert len(chunks) > 1
    for chunk in chunks:
        locator = chunk.source_locator
        assert locator["locator_version"] == 1
        assert locator["source_type"] == "markdown"
        assert locator["source_sha256"] == document.source_sha256
        assert locator["parser_version"] == parsing.MARKDOWN_PARSER_VERSION
        assert isinstance(locator["start_line"], int)
        assert isinstance(locator["end_line"], int)
        # locator 必须是 JSON-compatible 且不含伪造的文件名或页码字段。
        json.dumps(locator)
        assert "file" not in locator and "page" not in locator

        pieces = [
            blocks[segment["block_ordinal"]].text[
                segment["block_char_start"] : segment["block_char_end"]
            ]
            for segment in _segments(chunk)
        ]
        assert "\n\n".join(pieces) == chunk.text


def test_multi_block_chunk_locator_spans_all_blocks() -> None:
    document = _document("one\n\ntwo\n\nthree\n")
    budget = ChunkBudget(target_tokens=100, overlap_tokens=5, max_tokens=100)

    chunks = chunk_markdown(document, CharacterCounter(), budget)

    assert len(chunks) == 1
    locator = chunks[0].source_locator
    assert locator["block_ordinals"] == [0, 1, 2]
    assert locator["start_line"] == 1
    assert locator["end_line"] == 5


def test_code_block_locator_points_at_code_lines() -> None:
    document = _document("intro\n\n```py\ncode line\n```\n")
    budget = ChunkBudget(target_tokens=5, overlap_tokens=0, max_tokens=20)

    chunks = chunk_markdown(document, CharacterCounter(), budget)

    code_chunks = [
        chunk
        for chunk in chunks
        if any(segment["block_ordinal"] == 1 for segment in _segments(chunk))
    ]
    assert len(code_chunks) == 1
    locator = code_chunks[0].source_locator
    assert locator["start_line"] == 3
    assert locator["end_line"] == 5


def test_chunk_hashes_and_versions_are_stable() -> None:
    document = _document("# H\n\nbody text\n")
    chunks = chunk_markdown(document, CharacterCounter())

    chunk = chunks[0]
    assert chunk.parser_version == parsing.MARKDOWN_PARSER_VERSION
    assert chunk.chunker_version == CHUNKER_VERSION
    assert chunk.text_hash == hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
    assert chunk.model_input_hash == hashlib.sha256(
        build_model_input(chunk.heading_path, chunk.text).encode("utf-8")
    ).hexdigest()


def test_empty_content_raises_instead_of_returning_no_chunks() -> None:
    with pytest.raises(NoChunkableContent):
        chunk_markdown(_document("# only heading\n"), CharacterCounter())
    with pytest.raises(NoChunkableContent):
        chunk_markdown(_document("<!-- just a comment -->\n"), CharacterCounter())
    with pytest.raises(NoChunkableContent):
        chunk_markdown(_document("\n\n"), CharacterCounter())


def test_impossible_budget_raises_explicit_error() -> None:
    document = _document("# " + "H" * 100 + "\n\nx\n")
    budget = ChunkBudget(target_tokens=5, overlap_tokens=1, max_tokens=10)

    with pytest.raises(ChunkBudgetExceeded):
        chunk_markdown(document, CharacterCounter(), budget)


def test_chunking_error_hierarchy_is_explicit() -> None:
    assert issubclass(NoChunkableContent, ChunkingError)
    assert issubclass(ChunkBudgetExceeded, ChunkingError)
