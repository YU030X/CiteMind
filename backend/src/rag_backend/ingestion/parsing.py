"""Markdown 纯解析：把原文件字节解析为带来源位置的纯数据块。

本模块只做解析，不接触数据库、文件系统、队列或模型：输入是上传校验已接受的 UTF-8
字节，输出是不可变的数据结构。它不写文件、不写日志、不渲染 HTML，也不联网，因此可以
在单元测试里独立验证。

来源位置规则（与 ``docs/ingestion.md`` 的来源定位表一致）：

- markdown-it-py 的 ``token.map`` 是 0-based 且结束边界不含；这里统一转成 1-based
  闭区间 ``[start_line, end_line]``。
- 每个块保留 ``ordinal``（文档顺序，0-based）、所在 ``heading_path``、``list_depth``
  和规范化后的正文文本。正文文本会去掉行内 Markdown 标记，但绝不执行或渲染 HTML。
- 原始字节的 SHA-256 只按输入 bytes 计算（不按解码后的字符串），因此 LF/CRLF 与
  Unicode 变化都会反映在来源哈希上。解码使用 ``utf-8-sig``，仅忽略文件开头的 BOM，
  不会把 BOM 当作正文字符，也不改变行号。
- 引用块/列表内部的标题只影响该容器内的正文；退出容器时恢复标题栈，不污染外层。

原始 HTML（``html_block``/``html_inline``）只被识别、从不执行；本切片把它们排除在正文
之外，避免把可执行标记写入 chunk 文本。图片只保留 alt 文本，绝不抓取 URL。

解析器版本是上传事务与解析实现共用的单一真源：``MARKDOWN_PARSER_VERSION`` 既由上传事务
写入 ``document_version.parser_version``，也是解析结果声明的版本。既有旧行的占位值
``markdown-v1`` 不做就地迁移或升级，解析器不静默改写既有任务的解析器版本。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from markdown_it import MarkdownIt
from markdown_it.token import Token

# 上传事务与解析实现共用的解析器版本；上传写入 ``document_version.parser_version`` 的
# 即是本值。版本名包含所锁定的 markdown-it-py 精确版本，升级依赖时必须同步评审。
MARKDOWN_PARSER_VERSION = "markdown-it-py-4.2.0-v1"

# 容器块：进入时快照标题栈，退出时恢复，使引用块/列表内部的标题不渗到外层。
_CONTAINER_OPEN = frozenset(
    {"blockquote_open", "bullet_list_open", "ordered_list_open", "list_item_open"}
)
_CONTAINER_CLOSE = frozenset(
    {"blockquote_close", "bullet_list_close", "ordered_list_close", "list_item_close"}
)
_LIST_OPEN = frozenset({"bullet_list_open", "ordered_list_open"})
_LIST_CLOSE = frozenset({"bullet_list_close", "ordered_list_close"})


@dataclass(frozen=True)
class ParsedBlock:
    """一个带来源位置的纯数据块；所有线路字段都是 1-based 闭区间。"""

    ordinal: int
    kind: str
    heading_path: tuple[str, ...]
    text: str
    start_line: int
    end_line: int
    list_depth: int = 0
    level: int | None = None
    code_info: str | None = None


@dataclass(frozen=True)
class ParsedDocument:
    """一次解析结果；``source_sha256`` 只由输入 bytes 决定。"""

    source_sha256: str
    text: str
    blocks: tuple[ParsedBlock, ...]
    parser_version: str = MARKDOWN_PARSER_VERSION


def parse_markdown(content: bytes) -> ParsedDocument:
    """解析 UTF-8 Markdown 字节，返回带来源位置的块序列。

    调用方负责保证字节是有效 UTF-8（上传校验已做）；非法字节直接抛出
    ``UnicodeDecodeError``，不在这里静默替换。
    """

    # ``utf-8-sig`` 只在文件开头忽略 BOM；sha256 仍按原始 bytes 计算，CRLF 与行号不变。
    text = content.decode("utf-8-sig")
    source_sha256 = hashlib.sha256(content).hexdigest()
    parser = MarkdownIt("commonmark")
    tokens = parser.parse(text)

    blocks: list[ParsedBlock] = []
    heading_stack: list[tuple[int, str]] = []
    saved_heading_stacks: list[list[tuple[int, str]]] = []
    list_depth = 0

    for index, token in enumerate(tokens):
        if token.type in _CONTAINER_OPEN:
            if token.type in _LIST_OPEN:
                list_depth += 1
            saved_heading_stacks.append(list(heading_stack))
            continue
        if token.type in _CONTAINER_CLOSE:
            if token.type in _LIST_CLOSE:
                list_depth = max(0, list_depth - 1)
            if saved_heading_stacks:
                heading_stack = saved_heading_stacks.pop()
            continue
        if token.type == "heading_open":
            level = _heading_level(token)
            _update_heading_stack(heading_stack, level, _heading_title(tokens, index))
            continue
        if token.type in ("fence", "code_block"):
            code_block = _code_block(token, len(blocks), heading_stack, list_depth)
            if code_block is not None:
                blocks.append(code_block)
            continue
        if token.type != "inline":
            continue
        # heading 的 inline 文本已在上面的 heading_open 分支处理，这里只产出正文块。
        if index > 0 and tokens[index - 1].type == "heading_open":
            continue
        line_range = _line_range(token)
        if line_range is None:
            continue
        start_line, end_line = line_range
        body = _inline_text(token.children).strip()
        if not body:
            continue
        kind = "list_item" if list_depth > 0 else "paragraph"
        blocks.append(
            ParsedBlock(
                ordinal=len(blocks),
                kind=kind,
                heading_path=tuple(title for _, title in heading_stack),
                text=body,
                start_line=start_line,
                end_line=end_line,
                list_depth=list_depth,
            )
        )

    return ParsedDocument(
        source_sha256=source_sha256,
        text=text,
        blocks=tuple(blocks),
        parser_version=MARKDOWN_PARSER_VERSION,
    )


def _heading_level(token: Token) -> int:
    tag = token.tag
    if len(tag) == 2 and tag[0] == "h" and tag[1].isdigit():
        return int(tag[1])
    # CommonMark 只有 h1..h6；其它标签不应出现，保守按 h1 处理而不静默丢弃标题。
    return 1


def _heading_title(tokens: list[Token], index: int) -> str:
    inline = tokens[index + 1] if index + 1 < len(tokens) else None
    if inline is None or inline.type != "inline":
        return ""
    return _inline_text(inline.children).strip()


def _update_heading_stack(
    heading_stack: list[tuple[int, str]], level: int, title: str
) -> None:
    # 即使标题为空也要先弹出更深层级，否则后续正文会错误地继承旧路径。
    while heading_stack and heading_stack[-1][0] >= level:
        heading_stack.pop()
    if title:
        heading_stack.append((level, title))


def _code_block(
    token: Token,
    ordinal: int,
    heading_stack: list[tuple[int, str]],
    list_depth: int,
) -> ParsedBlock | None:
    line_range = _line_range(token)
    if line_range is None:
        return None
    code = token.content.rstrip("\n")
    if not code.strip():
        return None
    start_line, end_line = line_range
    info = token.info.strip()
    return ParsedBlock(
        ordinal=ordinal,
        kind="code_block",
        heading_path=tuple(title for _, title in heading_stack),
        text=code,
        start_line=start_line,
        end_line=end_line,
        list_depth=list_depth,
        code_info=info or None,
    )


def _line_range(token: Token) -> tuple[int, int] | None:
    """把 markdown-it 的 0-based 半开 ``[start, end)`` 转成 1-based 闭区间。"""

    token_map = token.map
    if token_map is None or len(token_map) != 2:
        return None
    start, end = token_map
    if end <= start:
        return None
    return start + 1, end


def _inline_text(children: list[Token] | None) -> str:
    """把行内 token 还原成纯文本；跳过 HTML，图片只取 alt 文本。"""

    if not children:
        return ""
    parts: list[str] = []
    for child in children:
        token_type = child.type
        if token_type in ("text", "code_inline"):
            parts.append(child.content)
        elif token_type == "softbreak":
            parts.append(" ")
        elif token_type == "hardbreak":
            parts.append("\n")
        elif token_type == "image":
            parts.append(_inline_text(child.children) or child.content)
        # html_inline 与其它标记 token 一律跳过：既不执行也不写入正文。
    return "".join(parts)
