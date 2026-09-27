"""生成评估语料中的文本层 PDF 样本（自制、无敏感内容、可离线复现）。

本脚本只用 dev 组已有的 ``pypdf`` 构造一个多页、带真实文本层的最小 PDF，供开发评估题集的
PDF 页定位 gold 引用使用；不引入任何新依赖、不联网、不读取环境文件。

中文无法用 pypdf 的标准 Type1 字体写入文本层（Helvetica 只能编码 latin-1），因此该 PDF 的
正文是 ASCII 文本；问题仍可用中文提问，gold 引用按页绑定 `page` 与原文引文即可。

用法（单行，在仓库根目录执行）::

    uv run python tests/evaluation/tools/build_corpus.py

写出 ``tests/evaluation/corpus/cafeteria.pdf``；重复运行内容稳定，页面文本可被
``rag_backend.ingestion.pdf_parsing.parse_pdf`` 逐页抽回。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

# 每页一段 ASCII 正文；页号从 1 开始，与 PDF locator 的 1-based 页号一致。
PDF_PAGES: tuple[str, ...] = (
    "Cafeteria Hours: Weekdays 07:30-19:30, Weekends 09:00-14:00.",
    "Meal Ordering: Place orders by 17:00 the day before; cancel by 09:00 on the same day.",
    "Visitor Meal Voucher: Each voucher is worth 30 CNY; max two per visitor per day.",
)

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "corpus" / "cafeteria.pdf"


def build_pdf(pages: tuple[str, ...] = PDF_PAGES) -> bytes:
    """用 pypdf 标准字体构造多页 PDF；空字符串表示空白页。"""

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
        page = writer.add_blank_page(width=300, height=200)
        if text:
            stream = DecodedStreamObject()
            stream.set_data(("BT /F1 12 Tf 10 100 Td (" + text + ") Tj ET").encode("latin-1"))
            page[NameObject("/Contents")] = writer._add_object(stream)
            page[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
            )
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def main() -> int:
    OUTPUT_PATH.write_bytes(build_pdf())
    print(f"写出评估样本 PDF：{OUTPUT_PATH.name}（{OUTPUT_PATH.stat().st_size} 字节）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
