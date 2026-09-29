"""原文下载路由的进程内 HTTP 契约测试：假仓储、假 blob 存储；不连数据库、不读真实文件。

覆盖：默认 active 与显式历史版本、原始字节与受控响应头、ACL/删除/跨文档统一 404、
默认 active 在读取期间变化返回 409、blob 损坏返回静态 500、读取前后各释放一次数据库连接。
真实 PostgreSQL 上的授权与字节一致性由 ``tests/integration/test_document_acl_flow.py`` 承担。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from rag_backend.api.documents import get_document_content_repository
from rag_backend.api.errors import (
    CODE_DOCUMENT_CONTENT_UNAVAILABLE,
    CODE_DOCUMENT_NOT_FOUND,
    CODE_DOCUMENT_VERSION_CONFLICT,
)
from rag_backend.app import create_app
from rag_backend.auth.context import AuthContext
from rag_backend.auth.dependencies import get_auth_context
from rag_backend.config import Settings
from rag_backend.database import get_database_session
from rag_backend.ingestion.errors import BlobCorrupt
from rag_backend.knowledge.document_content import DocumentContentTarget

ORIGIN = "http://127.0.0.1"
USER_ID = uuid.uuid4()
ORG_ID = uuid.uuid4()
KB_ID = uuid.uuid4()
DOC_ID = uuid.uuid4()
V1 = uuid.uuid4()
V2 = uuid.uuid4()


def _settings(storage_directory: str) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "session_cookie_secure": False,
        "allowed_origins": ORIGIN,
        "csrf_secret": "unit-content-secret",
        "document_storage_directory": storage_directory,
    }
    return Settings(**values)


def _context() -> AuthContext:
    return AuthContext(
        user_id=USER_ID,
        organization_id=ORG_ID,
        username="reader",
        is_admin=False,
        session_id=uuid.uuid4(),
        csrf_token="unit-content-csrf",
    )


def _target(*, version_id: uuid.UUID = V1, source_type: str = "markdown") -> DocumentContentTarget:
    return DocumentContentTarget(
        document_id=DOC_ID,
        kb_id=KB_ID,
        source_type=source_type,
        title="文档",
        version_id=version_id,
        file_ref=f"{KB_ID}/{'a' * 64}",
        file_hash="a" * 64,
        mime="application/pdf" if source_type == "pdf" else "text/markdown",
        version_status="READY",
    )


class FakeSession:
    def __init__(self) -> None:
        self.rollbacks = 0

    async def rollback(self) -> None:
        self.rollbacks += 1


class FakeContentRepository:
    """按调用顺序返回预置目标；记录入参。"""

    def __init__(self, targets: list[DocumentContentTarget | None]) -> None:
        self._targets = list(targets)
        self.calls: list[dict[str, Any]] = []

    async def load_content_target(self, **kwargs: Any) -> DocumentContentTarget | None:
        self.calls.append(kwargs)
        if not self._targets:
            raise AssertionError("出现未预置的内容查询")
        return self._targets.pop(0)


class FakeStore:
    def __init__(self, *, content: bytes = b"raw-bytes", error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.reads: list[tuple[uuid.UUID, str, str]] = []

    def read_verified_blob(
        self, kb_id: uuid.UUID, file_ref: str, file_hash: str
    ) -> bytes:
        self.reads.append((kb_id, file_ref, file_hash))
        if self.error is not None:
            raise self.error
        return self.content


def _app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    repository: FakeContentRepository,
    store: FakeStore,
    storage_directory: str,
) -> tuple[Any, FakeSession]:
    app = create_app(_settings(storage_directory))
    session = FakeSession()
    app.dependency_overrides[get_auth_context] = _context
    app.dependency_overrides[get_database_session] = lambda: session
    app.dependency_overrides[get_document_content_repository] = lambda: repository
    monkeypatch.setattr(
        "rag_backend.api.documents.DocumentBlobStore", lambda _root: store
    )
    return app, session


def _client(app: Any) -> AsyncClient:
    transport = ASGITransport(app=app, client=("10.20.0.1", 12345))
    return AsyncClient(transport=transport, base_url=ORIGIN)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_default_active_returns_bytes_with_controlled_headers(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = FakeContentRepository([_target(), _target()])
    store = FakeStore(content=b"# hello\n")
    app, session = _app(
        monkeypatch, repository=repository, store=store, storage_directory=str(tmp_path)
    )

    async with _client(app) as client:
        response = await client.get(f"/api/v1/documents/{DOC_ID}/content")

    assert response.status_code == 200, response.text
    assert response.content == b"# hello\n"
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="document-{DOC_ID}.md"'
    )
    assert store.reads == [(KB_ID, _target().file_ref, "a" * 64)]
    assert session.rollbacks == 2
    assert repository.calls[0]["version_id"] is None


@pytest.mark.anyio
async def test_explicit_version_downloads_pdf_attachment(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = FakeContentRepository(
        [_target(version_id=V2, source_type="pdf"), _target(version_id=V2, source_type="pdf")]
    )
    store = FakeStore(content=b"%PDF-1.7")
    app, _ = _app(
        monkeypatch, repository=repository, store=store, storage_directory=str(tmp_path)
    )

    async with _client(app) as client:
        response = await client.get(
            f"/api/v1/documents/{DOC_ID}/content", params={"versionId": str(V2)}
        )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="document-{DOC_ID}.pdf"'
    )
    assert repository.calls[0]["version_id"] == V2


@pytest.mark.anyio
async def test_docx_download_uses_docx_media_type(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = FakeContentRepository(
        [_target(source_type="docx"), _target(source_type="docx")]
    )
    store = FakeStore(content=b"PK\x03\x04docx")
    app, _ = _app(
        monkeypatch, repository=repository, store=store, storage_directory=str(tmp_path)
    )

    async with _client(app) as client:
        response = await client.get(f"/api/v1/documents/{DOC_ID}/content")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert response.headers["content-disposition"] == (
        f'attachment; filename="document-{DOC_ID}.docx"'
    )


@pytest.mark.anyio
async def test_unauthorized_or_deleted_returns_404(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = FakeContentRepository([None])
    store = FakeStore()
    app, session = _app(
        monkeypatch, repository=repository, store=store, storage_directory=str(tmp_path)
    )

    async with _client(app) as client:
        response = await client.get(f"/api/v1/documents/{DOC_ID}/content")

    assert response.status_code == 404
    assert response.json()["code"] == CODE_DOCUMENT_NOT_FOUND
    assert store.reads == []
    assert session.rollbacks == 1


@pytest.mark.anyio
async def test_default_active_change_returns_409(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = FakeContentRepository([_target(version_id=V1), _target(version_id=V2)])
    store = FakeStore(content=b"old")
    app, _ = _app(
        monkeypatch, repository=repository, store=store, storage_directory=str(tmp_path)
    )

    async with _client(app) as client:
        response = await client.get(f"/api/v1/documents/{DOC_ID}/content")

    assert response.status_code == 409
    assert response.json()["code"] == CODE_DOCUMENT_VERSION_CONFLICT


@pytest.mark.anyio
async def test_revocation_after_read_returns_404(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = FakeContentRepository([_target(version_id=V2), None])
    store = FakeStore(content=b"bytes")
    app, _ = _app(
        monkeypatch, repository=repository, store=store, storage_directory=str(tmp_path)
    )

    async with _client(app) as client:
        response = await client.get(
            f"/api/v1/documents/{DOC_ID}/content", params={"versionId": str(V2)}
        )

    assert response.status_code == 404
    assert response.json()["code"] == CODE_DOCUMENT_NOT_FOUND


@pytest.mark.anyio
async def test_default_version_revocation_after_read_returns_404(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认 active 撤权：交付前重核得到 None 时必须 404，不得当成 active 变化 409。"""

    repository = FakeContentRepository([_target(version_id=V1), None])
    store = FakeStore(content=b"bytes")
    app, _ = _app(
        monkeypatch, repository=repository, store=store, storage_directory=str(tmp_path)
    )

    async with _client(app) as client:
        response = await client.get(f"/api/v1/documents/{DOC_ID}/content")

    assert response.status_code == 404
    assert response.json()["code"] == CODE_DOCUMENT_NOT_FOUND


@pytest.mark.anyio
async def test_unknown_source_type_returns_static_500(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = FakeContentRepository([_target(source_type="html"), _target(source_type="html")])
    store = FakeStore(content=b"raw")
    app, _ = _app(
        monkeypatch, repository=repository, store=store, storage_directory=str(tmp_path)
    )

    async with _client(app) as client:
        response = await client.get(f"/api/v1/documents/{DOC_ID}/content")

    assert response.status_code == 500
    assert response.json()["code"] == CODE_DOCUMENT_CONTENT_UNAVAILABLE


@pytest.mark.anyio
async def test_corrupt_blob_returns_static_500(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = FakeContentRepository([_target()])
    store = FakeStore(error=BlobCorrupt("blob 内容摘要与登记值不一致"))
    app, _ = _app(
        monkeypatch, repository=repository, store=store, storage_directory=str(tmp_path)
    )

    async with _client(app) as client:
        response = await client.get(f"/api/v1/documents/{DOC_ID}/content")

    assert response.status_code == 500
    assert response.json()["code"] == CODE_DOCUMENT_CONTENT_UNAVAILABLE
    # 静态错误体不回显文件路径、摘要或底层异常。
    assert str(tmp_path) not in response.text
    assert "a" * 64 not in response.text
