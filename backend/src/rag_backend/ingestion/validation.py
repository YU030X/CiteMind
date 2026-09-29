"""上传输入的纯校验规则与去重键派生。

这里只放不依赖数据库与文件系统的规则，便于聚焦单测。校验不信任客户端 MIME：后缀、
实际字节的 UTF-8 解码与控制字节都按客户端字节判断，MIME 只用于说明性记录。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass

from rag_backend.ingestion.docx_parsing import (
    DocxParsingError,
    DocxTooLargeError,
    DocxUnsupportedError,
    inspect_docx_zip,
)
from rag_backend.ingestion.errors import (
    DocumentDocxUnsupported,
    DocumentEmpty,
    DocumentNotDocx,
    DocumentNotPdf,
    DocumentNotText,
    DocumentTooLarge,
    IdempotencyKeyInvalid,
    TitleInvalid,
    UnsupportedDocumentType,
)
from rag_backend.ingestion.pdf_parsing import PDF_MAGIC

# 单文件字节上限：与网关 ``client_max_body_size 20m`` 配合，API 侧再按流式计数与真实
# 文件长度精确判定。20m = 20 * 1024 * 1024 略大于该值，为 multipart 边界留出余量。
# ``MAX_MARKDOWN_BYTES`` 是历史名称，值与通用上限相同，继续供 Markdown 路径与既有文档引用。
MAX_DOCUMENT_BYTES = 20_000_000
MAX_MARKDOWN_BYTES = MAX_DOCUMENT_BYTES
MAX_TITLE_LENGTH = 500
MAX_IDEMPOTENCY_KEY_LENGTH = 255

SOURCE_TYPE_MARKDOWN = "markdown"
SOURCE_TYPE_PDF = "pdf"
SOURCE_TYPE_DOCX = "docx"
SOURCE_TYPE_WEB = "web"
MARKDOWN_EXTENSIONS = (".md", ".markdown")
PDF_EXTENSIONS = (".pdf",)
DOCX_EXTENSIONS = (".docx",)
# 服务端判定的规范化 MIME；不采用客户端声明的值，客户端 MIME 只作为兼容输入被忽略。
MARKDOWN_MEDIA_TYPE = "text/markdown"
PDF_MEDIA_TYPE = "application/pdf"
DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
# 网页原样 HTML 的受控 MIME；只在服务端抓取校验通过后才写入 ``document_version.mime``。
WEB_MEDIA_TYPE = "text/html"

# 允许出现在文本中的 C0 控制字符；其余 C0 与 DEL 视为伪装成文本的二进制。
# 这些值在 UTF-8 中只可能以单字节 ASCII 出现，因此可直接在字节层扫描。
_ALLOWED_CONTROL_BYTES = (0x09, 0x0A, 0x0D)
_FORBIDDEN_CONTROL_BYTES = bytes(
    byte
    for byte in list(range(0x20)) + [0x7F]
    if byte not in _ALLOWED_CONTROL_BYTES
)

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


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


# 文档新版本去重键的命名空间前缀。它与首次上传的裸 SHA-256 十六进制键互不匹配，
# 因此两类操作永远不会命中彼此的去重行。
VERSION_DEDUPE_NAMESPACE = "ver1"


def build_version_dedupe_key_prefix(
    organization_id: uuid.UUID,
    kb_id: uuid.UUID,
    document_id: uuid.UUID,
    normalized_key: str,
) -> str:
    """派生「文档新版本」去重键的稳定前缀（组织+KB+文档+Idempotency-Key 的摘要）。

    与首次上传不同，新版本去重必须在文档作用域内，否则同一 KB 内为文档 A 使用的
    Idempotency-Key 会命中文档 B 的 job。前缀是普通 text，不需要迁移；调用方用
    ``prefix + ':'`` 做 LIKE 前缀匹配，再结合 ``expected_active_version_id`` 判定复用或冲突。
    """

    scope = f"{organization_id}:{kb_id}:{document_id}:{normalized_key}"
    digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()
    return f"{VERSION_DEDUPE_NAMESPACE}:{document_id}:{digest}"


def build_version_dedupe_key(
    prefix: str, expected_active_version_id: uuid.UUID
) -> str:
    """拼接完整的文档新版本去重键：前缀 + 本次请求声明的 expected active version。"""

    return f"{prefix}:{expected_active_version_id}"


def parse_version_dedupe_key(dedupe_key: str) -> tuple[uuid.UUID, uuid.UUID] | None:
    """解析文档新版本去重键，返回 ``(document_id, expected_active_version_id)``。

    首次上传的裸 SHA-256 十六进制键、命名空间不符或字段非法的值都返回 ``None``；
    调用方据此区分「首次上传」与「文档新版本」，绝不对无法解析的键推断 expected。
    """

    parts = dedupe_key.split(":")
    if len(parts) != 4 or parts[0] != VERSION_DEDUPE_NAMESPACE:
        return None
    try:
        document_id = uuid.UUID(parts[1])
        expected_active_version_id = uuid.UUID(parts[3])
    except ValueError:
        return None
    if not _SHA256_PATTERN.fullmatch(parts[2]):
        return None
    return document_id, expected_active_version_id


def validate_markdown_filename(filename: str | None) -> str:
    """只接受 ``.md``/``.markdown`` 后缀，返回去路径后的文件名（仅用于说明）。

    保留为 Markdown 专用校验入口：PDF 上传改由 :func:`resolve_upload_format` 统一分派后，
    本函数不再被生产路径调用，但在单测中固定 Markdown 兼容边界，因此不随本切片删除。
    """

    if filename is None or not filename.strip():
        raise UnsupportedDocumentType("上传缺少文件名")
    # 兼容 Windows 客户端提交的反斜杠路径；只取最后一段，绝不用于拼装存储路径。
    base = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not base:
        raise UnsupportedDocumentType("上传缺少文件名")
    if not base.lower().endswith(MARKDOWN_EXTENSIONS):
        raise UnsupportedDocumentType("本切片只接受 .md 与 .markdown 文件")
    return base


@dataclass(frozen=True)
class UploadFormat:
    """一次上传按后缀判定的来源；API 只据此分派写路径。"""

    source_type: str


def _base_filename(filename: str | None) -> str:
    if filename is None or not filename.strip():
        raise UnsupportedDocumentType("上传缺少文件名")
    # 兼容 Windows 客户端提交的反斜杠路径；只取最后一段，绝不用于拼装存储路径。
    base = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not base:
        raise UnsupportedDocumentType("上传缺少文件名")
    return base


def resolve_upload_format(filename: str | None) -> UploadFormat:
    """按后缀判定受支持的来源。

    只接受 ``.md``/``.markdown``/``.pdf``/``.docx``；后缀大小写不敏感，存储路径不使用文件名。
    规范化 MIME 与真实解析器版本由各自写路径按来源选定，不在此重复登记。
    """

    base = _base_filename(filename)
    lowered = base.lower()
    if lowered.endswith(MARKDOWN_EXTENSIONS):
        return UploadFormat(source_type=SOURCE_TYPE_MARKDOWN)
    if lowered.endswith(PDF_EXTENSIONS):
        return UploadFormat(source_type=SOURCE_TYPE_PDF)
    if lowered.endswith(DOCX_EXTENSIONS):
        return UploadFormat(source_type=SOURCE_TYPE_DOCX)
    raise UnsupportedDocumentType("本切片只接受 .md、.markdown、.pdf 与 .docx 文件")


def decode_markdown_content(data: bytes) -> str:
    """校验字节是有效 UTF-8 文本；空内容与二进制内容都拒绝。"""

    if not data:
        raise DocumentEmpty("上传内容为空")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise DocumentTooLarge("上传内容超过单文件字节上限")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DocumentNotText("上传内容不是有效的 UTF-8 文本") from error
    if len(data.translate(None, _FORBIDDEN_CONTROL_BYTES)) != len(data):
        raise DocumentNotText("上传内容包含二进制控制字符，不是文本")
    return text


def validate_pdf_content(data: bytes) -> None:
    """校验 PDF 字节：非空、未超单文件上限、具备 ``%PDF-`` 魔数头。

    这里只做受理期可独立判断的二进制形状校验；页数、加密与结构损坏由 worker 解析子进程在
    写 blob 之后判定（API 镜像不安装 pypdf/pdfplumber）。
    """

    if not data:
        raise DocumentEmpty("上传内容为空")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise DocumentTooLarge("上传内容超过单文件字节上限")
    if not data.startswith(PDF_MAGIC):
        raise DocumentNotPdf("上传内容不是可识别的 PDF")


def validate_docx_content(data: bytes) -> None:
    """校验 DOCX 字节：非空、未超单文件上限、标准库 ZIP 元数据符合收窄子集。

    这里只做受理期可独立判断的 ZIP 元数据校验（非空/大小/PK 魔数/条目数/声明解压量/压缩比/
    加密/路径/重复名/必需部件/宏部件），不导入 ``python-docx``；嵌套表、实体声明与 CRC/实际
    解压总量由 worker 解析子进程在写 blob 之后静态判定。
    """

    if not data:
        raise DocumentEmpty("上传内容为空")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise DocumentTooLarge("上传内容超过单文件字节上限")
    try:
        inspect_docx_zip(data)
    except DocxTooLargeError as error:
        raise DocumentTooLarge("DOCX 内部条目超过解压上限") from error
    except DocxUnsupportedError as error:
        raise DocumentDocxUnsupported("DOCX 含宏部件，本切片不支持") from error
    except DocxParsingError as error:
        raise DocumentNotDocx("上传内容不是有效或可识别的 DOCX") from error
