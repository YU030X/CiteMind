"""CiteMind 业务模型。

第一切片包含 ``index_profile``、``knowledge_base``、``document``、
``document_version``、``ingest_job`` 与 ``outbox_event``；第二切片新增
``index_generation``、``chunk`` 与 ``chunk_embedding``，其中 ``chunk_embedding``
是固定 512 维的 pgvector 列；第三切片新增 append-only 的云 LLM 用量账本
``llm_usage``。
"""

from evidencehub.models.base import Base, metadata
from evidencehub.models.chunks import Chunk, ChunkEmbedding
from evidencehub.models.indexing import IndexGeneration, IndexProfile
from evidencehub.models.ingestion import IngestJob, OutboxEvent
from evidencehub.models.knowledge import Document, DocumentVersion, KnowledgeBase
from evidencehub.models.usage import LlmUsage

__all__ = [
    "Base",
    "Chunk",
    "ChunkEmbedding",
    "Document",
    "DocumentVersion",
    "IndexGeneration",
    "IndexProfile",
    "IngestJob",
    "KnowledgeBase",
    "LlmUsage",
    "OutboxEvent",
    "metadata",
]
