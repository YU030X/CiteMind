"""上传用例的领域错误；路由层负责映射为具名 HTTP 错误码。

这些错误只描述服务端判定结果，绝不携带上传正文、原始文件名或 Idempotency-Key 原值。
"""

from __future__ import annotations


class IngestionError(Exception):
    """上传用例错误基类。"""


class UnsupportedDocumentType(IngestionError):
    """后缀不是本切片支持的 Markdown 类型。"""


class DocumentEmpty(IngestionError):
    """上传内容为空，没有可登记的字节。"""


class DocumentNotText(IngestionError):
    """内容不是有效 UTF-8 文本，或含有伪装成文本的二进制控制字节。"""


class DocumentTooLarge(IngestionError):
    """内容超过单文件字节上限。"""


class TitleInvalid(IngestionError):
    """标题为空或超过长度上限。"""


class IdempotencyKeyInvalid(IngestionError):
    """Idempotency-Key 规范化后为空或超过长度上限。"""


class IdempotencyConflict(IngestionError):
    """同一 Idempotency-Key 已用于不同的内容或标题。"""
