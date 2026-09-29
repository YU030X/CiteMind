"""受限静态 HTML 解析的最短聚焦测试：5 份自制正样本、子进程一致性与 locator v4。

不联网、不执行脚本、不读数据库；HTML 全部为本文件自制。
"""

from __future__ import annotations

import dataclasses
import hashlib
from datetime import UTC, datetime

from rag_backend.ingestion import parse_subprocess as ps
from rag_backend.ingestion.chunking import (
    WEB_LOCATOR_VERSION,
    ChunkBudget,
    chunk_markdown,
)
from rag_backend.ingestion.parsing import ParsedDocument
from rag_backend.ingestion.web_parsing import WEB_PARSER_VERSION, parse_web

FETCHED_AT = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)

SAMPLES: dict[str, bytes] = {
    "article": (
        b"<html><head><title>t</title></head><body>"
        b"<h1>Doc</h1><h2>Intro</h2><p>First paragraph.</p>"
        b"<ul><li>one</li><li>two</li></ul></body></html>"
    ),
    "main_blockquote": (
        b"<body><main><h2>Quoted</h2>"
        b"<blockquote><p>Nested quote text.</p></blockquote>"
        b"<p>After quote.</p></main></body>"
    ),
    "code": (
        b"<body><article><h3>Code</h3>"
        b"<pre>def f():\n    return 1</pre>"
        b"<p>Explanation.</p></article></body>"
    ),
    "stripped_noise": (
        b"<body><header><h1>Site</h1></header>"
        b"<nav>menu</nav>"
        b"<main><p>Real body.</p></main>"
        b"<script>alert('x')</script><style>.a{}</style>"
        b"<footer>foot</footer></body>"
    ),
    "multi_heading": (
        b"<body><main><h1>A</h1><h2>B</h2><p>p1</p><h2>C</h2><p>p2</p>"
        b"<p>p3</p></main></body>"
    ),
}


class CharacterCounter:
    def count_tokens(self, text: str) -> int:
        return len(text)


def test_five_positive_samples_extract_expected_blocks() -> None:
    parsed = {name: parse_web(raw) for name, raw in SAMPLES.items()}
    assert len(parsed) == 5

    article = parsed["article"]
    assert article.source_type == "web"
    assert article.parser_version == WEB_PARSER_VERSION
    assert article.source_sha256 == hashlib.sha256(SAMPLES["article"]).hexdigest()
    assert [(b.kind, b.text, b.heading_path) for b in article.blocks] == [
        ("paragraph", "First paragraph.", ("Doc", "Intro")),
        ("list_item", "one", ("Doc", "Intro")),
        ("list_item", "two", ("Doc", "Intro")),
    ]

    assert [b.text for b in parsed["main_blockquote"].blocks] == [
        "Nested quote text.",
        "After quote.",
    ]
    assert [b.kind for b in parsed["code"].blocks] == ["code_block", "paragraph"]
    assert parsed["code"].blocks[0].text == "def f():\n    return 1"

    stripped = parsed["stripped_noise"]
    texts = [b.text for b in stripped.blocks]
    assert texts == ["Real body."]
    assert all("alert" not in text and "menu" not in text for text in texts)

    multi = parsed["multi_heading"]
    assert [(b.text, b.heading_path) for b in multi.blocks] == [
        ("p1", ("A", "B")),
        ("p2", ("A", "C")),
        ("p3", ("A", "C")),
    ]


def test_script_only_page_has_no_blocks() -> None:
    parsed = parse_web(b"<body><h1>Only title</h1><script>x()</script></body>")
    assert parsed.blocks == ()


def test_subprocess_parse_matches_direct_parse() -> None:
    raw = SAMPLES["article"]
    parsed = ps.parse_web_in_subprocess(raw)
    expected = parse_web(raw)

    assert parsed.source_type == "web"
    assert parsed.parser_version == expected.parser_version
    assert parsed.source_sha256 == expected.source_sha256
    assert [(b.kind, b.text, b.heading_path) for b in parsed.blocks] == [
        (b.kind, b.text, b.heading_path) for b in expected.blocks
    ]


def _with_metadata(parsed: ParsedDocument) -> ParsedDocument:
    return dataclasses.replace(
        parsed,
        source_url="https://example.com/doc",
        final_url="https://example.com/final",
        fetched_at=FETCHED_AT,
    )


def test_chunks_carry_locator_version_4_with_fetch_metadata() -> None:
    parsed = _with_metadata(parse_web(SAMPLES["multi_heading"]))
    chunks = chunk_markdown(
        parsed,
        CharacterCounter(),
        ChunkBudget(target_tokens=100, overlap_tokens=0, max_tokens=200),
    )
    assert chunks
    for chunk in chunks:
        locator = chunk.source_locator
        assert locator["locator_version"] == WEB_LOCATOR_VERSION
        assert locator["source_type"] == "web"
        assert locator["source_url"] == "https://example.com/doc"
        assert locator["final_url"] == "https://example.com/final"
        assert locator["fetched_at"] == FETCHED_AT.isoformat()
        assert locator["parser_version"] == WEB_PARSER_VERSION
        for segment in locator["segments"]:
            assert isinstance(segment["block_ordinal"], int)
            assert isinstance(segment["block_char_start"], int)
            assert isinstance(segment["block_char_end"], int)


def test_markdown_locator_golden_unaffected_by_web_metadata_fields() -> None:
    from rag_backend.ingestion.parsing import parse_markdown

    document = parse_markdown(b"# T\n\nBody\n")
    assert document.source_url is None
    assert document.final_url is None
    assert document.fetched_at is None
