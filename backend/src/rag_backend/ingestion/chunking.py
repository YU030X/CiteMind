"""Markdown 与文本 PDF 切分：按标题/页边界分段、按 token 预算打包，并保留可回到原文的来源位置。

本模块只做纯计算，不接触数据库、模型或日志。它接受一个注入的 :class:`TokenCounter`
接口来测量**完整模型输入**（标题前缀 + 正文）的 token 数，因此测试可以用显式假计数器
验证预算行为，不需要真实 tokenizer，也不声称已验收真实模型的 512 token 上限。

预算语义：

- ``target_tokens`` 是打包目标（约 360）；``overlap_tokens`` 是相邻 chunk 的重叠（约 60）；
  ``max_tokens`` 是硬上限（512）。token_count 统计的是标题与正文拼接后的完整模型输入。
- 单个块超过 ``max_tokens`` 时按字符确定性拆分并保留重叠；即使预算连一个字符都放不下，
  也显式抛出 :class:`ChunkBudgetExceeded`。绝不静默截断，也不进入无限循环。

来源位置：

- ``source_locator`` 是稳定、可版本化的 JSON-compatible 字典，只包含来源 hash、定位键、
  block ordinal 以及每个 piece 在其 *块规范化正文* 中的字符区间。它不包含文件名。
- Markdown 输出 ``locator_version=1``：1-based 块级 ``start_line``/``end_line``、
  ``block_ordinals`` 与 ``segments`` 的块内字符区间。因为块正文已去掉行内标记，字符区间是
  相对块正文的偏移，不是对原始文件的偏移；结合 ``source_sha256`` + ordinal + 行范围可重放。
- PDF 输出 ``locator_version=2``：不伪造行号，改用 ``pages`` 与每个 segment 的 ``page``；
  页边界强制 flush 且不携带上一页重叠，因此 chunk 绝不跨页。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from rag_backend.ingestion.parsing import ParsedBlock, ParsedDocument

CHUNKER_VERSION = "heading-pack-v1"
DEFAULT_TARGET_TOKENS = 360
DEFAULT_OVERLAP_TOKENS = 60
DEFAULT_MAX_TOKENS = 512
LOCATOR_VERSION = 1
# PDF 页定位版本；与 Markdown 的块级行范围 locator 区分，绝不混用键集合。
PDF_LOCATOR_VERSION = 2
SOURCE_TYPE_PDF = "pdf"


class ChunkingError(Exception):
    """切分错误基类。"""


class NoChunkableContent(ChunkingError):
    """文档没有可切分的正文；例如只有标题、只有 HTML 或空白。"""


class ChunkBudgetExceeded(ChunkingError):
    """在给定预算下无法容纳任何正文，无法在不截断的前提下继续切分。"""


class TokenCounter(Protocol):
    """可注入的 token 计数器。

    实现必须对传入的完整字符串计数（含特殊 token），且不截断。切分器传入的一定是
    ``build_model_input`` 拼好的完整模型输入。
    """

    def count_tokens(self, text: str) -> int: ...


@dataclass(frozen=True)
class ChunkBudget:
    """切分预算；使用前校验关系，非法组合快速失败。"""

    target_tokens: int = DEFAULT_TARGET_TOKENS
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS
    max_tokens: int = DEFAULT_MAX_TOKENS

    def __post_init__(self) -> None:
        if self.target_tokens <= 0:
            raise ValueError("target_tokens 必须为正数")
        if self.overlap_tokens < 0:
            raise ValueError("overlap_tokens 不能为负数")
        if self.max_tokens <= 0:
            raise ValueError("max_tokens 必须为正数")
        if self.target_tokens > self.max_tokens:
            raise ValueError("target_tokens 不能大于 max_tokens")
        if self.overlap_tokens >= self.target_tokens:
            raise ValueError("overlap_tokens 必须小于 target_tokens")


@dataclass(frozen=True)
class Chunk:
    """一个可检索单元；来源位置在切分时固定，模型不能补猜。"""

    chunk_index: int
    text: str
    heading_path: tuple[str, ...]
    token_count: int
    text_hash: str
    model_input_hash: str
    source_locator: dict[str, object]
    parser_version: str
    chunker_version: str


@dataclass(frozen=True)
class _Piece:
    """chunk 的最小组成；``char_start``/``char_end`` 是块正文内的偏移。"""

    ordinal: int
    char_start: int
    char_end: int
    text: str
    heading_path: tuple[str, ...]
    page: int | None = None


def build_model_input(heading_path: Sequence[str], body: str) -> str:
    """拼接编码模型输入：标题路径 + 空行 + 正文；预算按整个结果计算。"""

    if not heading_path:
        return body
    return "\n".join(heading_path) + "\n\n" + body


def chunk_markdown(
    document: ParsedDocument,
    counter: TokenCounter,
    budget: ChunkBudget | None = None,
) -> list[Chunk]:
    """把解析结果切成带来源位置的 chunk；空正文显式报错而不是产出空列表。"""

    effective_budget = budget if budget is not None else ChunkBudget()
    content = tuple(block for block in document.blocks if block.text.strip())
    if not content:
        raise NoChunkableContent("文档没有可切分的正文块；只有标题或 HTML 时不会生成 chunk")

    blocks_by_ordinal = {block.ordinal: block for block in document.blocks}
    units = _split_oversized_blocks(content, counter, effective_budget)

    chunks: list[Chunk] = []
    current: list[_Piece] = []
    current_heading: tuple[str, ...] | None = None
    current_page: int | None = None

    for unit in units:
        # 标题变化或页变化都强制新 chunk；PDF 的页边界绝不跨页合并，也不携带上一页重叠。
        if current and (
            unit.heading_path != current_heading or unit.page != current_page
        ):
            _emit(chunks, current, document, blocks_by_ordinal, counter)
            current = []
        if current and unit.ordinal == current[-1].ordinal:
            # 同一块的续段不同处一个 chunk：_join 会在不同 ordinal 之间插分隔符，
            # 把同一块的相邻片段拼在一起会伪造原文不存在的空行。
            _emit(chunks, current, document, blocks_by_ordinal, counter)
            current = []
        if not current:
            current = [unit]
            current_heading = unit.heading_path
            current_page = unit.page
            continue
        assert current_heading is not None
        trial = [*current, unit]
        if _model_token_count(current_heading, trial, counter) <= effective_budget.target_tokens:
            current = trial
            continue
        previous = current[-1]
        _emit(chunks, current, document, blocks_by_ordinal, counter)
        if unit.page != current_page:
            # 页边界不携带重叠，避免下一 chunk 混入上一页文本。
            current = [unit]
            current_page = unit.page
            continue
        # 顶部已保证 unit 与 current[-1] 属于不同块，这里可以直接取尾段重叠。
        overlap = _tail_piece(previous, counter, effective_budget)
        current = ([overlap] if overlap is not None else []) + [unit]
        if _model_token_count(current_heading, current, counter) > effective_budget.max_tokens:
            current = [unit]

    if current:
        _emit(chunks, current, document, blocks_by_ordinal, counter)
    return chunks


def _model_token_count(
    heading_path: tuple[str, ...], pieces: Sequence[_Piece], counter: TokenCounter
) -> int:
    return counter.count_tokens(build_model_input(heading_path, _join(pieces)))


def _join(pieces: Sequence[_Piece]) -> str:
    """按来源拼接：跨块用空行，同一块的连续片段直接拼接，不伪造原文没有的换行。"""

    parts: list[str] = []
    previous: _Piece | None = None
    for piece in pieces:
        if previous is None:
            parts.append(piece.text)
        elif piece.ordinal != previous.ordinal:
            parts.append("\n\n" + piece.text)
        elif piece.char_start == previous.char_end:
            parts.append(piece.text)
        else:
            raise ChunkingError(
                "同一块的片段在 chunk 内重叠或出现间隙，无法无损拼接；切分器应保证不发生"
            )
        previous = piece
    return "".join(parts)


def _split_oversized_blocks(
    blocks: Sequence[ParsedBlock], counter: TokenCounter, budget: ChunkBudget
) -> list[_Piece]:
    units: list[_Piece] = []
    for block in blocks:
        whole = _Piece(
            ordinal=block.ordinal,
            char_start=0,
            char_end=len(block.text),
            text=block.text,
            heading_path=block.heading_path,
            page=block.page,
        )
        if (
            counter.count_tokens(build_model_input(block.heading_path, block.text))
            <= budget.max_tokens
        ):
            units.append(whole)
            continue
        units.extend(_split_block(block, counter, budget))
    return units


def _split_block(
    block: ParsedBlock, counter: TokenCounter, budget: ChunkBudget
) -> list[_Piece]:
    text = block.text
    length = len(text)
    pieces: list[_Piece] = []
    start = 0
    while start < length:
        end = _fit_end(text, start, block.heading_path, counter, budget.max_tokens)
        if end is None:
            raise ChunkBudgetExceeded(
                f"块 ordinal={block.ordinal} 在 {budget.max_tokens} token 预算内无法容纳任何正文；"
                "标题前缀或单个字符已超限"
            )
        pieces.append(
            _Piece(
                ordinal=block.ordinal,
                char_start=start,
                char_end=end,
                text=text[start:end],
                heading_path=block.heading_path,
                page=block.page,
            )
        )
        if end >= length:
            break
        overlap = _fit_suffix(text, start, end, block.heading_path, counter, budget)
        next_start = end - overlap
        if next_start <= start:
            next_start = start + 1
        start = next_start
    return pieces


def _fit_end(
    text: str,
    start: int,
    heading_path: tuple[str, ...],
    counter: TokenCounter,
    max_tokens: int,
) -> int | None:
    """返回 ``text[start:end]`` 能放进 ``max_tokens`` 的最大 ``end``；放不下返回 None。"""

    length = len(text)

    def fits(end: int) -> bool:
        return (
            counter.count_tokens(build_model_input(heading_path, text[start:end])) <= max_tokens
        )

    # 先单独验证最小候选 start+1：二分窗口一旦塌缩到 start 就会漏掉它，
    # 把“单个字符能放下”误判为无法切分。返回的 end 一定已通过 fits(end)。
    if not fits(start + 1):
        return None
    low, high = start + 1, length
    best = start + 1
    while low <= high:
        mid = (low + high) // 2
        if fits(mid):
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    # 非严格单调 token 计数下，二分只是取上界的启发式；向两侧校正以保证结果有效。
    while best < length and fits(best + 1):
        best += 1
    while best > start + 1 and not fits(best):
        best -= 1
    return best


def _fit_suffix(
    text: str,
    start: int,
    end: int,
    heading_path: tuple[str, ...],
    counter: TokenCounter,
    budget: ChunkBudget,
) -> int:
    """返回可作重叠的最大后缀字符数，且该重叠单独加标题后不超过硬上限。"""

    if budget.overlap_tokens <= 0:
        return 0
    segment = text[start:end]
    length = len(segment)

    def suffix(count: int) -> str:
        return segment[length - count :]

    def within_overlap(count: int) -> bool:
        return counter.count_tokens(suffix(count)) <= budget.overlap_tokens

    low, high = 0, length
    best = 0
    while low <= high:
        mid = (low + high) // 2
        if within_overlap(mid):
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    while best < length and within_overlap(best + 1):
        best += 1
    while best > 0 and not within_overlap(best):
        best -= 1
    # 重叠不能把下一段（含至少一个新字符）挤出硬上限。
    while best > 0 and counter.count_tokens(
        build_model_input(heading_path, suffix(best))
    ) > budget.max_tokens:
        best //= 2
    return min(best, max(0, length - 1))


def _tail_piece(
    piece: _Piece, counter: TokenCounter, budget: ChunkBudget
) -> _Piece | None:
    if budget.overlap_tokens <= 0:
        return None
    text = piece.text
    length = len(text)

    def suffix(count: int) -> str:
        return text[length - count :]

    def within_overlap(count: int) -> bool:
        return counter.count_tokens(suffix(count)) <= budget.overlap_tokens

    low, high = 0, length
    best = 0
    while low <= high:
        mid = (low + high) // 2
        if within_overlap(mid):
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    while best < length and within_overlap(best + 1):
        best += 1
    while best > 0 and not within_overlap(best):
        best -= 1
    if best <= 0 or best >= length:
        # best >= length 表示整段都在 overlap 预算内（短段落）：不强加重叠，避免整段重复。
        return None
    return _Piece(
        ordinal=piece.ordinal,
        char_start=piece.char_end - best,
        char_end=piece.char_end,
        text=suffix(best),
        heading_path=piece.heading_path,
        page=piece.page,
    )


def _emit(
    chunks: list[Chunk],
    pieces: Sequence[_Piece],
    document: ParsedDocument,
    blocks_by_ordinal: dict[int, ParsedBlock],
    counter: TokenCounter,
) -> None:
    if not pieces:
        return
    heading_path = pieces[0].heading_path
    text = _join(pieces)
    model_input = build_model_input(heading_path, text)
    chunks.append(
        Chunk(
            chunk_index=len(chunks),
            text=text,
            heading_path=heading_path,
            token_count=counter.count_tokens(model_input),
            text_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            model_input_hash=hashlib.sha256(model_input.encode("utf-8")).hexdigest(),
            source_locator=_build_locator(document, pieces, blocks_by_ordinal),
            parser_version=document.parser_version,
            chunker_version=CHUNKER_VERSION,
        )
    )


def _build_locator(
    document: ParsedDocument,
    pieces: Sequence[_Piece],
    blocks_by_ordinal: dict[int, ParsedBlock],
) -> dict[str, object]:
    ordinals: list[int] = []
    for piece in pieces:
        if not ordinals or ordinals[-1] != piece.ordinal:
            ordinals.append(piece.ordinal)
    spanned = [blocks_by_ordinal[ordinal] for ordinal in ordinals]
    segments: list[dict[str, object]] = [
        {
            "block_ordinal": piece.ordinal,
            "block_char_start": piece.char_start,
            "block_char_end": piece.char_end,
        }
        for piece in pieces
    ]
    if document.source_type == SOURCE_TYPE_PDF:
        pages: list[int] = []
        for piece in pieces:
            if piece.page is not None and piece.page not in pages:
                pages.append(piece.page)
        for segment, piece in zip(segments, pieces):
            segment["page"] = piece.page
        return {
            "locator_version": PDF_LOCATOR_VERSION,
            "source_type": SOURCE_TYPE_PDF,
            "parser_version": document.parser_version,
            "source_sha256": document.source_sha256,
            "pages": pages,
            "block_ordinals": ordinals,
            "segments": segments,
        }
    start_lines = [block.start_line for block in spanned if block.start_line is not None]
    end_lines = [block.end_line for block in spanned if block.end_line is not None]
    return {
        "locator_version": LOCATOR_VERSION,
        "source_type": "markdown",
        "parser_version": document.parser_version,
        "source_sha256": document.source_sha256,
        "start_line": min(start_lines),
        "end_line": max(end_lines),
        "block_ordinals": ordinals,
        "segments": segments,
    }
