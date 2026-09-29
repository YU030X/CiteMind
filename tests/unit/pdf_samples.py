"""自制 PDF 样本构造器（无敏感内容、可离线复现、字节确定）。

只用标准库按 PDF 语法拼装样本，不联网、不读取环境文件、不使用任何真实企业资料；因此
``pdfplumber`` 是唯一的读取依赖，生成端不引入任何新生产依赖。ASCII 样本用标准 Type1
Helvetica 字体；中文样本用标准 CJK ``STSong-Light`` 加上 ``UniGB-UCS2-H`` 编码，不嵌入字体，
``pdfplumber``/``pdfminer`` 通过其内置 Adobe-GB1 映射逐页抽回中文文本层。同一 Python 版本下
重复调用得到相同字节。

5 份正样本（每份都只含可提取文本层，且彼此内容不同）：

1. ``positive_ascii_multipage``：多页 ASCII 文本；
2. ``positive_cjk_text_layer``：中文文本层（标准 CJK 字体，不嵌入字体）；
3. ``positive_multi_column_indent``：同一页上左右两栏 + 缩进行文本；
4. ``positive_long_single_page``：单页长文本，用于验证页内超预算切分；
5. ``positive_blank_and_text_pages``：空白页 + 有文本页，验证只忽略空白页。

负样本：全空白页（``NEEDS_OCR``）、加密、结构损坏、超过 ``MAX_PDF_PAGES``；它们**不计入**
5 份可支持正样本。
"""

from __future__ import annotations

import io

from rag_backend.ingestion.pdf_parsing import MAX_PDF_PAGES

# PDF 语法字符集与固定头；同一 Python 版本下输出字节稳定。
_HEADER = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"

# 每页渲染的行距与起始坐标；只影响文本层内容，不参与 locator 计算。
_ASCII_LINE_HEIGHT = 16
_CJK_LINE_HEIGHT = 22
_START_X = 50
_START_Y = 760


def _stream(data: bytes) -> bytes:
    """把字节包装成 PDF stream 对象体。"""

    return b"<< /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"


def _escape_ascii(text: str) -> str:
    """转义 PDF 字面字符串中的反斜杠与圆括号。"""

    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _ascii_content(lines: tuple[str, ...]) -> bytes:
    """用 Helvetica 渲染 ASCII 行；每行 ``Td`` 下移固定行距。"""

    ops = ["BT", "/F1 12 Tf", f"{_START_X} {_START_Y} Td"]
    for index, line in enumerate(lines):
        if index:
            ops.append(f"0 -{_ASCII_LINE_HEIGHT} Td")
        ops.append(f"({_escape_ascii(line)}) Tj")
    ops.append("ET")
    return "\n".join(ops).encode("latin-1")


def _cjk_content(lines: tuple[str, ...]) -> bytes:
    """用 STSong-Light + UniGB-UCS2-H 渲染中文行；文本按 UTF-16BE 十六进制写入。"""

    ops = ["BT", "/F1 14 Tf", f"{_START_X} {_START_Y} Td"]
    for index, line in enumerate(lines):
        if index:
            ops.append(f"0 -{_CJK_LINE_HEIGHT} Td")
        ops.append(f"<{line.encode('utf-16-be').hex().upper()}> Tj")
    ops.append("ET")
    return "\n".join(ops).encode("ascii")


def _two_column_content(
    columns: tuple[tuple[int, int, tuple[str, ...]], ...]
) -> bytes:
    """在同一页上按给定 (x, top_y) 渲染多栏/缩进行；只影响文本层排列。"""

    ops = ["BT", "/F1 12 Tf"]
    for x_position, top_y, lines in columns:
        first = True
        for line in lines:
            if first:
                ops.append(f"1 0 0 1 {x_position} {top_y} Tm")
                first = False
            else:
                ops.append("0 -18 Td")
            ops.append(f"({_escape_ascii(line)}) Tj")
    ops.append("ET")
    return "\n".join(ops).encode("latin-1")


def _assemble(objects: dict[int, bytes]) -> bytes:
    """按对象号连续写出对象与 xref；调用方保证对象号从 1 到最大号无缺号。"""

    max_id = max(objects)
    out = io.BytesIO()
    out.write(_HEADER)
    offsets = [0] * (max_id + 1)
    for object_id in range(1, max_id + 1):
        offsets[object_id] = out.tell()
        out.write(f"{object_id} 0 obj\n".encode("ascii"))
        out.write(objects[object_id])
        out.write(b"\nendobj\n")
    xref_offset = out.tell()
    out.write(f"xref\n0 {max_id + 1}\n".encode("ascii"))
    out.write(b"0000000000 65535 f \n")
    for object_id in range(1, max_id + 1):
        out.write(f"{offsets[object_id]:010d} 00000 n \n".encode("ascii"))
    out.write(
        f"trailer\n<< /Size {max_id + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode(
            "ascii"
        )
    )
    return out.getvalue()


