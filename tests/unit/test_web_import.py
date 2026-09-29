"""网页导入的免联网幂等与路由注册契约测试；不连接数据库、不发起 HTTP。

真实事务、抓取与授权行为由集成测试另行覆盖；这里只固定「重放不触网」与路由存在性。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from rag_backend.api.documents import router as documents_router
from rag_backend.ingestion import service as ingestion_service
from rag_backend.ingestion.errors import IdempotencyConflict
from rag_backend.ingestion.web_fetch import WebFetchError

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _FakeSession:
    def __init__(self) -> None:
        self.rollbacks = 0

    async def rollback(self) -> None:
        self.rollbacks += 1


def _existing(**overrides: Any) -> ingestion_service._ExistingJob:
    values: dict[str, Any] = {
        "job_id": uuid.uuid4(),
        "document_id": uuid.uuid4(),
        "version_id": uuid.uuid4(),
        "title": "Doc",
        "file_hash": "a" * 64,
        "document_deleted": False,
        "expected_active_version_id": None,
        "source_url": "https://example.com/a",
    }
    values.update(overrides)
    return ingestion_service._ExistingJob(**values)


def _patch_existing(
    monkeypatch: pytest.MonkeyPatch, existing: ingestion_service._ExistingJob | None
) -> None:
    async def loader(*_args: Any, **_kwargs: Any) -> Any:
        return existing

    monkeypatch.setattr(ingestion_service, "_load_existing_job", loader)


def test_web_routes_are_registered() -> None:
    paths = {getattr(route, "path", None) for route in documents_router.routes}
    assert "/api/v1/knowledge-bases/{kb_id}/documents/web" in paths
    assert "/api/v1/documents/{document_id}/versions/web" in paths


def test_web_download_media_type_is_html_attachment() -> None:
    from rag_backend.api.documents import _content_media_type

    assert _content_media_type("web") == ("text/html", ".html")


async def test_replay_same_url_and_title_returns_original_ids_without_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    _patch_existing(monkeypatch, _existing())
    calls: list[str] = []

    outcome = await ingestion_service.create_web_document(
        session,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        kb_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        title="Doc",
        url="HTTPS://Example.com/a",
        idempotency_key="key-1",
        fetcher=lambda target: calls.append(target),  # type: ignore[arg-type,return-value]
        allowed_hosts=frozenset({"example.com"}),
    )

    assert outcome.reused is True
    assert calls == []
    assert session.rollbacks == 1


async def test_replay_different_title_conflicts_without_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    _patch_existing(monkeypatch, _existing())
    calls: list[str] = []

    with pytest.raises(IdempotencyConflict):
        await ingestion_service.create_web_document(
            session,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            kb_id=uuid.uuid4(),
            organization_id=uuid.uuid4(),
            title="Other",
            url="https://example.com/a",
            idempotency_key="key-1",
            fetcher=lambda target: calls.append(target),  # type: ignore[arg-type,return-value]
            allowed_hosts=frozenset({"example.com"}),
        )

    assert calls == []


async def test_replay_different_url_conflicts_without_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    _patch_existing(monkeypatch, _existing())
    calls: list[str] = []

    with pytest.raises(IdempotencyConflict):
        await ingestion_service.create_web_document(
            session,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            kb_id=uuid.uuid4(),
            organization_id=uuid.uuid4(),
            title="Doc",
            url="https://example.com/b",
            idempotency_key="key-1",
            fetcher=lambda target: calls.append(target),  # type: ignore[arg-type,return-value]
            allowed_hosts=frozenset({"example.com"}),
        )

    assert calls == []


async def test_disallowed_host_fails_before_any_db_or_fetch() -> None:
    calls: list[str] = []

    with pytest.raises(WebFetchError) as error:
        await ingestion_service.create_web_document(
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            kb_id=uuid.uuid4(),
            organization_id=uuid.uuid4(),
            title="Doc",
            url="https://example.com/a",
            idempotency_key="key-1",
            fetcher=lambda target: calls.append(target),  # type: ignore[arg-type,return-value]
            allowed_hosts=frozenset(),
        )

    assert error.value.code == "NOT_ALLOWED"
    assert calls == []


async def test_invalid_url_fails_before_any_db_or_fetch() -> None:
    with pytest.raises(WebFetchError) as error:
        await ingestion_service.create_web_document(
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            kb_id=uuid.uuid4(),
            organization_id=uuid.uuid4(),
            title="Doc",
            url="ftp://example.com/a",
            idempotency_key="key-1",
            fetcher=lambda _target: None,  # type: ignore[arg-type,return-value]
            allowed_hosts=frozenset({"example.com"}),
        )
    assert error.value.code == "URL_INVALID"
