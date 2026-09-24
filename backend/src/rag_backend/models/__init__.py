"""CiteMind 业务模型。

第一切片包含 ``index_profile``、``knowledge_base``、``document``、
``document_version``、``ingest_job`` 与 ``outbox_event``；第二切片新增
``index_generation``、``chunk`` 与 ``chunk_embedding``，其中 ``chunk_embedding``
是固定 512 维的 pgvector 列；第三切片新增 append-only 的云 LLM 用量账本
``llm_usage``；第四切片新增身份与会话基础 ``user_account``、``auth_session``
与 ``kb_member``。
"""

from rag_backend.models.base import Base, metadata
from rag_backend.models.chunks import Chunk, ChunkEmbedding
from rag_backend.models.identity import AuthSession, KbMember, UserAccount
from rag_backend.models.indexing import IndexGeneration, IndexProfile
from rag_backend.models.ingestion import IngestJob, OutboxEvent
from rag_backend.models.knowledge import Document, DocumentVersion, KnowledgeBase
from rag_backend.models.usage import LlmUsage

__all__ = [
    "AuthSession",
    "Base",
    "Chunk",
    "ChunkEmbedding",
    "Document",
    "DocumentVersion",
    "IndexGeneration",
    "IndexProfile",
    "IngestJob",
    "KbMember",
    "KnowledgeBase",
    "LlmUsage",
    "OutboxEvent",
    "UserAccount",
    "metadata",
]
