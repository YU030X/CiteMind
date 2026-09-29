"""DOCX 纯解析：按文档原顺序把段落与表格行解析为带来源位置的纯数据块。

本模块只在 worker 子进程里被真正调用时才导入 ``python-docx``；顶层只引入标准库，因此 API
镜像（不安装 ``python-docx``/``lxml``）仍可导入本模块取常量与受理期 ZIP 元数据校验。解析函数
不接触数据库、队列或模型，不写日志、不联网、不渲染 HTML。

允许的 DOCX 子集（刻意收窄，只索引 ``word/document.xml`` 的正文）：

- 只解析 ``word/document.xml`` 的 body：页眉、页脚、脚注、尾注、批注与文本框不属于正文，
  一律不索引，也不在收窄子集的静态拒绝范围内；正文之外的结构不被检测也不报错。
- 正文按 XML 原顺序遍历 ``w:p``（段落）与 ``w:tbl``（表格）：段落按文档顺序编号，空段落也
  占用 1-based ``paragraph_index``，但不产出块。
- 段落标题沿用 Markdown 的标题栈语义：样式名/样式 id 匹配 ``Heading N`` 或中文 ``标题 N``
  时更新 ``heading_path``，标题自身不产出正文块。
- 表格逐行产出块：每个非空行一个块，横向合并（``gridSpan``）只记录真实 origin 一次，
  纵向合并的 ``continue`` 单元格不复制上一行文字，``gridBefore`` 偏移计入 1-based 网格列。
- 表格嵌套在单元格内、结构化文档标签 ``w:sdt``、外部内容 ``w:altChunk``/``w:customXml``
  一律静态 ``DocxUnsupportedError``；不处理宏（含宏部件的文件拒绝）、不访问外链、不推测 Word
  页码。
- ``source_sha256`` 只按输入 bytes 计算；``heading_path`` 复用与 Markdown 相同的层级栈。

ZIP/解析安全（数值见下方常量）：

- 受理期用标准库 ``zipfile`` 只读元数据快速拒绝：非 ZIP、条目数超限、声明累计解压超限、
  单条目声明过大且压缩比过高、加密、路径绝对/含 ``..``/反斜杠/NUL/重复名、缺必需部件、
  含宏部件。
- worker 侧在把字节交给 ``python-docx`` 之前，用同一策略逐条目**有界流式实际读取**并累计
  实际字节，读取同时校验 CRC；超限、坏 ZIP/XML 或含 ``<!DOCTYPE``/``<!ENTITY`` 声明的部件
  静态失败。子进程总时长与返回体另有既有硬限，本模块不声称内存硬隔离。
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass

from rag_backend.ingestion.parsing import DocxCellSpan, ParsedBlock, ParsedDocument

# 上传事务与解析实现共用的单一真源；含精确 python-docx 版本，升级依赖时必须同步评审。
DOCX_PARSER_VERSION = "python-docx-1.2.0-v1"
SOURCE_TYPE_DOCX = "docx"

# ZIP/解析安全上限；受理期与 worker 侧共用同一份策略。
DOCX_MAGIC = b"PK\x03\x04"
MAX_DOCX_ZIP_ENTRIES = 512
MAX_DOCX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
DOCX_ENTRY_LARGE_BYTES = 1024 * 1024
MAX_DOCX_COMPRESSION_RATIO = 100
_REQUIRED_PARTS = ("[Content_Types].xml", "_rels/.rels", "word/document.xml")
_MACRO_PART_MARKERS = ("vbaproject", "vbadata")
_ENTITY_DECLARATION = re.compile(rb"<!(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W_TAG = f"{{{_W_NS}}}t"
_W_P_TAG = f"{{{_W_NS}}}p"
_W_TBL_TAG = f"{{{_W_NS}}}tbl"
_NSMAP = {"w": _W_NS}


def _has(element: object, expression: str) -> bool:
    """执行 XPath 并返回是否有命中（python-docx 元素已内置 w 命名空间映射）。"""

    return bool(element.xpath(expression))  # type: ignore[attr-defined]


class DocxParsingError(Exception):
    """DOCX 解析失败基类；消息只含静态类别，不含正文、路径或凭据。"""


class DocxInvalidError(DocxParsingError):
    """DOCX 不是可识别的 ZIP/OPC 包，或 CRC/XML/必需部件损坏。"""


class DocxUnsupportedError(DocxParsingError):
    """DOCX 包有效但属于刻意不收窄支持的结构（嵌套表、SDT、宏、实体声明等）。"""


class DocxTooLargeError(DocxParsingError):
    """DOCX 声明的或实际解压内容超过安全上限。"""


def _reject_path(name: str) -> None:
    """拒绝绝对路径、``..``、反斜杠、NUL 与空名；只接受正向相对部件名。"""

    if not name:
        raise DocxInvalidError("DOCX 含空部件名")
    if "\x00" in name or "\\" in name:
        raise DocxInvalidError("DOCX 部件名含非法字符")
    if name.startswith("/") or _DRIVE_PREFIX.match(name):
        raise DocxInvalidError("DOCX 部件名是绝对路径")
    if any(part == ".." for part in name.split("/")):
        raise DocxInvalidError("DOCX 部件名含上级目录")


def _declared_ratio_rejected(info: zipfile.ZipInfo) -> bool:
    """单条声明解压超过 1 MiB 且压缩比超过 100 时拒绝（防 zip bomb 的早筛）。"""

    if info.file_size <= DOCX_ENTRY_LARGE_BYTES:
        return False
    if info.compress_size <= 0:
        # 声明有解压内容却没有压缩字节：压缩比无限，直接拒绝。
        return info.file_size > 0
    return info.file_size / info.compress_size > MAX_DOCX_COMPRESSION_RATIO


def _check_metadata(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """按元数据校验条目集合，返回待读取的条目；只读 ``infolist``，不解压正文。"""

    infos = archive.infolist()
    if len(infos) > MAX_DOCX_ZIP_ENTRIES:
        raise DocxTooLargeError("DOCX 条目数超过上限")
    declared_total = 0
    seen_names: set[str] = set()
    names_set: set[str] = set()
    for info in infos:
        name = info.filename
        _reject_path(name)
        folded = name.casefold()
        if folded in seen_names:
            raise DocxInvalidError("DOCX 含重复部件名")
        seen_names.add(folded)
        names_set.add(name)
        if info.flag_bits & 0x1:
            raise DocxInvalidError("DOCX 含加密条目")
        if any(marker in folded for marker in _MACRO_PART_MARKERS):
            raise DocxUnsupportedError("DOCX 含宏部件")
        if _declared_ratio_rejected(info):
            raise DocxTooLargeError("DOCX 单条目压缩比超过上限")
        declared_total += info.file_size
        if declared_total > MAX_DOCX_UNCOMPRESSED_BYTES:
            raise DocxTooLargeError("DOCX 声明解压总量超过上限")
    missing = [part for part in _REQUIRED_PARTS if part not in names_set]
    if missing:
        raise DocxInvalidError("DOCX 缺少必需部件")
    return infos


def inspect_docx_zip(content: bytes) -> None:
    """受理期快速校验：只读 ZIP 元数据，不导入 ``python-docx``、不解压正文。

    非 ZIP、条目/声明大小/压缩比/加密/路径/重复名/宏部件/缺必需部件都静态失败；调用方
    （上传受理）据此在不安装 ``python-docx`` 的 API 进程内拒绝明显不合格的 DOCX。
    """

    if not content:
        raise DocxInvalidError("DOCX 内容为空")
    if not content.startswith(DOCX_MAGIC):
        raise DocxInvalidError("文件不是可识别的 DOCX")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            _check_metadata(archive)
    except (zipfile.BadZipFile, OSError, EOFError):
        raise DocxInvalidError("DOCX ZIP 结构损坏") from None


def _read_bounded_entries(archive: zipfile.ZipFile, infos: list[zipfile.ZipInfo]) -> None:
    """逐条目有界流式实际读取并累计字节；校验 CRC、XML 实体声明与宏部件。"""

    total = 0
    for info in infos:
        if info.is_dir():
            continue
        folded = info.filename.casefold()
        if any(marker in folded for marker in _MACRO_PART_MARKERS):
            raise DocxUnsupportedError("DOCX 含宏部件")
        scan_xml = folded.endswith(".xml") or folded.endswith(".rels")
        window = b""
        try:
            with archive.open(info) as handle:
                while True:
                    chunk = handle.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_DOCX_UNCOMPRESSED_BYTES:
                        raise DocxTooLargeError("DOCX 实际解压总量超过上限")
                    if scan_xml:
                        # 保留 16 字节重叠，避免声明跨读取块边界时漏判。
                        if _ENTITY_DECLARATION.search(window + chunk):
                            raise DocxUnsupportedError("DOCX 部件含实体声明")
                        window = (window + chunk)[-16:]
        except (zipfile.BadZipFile, OSError, EOFError, RuntimeError):
            raise DocxInvalidError("DOCX 条目读取失败或 CRC 校验不通过") from None


def _bounded_validate(content: bytes) -> None:
    """worker 侧完整校验：元数据 + 逐条目有界流式实际读取（CRC/实体/宏/总量）。"""

    inspect_docx_zip(content)
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = _check_metadata(archive)
            _read_bounded_entries(archive, infos)
    except DocxParsingError:
        raise
    except (zipfile.BadZipFile, OSError, EOFError):
        raise DocxInvalidError("DOCX ZIP 结构损坏") from None


def _paragraph_text(paragraph_element: object) -> str:
    """取 ``w:p`` 下所有 ``w:t`` 的文字（含超链接显示文字），规范化空白、不取 URL。"""

    pieces = [
        node.text
        for node in paragraph_element.iter(_W_TAG)  # type: ignore[attr-defined]
        if node.text
    ]
    return " ".join("".join(pieces).split())


_HEADING_ID = re.compile(r"^Heading\s*([1-9])$")
_HEADING_NAME = re.compile(r"^(?:Heading|标题)\s*([1-9])$")


def _heading_level(document: object, paragraph_element: object) -> int | None:
    """按段落样式判定标题层级；仅识别内置 Heading/标题 样式，其余按普通正文。"""

    from docx.text.paragraph import Paragraph

    try:
        paragraph = Paragraph(paragraph_element, document)  # type: ignore[arg-type]
        style = paragraph.style
    except (KeyError, ValueError, AttributeError):
        return None
    if style is None:
        return None
    candidates = []
    try:
        candidates.append(style.style_id or "")
        candidates.append(style.name or "")
    except Exception:  # noqa: BLE001 - 样式解析失败按普通正文处理，不静默丢弃文字
        return None
    for candidate in candidates:
        match = _HEADING_ID.match(candidate) or _HEADING_NAME.match(candidate)
        if match is not None:
            return int(match.group(1))
    return None


def _update_heading_stack(
    heading_stack: list[tuple[int, str]], level: int, title: str
) -> None:
    while heading_stack and heading_stack[-1][0] >= level:
        heading_stack.pop()
    if title:
        heading_stack.append((level, title))


@dataclass(frozen=True)
class _TableRow:
    text: str
    cells: tuple[DocxCellSpan, ...]


def _row_block(row_element: object) -> _TableRow | None:
    """把一个 ``w:tr`` 转成规范化行文字与单元格位置；全空返回 ``None``。

    横向合并只取真实 ``w:tc`` origin（``gridSpan`` 只出现一次）；纵向合并 continue 单元格
    不复制上一行文字；``gridBefore`` 偏移由 ``CT_Tc.grid_offset`` 计入 1-based 网格列。
    """

    emitted: list[tuple[int, int, str]] = []
    for cell in row_element.iterfind("w:tc", _NSMAP):  # type: ignore[attr-defined]
        if _has(cell, ".//w:tbl"):
            raise DocxUnsupportedError("DOCX 含嵌套表格")
        v_merge = cell.vMerge
        if v_merge == "continue":
            # 纵向合并续格：文字归属上方 origin，绝不复制到本行伪造来源。
            continue
        cell_text = " ".join(
            filter(None, (_paragraph_text(p) for p in cell.iter(_W_P_TAG)))
        )
        emitted.append((int(cell.grid_offset) + 1, int(cell.grid_span), cell_text))

    # 去掉首尾空单元格，避免行文字以空分隔符开头/结尾；保留中间空单元格以维持列对齐。
    while emitted and not emitted[0][2]:
        emitted.pop(0)
    while emitted and not emitted[-1][2]:
        emitted.pop()
    if not emitted:
        return None

    text_parts: list[str] = []
    cells: list[DocxCellSpan] = []
    cursor = 0
    for index, (grid_column, grid_span, cell_text) in enumerate(emitted):
        if index:
            text_parts.append(" | ")
            cursor += 3
        char_start = cursor
        text_parts.append(cell_text)
        cursor += len(cell_text)
        cells.append(
            DocxCellSpan(
                grid_column=grid_column,
                grid_span=grid_span,
                char_start=char_start,
                char_end=cursor,
            )
        )
    return _TableRow(text="".join(text_parts), cells=tuple(cells))


def _assert_supported_package(document: object) -> None:
    """拒绝正文 body 中可检测到的收窄外结构（嵌套表、``sdt``、``altChunk``、``customXml``）。"""

    body = document.element.body  # type: ignore[attr-defined]
    if _has(body, ".//w:altChunk"):
        raise DocxUnsupportedError("DOCX 含外部内容 altChunk，不支持")
    if _has(body, ".//w:customXml"):
        raise DocxUnsupportedError("DOCX 含 customXml，不支持")
    if _has(body, ".//w:sdt"):
        raise DocxUnsupportedError("DOCX 含结构化文档标签 sdt，不支持")
    if _has(body, ".//w:tbl//w:tbl"):
        raise DocxUnsupportedError("DOCX 含嵌套表格，不支持")


def parse_docx(content: bytes) -> ParsedDocument:
    """解析 DOCX 字节为带来源位置的块序列；嵌套表/宏/实体等收窄外结构静态失败。

    ``python-docx`` 在函数内延迟导入，避免 API 镜像导入本模块时失败。读取前先做有界 ZIP
    校验；解析只读 ``w:t`` 文字，不访问外链、不渲染、不执行宏。
    """

    _bounded_validate(content)
    source_sha256 = hashlib.sha256(content).hexdigest()

    from docx import Document

    try:
        document = Document(io.BytesIO(content))
        _assert_supported_package(document)
    except DocxParsingError:
        raise
    except Exception:  # noqa: BLE001 - python-docx 已知/未知错误统一收敛为静态损坏
        raise DocxInvalidError("DOCX 结构损坏或无法解析") from None

    body = document.element.body
    heading_stack: list[tuple[int, str]] = []
    blocks: list[ParsedBlock] = []
    paragraph_index = 0
    table_index = 0

    for child in body:
        if child.tag == _W_P_TAG:
            paragraph_index += 1
            level = _heading_level(document, child)
            if level is not None:
                _update_heading_stack(heading_stack, level, _paragraph_text(child))
                continue
            text = _paragraph_text(child)
            if not text:
                continue
            blocks.append(
                ParsedBlock(
                    ordinal=len(blocks),
                    kind="paragraph",
                    heading_path=tuple(title for _, title in heading_stack),
                    text=text,
                    paragraph_index=paragraph_index,
                )
            )
        elif child.tag == _W_TBL_TAG:
            table_index += 1
            for row_index, row in enumerate(child.iterfind("w:tr", _NSMAP), start=1):
                parsed = _row_block(row)
                if parsed is None:
                    continue
                blocks.append(
                    ParsedBlock(
                        ordinal=len(blocks),
                        kind="table_row",
                        heading_path=tuple(title for _, title in heading_stack),
                        text=parsed.text,
                        table_index=table_index,
                        row_index=row_index,
                        cells=parsed.cells,
                    )
                )
        # 其余 body 直接子节点（节属性、书签标记等）不承载正文，忽略即可。

    text = "\n\n".join(block.text for block in blocks)
    return ParsedDocument(
        source_sha256=source_sha256,
        text=text,
        blocks=tuple(blocks),
        parser_version=DOCX_PARSER_VERSION,
        source_type=SOURCE_TYPE_DOCX,
    )


__all__ = [
    "DOCX_MAGIC",
    "DOCX_PARSER_VERSION",
    "DOCX_ENTRY_LARGE_BYTES",
    "MAX_DOCX_COMPRESSION_RATIO",
    "MAX_DOCX_UNCOMPRESSED_BYTES",
    "MAX_DOCX_ZIP_ENTRIES",
    "SOURCE_TYPE_DOCX",
    "DocxInvalidError",
    "DocxParsingError",
    "DocxTooLargeError",
    "DocxUnsupportedError",
    "inspect_docx_zip",
    "parse_docx",
]
