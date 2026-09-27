"""文档接口的请求/响应 schema。"""

from __future__ import annotations

import uuid
from datetime import datetime

from rag_backend.schemas.base import CamelModel


class DocumentUploadResponse(CamelModel):
    """上传受理成功后返回的入库事实标识；``202`` 不代表解析或索引已完成。"""

    document_id: uuid.UUID
    version_id: uuid.UUID
    job_id: uuid.UUID


class DocumentVersionSummary(CamelModel):
    """文档列表/详情中的版本摘要；只暴露 id、序号与状态。"""

    id: uuid.UUID
    version_no: int
    status: str


class DocumentJobSummary(CamelModel):
    """最新版本的入库任务摘要；只暴露 id、状态与静态诊断码。"""

    id: uuid.UUID
    status: str
    error_code: str | None


class DocumentSummary(CamelModel):
    """文档列表/详情共用的对象；不包含 ``fileRef``/``fileHash``/租约或正文。

    ``activeVersion`` 可能落后于 ``latestVersion``（新版本入库中时旧 active 仍继续服务），
    两者状态各自独立；``latestJob`` 只属于 ``latestVersion``。
    """

    id: uuid.UUID
    title: str
    source_type: str
    lifecycle_status: str
    active_version: DocumentVersionSummary | None
    latest_version: DocumentVersionSummary | None
    latest_job: DocumentJobSummary | None
    created_at: datetime
    updated_at: datetime


class DocumentListResponse(CamelModel):
    """KB 内未删除文档列表；本片不分页。"""

    documents: list[DocumentSummary]
