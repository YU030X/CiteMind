"""原文下载的授权读取目标查询。

只在服务端解析「当前用户能否读该文档的某个版本」，返回下载所需的静态引用
（``file_ref``/``file_hash``/``mime``/``source_type``）。授权与文档读取共用同一判定：
同组织、未撤销 ``kb_member``、未删除文档、ACL 放行。跨文档、跨组织、未授权与已删除
一律返回 ``None``，由路由统一映射为不暴露存在性的 404。

本模块不读取文件、不判断字节；blob 读取由 :class:`DocumentBlobStore` 在 IO 线程执行。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rag_backend.knowledge.document_acl import acl_read_clause
from rag_backend.models.identity import KbMember
from rag_backend.models.knowledge import Document, DocumentVersion, KnowledgeBase

DOCUMENT_LIFECYCLE_DELETED = "DELETED"


@dataclass(frozen=True, slots=True)
class DocumentContentTarget:
    """一次授权的下载目标；``version_id`` 是本次实际要交付的版本。"""

    document_id: uuid.UUID
    kb_id: uuid.UUID
    source_type: str
    title: str
    version_id: uuid.UUID
    file_ref: str
    file_hash: str
    mime: str
    version_status: str


class DocumentContentRepository(Protocol):
    """下载目标读取接口；实现持有调用方 ``AsyncSession``，不自建事务边界。"""

    async def load_content_target(
        self,
        *,
        document_id: uuid.UUID,
        user_id: uuid.UUID,
        organization_id: uuid.UUID,
        version_id: uuid.UUID | None,
    ) -> DocumentContentTarget | None: ...


class SqlDocumentContentRepository:
    """基于 SQLAlchemy 异步会话的实现；单次参数化查询，不产生 N+1。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_content_target(
        self,
        *,
        document_id: uuid.UUID,
        user_id: uuid.UUID,
        organization_id: uuid.UUID,
        version_id: uuid.UUID | None,
    ) -> DocumentContentTarget | None:
        if version_id is None:
            version_condition = DocumentVersion.id == Document.active_version_id
        else:
            version_condition = DocumentVersion.id == version_id
        statement = (
            select(
                Document.id,
                Document.kb_id,
                Document.source_type,
                Document.title,
                DocumentVersion.id,
                DocumentVersion.file_ref,
                DocumentVersion.file_hash,
                DocumentVersion.mime,
                DocumentVersion.status,
            )
            .join(DocumentVersion, DocumentVersion.document_id == Document.id)
            .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
            .join(KbMember, KbMember.kb_id == Document.kb_id)
            .where(
                Document.id == document_id,
                KnowledgeBase.organization_id == organization_id,
                KbMember.user_id == user_id,
                KbMember.revoked_at.is_(None),
                Document.deleted_at.is_(None),
                Document.lifecycle_status != DOCUMENT_LIFECYCLE_DELETED,
                acl_read_clause(user_id),
                version_condition,
            )
        )
        row = (await self._session.execute(statement)).first()
        if row is None:
            return None
        return DocumentContentTarget(
            document_id=row[0],
            kb_id=row[1],
            source_type=row[2],
            title=row[3],
            version_id=row[4],
            file_ref=row[5],
            file_hash=row[6],
            mime=row[7],
            version_status=row[8],
        )


async def load_document_content_target(
    repository: DocumentContentRepository,
    *,
    document_id: uuid.UUID,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    version_id: uuid.UUID | None,
) -> DocumentContentTarget | None:
    """读取下载目标；不存在、跨组织、已删除、ACL 拒绝或版本不属于该文档都返回 None。"""

    return await repository.load_content_target(
        document_id=document_id,
        user_id=user_id,
        organization_id=organization_id,
        version_id=version_id,
    )


__all__ = [
    "DocumentContentRepository",
    "DocumentContentTarget",
    "SqlDocumentContentRepository",
    "load_document_content_target",
]
