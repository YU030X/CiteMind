"""文档上传接口的请求/响应 schema。"""

from __future__ import annotations

import uuid

from rag_backend.schemas.base import CamelModel


class DocumentUploadResponse(CamelModel):
    """上传受理成功后返回的入库事实标识；``202`` 不代表解析或索引已完成。"""

    document_id: uuid.UUID
    version_id: uuid.UUID
    job_id: uuid.UUID
