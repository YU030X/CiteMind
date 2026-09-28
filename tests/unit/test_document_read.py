"""文档读取接口单测：纯装配 + 进程内 HTTP 契约，不连数据库。

覆盖：更新中旧 active 与最新版本并存、``NEEDS_OCR``、最新任务只归属最新版本、
无成员权限时复用统一静态 404、详情返回裸文档对象、camelCase/ISO8601 字段，以及
「未删除 + 组织过滤」在仓储 SQL 里真实存在（编译语句静态检查，不连真实数据库）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from rag_backend.api.documents import get_document_read_repository
from rag_backend.api.errors import (
    CODE_DOCUMENT_NOT_FOUND,
    CODE_KNOWLEDGE_BASE_NOT_FOUND,
)
from rag_backend.app import create_app
from rag_backend.auth.context import AuthContext
from rag_backend.auth.dependencies import get_auth_context
from rag_backend.config import Settings
from rag_backend.database import get_database_session
from rag_backend.knowledge.document_acl import DocumentReadAccess
from rag_backend.knowledge.document_read import (
    DocumentRow,
    JobRow,
    SqlDocumentReadRepository,
    VersionRow,
    build_document_view,
    list_knowledge_base_documents,
)
from rag_backend.knowledge.roles import KbRole
from rag_backend.knowledge.service import KbAccess

ORIGIN = "http://127.0.0.1"
USER_ID = uuid.uuid4()
ORG_ID = uuid.uuid4()
KB_ID = uuid.uuid4()
DOC_ID = uuid.uuid4()
V1 = uuid.uuid4()
V2 = uuid.uuid4()

DOCUMENT_KEYS = {
    "id",
    "title",
    "sourceType",
    "lifecycleStatus",
    "activeVersion",
    "latestVersion",
    "latestJob",
    "createdAt",
    "updatedAt",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _document(active_version_id: uuid.UUID | None = V1) -> DocumentRow:
    return DocumentRow(
        id=DOC_ID,
        title="制度文档",
        source_type="markdown",
        lifecycle_status="CREATED",
        active_version_id=active_version_id,
        created_at=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 1, 2, 0, 0, tzinfo=UTC),
    )


def _version(version_id: uuid.UUID, version_no: int, status: str) -> VersionRow:
    return VersionRow(
        id=version_id, document_id=DOC_ID, version_no=version_no, status=status
    )


def _job(
    version_id: uuid.UUID, *, created_at: datetime, status: str = "QUEUED"
) -> JobRow:
    return JobRow(
        id=uuid.uuid4(),
        version_id=version_id,
        status=status,
        error_code=None,
        created_at=created_at,
    )


# --- 纯装配：状态关联 ---------------------------------------------------------------


def test_updating_document_keeps_old_active_and_new_latest() -> None:
    """新版本入库中时，activeVersion 仍是旧 READY，latestVersion 是新 PENDING。"""

    versions = [_version(V1, 1, "READY"), _version(V2, 2, "PENDING")]
    jobs = [
        _job(V2, created_at=datetime(2026, 1, 3, tzinfo=UTC), status="QUEUED"),
    ]
    view = build_document_view(_document(active_version_id=V1), versions, jobs)

    assert view.active_version is not None
    assert (view.active_version.id, view.active_version.status) == (V1, "READY")
    assert view.latest_version is not None
    assert (view.latest_version.id, view.latest_version.version_no, view.latest_version.status) == (
        V2,
        2,
        "PENDING",
    )
    assert view.latest_job is not None
    assert view.latest_job.status == "QUEUED"


def test_latest_version_reports_needs_ocr() -> None:
    """零可提取文本的 PDF 以 ``NEEDS_OCR`` 直接暴露，不被吞成 READY。"""

    versions = [_version(V1, 1, "NEEDS_OCR")]
    view = build_document_view(_document(active_version_id=None), versions, [])

    assert view.latest_version is not None
    assert view.latest_version.status == "NEEDS_OCR"
    assert view.active_version is None
    assert view.latest_job is None


def test_latest_job_belongs_to_latest_version_only() -> None:
    """最新任务只从最新版本的 job 里取，旧版本更新的 job 被忽略。"""

    versions = [_version(V1, 1, "READY"), _version(V2, 2, "PENDING")]
    old_version_job = _job(V1, created_at=datetime(2026, 1, 9, tzinfo=UTC))
    older_latest_job = _job(V2, created_at=datetime(2026, 1, 3, tzinfo=UTC))
    newer_latest_job = _job(V2, created_at=datetime(2026, 1, 5, tzinfo=UTC), status="PARSING")
    view = build_document_view(
        _document(active_version_id=V1),
        versions,
        [old_version_job, older_latest_job, newer_latest_job],
    )

    assert view.latest_job is not None
    assert view.latest_job.id == newer_latest_job.id


def test_document_without_versions_has_null_relations() -> None:
    view = build_document_view(_document(active_version_id=None), [], [])

    assert view.latest_version is None
    assert view.active_version is None
    assert view.latest_job is None


# --- 纯装配：仓储 SQL 的未删除/组织过滤 --------------------------------------------


class _CapturingResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)

    def first(self) -> Any:
        return self._rows[0] if self._rows else None


class _CapturingSession:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.statements: list[Any] = []
        self._rows = rows or []

    async def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> _CapturingResult:
        self.statements.append(statement)
        return _CapturingResult(self._rows)


@pytest.mark.anyio
async def test_list_documents_query_excludes_deleted_and_filters_organization() -> None:
    session = _CapturingSession()
    repository = SqlDocumentReadRepository(session)  # type: ignore[arg-type]

    await repository.list_documents(kb_id=KB_ID, organization_id=ORG_ID, user_id=USER_ID)

    sql = str(session.statements[0])
    assert "deleted_at IS NULL" in sql
    assert "lifecycle_status" in sql
    assert "knowledge_base.organization_id" in sql
    assert "kb_member.revoked_at IS NULL" in sql
    assert "document_acl" in sql
    assert "acl_mode" in sql
    assert "ORDER BY document.created_at DESC" in sql


# --- HTTP 契约 ---------------------------------------------------------------------


def _settings() -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "session_cookie_secure": False,
        "allowed_origins": ORIGIN,
        "csrf_secret": "unit-document-read-secret",
    }
    return Settings(**values)


def _context() -> AuthContext:
    return AuthContext(
        user_id=USER_ID,
        organization_id=ORG_ID,
        username="reader",
        is_admin=False,
        session_id=uuid.uuid4(),
        csrf_token="unit-csrf-token",
    )


def _kb_access(role: KbRole = KbRole.READER) -> KbAccess:
    return KbAccess(
        kb_id=KB_ID,
        name="制度库",
        role=role,
        organization_id=ORG_ID,
        acl_revision=0,
        kb_revision=0,
    )


class FakeDocumentReadRepository:
    """内存只读仓储；只按传入的未删除行返回。"""

    def __init__(
        self,
        documents: list[DocumentRow] | None = None,
        *,
        versions: list[VersionRow] | None = None,
        jobs: list[JobRow] | None = None,
    ) -> None:
        self.documents = {document.id: document for document in documents or []}
        self.versions = list(versions or [])
        self.jobs = list(jobs or [])

    async def list_documents(
        self, *, kb_id: uuid.UUID, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> list[DocumentRow]:
        return list(self.documents.values())

    async def get_document(
        self, *, document_id: uuid.UUID, organization_id: uuid.UUID
    ) -> DocumentRow | None:
        return self.documents.get(document_id)

    async def list_versions(
        self, *, document_ids: Any
    ) -> list[VersionRow]:
        wanted = set(document_ids)
        return [row for row in self.versions if row.document_id in wanted]

    async def list_jobs(self, *, version_ids: Any) -> list[JobRow]:
        wanted = set(version_ids)
        return [row for row in self.jobs if row.version_id in wanted]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def read_app() -> Any:
    app = create_app(_settings())
    app.dependency_overrides[get_auth_context] = _context
    app.dependency_overrides[get_database_session] = lambda: object()
    yield app


def _client(app: Any) -> AsyncClient:
    transport = ASGITransport(app=app, client=("10.20.0.1", 12345))
    return AsyncClient(transport=transport, base_url="http://127.0.0.1")


def _allow_kb(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_resolve(*_args: Any, **_kwargs: Any) -> KbAccess:
        return _kb_access()

    monkeypatch.setattr(
        "rag_backend.auth.dependencies.resolve_knowledge_base_access", fake_resolve
    )


def _allow_document(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_resolve(*_args: Any, **_kwargs: Any) -> DocumentReadAccess:
        return DocumentReadAccess(
            document_id=DOC_ID, kb_id=KB_ID, organization_id=ORG_ID, role=KbRole.READER
        )

    monkeypatch.setattr(
        "rag_backend.auth.dependencies.resolve_document_read_access", fake_resolve
    )


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.mark.anyio
async def test_list_documents_returns_camel_case_summaries(
    read_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow_kb(monkeypatch)
    repository = FakeDocumentReadRepository(
        [_document()],
        versions=[_version(V1, 1, "READY")],
        jobs=[_job(V1, created_at=datetime(2026, 1, 3, tzinfo=UTC))],
    )
    read_app.dependency_overrides[get_document_read_repository] = lambda: repository

    async with read_app.router.lifespan_context(read_app):
        async with _client(read_app) as client:
            response = await client.get(f"/api/v1/knowledge-bases/{KB_ID}/documents")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload.keys()) == {"documents"}
    assert len(payload["documents"]) == 1
    document = payload["documents"][0]
    assert set(document.keys()) == DOCUMENT_KEYS
    assert document["id"] == str(DOC_ID)
    assert document["sourceType"] == "markdown"
    assert document["lifecycleStatus"] == "CREATED"
    assert document["activeVersion"] == {
        "id": str(V1),
        "versionNo": 1,
        "status": "READY",
    }
    assert document["latestVersion"]["versionNo"] == 1
    assert document["latestJob"]["status"] == "QUEUED"
    assert _parse_iso(document["createdAt"]) == datetime(2026, 1, 1, tzinfo=UTC)
    assert _parse_iso(document["updatedAt"]) == datetime(2026, 1, 2, tzinfo=UTC)


@pytest.mark.anyio
async def test_document_detail_returns_bare_object(
    read_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow_document(monkeypatch)
    repository = FakeDocumentReadRepository(
        [_document()], versions=[_version(V1, 1, "READY")]
    )
    read_app.dependency_overrides[get_document_read_repository] = lambda: repository

    async with read_app.router.lifespan_context(read_app):
        async with _client(read_app) as client:
            response = await client.get(f"/api/v1/documents/{DOC_ID}")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload.keys()) == DOCUMENT_KEYS
    assert payload["id"] == str(DOC_ID)
    assert payload["latestJob"] is None


@pytest.mark.anyio
async def test_list_documents_returns_404_without_kb_membership(
    read_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def deny(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "rag_backend.auth.dependencies.resolve_knowledge_base_access", deny
    )
    read_app.dependency_overrides[get_document_read_repository] = (
        lambda: FakeDocumentReadRepository()
    )

    async with read_app.router.lifespan_context(read_app):
        async with _client(read_app) as client:
            response = await client.get(f"/api/v1/knowledge-bases/{KB_ID}/documents")

    assert response.status_code == 404
    assert response.json()["code"] == CODE_KNOWLEDGE_BASE_NOT_FOUND


@pytest.mark.anyio
async def test_document_detail_returns_404_without_access(
    read_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def deny(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr("rag_backend.auth.dependencies.resolve_document_read_access", deny)
    read_app.dependency_overrides[get_document_read_repository] = (
        lambda: FakeDocumentReadRepository()
    )

    async with read_app.router.lifespan_context(read_app):
        async with _client(read_app) as client:
            response = await client.get(f"/api/v1/documents/{DOC_ID}")

    assert response.status_code == 404
    assert response.json()["code"] == CODE_DOCUMENT_NOT_FOUND


@pytest.mark.anyio
async def test_document_detail_returns_404_for_deleted_document(
    read_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """仓库按 ``deleted_at`` 过滤后查不到：详情返回与无权相同的静态 404。"""

    _allow_document(monkeypatch)
    read_app.dependency_overrides[get_document_read_repository] = (
        lambda: FakeDocumentReadRepository()
    )

    async with read_app.router.lifespan_context(read_app):
        async with _client(read_app) as client:
            response = await client.get(f"/api/v1/documents/{DOC_ID}")

    assert response.status_code == 404
    assert response.json()["code"] == CODE_DOCUMENT_NOT_FOUND


@pytest.mark.anyio
async def test_list_service_uses_repository_once_per_level() -> None:
    """列表装配只调用仓储一次（不产生 N+1）：文档、版本、任务各一次查询。"""

    calls = {"documents": 0, "versions": 0, "jobs": 0}

    class CountingRepository(FakeDocumentReadRepository):
        async def list_documents(self, **kwargs: Any) -> list[DocumentRow]:
            calls["documents"] += 1
            return await super().list_documents(**kwargs)

        async def list_versions(self, **kwargs: Any) -> list[VersionRow]:
            calls["versions"] += 1
            return await super().list_versions(**kwargs)

        async def list_jobs(self, **kwargs: Any) -> list[JobRow]:
            calls["jobs"] += 1
            return await super().list_jobs(**kwargs)

    repository = CountingRepository(
        [_document()], versions=[_version(V1, 1, "READY")]
    )
    views = await list_knowledge_base_documents(
        repository, kb_id=KB_ID, organization_id=ORG_ID, user_id=USER_ID
    )

    assert len(views) == 1
    assert calls == {"documents": 1, "versions": 1, "jobs": 1}
