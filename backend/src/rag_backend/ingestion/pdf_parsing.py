"""文本层 PDF 纯解析：把 PDF 字节按页抽取为带页定位的纯数据块。

本模块只做解析，不接触数据库、队列或模型：输入是上传校验已接受的 PDF 字节，输出是不可变
的数据结构。它不写文件、不写日志、不渲染、不联网，因此可在单元测试里独立验证。

边界（与 [文档入库](../../../docs/ingestion.md) 的来源定位表一致）：

- 逐页调用 pypdf ``extract_text``；每个非空页产生一个块，``heading_path`` 为空元组，
  ``start_line``/``end_line`` 保持 ``None``——PDF 没有可靠行号，绝不伪造。
- ``source_sha256`` 只按原始 bytes 计算；``page`` 为 1-based 页号，供 ``locator_version=2``
  的页定位使用。
- 页数上限 ``MAX_PDF_PAGES``（200）：超过即拒绝；零可提取文本（扫描件）返回空块序列，由
  入库管线判为 ``NEEDS_OCR``，不把空提取当成功。
- 加密、结构损坏分别抛 :class:`PdfEncryptedError` / :class:`PdfInvalidError`，由上层映射为
  静态失败，不携带正文、路径或凭据。任何 ``is_encrypted`` 标志都拒绝（包括仅用空口令即可
  解密的 PDF）；本模块不做 ``decrypt``，也暂不区分加密种类。

``pypdf`` 是 worker 组依赖：本模块顶层**不**导入 pypdf，只有 ``parse_pdf`` 被真正调用时才
延迟导入，因此 API 镜像（不安装 worker 组）仍可导入本模块取得 ``PDF_PARSER_VERSION``。
"""

from __future__ import annotations

import hashlib
import io

from rag_backend.ingestion.parsing import ParsedBlock, ParsedDocument

# 上传事务与解析实现共用的单一真源：包含精确 pypdf 版本；升级依赖时必须同步评审。
PDF_PARSER_VERSION = "pypdf-6.19.0-v1"

# 单文档最多解析的页数；超过则不抽取、静态失败，避免资源无界占用。
MAX_PDF_PAGES = 200

SOURCE_TYPE_PDF = "pdf"

# PDF 头魔数（1-based 偏移的前缀）；用于上传与只读读取的二进制校验。
PDF_MAGIC = b"%PDF-"


class PdfParsingError(Exception):
    """PDF 解析失败基类；消息只含静态类别，不含正文、路径或凭据。"""


class PdfEncryptedError(PdfParsingError):
    """PDF 带任意加密标志，统一拒绝；不尝试空白口令 ``decrypt``。"""


class PdfTooManyPagesError(PdfParsingError):
    """PDF 页数超过 ``MAX_PDF_PAGES``。"""


class PdfInvalidError(PdfParsingError):
    """PDF 结构损坏、不是 PDF 或 pypdf 无法解析。"""


def _normalize_page_text(raw: str) -> str:
    """只去掉首尾空白；不改动页内换行与字符，保持文本可回放到页。"""

    return raw.strip()


def parse_pdf(content: bytes) -> ParsedDocument:
    """逐页抽取 PDF 文本层，返回带页定位的块序列。

    零可提取文本返回空块序列（调用方据此判 ``NEEDS_OCR``）；加密、页数超限、结构损坏分
    别抛具名错误。``pypdf`` 在函数内延迟导入，避免 API 镜像导入本模块时失败。
    """

    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    source_sha256 = hashlib.sha256(content).hexdigest()
    try:
        reader = PdfReader(io.BytesIO(content), strict=False)
        if reader.is_encrypted:
            raise PdfEncryptedError("PDF 已加密")
        page_count = len(reader.pages)
    except PdfEncryptedError:
        raise
    except PdfReadError:
        raise PdfInvalidError("PDF 结构损坏") from None
    except Exception:
        # 非 pypdf 已知错误（含畸形对象/内存边界外的异常）统一收敛为静态损坏。
        raise PdfInvalidError("PDF 结构损坏") from None

    if page_count > MAX_PDF_PAGES:
        raise PdfTooManyPagesError("PDF 页数超过上限")

    blocks: list[ParsedBlock] = []
    page_texts: list[str] = []
    for index in range(page_count):
        try:
            text = reader.pages[index].extract_text() or ""
        except Exception:
            raise PdfInvalidError("PDF 页解析失败") from None
        normalized = _normalize_page_text(text)
        page_texts.append(normalized)
        if not normalized:
            continue
        blocks.append(
            ParsedBlock(
                ordinal=len(blocks),
                kind="pdf_page",
                heading_path=(),
                text=normalized,
                start_line=None,
                end_line=None,
                page=index + 1,
            )
        )

    return ParsedDocument(
        source_sha256=source_sha256,
        text="\n\n".join(page_texts),
        blocks=tuple(blocks),
        parser_version=PDF_PARSER_VERSION,
        source_type=SOURCE_TYPE_PDF,
    )


__all__ = [
    "MAX_PDF_PAGES",
    "PDF_MAGIC",
    "PDF_PARSER_VERSION",
    "SOURCE_TYPE_PDF",
    "PdfEncryptedError",
    "PdfInvalidError",
    "PdfParsingError",
    "PdfTooManyPagesError",
    "parse_pdf",
]
