"""入库用例的领域错误；HTTP 路由层负责把上传相关错误映射为具名错误码。

这些错误只描述服务端判定结果，绝不携带上传正文、原始文件名、绝对路径或
Idempotency-Key 原值。worker 只读读取原文件的错误也归此类，供后续映射 error_code。
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


class BlobReadError(IngestionError):
    """worker 只读校验式读取原文件时的错误基类。

    只描述服务端判定结果，绝不携带绝对路径、正文、凭据或原始文件名；后续 worker 把
    子类映射为 ``ingest_job.error_code`` 时也不得回显这些内容。
    """


class BlobNotFound(BlobReadError):
    """blob 缺失：数据库已登记但存储中不存在对应文件。"""


class BlobUnsafe(BlobReadError):
    """blob 不是可信普通文件：符号链接、目录、FIFO、设备或其他非常规文件。"""


class BlobTooLarge(BlobReadError):
    """blob 超过单文件字节上限，或读取过程中实际字节超限。"""


class BlobCorrupt(BlobReadError):
    """blob 内容摘要与登记值不一致，文件被篡改或写坏。"""
