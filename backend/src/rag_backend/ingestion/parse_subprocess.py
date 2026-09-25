"""在独立子进程中执行 Markdown 解析，给解析阶段一个真实、有界、可终止的硬时限。

设计与边界（与 [文档入库](../../../docs/ingestion.md) 一致）：

- 解析只在独立子进程里运行，父进程用 ``communicate(input=raw_bytes, timeout=...)`` 有界等待；
  超时后真实 ``kill`` 并 ``wait`` 回收，再抛出 :class:`ParseSubprocessTimeout`。这样 Celery
  worker 的 lease 心跳不会无限期掩盖一个挂起的解析。
- 子进程只接收**原始 Markdown 字节**（stdin），只返回结构化的解析块（stdout JSON），不读
  ``.env``、不接收数据库 DSN 或 inference 凭据；父进程用白名单环境变量启动子进程，剥离
  ``DATABASE_URL``/``REDIS_URL``/``INFERENCE_TOKEN``/``LLM_API_KEY`` 等键。
- 返回体有界：``MAX_PARSE_RESULT_BYTES`` 同时约束子进程写出与父进程读取；超限按静态失败处理，
  不把无界数据读进内存。子进程 stderr 丢弃，避免未捕获 traceback 泄露路径并限制内存。
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

from rag_backend.ingestion.parsing import ParsedBlock, ParsedDocument, parse_markdown
from rag_backend.ingestion.validation import MAX_MARKDOWN_BYTES

# 受控入口模块名；父进程用 ``python -m`` 启动它。
PARSE_SUBPROCESS_MODULE: Final = "rag_backend.ingestion.parse_subprocess"

# 解析硬时限与返回体上限；返回体上限覆盖 JSON 转义膨胀（CJK 不转义，控制字符会转义）。
PARSE_TIMEOUT_SECONDS: Final = 60.0
MAX_PARSE_RESULT_BYTES: Final = 4 * MAX_MARKDOWN_BYTES

EXIT_OK: Final = 0
EXIT_INPUT_TOO_LARGE: Final = 2
EXIT_INVALID_INPUT: Final = 3
EXIT_RESULT_TOO_LARGE: Final = 4
EXIT_INTERNAL_ERROR: Final = 1

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


def sanitized_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """按白名单构造子进程环境，剥离业务凭据与 DSN。"""

    source = environment if environment is not None else os.environ
    return {key: source[key] for key in _SAFE_ENV_KEYS if key in source}


def _serialize(document: ParsedDocument) -> dict[str, object]:
    """把解析结果摊平成可 JSON 序列化的结构；刻意不含整篇 ``text``。"""

    return {
        "source_sha256": document.source_sha256,
        "parser_version": document.parser_version,
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
    raw_blocks = payload.get("blocks")
    if not isinstance(source_sha256, str) or not isinstance(parser_version, str):
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
            if level is not None and not isinstance(level, int):
                raise ParseSubprocessFailed("解析子进程返回结构不合法")
            if code_info is not None and not isinstance(code_info, str):
                raise ParseSubprocessFailed("解析子进程返回结构不合法")
            blocks.append(
                ParsedBlock(
                    ordinal=int(raw["ordinal"]),
                    kind=str(raw["kind"]),
                    heading_path=tuple(heading_path),
                    text=str(raw["text"]),
                    start_line=int(raw["start_line"]),
                    end_line=int(raw["end_line"]),
                    list_depth=int(raw["list_depth"]),
                    level=level,
                    code_info=code_info,
                )
            )
        except (KeyError, TypeError, ValueError):
            raise ParseSubprocessFailed("解析子进程返回结构不合法") from None

    return ParsedDocument(
        source_sha256=source_sha256,
        text=text,
        blocks=tuple(blocks),
        parser_version=parser_version,
    )


def _read_bounded_stdin() -> bytes:
    """从 stdin 最多读取 ``MAX_MARKDOWN_BYTES + 1`` 字节。"""

    return sys.stdin.buffer.read(MAX_MARKDOWN_BYTES + 1)


def main() -> int:
    """子进程入口：读取原始字节，解析，输出有界 JSON；不输出任何凭据或路径。"""

    try:
        data = _read_bounded_stdin()
    except OSError:
        return EXIT_INTERNAL_ERROR
    if len(data) > MAX_MARKDOWN_BYTES:
        return EXIT_INPUT_TOO_LARGE
    try:
        document = parse_markdown(data)
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

    if len(content) > MAX_MARKDOWN_BYTES:
        raise ParseSubprocessFailed("解析输入超过单文件字节上限")
    if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise ValueError("解析超时必须是正数")

    process = subprocess.Popen(
        [sys.executable, "-m", PARSE_SUBPROCESS_MODULE],
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
        raise ParseSubprocessFailed("解析子进程以非零状态退出")
    if len(stdout) > MAX_PARSE_RESULT_BYTES:
        raise ParseSubprocessFailed("解析子进程返回体超过上限")
    try:
        payload = json.loads(stdout)
    except (ValueError, RecursionError):
        raise ParseSubprocessFailed("解析子进程返回体不是合法 JSON") from None
    return _deserialize(payload, text=content.decode("utf-8-sig"))


if __name__ == "__main__":
    sys.exit(main())