def build_pdf(pages: list[tuple[str, object]]) -> bytes:
    """按页构造 PDF；``kind`` 为 ``ascii``/``cjk``/``raw_ascii``/``blank``。

    ``ascii``/``cjk`` 的 payload 是文本行元组；``raw_ascii`` 的 payload 是已渲染好的 ASCII
    内容流字节（用于多栏等自定义排列）；``blank`` 页无文本层。
    """

    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        4: (
            b"<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light "
            b"/Encoding /UniGB-UCS2-H /DescendantFonts [5 0 R] >>"
        ),
        5: (
            b"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light "
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 2 >> "
            b"/FontDescriptor 6 0 R /DW 1000 >>"
        ),
        6: (
            b"<< /Type /FontDescriptor /FontName /STSong-Light /Flags 4 "
            b"/FontBBox [0 -140 1000 860] /ItalicAngle 0 /Ascent 860 /Descent -140 "
            b"/CapHeight 860 /StemV 80 >>"
        ),
    }
    page_ids: list[int] = []
    next_id = 7
    for kind, payload in pages:
        if kind == "blank":
            page_id = next_id
            next_id += 1
            objects[page_id] = (
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] >>"
            )
        else:
            content_id = next_id
            page_id = next_id + 1
            next_id += 2
            if kind == "ascii":
                assert isinstance(payload, tuple)
                data = _ascii_content(payload)
                font_id = 3
            elif kind == "cjk":
                assert isinstance(payload, tuple)
                data = _cjk_content(payload)
                font_id = 4
            elif kind == "raw_ascii":
                assert isinstance(payload, bytes)
                data = payload
                font_id = 3
            else:
                raise ValueError(f"未知页面类型：{kind}")
            objects[content_id] = _stream(data)
            objects[page_id] = (
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
                % (font_id, content_id)
            )
        page_ids.append(page_id)
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects[2] = (
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("ascii")
    )
    return _assemble(objects)


def positive_ascii_multipage() -> bytes:
    """多页 ASCII 文本。"""

    return build_pdf(
        [
            ("ascii", ("Employee Handbook", "Section 1: Attendance")),
            ("ascii", ("Section 2: Leave Policy", "Ten working days per year.")),
            ("ascii", ("Section 3: Travel", "Submit receipts within 30 days.")),
        ]
    )


def positive_cjk_text_layer() -> bytes:
    """中文文本层；使用标准 CJK 字体，不嵌入字体文件。"""

    return build_pdf(
        [
            (
                "cjk",
                (
                    "员工餐厅营业时间：工作日 07:30-19:30，周末 09:00-14:00。",
                    "餐食订购：需在前一天 17:00 前下单，当日 09:00 前可取消。",
                    "访客餐券：每张面值 30 元，每位访客每日最多两张。",
                ),
            )
        ]
    )


def positive_multi_column_indent() -> bytes:
    """同一页上左右两栏与缩进行的文本层。"""

    content = _two_column_content(
        (
            (50, _START_Y, ("Left column first line", "Left column second line")),
            (70, _START_Y - 72, ("Indented continuation line",)),
            (320, _START_Y, ("Right column first line", "Right column second line")),
        )
    )
    return build_pdf([("raw_ascii", content)])


def positive_long_single_page() -> bytes:
    """单页长文本，用于验证页内超预算切分且 chunk 不跨页。"""

    return build_pdf([("ascii", ("Long single page body " * 60,))])


def positive_blank_and_text_pages() -> bytes:
    """空白页 + 有文本页 + 空白页；只有有文本的中间页产出块。"""

    return build_pdf(
        [
            ("blank", ()),
            ("ascii", ("Only the middle page carries text.",)),
            ("blank", ()),
        ]
    )


def positive_samples() -> dict[str, bytes]:
    """5 份内容互不相同的正样本，键为稳定名称。"""

    return {
        "ascii_multipage": positive_ascii_multipage(),
        "cjk_text_layer": positive_cjk_text_layer(),
        "multi_column_indent": positive_multi_column_indent(),
        "long_single_page": positive_long_single_page(),
        "blank_and_text_pages": positive_blank_and_text_pages(),
    }


def all_blank_pdf() -> bytes:
    """全空白页 PDF：零可提取文本，由管线判为 ``NEEDS_OCR``。"""

    return build_pdf([("blank", ()), ("blank", ())])


def corrupt_pdf() -> bytes:
    """带 PDF 头但结构损坏的字节。"""

    return b"%PDF-1.4 this is not a real pdf"


def too_many_pages_pdf() -> bytes:
    """页数超过 ``MAX_PDF_PAGES`` 的 PDF。"""

    return build_pdf([("blank", ())] * (MAX_PDF_PAGES + 1))


def encrypted_pdf() -> bytes:
    """带用户口令的加密 PDF；预检必须在抽取正文前拒绝。"""

    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(positive_ascii_multipage()))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt("s3cret")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


__all__ = [
    "all_blank_pdf",
    "build_pdf",
    "corrupt_pdf",
    "encrypted_pdf",
    "positive_ascii_multipage",
    "positive_blank_and_text_pages",
    "positive_cjk_text_layer",
    "positive_long_single_page",
    "positive_multi_column_indent",
    "positive_samples",
    "too_many_pages_pdf",
]
