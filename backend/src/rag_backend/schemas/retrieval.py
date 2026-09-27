"""检索接口的请求/响应 schema（外部字段 camelCase）。

请求只接受原始查询与请求的 KB 集合：组织、成员身份与 active profile 都由服务端确定，
请求体不能提交 ``organizationId`` 或 profile。响应只回传候选标识与两路排名/分数、融合
名次，不回传 chunk 原文、向量、组织数据或 KB 是否存在的信息。
"""

from __future__ import annotations

import uuid

from pydantic import Field, field_validator

from rag_backend.retrieval.query_embedding_client import MAX_QUERY_CHARS
from rag_backend.schemas.base import CamelModel


class RetrievalSearchRequest(CamelModel):
    """``POST /retrieval/search`` 的输入；``kbIds`` 必须是会话可访问集合的子集。

    ``query`` 的字符上限对齐查询编码客户端对**完整模型输入**（服务端追加 instruction
    前缀后的文本）的实际限制，使超长输入在 pydantic 层就 422，不进入分词或网络调用。
    """

    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    kb_ids: list[uuid.UUID] = Field(min_length=1)

    @field_validator("query")
    @classmethod
    def _reject_blank_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("查询不能为空或纯空白")
        return value


class RetrievalCandidate(CamelModel):
    """一个候选 chunk 的两路排名/分数与融合名次；未命中的一路为 ``null``。"""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    kb_id: uuid.UUID
    version_id: uuid.UUID
    vector_rank: int | None = None
    vector_score: float | None = None
    keyword_rank: int | None = None
    keyword_score: float | None = None
    fusion_rank: int
    fusion_score: float


class RetrievalSearchResponse(CamelModel):
    """按融合名次升序排列的候选列表；无可检索 KB 或未命中时返回空列表。"""

    candidates: list[RetrievalCandidate]


__all__ = [
    "RetrievalCandidate",
    "RetrievalSearchRequest",
    "RetrievalSearchResponse",
]
