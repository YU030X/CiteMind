"""文档列表/详情的只读仓储与纯装配。

本模块只读取 ``document``/``document_version``/``ingest_job`` 三个不可变事实，供
``GET /api/v1/knowledge-bases/{kb_id}/documents`` 与 ``GET /api/v1/documents/{document_id}``
使用。授权由路由层的 ``require_kb_role``/``require_document_role`` 判定；SQL 内再按
``organization_id`` 与 ``deleted_at`` 过滤，已删除文档不出现在列表或详情中。

「最新版本」取 ``version_no`` 最大者；「最新任务」只属于该最新版本，按
``(created_at, id)`` 稳定取最新。装配是纯函数，便于不依赖数据库地验证「更新中仍有旧
active」与 ``NEEDS_OCR`` 等状态关联。本模块不返回 ``file_ref``/``file_hash``/租约或任何
正文，也不做分页（个人规模本片明确不分页）。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rag_backend.knowledge.document_acl import acl_read_clause
from rag_backend.models.identity import KbMember
from rag_backend.models.ingestion import IngestJob
from rag_backend.models.knowledge import Document, DocumentVersion, KnowledgeBase

# 与 ``ingestion.service`` 的 tombstone 值一致；这里额外按生命周期过滤，防止
# ``deleted_at`` 与实际状态短暂不一致时泄露已删除文档。
DOCUMENT_LIFECYCLE_DELETED = "DELETED"


@dataclass(frozen=True, slots=True, kw_only=True)
class DocumentRow:
    """文档主体行；不含版本与任务。"""

    id: uuid.UUID
    title: str
    source_type: str
    lifecycle_status: str
    active_version_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class VersionRow:
    """文档的一个版本；只用于本模块的装配，不外泄 ``file_ref``/``file_hash``。"""

    id: uuid.UUID
    document_id: uuid.UUID
    version_no: int
    status: str


@dataclass(frozen=True, slots=True, kw_only=True)
class JobRow:
    """入库任务；只暴露 ``id``/``status``/``errorCode``，不含租约或诊断正文。"""

    id: uuid.UUID
    version_id: uuid.UUID
    status: str
    error_code: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class VersionView:
    """对外暴露的版本摘要。"""

    id: uuid.UUID
    version_no: int
    status: str


@dataclass(frozen=True, slots=True, kw_only=True)
class JobView:
    """对外暴露的任务摘要。"""

    id: uuid.UUID
    status: str
    error_code: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class DocumentView:
    """一个文档的完整只读视图；``activeVersion`` 与 ``latestVersion`` 可以不同。"""

    id: uuid.UUID
    title: str
    source_type: str
    lifecycle_status: str
    active_version: VersionView | None
    latest_version: VersionView | None
    latest_job: JobView | None
    created_at: datetime
    updated_at: datetime


def _latest_version(versions: Sequence[VersionRow]) -> VersionRow | None:
    """取 ``version_no`` 最大的版本；``version_no`` 在文档内唯一，id 仅作稳定兜底。"""

    if not versions:
        return None
    return max(versions, key=lambda version: (version.version_no, str(version.id)))


def build_document_view(
    document: DocumentRow,
    versions: Sequence[VersionRow],
    jobs: Sequence[JobRow],
) -> DocumentView:
    """把文档、其全部版本与这些版本的任务装配成一个视图。

    ``latestVersion`` 是 ``version_no`` 最大的版本；``latestJob`` 只在该版本的 job 中按
    ``(created_at, id)`` 取最新，因此「更新中仍有旧 active」时 ``activeVersion`` 与
    ``latestVersion`` 会同时出现且状态各自独立。
    """

    latest = _latest_version(versions)
    active = None
    if document.active_version_id is not None:
        active = next(
            (version for version in versions if version.id == document.active_version_id),
            None,
        )
    latest_job = None
    if latest is not None:
        candidates = [job for job in jobs if job.version_id == latest.id]
        if candidates:
            latest_job = max(candidates, key=lambda job: (job.created_at, str(job.id)))

    return DocumentView(
        id=document.id,
        title=document.title,
        source_type=document.source_type,
        lifecycle_status=document.lifecycle_status,
        active_version=(
            None
            if active is None
            else VersionView(
                id=active.id, version_no=active.version_no, status=active.status
            )
        ),
        latest_version=(
            None
            if latest is None
            else VersionView(
                id=latest.id, version_no=latest.version_no, status=latest.status
            )
        ),
        latest_job=(
            None
            if latest_job is None
            else JobView(
                id=latest_job.id,
                status=latest_job.status,
                error_code=latest_job.error_code,
            )
        ),
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


class DocumentReadRepository(Protocol):
    """文档只读仓储；实现持有调用方 ``AsyncSession``，不自建事务边界。"""

    async def list_documents(
        self,
        *,
        kb_id: uuid.UUID,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> list[DocumentRow]: ...

    async def get_document(
        self, *, document_id: uuid.UUID, organization_id: uuid.UUID
    ) -> DocumentRow | None: ...

    async def list_versions(
        self, *, document_ids: Sequence[uuid.UUID]
    ) -> list[VersionRow]: ...

    async def list_jobs(self, *, version_ids: Sequence[uuid.UUID]) -> list[JobRow]: ...


class SqlDocumentReadRepository:
    """基于 SQLAlchemy 异步会话的实现；每次调用最多三次参数化查询，不产生 N+1。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_documents(
        self,
        *,
        kb_id: uuid.UUID,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> list[DocumentRow]:
        statement = (
            select(
                Document.id,
                Document.title,
                Document.source_type,
                Document.lifecycle_status,
                Document.active_version_id,
                Document.created_at,
                Document.updated_at,
            )
            .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
            .join(KbMember, KbMember.kb_id == Document.kb_id)
            .where(
                Document.kb_id == kb_id,
                KnowledgeBase.organization_id == organization_id,
                KbMember.user_id == user_id,
                KbMember.revoked_at.is_(None),
                Document.deleted_at.is_(None),
                Document.lifecycle_status != DOCUMENT_LIFECYCLE_DELETED,
                acl_read_clause(user_id),
            )
            .order_by(Document.created_at.desc(), Document.id.desc())
        )
        rows = (await self._session.execute(statement)).all()
        return [self._to_document(row) for row in rows]

    async def get_document(
        self, *, document_id: uuid.UUID, organization_id: uuid.UUID
    ) -> DocumentRow | None:
        statement = (
            select(
                Document.id,
                Document.title,
                Document.source_type,
                Document.lifecycle_status,
                Document.active_version_id,
                Document.created_at,
                Document.updated_at,
            )
            .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
            .where(
                Document.id == document_id,
                KnowledgeBase.organization_id == organization_id,
                Document.deleted_at.is_(None),
                Document.lifecycle_status != DOCUMENT_LIFECYCLE_DELETED,
            )
        )
        row = (await self._session.execute(statement)).first()
        return None if row is None else self._to_document(row)

    async def list_versions(
        self, *, document_ids: Sequence[uuid.UUID]
    ) -> list[VersionRow]:
        if not document_ids:
            return []
        statement = (
            select(
                DocumentVersion.id,
                DocumentVersion.document_id,
                DocumentVersion.version_no,
                DocumentVersion.status,
            )
            .where(DocumentVersion.document_id.in_(list(document_ids)))
            .order_by(
                DocumentVersion.document_id,
                DocumentVersion.version_no.desc(),
                DocumentVersion.id,
            )
        )
        rows = (await self._session.execute(statement)).all()
        return [
            VersionRow(id=row[0], document_id=row[1], version_no=row[2], status=row[3])
            for row in rows
        ]

    async def list_jobs(self, *, version_ids: Sequence[uuid.UUID]) -> list[JobRow]:
        if not version_ids:
            return []
        statement = (
            select(
                IngestJob.id,
                IngestJob.version_id,
                IngestJob.status,
                IngestJob.error_code,
                IngestJob.created_at,
            )
            .where(IngestJob.version_id.in_(list(version_ids)))
            .order_by(IngestJob.created_at.desc(), IngestJob.id.desc())
        )
        rows = (await self._session.execute(statement)).all()
        return [
            JobRow(
                id=row[0],
                version_id=row[1],
                status=row[2],
                error_code=row[3],
                created_at=row[4],
            )
            for row in rows
        ]

    @staticmethod
    def _to_document(row: Any) -> DocumentRow:
        return DocumentRow(
            id=row.id,
            title=row.title,
            source_type=row.source_type,
            lifecycle_status=row.lifecycle_status,
            active_version_id=row.active_version_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


def _group_versions(versions: Sequence[VersionRow]) -> dict[uuid.UUID, list[VersionRow]]:
    grouped: dict[uuid.UUID, list[VersionRow]] = {}
    for version in versions:
        grouped.setdefault(version.document_id, []).append(version)
    return grouped


def _group_jobs(jobs: Sequence[JobRow]) -> dict[uuid.UUID, list[JobRow]]:
    grouped: dict[uuid.UUID, list[JobRow]] = {}
    for job in jobs:
        grouped.setdefault(job.version_id, []).append(job)
    return grouped


async def list_knowledge_base_documents(
    repository: DocumentReadRepository,
    *,
    kb_id: uuid.UUID,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> list[DocumentView]:
    """列出 KB 内当前用户可读的未删除文档及其版本/任务视图；保持仓储稳定倒序。"""

    documents = await repository.list_documents(
        kb_id=kb_id, organization_id=organization_id, user_id=user_id
    )
    if not documents:
        return []
    versions = await repository.list_versions(
        document_ids=[document.id for document in documents]
    )
    jobs = await repository.list_jobs(version_ids=[version.id for version in versions])
    versions_by_document = _group_versions(versions)
    jobs_by_version = _group_jobs(jobs)

    views: list[DocumentView] = []
    for document in documents:
        document_versions = versions_by_document.get(document.id, [])
        document_jobs = [
            job
            for version in document_versions
            for job in jobs_by_version.get(version.id, ())
        ]
        views.append(build_document_view(document, document_versions, document_jobs))
    return views


async def load_document_detail(
    repository: DocumentReadRepository,
    *,
    document_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> DocumentView | None:
    """读取单个未删除文档的视图；不存在、跨组织或已删除返回 None。"""

    document = await repository.get_document(
        document_id=document_id, organization_id=organization_id
    )
    if document is None:
        return None
    versions = await repository.list_versions(document_ids=[document.id])
    jobs = await repository.list_jobs(version_ids=[version.id for version in versions])
    return build_document_view(document, versions, jobs)


__all__ = [
    "DOCUMENT_LIFECYCLE_DELETED",
    "DocumentReadRepository",
    "DocumentRow",
    "DocumentView",
    "JobRow",
    "JobView",
    "SqlDocumentReadRepository",
    "VersionRow",
    "VersionView",
    "build_document_view",
    "list_knowledge_base_documents",
    "load_document_detail",
]
