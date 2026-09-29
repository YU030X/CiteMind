"""DOCX 解析、来源定位与 ZIP 安全边界的聚焦单测（不连数据库、Redis 或模型）。

用 ``docx_samples`` 在内存中构造 5 份自制正样本与嵌套表/空/图片-only/宏/实体/损坏负例；假
计数器把每个字符当成一个 token，因此这里只验证块顺序、段落索引、表格合并、locator_version=3
形状与可重放，不代表真实 tokenizer 预算。
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable

import pytest
from docx_samples import (
    corrupted_zip_docx,
    empty_docx,
    entity_declaration_docx,
    image_only_docx,
    macro_part_docx,
    nested_table_docx,
    positive_long_content,
    positive_multi_heading_interleaved,
    positive_samples,
    positive_simple_table,
)
from rag_backend.ingestion.chunking import DOCX_LOCATOR_VERSION, ChunkBudget, chunk_markdown
from rag_backend.ingestion.docx_parsing import (
    DOCX_PARSER_VERSION,
    SOURCE_TYPE_DOCX,
    DocxInvalidError,
    DocxTooLargeError,
    DocxUnsupportedError,
    inspect_docx_zip,
    parse_docx,
)
from rag_backend.ingestion.validation import (
    DOCX_MEDIA_TYPE,
    resolve_upload_format,
    validate_docx_content,
)
from rag_backend.ingestion.validation import (
    SOURCE_TYPE_DOCX as VALIDATION_SOURCE_TYPE_DOCX,
)

pytest.importorskip("docx")


class CharacterCounter:
    """显式测试假计数器：每个字符一个 token。"""

    def count_tokens(self, text: str) -> int:
        return len(text)


@pytest.fixture(scope="module")
def samples() -> dict[str, bytes]:
    return positive_samples()


def _zip_with(entries: dict[str, bytes], *, flag_bits: int = 0) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            info = zipfile.ZipInfo(name)
            info.flag_bits = flag_bits
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payload)
    return buffer.getvalue()


def _valid_core_parts() -> dict[str, bytes]:
    return {
        "[Content_Types].xml": b"<Types/>",
        "_rels/.rels": b"<Relationships/>",
        "word/document.xml": b"<w:document/>",
    }


def test_all_positive_samples_parse_with_docx_parser_version(
    samples: dict[str, bytes],
) -> None:
    """5 份自制正样本都能解析并声明 DOCX 解析器版本与来源。"""

    assert len(samples) == 5
    for data in samples.values():
        parsed = parse_docx(data)
        assert parsed.source_type == SOURCE_TYPE_DOCX
        assert parsed.parser_version == DOCX_PARSER_VERSION
        assert parsed.blocks  # 每份正样本至少一个正文块


def test_empty_paragraphs_count_toward_paragraph_index() -> None:
    from docx import Document

    document = Document()
    document.add_paragraph("第一段")
    document.add_paragraph("")
    document.add_paragraph("第三段")
    buffer = io.BytesIO()
    document.save(buffer)

    parsed = parse_docx(buffer.getvalue())
    body_paragraphs = [block for block in parsed.blocks if block.kind == "paragraph"]
    assert [block.paragraph_index for block in body_paragraphs] == [1, 3]
    assert [block.text for block in body_paragraphs] == ["第一段", "第三段"]


def test_multi_heading_reuses_heading_path(samples: dict[str, bytes]) -> None:
    parsed = parse_docx(samples["multi_heading_interleaved"])
    paths = [(block.kind, block.heading_path) for block in parsed.blocks]
    assert paths[0] == ("paragraph", ("员工手册",))
    assert paths[1] == ("paragraph", ("员工手册", "考勤"))
    assert paths[2][0] == "table_row"
    assert paths[2][1] == ("员工手册", "考勤")
    # 标题自身不产出正文块，但占据段落索引；最后一个正文段在「补充」标题下。
    assert paths[-1] == ("paragraph", ("员工手册", "补充"))
    paragraph_indices = [
        int(block.paragraph_index)
        for block in parsed.blocks
        if block.kind == "paragraph" and block.paragraph_index is not None
    ]
    assert paragraph_indices == sorted(paragraph_indices)
    assert paragraph_indices[0] == 2


def test_simple_table_rows_carry_grid_columns_and_char_spans(
    samples: dict[str, bytes],
) -> None:
    parsed = parse_docx(samples["simple_table"])
    rows = [block for block in parsed.blocks if block.kind == "table_row"]
    assert len(rows) == 3
    header = rows[0]
    assert header.table_index == 1
    assert header.row_index == 1
    assert header.text == "项目 | 上限 | 备注"
    for cell in header.cells:
        assert header.text[cell.char_start : cell.char_end]
    assert [cell.grid_column for cell in header.cells] == [1, 2, 3]
    assert all(cell.grid_span == 1 for cell in header.cells)


def test_horizontal_merge_emits_origin_once_and_grid_before_offsets(
    samples: dict[str, bytes],
) -> None:
    parsed = parse_docx(samples["horizontal_merge_grid_before"])
    rows = [block for block in parsed.blocks if block.kind == "table_row"]
    first = rows[0]
    # 横向合并只记录真实 origin 一次（grid_span=2），不重复输出被合并列。
    assert first.cells[0].grid_span == 2
    assert first.text.count("跨两列") == 1
    assert first.cells[0].grid_column == 1
    # gridBefore=1 让第二行第一格落在网格列 2，而不是被当作列 1。
    second = rows[1]
    assert second.cells[0].grid_column == 2
    assert second.cells[1].grid_column == 3


def test_vertical_merge_does_not_copy_previous_row_text(
    samples: dict[str, bytes],
) -> None:
    parsed = parse_docx(samples["vertical_merge"])
    rows = [block for block in parsed.blocks if block.kind == "table_row"]
    assert rows[0].text == "跨行 | 第一行"
    # 续格被跳过：第二、三行不复制「跨行」，也不产生该列的伪造来源。
    assert rows[1].text == "第二行"
    assert rows[2].text == "第三行"
    assert [cell.grid_column for cell in rows[1].cells] == [2]


def test_docx_locator_version3_replays_without_fabricating_pages(
    samples: dict[str, bytes],
) -> None:
    counter = CharacterCounter()
    for name, data in samples.items():
        parsed = parse_docx(data)
        chunks = chunk_markdown(parsed, counter, ChunkBudget())
        assert chunks, name
        for chunk in chunks:
            locator = chunk.source_locator
            assert locator["locator_version"] == DOCX_LOCATOR_VERSION
            assert locator["source_type"] == SOURCE_TYPE_DOCX
            assert locator["source_sha256"] == parsed.source_sha256
            assert "pages" not in locator
            assert "start_line" not in locator
            segments = locator["segments"]
            assert isinstance(segments, list) and segments
            expected = ""
            previous_ordinal: int | None = None
            for segment in segments:
                ordinal = int(segment["block_ordinal"])
                block = parsed.blocks[ordinal]
                start = int(segment["block_char_start"])
                end = int(segment["block_char_end"])
                assert 0 <= start <= end <= len(block.text)
                if previous_ordinal is not None and ordinal != previous_ordinal:
                    expected += "\n\n"
                expected += block.text[start:end]
                previous_ordinal = ordinal
                if block.kind == "table_row":
                    assert "table_index" in segment and "row_index" in segment
                    for cell in segment["cells"]:
                        assert block.text[cell["char_start"] : cell["char_end"]]
            assert expected == chunk.text


def test_long_content_splits_into_multiple_chunks_with_contiguous_segments(
    samples: dict[str, bytes],
) -> None:
    parsed = parse_docx(samples["long_content"])
    counter = CharacterCounter()
    budget = ChunkBudget(target_tokens=120, overlap_tokens=20, max_tokens=160)
    chunks = chunk_markdown(parsed, counter, budget)
    assert len(chunks) > 1
    for chunk in chunks:
        segments = chunk.source_locator["segments"]
        assert isinstance(segments, list)
        for segment in segments:
            assert isinstance(segment, dict)
            block = parsed.blocks[int(segment["block_ordinal"])]
            assert (
                block.text[int(segment["block_char_start"]) : int(segment["block_char_end"])]
                in chunk.text
            )


@pytest.mark.parametrize(
    ("builder", "expected"),
    [
        (nested_table_docx, DocxUnsupportedError),
        (macro_part_docx, DocxUnsupportedError),
        (entity_declaration_docx, DocxUnsupportedError),
        (corrupted_zip_docx, DocxInvalidError),
    ],
)
def test_unsupported_or_invalid_samples_are_statically_rejected(
    builder: Callable[[], bytes], expected: type[Exception]
) -> None:
    data = builder()
    with pytest.raises(expected):
        parse_docx(data)


def test_empty_and_image_only_documents_have_no_blocks() -> None:
    assert parse_docx(empty_docx()).blocks == ()
    assert parse_docx(image_only_docx()).blocks == ()


def test_zip_metadata_rejects_entry_count_size_ratio_path_and_duplicates() -> None:
    with pytest.raises(DocxTooLargeError):
        inspect_docx_zip(_zip_with({**{f"p{i}": b"x" for i in range(513)}}))
    # 每条约 900 KiB、80 条，声明累计约 72 MiB，超过 64 MiB 上限。
    big = _zip_with(
        {
            **_valid_core_parts(),
            **{f"word/part{i}.xml": b"\x00" * (900 * 1024) for i in range(80)},
        }
    )
    with pytest.raises(DocxTooLargeError):
        inspect_docx_zip(big)
    # 单条 >1 MiB 且压缩比过高。
    with pytest.raises(DocxTooLargeError):
        inspect_docx_zip(
            _zip_with({**_valid_core_parts(), "word/big.xml": b"\x00" * (2 * 1024 * 1024)})
        )
    with pytest.raises(DocxInvalidError):
        inspect_docx_zip(_zip_with({**_valid_core_parts(), "../evil.xml": b"x"}))
    with pytest.raises(DocxInvalidError):
        inspect_docx_zip(
            _zip_with({**_valid_core_parts(), "word/a.xml": b"x", "word/A.XML": b"y"})
        )
    with pytest.raises(DocxInvalidError):
        inspect_docx_zip(_zip_with({"word/document.xml": b"<w:document/>"}))
    with pytest.raises(DocxInvalidError):
        inspect_docx_zip(b"%PDF-1.4 not a docx")


def test_encrypted_entry_is_rejected() -> None:
    data = _zip_with(_valid_core_parts())
    with pytest.raises(DocxInvalidError):
        inspect_docx_zip(_with_encrypted_flag(data))


def _with_encrypted_flag(data: bytes) -> bytes:
    """直接置位 ZIP 通用标志的加密位（``zipfile`` 写入时会清除它）。"""

    out = bytearray(data)
    for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        start = 0
        while True:
            index = out.find(signature, start)
            if index < 0:
                break
            out[index + offset] |= 0x1
            start = index + 4
    return bytes(out)


def test_upload_validation_accepts_positive_samples_and_rejects_macro(
    samples: dict[str, bytes],
) -> None:
    for data in samples.values():
        validate_docx_content(data)
    with pytest.raises(Exception):
        validate_docx_content(macro_part_docx())
    with pytest.raises(Exception):
        validate_docx_content(corrupted_zip_docx())


def test_resolve_upload_format_maps_docx_and_media_type() -> None:
    upload_format = resolve_upload_format("报告.docx")
    assert upload_format.source_type == VALIDATION_SOURCE_TYPE_DOCX
    assert VALIDATION_SOURCE_TYPE_DOCX == "docx"
    assert DOCX_MEDIA_TYPE.endswith("wordprocessingml.document")


def test_positive_sample_builders_are_deterministic() -> None:
    assert positive_simple_table() == positive_simple_table()
    assert positive_multi_heading_interleaved() == positive_multi_heading_interleaved()
    assert positive_long_content() == positive_long_content()
