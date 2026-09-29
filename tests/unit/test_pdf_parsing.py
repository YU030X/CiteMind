"""文本 PDF 解析/切分与页定位的聚焦单测（不连数据库、Redis 或模型）。

用 :mod:`pdf_samples` 的内存样本与 pypdf 构造的加密样本覆盖：可提取文本、中文文本层、多栏/缩进、
单页长文本、空白页+文本页，以及加密、损坏与超页数等负例。假计数器把每个字符当成一个 token，
因此这里只验证页边界与 locator 形状，不代表真实 tokenizer 预算。
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import cast

import pytest
from pdf_samples import (
    all_blank_pdf,
    corrupt_pdf,
    encrypted_pdf,
    positive_ascii_multipage,
    positive_blank_and_text_pages,
    positive_cjk_text_layer,
    positive_long_single_page,
    positive_multi_column_indent,
    positive_samples,
    too_many_pages_pdf,
)
from rag_backend.ingestion import pdf_parsing
from rag_backend.ingestion.chunking import (
    PDF_LOCATOR_VERSION,
    Chunk,
    ChunkBudget,
    NoChunkableContent,
    chunk_markdown,
)
from rag_backend.ingestion.errors import DocumentNotPdf, DocumentTooLarge, UnsupportedDocumentType
from rag_backend.ingestion.pdf_parsing import (
    MAX_PDF_PAGES,
    PdfEncryptedError,
    PdfInvalidError,
    PdfTooManyPagesError,
    parse_pdf,
)
from rag_backend.ingestion.storage import DocumentBlobStore, content_hash
from rag_backend.ingestion.validation import (
    MAX_DOCUMENT_BYTES,
    SOURCE_TYPE_PDF,
    resolve_upload_format,
    validate_pdf_content,
)

pypdf = pytest.importorskip("pypdf")
pdfplumber = pytest.importorskip("pdfplumber")


class CharacterCounter:
    """显式测试假计数器：每个字符一个 token。"""

    def count_tokens(self, text: str) -> int:
        return len(text)


def _build_pdf(
    pages: list[str],
    *,
    password: str | None = None,
    owner_password: str | None = None,
) -> bytes:
    """用 pypdf 构造带真实文本层的最小 PDF；空字符串表示空白页。"""

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    for text in pages:
        page = writer.add_blank_page(width=200, height=200)
        if text:
            stream = DecodedStreamObject()
            stream.set_data(
                ("BT /F1 12 Tf 10 100 Td (" + text + ") Tj ET").encode("latin-1")
            )
            page[NameObject("/Contents")] = writer._add_object(stream)
            page[NameObject("/Resources")] = DictionaryObject(
                {
                    NameObject("/Font"): DictionaryObject(
                        {NameObject("/F1"): font_ref}
                    )
                }
            )
    buffer = io.BytesIO()
    writer.write(buffer)
    raw = buffer.getvalue()
    if password is not None or owner_password is not None:
        encrypted = io.BytesIO()
        reader = pypdf.PdfReader(io.BytesIO(raw))
        encrypted_writer = pypdf.PdfWriter()
        for page in reader.pages:
            encrypted_writer.add_page(page)
        encrypted_writer.encrypt(password or "", owner_password=owner_password)
        encrypted_writer.write(encrypted)
        raw = encrypted.getvalue()
    return raw


def _segments(chunk: Chunk) -> list[dict[str, object]]:
    return cast("list[dict[str, object]]", chunk.source_locator["segments"])


def test_parser_version_matches_installed_libraries() -> None:
    assert pdf_parsing.PDF_PARSER_VERSION == "pypdf-6.19.0+pdfplumber-0.11.10-v1"
    assert pdf_parsing.PYPDF_VERSION == pypdf.__version__
    assert pdf_parsing.PDFPLUMBER_VERSION == pdfplumber.__version__
    assert pdf_parsing.PDF_PARSER_VERSION == (
        f"pypdf-{pypdf.__version__}+pdfplumber-{pdfplumber.__version__}-v1"
    )


def test_api_entrypoint_import_does_not_load_pdf_engines() -> None:
    """全新子进程导入 API 入口后，两个 PDF 引擎及其依赖都不得进入 ``sys.modules``。

    在本进程断言会受已导入状态污染，因此必须另起解释器；只证明 API 入口不导入这些依赖，
    不代表它们未安装在 dev/worker 组。
    """

    code = (
        "import sys; import rag_backend.main; "
        "forbidden = {'pdfplumber', 'pdfminer', 'PIL', 'pypdf'}; "
        "loaded = forbidden & set(sys.modules); "
        "assert not loaded, sorted(loaded)"
    )

    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


def test_positive_samples_are_five_distinct_documents() -> None:
    samples = positive_samples()
    assert len(samples) == 5
    # 5 份样本必须字节不同，不能用同一份样本冒充多份。
    assert len(set(samples.values())) == 5


def test_positive_ascii_multipage_extracts_each_page() -> None:
    document = parse_pdf(positive_ascii_multipage())

    assert [block.page for block in document.blocks] == [1, 2, 3]
    assert "Employee Handbook" in document.blocks[0].text
    assert "Leave Policy" in document.blocks[1].text
    assert "Travel" in document.blocks[2].text
    assert all(block.heading_path == () for block in document.blocks)
    assert all(
        block.start_line is None and block.end_line is None for block in document.blocks
    )


def test_positive_cjk_text_layer_extracts_chinese() -> None:
    document = parse_pdf(positive_cjk_text_layer())

    assert len(document.blocks) == 1
    assert document.blocks[0].page == 1
    assert "员工餐厅营业时间" in document.blocks[0].text
    assert "访客餐券" in document.blocks[0].text
    assert document.blocks[0].heading_path == ()


def test_positive_multi_column_indent_keeps_all_fragments() -> None:
    document = parse_pdf(positive_multi_column_indent())

    assert len(document.blocks) == 1
    text = document.blocks[0].text
    for fragment in (
        "Left column first line",
        "Indented continuation line",
        "Right column second line",
    ):
        assert fragment in text


def test_positive_long_single_page_splits_within_page() -> None:
    document = parse_pdf(positive_long_single_page())

    chunks = chunk_markdown(
        document,
        CharacterCounter(),
        ChunkBudget(target_tokens=100, overlap_tokens=0, max_tokens=200),
    )

    assert len(chunks) > 1
    assert all(chunk.source_locator["pages"] == [1] for chunk in chunks)


def test_positive_blank_and_text_pages_ignore_blank_pages() -> None:
    document = parse_pdf(positive_blank_and_text_pages())

    assert [block.page for block in document.blocks] == [2]
    assert "middle page" in document.blocks[0].text


def test_positive_samples_chunk_to_locator_version_two() -> None:
    for name, raw in positive_samples().items():
        parsed = parse_pdf(raw)
        if not parsed.blocks:
            continue
        chunks = chunk_markdown(
            parsed,
            CharacterCounter(),
            ChunkBudget(target_tokens=100, overlap_tokens=0, max_tokens=200),
        )
        assert chunks, name
        for chunk in chunks:
            locator = chunk.source_locator
            assert locator["locator_version"] == PDF_LOCATOR_VERSION == 2, name
            assert locator["source_type"] == "pdf", name
            assert locator["parser_version"] == pdf_parsing.PDF_PARSER_VERSION, name
            assert locator["source_sha256"] == hashlib.sha256(raw).hexdigest(), name
            pages = locator["pages"]
            assert isinstance(pages, list) and len(pages) == 1, name
            assert "start_line" not in locator and "end_line" not in locator, name


def test_all_blank_pdf_has_no_blocks_and_is_not_chunkable() -> None:
    document = parse_pdf(all_blank_pdf())

    assert document.blocks == ()
    assert document.parser_version == pdf_parsing.PDF_PARSER_VERSION
    with pytest.raises(NoChunkableContent):
        chunk_markdown(document, CharacterCounter())


def test_negative_pdf_samples_fail_statically() -> None:
    with pytest.raises(PdfEncryptedError):
        parse_pdf(encrypted_pdf())
    with pytest.raises(PdfInvalidError):
        parse_pdf(corrupt_pdf())
    with pytest.raises(PdfTooManyPagesError):
        parse_pdf(too_many_pages_pdf())


def test_pages_extracted_with_empty_heading_and_no_fabricated_lines() -> None:
    raw = _build_pdf(["Hello page one", "World page two"])

    document = parse_pdf(raw)

    assert document.source_type == SOURCE_TYPE_PDF
    assert document.parser_version == pdf_parsing.PDF_PARSER_VERSION
    assert document.source_sha256 == hashlib.sha256(raw).hexdigest()
    assert [block.text for block in document.blocks] == [
        "Hello page one",
        "World page two",
    ]
    assert [block.page for block in document.blocks] == [1, 2]
    assert all(block.heading_path == () for block in document.blocks)
    assert all(block.start_line is None and block.end_line is None for block in document.blocks)
    assert all(block.kind == "pdf_page" for block in document.blocks)


def test_chunk_locator_version_two_points_to_original_page() -> None:
    raw = _build_pdf(["Alpha text", "Beta text"])

    chunks = chunk_markdown(
        parse_pdf(raw),
        CharacterCounter(),
        ChunkBudget(target_tokens=100, overlap_tokens=0, max_tokens=200),
    )

    assert len(chunks) == 2
    for chunk, page in zip(chunks, (1, 2)):
        locator = chunk.source_locator
        assert locator["locator_version"] == PDF_LOCATOR_VERSION == 2
        assert locator["source_type"] == "pdf"
        assert locator["parser_version"] == pdf_parsing.PDF_PARSER_VERSION
        assert locator["source_sha256"] == hashlib.sha256(raw).hexdigest()
        assert locator["pages"] == [page]
        assert "start_line" not in locator and "end_line" not in locator
        assert all(segment["page"] == page for segment in _segments(chunk))
        # locator 必须是 JSON-compatible；不得出现伪造行号字段。
        json.dumps(locator)


def test_chunks_never_cross_page_boundaries() -> None:
    raw = _build_pdf(["one", "two", "three"])

    chunks = chunk_markdown(
        parse_pdf(raw),
        CharacterCounter(),
        # 预算远大于全部正文：若无页边界将合并成单 chunk。
        ChunkBudget(target_tokens=1000, overlap_tokens=50, max_tokens=1000),
    )

    assert len(chunks) == 3
    page_sets = [chunk.source_locator["pages"] for chunk in chunks]
    assert page_sets == [[1], [2], [3]]
    assert [chunk.text for chunk in chunks] == ["one", "two", "three"]


def test_oversized_page_splits_within_single_page() -> None:
    raw = _build_pdf(["x" * 60])

    chunks = chunk_markdown(
        parse_pdf(raw),
        CharacterCounter(),
        ChunkBudget(target_tokens=10, overlap_tokens=0, max_tokens=10),
    )

    assert len(chunks) > 1
    assert all(chunk.source_locator["pages"] == [1] for chunk in chunks)
    assert "".join(chunk.text for chunk in chunks) == "x" * 60


def test_zero_text_pdf_has_no_blocks_and_is_not_chunkable() -> None:
    raw = _build_pdf(["", "   "])

    document = parse_pdf(raw)

    assert document.blocks == ()
    with pytest.raises(NoChunkableContent):
        chunk_markdown(document, CharacterCounter())


def test_page_limit_is_enforced() -> None:
    raw = _build_pdf([""] * (MAX_PDF_PAGES + 1))

    with pytest.raises(PdfTooManyPagesError):
        parse_pdf(raw)


def test_corrupt_pdf_is_rejected() -> None:
    with pytest.raises(PdfInvalidError):
        parse_pdf(b"%PDF-1.4 this is not a real pdf")


def test_non_pdf_bytes_are_rejected() -> None:
    with pytest.raises(PdfInvalidError):
        parse_pdf(b"not a pdf at all")


def test_encrypted_pdf_is_rejected() -> None:
    raw = _build_pdf(["secret"], password="s3cret")

    with pytest.raises(PdfEncryptedError):
        parse_pdf(raw)


def test_encrypted_pdf_with_empty_user_password_is_still_rejected() -> None:
    # 空用户口令即可解密的 PDF 仍带加密标志；合同不保证支持，统一拒绝而非擅自 decrypt。
    raw = _build_pdf(["secret"], password="", owner_password="owner")
    assert pypdf.PdfReader(io.BytesIO(raw)).decrypt("")  # 仅证明该样本确可被空口令解密

    with pytest.raises(PdfEncryptedError):
        parse_pdf(raw)


# ---------------------------------------------------------------------------
# 上传校验与二进制只读
# ---------------------------------------------------------------------------


def test_resolve_upload_format_dispatches_markdown_and_pdf() -> None:
    assert resolve_upload_format("report.PDF").source_type == SOURCE_TYPE_PDF
    assert resolve_upload_format("notes.md").source_type == "markdown"
    assert resolve_upload_format("notes.markdown").source_type == "markdown"

    with pytest.raises(UnsupportedDocumentType):
        resolve_upload_format("archive.zip")


def test_validate_pdf_content_checks_size_empty_and_magic() -> None:
    validate_pdf_content(_build_pdf(["ok"]))

    with pytest.raises(DocumentNotPdf):
        validate_pdf_content(b"plain text")
    with pytest.raises(DocumentTooLarge):
        validate_pdf_content(b"%PDF-" + b"x" * MAX_DOCUMENT_BYTES)


def test_read_verified_blob_and_pdf_round_trip(tmp_path: Path) -> None:
    store = DocumentBlobStore(tmp_path)
    kb_id = uuid.uuid4()
    raw = _build_pdf(["Verifiable page"])
    file_hash = content_hash(raw)
    file_ref = store.publish(kb_id, file_hash, raw)

    assert store.read_verified_blob(kb_id, file_ref, file_hash) == raw
    assert store.read_verified_pdf(kb_id, file_ref, file_hash) == raw
