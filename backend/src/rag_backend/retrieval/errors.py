"""检索领域错误：与 HTTP 状态码解耦，消息一律静态、不回显查询、向量或组织数据。

路由层把这里的错误映射为统一错误体；错误码不含 KB 是否存在的信息。
"""

from __future__ import annotations


class RetrievalError(Exception):
    """检索错误基类；调用方只据此分类，不依赖底层异常文本。"""


class RetrievalQueryInvalid(RetrievalError):
    """查询本身不可用（空白、超长、含非法 Unicode）；调用方从未发出向量请求。"""


class KnowledgeBaseNotAccessible(RetrievalError):
    """请求的 KB 不在当前会话可访问集合内；统一按不暴露存在性处理。"""


class RetrievalProfileConflict(RetrievalError):
    """请求范围跨越多个不同的 index profile，无法用同一次查询编码检索。"""


class RetrievalAnalyzerMismatch(RetrievalError):
    """active profile 的 ``keyword_analyzer_version`` 与运行期分析器身份不一致。

    换词典/升级 jieba 后必须新建 profile 并重索引；在新索引发布前，检索必须显式失败，
    而不是拿新词项流去查旧 ``chunk.fts``。
    """


class RetrievalScopeUnavailable(RetrievalError):
    """作用域内的 index profile 行不完整（缺 revision/dimension），静态失败。"""


class RetrievalEmbeddingError(RetrievalError):
    """查询编码服务失败；``retryable``/``retry_after_seconds`` 只给调用方分类。"""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


__all__ = [
    "KnowledgeBaseNotAccessible",
    "RetrievalAnalyzerMismatch",
    "RetrievalEmbeddingError",
    "RetrievalError",
    "RetrievalProfileConflict",
    "RetrievalQueryInvalid",
    "RetrievalScopeUnavailable",
]
