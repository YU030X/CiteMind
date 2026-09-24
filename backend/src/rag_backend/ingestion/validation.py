"""上传输入的纯校验规则与去重键派生。

这里只放不依赖数据库与文件系统的规则，便于聚焦单测。校验不信任客户端 MIME：后缀、
实际字节的 UTF-8 解码与控制字节都按客户端字节判断，MIME 只用于说明性记录。
"""

from __future__ import annotations

import hashlib
import uuid

from rag_backend.ingestion.errors import (
    DocumentEmpty,
    DocumentNotText,
    DocumentTooLarge,
    IdempotencyKeyInvalid,
    TitleInvalid,
    UnsupportedDocumentType,
)

# 单文件字节上限：与网关 ``client_max_body_size 20m`` 配合，API 侧再按流式计数与真实
# 文件长度精确判定。20m = 20 * 1024 * 1024 略大于该值，为 multipart 边界留出余量。
MAX_MARKDOWN_BYTES = 20_000_000
MAX_TITLE_LENGTH = 500
MAX_IDEMPOTENCY_KEY_LENGTH = 255

MARKDOWN_EXTENSIONS = (".md", ".markdown")
# 服务端判定的规范化 MIME；不采用客户端声明的值，客户端 MIME 只作为兼容输入被忽略。
MARKDOWN_MEDIA_TYPE = "text/markdown"
# 声明的解析器版本。本切片不解析正文，仅登记契约版本；真正解析实现后应换用对应版本。
MARKDOWN_PARSER_VERSION = "markdown-v1"

# 允许出现在文本中的 C0 控制字符；其余 C0 与 DEL 视为伪装成文本的二进制。
# 这些值在 UTF-8 中只可能以单字节 ASCII 出现，因此可直接在字节层扫描。
_ALLOWED_CONTROL_BYTES = (0x09, 0x0A, 0x0D)
_FORBIDDEN_CONTROL_BYTES = bytes(
    byte
    for byte in list(range(0x20)) + [0x7F]
    if byte not in _ALLOWED_CONTROL_BYTES
)


def normalize_title(raw: str) -> str:
    """去掉首尾空白并校验长度；空标题与超长标题都拒绝。"""

    title = raw.strip()
    if not title:
        raise TitleInvalid("文档标题不能为空")
    if len(title) > MAX_TITLE_LENGTH:
        raise TitleInvalid(f"文档标题最长 {MAX_TITLE_LENGTH} 个字符")
    return title


def normalize_idempotency_key(raw: str) -> str:
    """规范化 Idempotency-Key；空值与超长值都拒绝。"""

    key = raw.strip()
    if not key:
        raise IdempotencyKeyInvalid("Idempotency-Key 不能为空")
    if len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise IdempotencyKeyInvalid(
            f"Idempotency-Key 最长 {MAX_IDEMPOTENCY_KEY_LENGTH} 个字符"
        )
    return key


def build_dedupe_key(
    organization_id: uuid.UUID, kb_id: uuid.UUID, normalized_key: str
) -> str:
    """派生组织+KB 作用域的去重键，不保存也不回显 Idempotency-Key 原值。

    同一 key 在不同 KB 或不同组织下得到不同摘要，避免跨 KB 冲突与存在性探测；
    由于原文不落库，唯一约束冲突也不会泄露原始 key。
    """

    scope = f"{organization_id}:{kb_id}:{normalized_key}"
    return hashlib.sha256(scope.encode("utf-8")).hexdigest()


def validate_markdown_filename(filename: str | None) -> str:
    """只接受 ``.md``/``.markdown`` 后缀，返回去路径后的文件名（仅用于说明）。"""

    if filename is None or not filename.strip():
        raise UnsupportedDocumentType("上传缺少文件名")
    # 兼容 Windows 客户端提交的反斜杠路径；只取最后一段，绝不用于拼装存储路径。
    base = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not base:
        raise UnsupportedDocumentType("上传缺少文件名")
    if not base.lower().endswith(MARKDOWN_EXTENSIONS):
        raise UnsupportedDocumentType("本切片只接受 .md 与 .markdown 文件")
    return base


def decode_markdown_content(data: bytes) -> str:
    """校验字节是有效 UTF-8 文本；空内容与二进制内容都拒绝。"""

    if not data:
        raise DocumentEmpty("上传内容为空")
    if len(data) > MAX_MARKDOWN_BYTES:
        raise DocumentTooLarge("上传内容超过单文件字节上限")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DocumentNotText("上传内容不是有效的 UTF-8 文本") from error
    if len(data.translate(None, _FORBIDDEN_CONTROL_BYTES)) != len(data):
        raise DocumentNotText("上传内容包含二进制控制字符，不是文本")
    return text
