"""在独立子进程中执行 Markdown 与文本 PDF 解析，给解析阶段一个真实、有界、可终止的硬时限。

设计与边界（与 [文档入库](../../../docs/ingestion.md) 一致）：

- 解析只在独立子进程里运行，父进程用 ``communicate(input=raw_bytes, timeout=...)`` 有界等待；
  超时后真实 ``kill`` 并 ``wait`` 回收，再抛出 :class:`ParseSubprocessTimeout`。这样 Celery
  worker 的 lease 心跳不会无限期掩盖一个挂起的解析。
- 子进程只接收**原始文档字节**（stdin），只返回结构化的解析块（stdout JSON），不读
  ``.env``、不接收数据库 DSN 或 inference 凭据；父进程用白名单环境变量启动子进程，剥离
  ``DATABASE_URL``/``REDIS_URL``/``INFERENCE_TOKEN``/``LLM_API_KEY`` 等键。
- 返回体有界：子进程在单次写出前检查 ``MAX_PARSE_RESULT_BYTES``，超限直接以
  ``EXIT_RESULT_TOO_LARGE`` 失败、不写出任何字节，因此父进程 ``communicate`` 缓冲最多该上限，
  并在收到后再次校验。父子管道本身没有独立的流式硬限：若子进程逻辑失效绕过检查，父进程仍会先
  缓冲再校验（已知残余，未为此引入通用超时/限流框架）。子进程 stderr 丢弃，避免未捕获 traceback
  泄露路径并限制内存。
- 不依赖 ``multiprocessing``：Celery daemon 进程不能安全创建 multiprocessing 子进程，这里使用
  受控 ``subprocess`` 入口，因此 Windows spawn 与 Linux prefork 行为一致。管道、进程与描述符都在
  ``finally`` 中显式回收。
- ``ParsedDocument.text`` 不再跨进程传输（父进程已有解码后的原文），只回传 ``source_sha256``、
  ``parser_version`` 与 ``blocks``，避免把整篇正文重复序列化。
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from typing import Final

from rag_backend.ingestion.docx_parsing import (
    DocxInvalidError,
    DocxParsingError,
    DocxTooLargeError,
    DocxUnsupportedError,
    parse_docx,
)
from rag_backend.ingestion.parsing import DocxCellSpan, ParsedBlock, ParsedDocument, parse_markdown
from rag_backend.ingestion.pdf_parsing import (
    PdfEncryptedError,
    PdfParsingError,
    PdfTooManyPagesError,
    parse_pdf,
)
from rag_backend.ingestion.validation import MAX_DOCUMENT_BYTES

# 受控入口模块名；父进程用 ``python -m`` 启动它，并通过 argv 指定来源类型。
PARSE_SUBPROCESS_MODULE: Final = "rag_backend.ingestion.parse_subprocess"
SOURCE_TYPE_MARKDOWN: Final = "markdown"
SOURCE_TYPE_PDF: Final = "pdf"
SOURCE_TYPE_DOCX: Final = "docx"

# 解析硬时限与返回体上限；返回体上限覆盖 JSON 转义膨胀（CJK 不转义，控制字符会转义）。
PARSE_TIMEOUT_SECONDS: Final = 60.0
MAX_PARSE_RESULT_BYTES: Final = 4 * MAX_DOCUMENT_BYTES

EXIT_OK: Final = 0
EXIT_INPUT_TOO_LARGE: Final = 2
EXIT_INVALID_INPUT: Final = 3
EXIT_RESULT_TOO_LARGE: Final = 4
EXIT_INTERNAL_ERROR: Final = 1
# PDF 具名退出码：父进程据此映射为可区分的静态失败，不与通用解析失败混用。
EXIT_PDF_ENCRYPTED: Final = 5
EXIT_PDF_TOO_MANY_PAGES: Final = 6
EXIT_PDF_INVALID: Final = 7
# DOCX 具名退出码：收窄子集外的结构与无效/超限包分开，父进程映射为可区分静态失败。
EXIT_DOCX_UNSUPPORTED: Final = 8
EXIT_DOCX_INVALID: Final = 9

# 子进程环境白名单：只保留解释器与临时目录所需键，剥离任何业务凭据。
_SAFE_ENV_KEYS: Final = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
    "TMPDIR",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONIOENCODING",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
)


class ParseSubprocessError(RuntimeError):
    """解析子进程无法给出可用结果；静态失败，不携带正文、路径或凭据。"""


class ParseSubprocessTimeout(ParseSubprocessError):
    """解析子进程超过硬时限，已被终止并回收。"""


class ParseSubprocessFailed(ParseSubprocessError):
    """解析子进程非零退出、返回体超限或结果结构不合法。"""


class PdfSubprocessError(ParseSubprocessError):
    """父进程对 PDF 子进程具名退出码的静态映射基类。"""


class PdfEncryptedSubprocessError(PdfSubprocessError):
    """子进程判定 PDF 已加密。"""


class PdfTooManyPagesSubprocessError(PdfSubprocessError):
    """子进程判定 PDF 页数超限。"""


class PdfInvalidSubprocessError(PdfSubprocessError):
    """子进程判定 PDF 结构损坏或无法解析。"""


class DocxSubprocessError(ParseSubprocessError):
    """父进程对 DOCX 子进程具名退出码的静态映射基类。"""


class DocxUnsupportedSubprocessError(DocxSubprocessError):
    """子进程判定 DOCX 属于刻意不收窄支持的结构（嵌套表、宏、实体等）。"""


class DocxInvalidSubprocessError(DocxSubprocessError):
    """子进程判定 DOCX 无效、超限或 CRC/XML 损坏。"""


def sanitized_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """按白名单构造子进程环境，剥离业务凭据与 DSN。"""

    source = environment if environment is not None else os.environ
    return {key: source[key] for key in _SAFE_ENV_KEYS if key in source}


def _serialize(document: ParsedDocument) -> dict[str, object]:
    """把解析结果摊平成可 JSON 序列化的结构；刻意不含整篇 ``text``。"""

    return {
        "source_sha256": document.source_sha256,
        "parser_version": document.parser_version,
        "source_type": document.source_type,
        "blocks": [
            {
                "ordinal": block.ordinal,
                "kind": block.kind,
                "heading_path": list(block.heading_path),
                "text": block.text,
                "start_line": block.start_line,
                "end_line": block.end_line,
                "list_depth": block.list_depth,
                "level": block.level,
                "code_info": block.code_info,
                "page": block.page,
                "paragraph_index": block.paragraph_index,
                "table_index": block.table_index,
                "row_index": block.row_index,
                "cells": [
                    {
                        "grid_column": cell.grid_column,
                        "grid_span": cell.grid_span,
                        "char_start": cell.char_start,
                        "char_end": cell.char_end,
                    }
                    for cell in block.cells
                ],
            }
            for block in document.blocks
        ],
    }


def _deserialize(payload: object, *, text: str) -> ParsedDocument:
    """严格校验子进程回传的结构并重建 :class:`ParsedDocument`。"""

    if not isinstance(payload, dict):
        raise ParseSubprocessFailed("解析子进程返回结构不合法")
    source_sha256 = payload.get("source_sha256")
    parser_version = payload.get("parser_version")
    source_type = payload.get("source_type", SOURCE_TYPE_MARKDOWN)
    raw_blocks = payload.get("blocks")
    if not isinstance(source_sha256, str) or not isinstance(parser_version, str):
        raise ParseSubprocessFailed("解析子进程返回结构不合法")
    if source_type not in (SOURCE_TYPE_MARKDOWN, SOURCE_TYPE_PDF, SOURCE_TYPE_DOCX):
        raise ParseSubprocessFailed("解析子进程返回结构不合法")
    if not isinstance(raw_blocks, list):
        raise ParseSubprocessFailed("解析子进程返回结构不合法")

    blocks: list[ParsedBlock] = []
    for raw in raw_blocks:
        if not isinstance(raw, dict):
            raise ParseSubprocessFailed("解析子进程返回结构不合法")
        try:
            heading_path = raw["heading_path"]
            if not isinstance(heading_path, list) or not all(
                isinstance(item, str) for item in heading_path
            ):
                raise ParseSubprocessFailed("解析子进程返回结构不合法")
            level = raw["level"]
            code_info = raw["code_info"]
            start_line = raw.get("start_line")
            end_line = raw.get("end_line")
            page = raw.get("page")
            paragraph_index = raw.get("paragraph_index")
            table_index = raw.get("table_index")
            row_index = raw.get("row_index")
            if level is not None and not isinstance(level, int):
                raise ParseSubprocessFailed("解析子进程返回结构不合法")
            if code_info is not None and not isinstance(code_info, str):
                raise ParseSubprocessFailed("解析子进程返回结构不合法")
            for value in (
                start_line,
                end_line,
                page,
                paragraph_index,
                table_index,
                row_index,
            ):
                if value is not None and not isinstance(value, int):
                    raise ParseSubprocessFailed("解析子进程返回结构不合法")
            blocks.append(
                ParsedBlock(
                    ordinal=int(raw["ordinal"]),
                    kind=str(raw["kind"]),
                    heading_path=tuple(heading_path),
                    text=str(raw["text"]),
                    start_line=start_line,
                    end_line=end_line,
                    list_depth=int(raw["list_depth"]),
                    level=level,
                    code_info=code_info,
                    page=page,
                    paragraph_index=paragraph_index,
                    table_index=table_index,
                    row_index=row_index,
                    cells=_cells_from_raw(raw.get("cells", [])),
                )
            )
        except (KeyError, TypeError, ValueError):
            raise ParseSubprocessFailed("解析子进程返回结构不合法") from None

    return ParsedDocument(
        source_sha256=source_sha256,
        text=text,
        blocks=tuple(blocks),
        parser_version=parser_version,
        source_type=source_type,
    )


def _cells_from_raw(raw_cells: object) -> tuple[DocxCellSpan, ...]:
    """严格校验子进程回传的 DOCX 单元格位置；不合法直接静态失败。"""

    if not isinstance(raw_cells, list):
        raise ParseSubprocessFailed("解析子进程返回结构不合法")
    cells: list[DocxCellSpan] = []
    for raw in raw_cells:
        if not isinstance(raw, dict):
            raise ParseSubprocessFailed("解析子进程返回结构不合法")
        try:
            grid_column = int(raw["grid_column"])
            grid_span = int(raw["grid_span"])
            char_start = int(raw["char_start"])
            char_end = int(raw["char_end"])
        except (KeyError, TypeError, ValueError):
            raise ParseSubprocessFailed("解析子进程返回结构不合法") from None
        if grid_column < 1 or grid_span < 1 or char_start < 0 or char_end < char_start:
            raise ParseSubprocessFailed("解析子进程返回结构不合法")
        cells.append(
            DocxCellSpan(
                grid_column=grid_column,
                grid_span=grid_span,
                char_start=char_start,
                char_end=char_end,
            )
        )
    return tuple(cells)


def _read_bounded_stdin() -> bytes:
    """从 stdin 最多读取 ``MAX_DOCUMENT_BYTES + 1`` 字节。"""

    return sys.stdin.buffer.read(MAX_DOCUMENT_BYTES + 1)


def _parse_docx_or_raise(data: bytes) -> ParsedDocument:
    """调用 DOCX 解析并把具名错误转成子进程退出码。"""

    try:
        return parse_docx(data)
    except DocxUnsupportedError:
        raise _ExitWithCode(EXIT_DOCX_UNSUPPORTED) from None
    except (DocxTooLargeError, DocxInvalidError):
        raise _ExitWithCode(EXIT_DOCX_INVALID) from None
    except DocxParsingError:
        raise _ExitWithCode(EXIT_DOCX_INVALID) from None


def _parse_pdf_or_raise(data: bytes) -> ParsedDocument:
    """调用 PDF 解析并把具名错误转成子进程退出码。"""

    try:
        return parse_pdf(data)
    except PdfEncryptedError:
        raise _ExitWithCode(EXIT_PDF_ENCRYPTED) from None
    except PdfTooManyPagesError:
        raise _ExitWithCode(EXIT_PDF_TOO_MANY_PAGES) from None
    except PdfParsingError:
        raise _ExitWithCode(EXIT_PDF_INVALID) from None


class _ExitWithCode(Exception):
    """子进程专用控制流：携带具名退出码，不携带正文。"""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def main(argv: list[str] | None = None) -> int:
    """子进程入口：读取原始字节，按 argv 指定的来源解析，输出有界 JSON。

    ``argv[1]`` 为来源类型（缺省 ``markdown``）；只接受受支持的来源，否则静态失败。不输出
    任何凭据或路径。
    """

    arguments = sys.argv[1:] if argv is None else argv
    source_type = arguments[0] if arguments else SOURCE_TYPE_MARKDOWN
    if source_type not in (SOURCE_TYPE_MARKDOWN, SOURCE_TYPE_PDF, SOURCE_TYPE_DOCX):
        return EXIT_INVALID_INPUT
    try:
        data = _read_bounded_stdin()
    except OSError:
        return EXIT_INTERNAL_ERROR
    if len(data) > MAX_DOCUMENT_BYTES:
        return EXIT_INPUT_TOO_LARGE
    try:
        if source_type == SOURCE_TYPE_PDF:
            document = _parse_pdf_or_raise(data)
        elif source_type == SOURCE_TYPE_DOCX:
            document = _parse_docx_or_raise(data)
        else:
            document = parse_markdown(data)
    except _ExitWithCode as exit_error:
        return exit_error.code
    except UnicodeDecodeError:
        return EXIT_INVALID_INPUT
    except Exception:  # noqa: BLE001 - 子进程边界必须收敛为退出码而非 traceback
        return EXIT_INTERNAL_ERROR
    try:
        raw = json.dumps(
            _serialize(document),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return EXIT_INTERNAL_ERROR
    if len(raw) > MAX_PARSE_RESULT_BYTES:
        return EXIT_RESULT_TOO_LARGE
    try:
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()
    except OSError:
        return EXIT_INTERNAL_ERROR
    return EXIT_OK


def _cleanup_process(process: subprocess.Popen[bytes]) -> None:
    """确保进程被终止并回收、三个管道描述符都关闭；幂等且不抛异常。"""

    if process.poll() is None:
        with contextlib.suppress(OSError):
            process.kill()
        with contextlib.suppress(Exception):
            process.wait(timeout=5)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.close()


def parse_markdown_in_subprocess(
    content: bytes,
    *,
    timeout_seconds: float = PARSE_TIMEOUT_SECONDS,
    environment: Mapping[str, str] | None = None,
) -> ParsedDocument:
    """在独立子进程中解析 Markdown 字节，超时真实终止并回收后静态失败。

    返回可用结果时才返回 :class:`ParsedDocument`；``text`` 由调用方已有的解码文本回填。
    """

    return _parse_in_subprocess(
        content,
        source_type=SOURCE_TYPE_MARKDOWN,
        timeout_seconds=timeout_seconds,
        environment=environment,
    )


def parse_pdf_in_subprocess(
    content: bytes,
    *,
    timeout_seconds: float = PARSE_TIMEOUT_SECONDS,
    environment: Mapping[str, str] | None = None,
) -> ParsedDocument:
    """在独立子进程中按页抽取 PDF 文本层；加密/超页/损坏映射为具名静态失败。

    与 Markdown 共用同一硬时限、环境白名单与返回体上限；``text`` 不回传，父进程用空串重建，
    chunk 只依赖 ``blocks`` 与 ``source_sha256``。
    """

    return _parse_in_subprocess(
        content,
        source_type=SOURCE_TYPE_PDF,
        timeout_seconds=timeout_seconds,
        environment=environment,
    )


def parse_docx_in_subprocess(
    content: bytes,
    *,
    timeout_seconds: float = PARSE_TIMEOUT_SECONDS,
    environment: Mapping[str, str] | None = None,
) -> ParsedDocument:
    """在独立子进程中按段落/表格行解析 DOCX；嵌套表/宏/实体/损坏映射为具名静态失败。

    与 Markdown/PDF 共用同一硬时限、环境白名单与返回体上限；``text`` 不回传，父进程用空串
    重建，chunk 只依赖 ``blocks`` 与 ``source_sha256``。
    """

    return _parse_in_subprocess(
        content,
        source_type=SOURCE_TYPE_DOCX,
        timeout_seconds=timeout_seconds,
        environment=environment,
    )


def _parse_in_subprocess(
    content: bytes,
    *,
    source_type: str,
    timeout_seconds: float,
    environment: Mapping[str, str] | None,
) -> ParsedDocument:
    """受控子进程通用执行体：有界输入、有界等待、具名退出码映射。"""

    if len(content) > MAX_DOCUMENT_BYTES:
        raise ParseSubprocessFailed("解析输入超过单文件字节上限")
    if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise ValueError("解析超时必须是正数")

    process = subprocess.Popen(
        [sys.executable, "-m", PARSE_SUBPROCESS_MODULE, source_type],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=sanitized_environment(environment),
        close_fds=True,
    )
    stdout: bytes = b""
    try:
        try:
            stdout, _ = process.communicate(input=content, timeout=float(timeout_seconds))
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                process.kill()
            # 再次 communicate 以排空并等待，避免僵尸与未读管道。
            with contextlib.suppress(Exception):
                process.communicate(timeout=5)
            raise ParseSubprocessTimeout("解析子进程超时") from None
    finally:
        _cleanup_process(process)

    if process.returncode != EXIT_OK:
        raise _map_nonzero_exit(process.returncode)
    if len(stdout) > MAX_PARSE_RESULT_BYTES:
        raise ParseSubprocessFailed("解析子进程返回体超过上限")
    try:
        payload = json.loads(stdout)
    except (ValueError, RecursionError):
        raise ParseSubprocessFailed("解析子进程返回体不是合法 JSON") from None
    decoded_text = content.decode("utf-8-sig") if source_type == SOURCE_TYPE_MARKDOWN else ""
    return _deserialize(payload, text=decoded_text)


def _map_nonzero_exit(returncode: int) -> ParseSubprocessError:
    """把子进程具名退出码映射为可区分的静态异常；未知码归为通用解析失败。"""

    if returncode == EXIT_PDF_ENCRYPTED:
        return PdfEncryptedSubprocessError("PDF 已加密")
    if returncode == EXIT_PDF_TOO_MANY_PAGES:
        return PdfTooManyPagesSubprocessError("PDF 页数超过上限")
    if returncode == EXIT_PDF_INVALID:
        return PdfInvalidSubprocessError("PDF 结构损坏")
    if returncode == EXIT_DOCX_UNSUPPORTED:
        return DocxUnsupportedSubprocessError("DOCX 属于不收窄支持的结构")
    if returncode == EXIT_DOCX_INVALID:
        return DocxInvalidSubprocessError("DOCX 无效或损坏")
    return ParseSubprocessFailed("解析子进程以非零状态退出")


if __name__ == "__main__":
    sys.exit(main())
