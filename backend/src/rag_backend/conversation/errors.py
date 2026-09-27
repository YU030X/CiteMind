"""问答用例领域错误：与 HTTP 状态解耦，消息一律静态、不回显问题、证据或组织数据。"""

from __future__ import annotations


class ConversationError(Exception):
    """问答错误基类；路由层只据类型映射，不依赖底层异常文本。"""


class ConversationNotFound(ConversationError):
    """会话不存在、不属于当前用户或不属于当前组织；统一按不暴露存在性处理。"""


class CitationNotFound(ConversationError):
    """引用不存在、不属于当前用户，或其来源已被撤权/删除。"""


class ConversationQuestionTooLong(ConversationError):
    """系统提示与当前问题单独就超出输入预算；不做静默截断。"""


class GenerationFailed(ConversationError):
    """provider 调用失败、超时或响应被截断；用量事实已按失败落账。"""

    def __init__(self, error_code: str) -> None:
        super().__init__("生成服务暂时不可用")
        self.error_code = error_code


class GenerationInvalidResponse(ConversationError):
    """模型响应结构非法或引用越权；用量事实已按失败落账。"""


class ConversationSourcesChanged(ConversationError):
    """证据来源版本/删除/权限在重检索后仍持续变化；返回静态可重试状态。"""


__all__ = [
    "CitationNotFound",
    "ConversationError",
    "ConversationNotFound",
    "ConversationQuestionTooLong",
    "ConversationSourcesChanged",
    "GenerationFailed",
    "GenerationInvalidResponse",
]
