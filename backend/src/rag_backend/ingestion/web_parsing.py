"""受限静态 HTML 纯解析：抽取正文块与标题层级，绝不执行脚本或抓取外链。

只在 worker 解析子进程中真正调用时才导入 ``bs4``/``lxml``；顶层只引入标准库与
:mod:`rag_backend.ingestion.parsing` 的纯数据结构，因此 API 镜像（不安装
``beautifulsoup4``/``lxml``）仍可导入本模块取常量。解析不接触数据库、队列、模型或网络，
不写日志、不渲染 HTML。

收窄规则：

- 先整体移除 ``script``/``style``/``template``/``noscript``/``nav``/``aside``/``header``/
  ``footer`` 子树，再在 ``main``/``article``/``body`` 中优先选择 ``main`` 作为正文根。
- 只产出确定块：``p``、``li``、``pre``、``blockquote``；``h1``-``h6`` 只更新
  ``heading_path`` 层级栈，不单独产出块。嵌套块（如 ``li > p``）只取最外层，避免重复。
- ``source_sha256`` 只按输入 bytes 计算；正文文本做空白折叠，``pre`` 保留原始换行。
- 空正文返回空块序列，由上层映射为 ``PIPELINE_CONTENT_EMPTY``，不新增状态。
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Final

from rag_backend.ingestion.parsing import ParsedBlock, ParsedDocument

if TYPE_CHECKING:
    from bs4.element import Tag

# 上传事务与解析实现共用的解析器版本；升级 bs4/lxml 依赖时必须同步评审。
WEB_PARSER_VERSION: Final = "beautifulsoup4-4.15.0+lxml-6.1.3-v1"
SOURCE_TYPE_WEB: Final = "web"
WEB_MEDIA_TYPE: Final = "text/html"

REMOVED_TAGS: Final = (
    "script",
    "style",
    "template",
    "noscript",
    "nav",
    "aside",
    "header",
    "footer",
)
HEADING_TAGS: Final = ("h1", "h2", "h3", "h4", "h5", "h6")
BLOCK_TAGS: Final = ("p", "li", "pre", "blockquote")
SELECTED_TAGS: Final = [*HEADING_TAGS, *BLOCK_TAGS]
LIST_TAGS: Final = ("ul", "ol")

_WHITESPACE = re.compile(r"\s+")


def _normalize_text(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip()


def _block_text(element: Tag) -> str:
    if element.name == "pre":
        # 代码/预格式块保留换行，仅去掉首尾空白。
        return element.get_text().strip("\n").rstrip()
    return _normalize_text(element.get_text(" "))


def _list_depth(element: Tag) -> int:
    return len(element.find_parents(list(LIST_TAGS)))


def _update_heading_stack(
    heading_stack: list[tuple[int, str]], level: int, title: str
) -> None:
    while heading_stack and heading_stack[-1][0] >= level:
        heading_stack.pop()
    if title:
        heading_stack.append((level, title))


def parse_web(content: bytes) -> ParsedDocument:
    """解析原始 HTML 字节，返回带标题路径的正文块序列。"""

    # ``bs4``/``lxml`` 只在真正解析时导入：API 镜像不安装它们，导入本模块取常量不受影响。
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(content, "lxml")
    for name in REMOVED_TAGS:
        for element in soup.find_all(name):
            element.decompose()
    root: Tag = soup.find("main") or soup.find("article") or soup.body or soup

    blocks: list[ParsedBlock] = []
    heading_stack: list[tuple[int, str]] = []
    for element in root.find_all(list(SELECTED_TAGS)):
        name = element.name
        if name in HEADING_TAGS:
            _update_heading_stack(
                heading_stack, int(name[1]), _normalize_text(element.get_text(" "))
            )
            continue
        # 嵌套块只取最外层，避免 ``li > p`` 或 ``blockquote > p`` 重复产出。
        if element.find_parent(list(BLOCK_TAGS)) is not None:
            continue
        text = _block_text(element)
        if not text:
            continue
        kind = {
            "p": "paragraph",
            "li": "list_item",
            "pre": "code_block",
            "blockquote": "blockquote",
        }[name]
        blocks.append(
            ParsedBlock(
                ordinal=len(blocks),
                kind=kind,
                heading_path=tuple(title for _, title in heading_stack),
                text=text,
                list_depth=_list_depth(element) if name == "li" else 0,
            )
        )

    return ParsedDocument(
        source_sha256=hashlib.sha256(content).hexdigest(),
        text="",
        blocks=tuple(blocks),
        parser_version=WEB_PARSER_VERSION,
        source_type=SOURCE_TYPE_WEB,
    )


__all__ = [
    "BLOCK_TAGS",
    "HEADING_TAGS",
    "REMOVED_TAGS",
    "SOURCE_TYPE_WEB",
    "WEB_MEDIA_TYPE",
    "WEB_PARSER_VERSION",
    "parse_web",
]
