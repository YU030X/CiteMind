"""Markdown 上传片的聚焦单测：纯校验、私有存储、接收阶段体积上限与路由顺序契约。

这些测试不连接数据库；真实事务与授权行为由 ``tests/integration/test_document_upload_flow.py``
在隔离测试库上验收。
"""

from __future__ import annotations

import errno
import os
import threading
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from rag_backend.api.documents import (
    MAX_UPLOAD_REQUEST_BYTES,
    UPLOAD_MALFORMED_MESSAGE,
    UPLOAD_TOO_LARGE_MESSAGE,
    _ingestion_error,
    enforce_upload_body_limit,
    require_document_editor,
    require_editor,
    router,
)
from rag_backend.api.errors import (
    CODE_DOCUMENT_EMPTY,
    CODE_DOCUMENT_NOT_PDF,
    CODE_DOCUMENT_NOT_TEXT,
    CODE_DOCUMENT_TITLE_INVALID,
    CODE_DOCUMENT_TOO_LARGE,
    CODE_IDEMPOTENCY_KEY_INVALID,
    CODE_IDEMPOTENCY_KEY_REUSED,
    CODE_INTERNAL_ERROR,
    CODE_UNSUPPORTED_DOCUMENT_TYPE,
    CODE_UPLOAD_MALFORMED,
    ApiError,
)
from rag_backend.app import create_app
from rag_backend.auth.dependencies import require_csrf
from rag_backend.config import DEFAULT_ORGANIZATION_ID, Settings
from rag_backend.database import get_database_session
from rag_backend.ingestion import parsing
from rag_backend.ingestion import service as ingestion_service
from rag_backend.ingestion.errors import (
    DocumentEmpty,
    DocumentNotPdf,
    DocumentNotText,
    DocumentTooLarge,
    IdempotencyConflict,
    IdempotencyKeyInvalid,
    IngestionError,
    TitleInvalid,
    UnsupportedDocumentType,
)
from rag_backend.ingestion.profile_repository import IndexProfileConflictError
from rag_backend.ingestion.storage import (
    DocumentBlobStore,
    InvalidBlobReference,
    content_hash,
)
from rag_backend.ingestion.validation import (
    MARKDOWN_MEDIA_TYPE,
    MAX_IDEMPOTENCY_KEY_LENGTH,
    MAX_MARKDOWN_BYTES,
    build_dedupe_key,
    decode_markdown_content,
    normalize_idempotency_key,
    normalize_title,
    validate_markdown_filename,
)
from rag_backend.knowledge.roles import KbRole
from rag_backend.knowledge.service import DocumentAccess, KbAccess
from rag_backend.models.ingestion import IngestJob
from rag_backend.models.knowledge import DocumentVersion
from rag_backend.retrieval.keyword_analyzer import KeywordAnalyzerError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# --- 纯校验规则 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "filename",
    ["notes.md", "notes.MD", "a.markdown", "dir/sub/notes.md", "C:\\docs\\notes.md"],
)
def test_validate_markdown_filename_accepts_markdown_suffixes(filename: str) -> None:
    assert validate_markdown_filename(filename)


@pytest.mark.parametrize("filename", [None, "", "  ", "notes.pdf", "notes.txt", "notes"])
def test_validate_markdown_filename_rejects_other_types(filename: str | None) -> None:
    with pytest.raises(UnsupportedDocumentType):
        validate_markdown_filename(filename)


def test_decode_markdown_content_accepts_utf8_text() -> None:
    assert decode_markdown_content("# 标题\n\n正文".encode()) == "# 标题\n\n正文"


def test_decode_markdown_content_rejects_empty() -> None:
    with pytest.raises(DocumentEmpty):
        decode_markdown_content(b"")


def test_decode_markdown_content_rejects_invalid_utf8() -> None:
    with pytest.raises(DocumentNotText):
        decode_markdown_content(b"# \xff\xfe not utf8")


def test_decode_markdown_content_rejects_binary_control_bytes() -> None:
    # PNG 头在 UTF-8 解码成功后仍含 NUL / 控制字节，属于伪装成 .md 的二进制。
    with pytest.raises(DocumentNotText):
        decode_markdown_content(b"\x89PNG\r\n\x1a\n\x00data")


def test_decode_markdown_content_rejects_oversize() -> None:
    with pytest.raises(DocumentTooLarge):
        decode_markdown_content(b"a" * (MAX_MARKDOWN_BYTES + 1))


def test_normalize_title_strips_and_enforces_bounds() -> None:
    assert normalize_title("  hello  ") == "hello"
    with pytest.raises(TitleInvalid):
        normalize_title("   ")
    with pytest.raises(TitleInvalid):
        normalize_title("x" * 501)


def test_normalize_idempotency_key_strips_and_enforces_bounds() -> None:
    assert normalize_idempotency_key("  abc  ") == "abc"
    with pytest.raises(IdempotencyKeyInvalid):
        normalize_idempotency_key("   ")
    with pytest.raises(IdempotencyKeyInvalid):
        normalize_idempotency_key("x" * (MAX_IDEMPOTENCY_KEY_LENGTH + 1))


def test_dedupe_key_is_scoped_and_does_not_store_raw_value() -> None:
    organization_id = uuid.uuid4()
    kb_a = uuid.uuid4()
    kb_b = uuid.uuid4()
    key = "raw-idempotency-key"

    base = build_dedupe_key(organization_id, kb_a, key)
    assert key not in base
    assert len(base) == 64
    # 同 key 不同 KB / 不同组织必须得到不同去重键，避免跨 KB 冲突与存在性探测。
    assert build_dedupe_key(organization_id, kb_b, key) != base
    assert build_dedupe_key(uuid.uuid4(), kb_a, key) != base
    assert build_dedupe_key(organization_id, kb_a, key) == base


# --- 私有内容寻址存储 ----------------------------------------------------------


def test_blob_store_publishes_at_kb_and_hash_path_without_filename(tmp_path: Path) -> None:
    store = DocumentBlobStore(tmp_path)
    kb_id = uuid.uuid4()
    content = b"# doc"

    file_ref = store.publish(kb_id, content_hash(content), content)

    assert file_ref == f"{kb_id}/{content_hash(content)}"
    target = tmp_path / file_ref
    assert target.read_bytes() == content
    # 路径只由 KB ID 与 SHA-256 派生，写入成功后不残留临时文件。
    assert not list(tmp_path.rglob("*.tmp"))


def test_blob_store_reuses_existing_blob(tmp_path: Path) -> None:
    store = DocumentBlobStore(tmp_path)
    kb_id = uuid.uuid4()
    content = b"# same"
    digest = content_hash(content)

    first = store.publish(kb_id, digest, content)
    target = tmp_path / first
    before = target.stat().st_mtime_ns

    second = store.publish(kb_id, digest, b"# same")
    assert second == first
    assert target.stat().st_mtime_ns == before


def test_blob_store_path_for_rejects_traversal(tmp_path: Path) -> None:
    store = DocumentBlobStore(tmp_path)
    for bad in ("../escape", "a/b/c", f"{uuid.uuid4()}/nothex", "no-slash"):
        with pytest.raises(InvalidBlobReference):
            store.path_for(bad)


def test_blob_publish_does_not_trust_target_with_mismatched_content(
    tmp_path: Path,
) -> None:
    """快速复用前必须校验内容摘要：目标存在但内容不符时重写为正确内容。"""

    store = DocumentBlobStore(tmp_path)
    kb_id = uuid.uuid4()
    content = b"# correct"
    digest = content_hash(content)
    target = tmp_path / store.blob_ref(kb_id, digest)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"# corrupt")

    assert store.publish(kb_id, digest, content) == store.blob_ref(kb_id, digest)
    assert target.read_bytes() == content
    assert not list(tmp_path.rglob("*.tmp"))


def test_blob_publish_recovers_when_concurrent_replace_published_same_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """os.replace 报 WinError 5 但同内容已由别的写入者发布时，承认复用且清理临时文件。"""

    store = DocumentBlobStore(tmp_path)
    kb_id = uuid.uuid4()
    content = b"# raced"
    digest = content_hash(content)
    file_ref = store.blob_ref(kb_id, digest)
    target = tmp_path / file_ref
    target.parent.mkdir(parents=True, exist_ok=True)

    def fake_replace(source: str, destination: str | os.PathLike[str]) -> None:
        # 模拟另一写入者已原子发布同内容，随后本次 replace 仍报访问拒绝。
        Path(destination).write_bytes(content)
        raise PermissionError(errno.EACCES, "Access is denied", str(destination))

    monkeypatch.setattr(os, "replace", fake_replace)

    assert store.publish(kb_id, digest, content) == file_ref
    assert target.read_bytes() == content
    assert not list(tmp_path.rglob("*.tmp"))


def test_blob_publish_reraises_replace_error_without_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """目标缺失时不得据异常伪装成功：原样抛出且不留临时文件。"""

    store = DocumentBlobStore(tmp_path)
    kb_id = uuid.uuid4()
    content = b"# missing"
    digest = content_hash(content)

    def fake_replace(source: str, destination: str | os.PathLike[str]) -> None:
        raise PermissionError(errno.EACCES, "Access is denied", str(destination))

    monkeypatch.setattr(os, "replace", fake_replace)

    with pytest.raises(PermissionError):
        store.publish(kb_id, digest, content)
    assert not (tmp_path / store.blob_ref(kb_id, digest)).exists()
    assert not list(tmp_path.rglob("*.tmp"))


def test_blob_publish_reraises_replace_error_on_content_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """目标存在但 SHA-256 不匹配时不得复用，也不删除/改动该目标。"""

    store = DocumentBlobStore(tmp_path)
    kb_id = uuid.uuid4()
    content = b"# expected"
    digest = content_hash(content)
    target = tmp_path / store.blob_ref(kb_id, digest)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"# different")

    def fake_replace(source: str, destination: str | os.PathLike[str]) -> None:
        raise PermissionError(errno.EACCES, "Access is denied", str(destination))

    monkeypatch.setattr(os, "replace", fake_replace)

    with pytest.raises(PermissionError):
        store.publish(kb_id, digest, content)
    assert target.read_bytes() == b"# different"
    assert not list(tmp_path.rglob("*.tmp"))


def test_blob_publish_never_reuses_symlink_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """目标是符号链接时，即使内容摘要吻合也不复用，且原样抛出。"""

    store = DocumentBlobStore(tmp_path)
    kb_id = uuid.uuid4()
    content = b"# symlinked"
    digest = content_hash(content)
    target = tmp_path / store.blob_ref(kb_id, digest)
    target.parent.mkdir(parents=True, exist_ok=True)
    real = tmp_path / "outside.bin"
    real.write_bytes(content)
    try:
        target.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")

    def fake_replace(source: str, destination: str | os.PathLike[str]) -> None:
        raise PermissionError(errno.EACCES, "Access is denied", str(destination))

    monkeypatch.setattr(os, "replace", fake_replace)

    with pytest.raises(PermissionError):
        store.publish(kb_id, digest, content)
    assert target.is_symlink()
    assert not list(tmp_path.rglob("*.tmp"))


def test_blob_store_concurrent_same_target_stress(tmp_path: Path) -> None:
    """真实双线程、500 轮同 KB 同内容发布：每轮都成功且无临时文件残留。"""

    store = DocumentBlobStore(tmp_path)
    content = b"# concurrent\n"
    digest = content_hash(content)
    rounds = 500
    failures: list[BaseException] = []
    lock = threading.Lock()

    for _ in range(rounds):
        kb_id = uuid.uuid4()
        expected_ref = store.blob_ref(kb_id, digest)
        barrier = threading.Barrier(2)

        def worker(
            kb_id: uuid.UUID = kb_id, expected_ref: str = expected_ref
        ) -> None:
            try:
                barrier.wait(timeout=10)
                assert store.publish(kb_id, digest, content) == expected_ref
            except BaseException as error:  # noqa: BLE001 - 压力测试需收集全部失败
                with lock:
                    failures.append(error)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not any(thread.is_alive() for thread in threads)

    assert failures == []
    assert not list(tmp_path.rglob("*.tmp"))
    for blob in tmp_path.rglob("*"):
        if blob.is_file():
            assert blob.read_bytes() == content


# --- 接收阶段体积上限 ----------------------------------------------------------


def make_request(*, headers: list[tuple[bytes, bytes]], chunks: list[bytes]) -> Request:
    scope = {"type": "http", "method": "POST", "headers": headers}
    remaining = list(chunks)

    async def receive() -> dict[str, object]:
        if remaining:
            body = remaining.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(remaining)}
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


@pytest.mark.anyio
async def test_body_limit_rejects_declared_content_length_without_reading() -> None:
    request = make_request(
        headers=[(b"content-length", str(MAX_UPLOAD_REQUEST_BYTES + 1).encode())],
        chunks=[],
    )
    with pytest.raises(ApiError) as error:
        await enforce_upload_body_limit(request)
    assert error.value.status_code == 413
    assert error.value.code == CODE_DOCUMENT_TOO_LARGE


@pytest.mark.anyio
async def test_body_limit_rejects_invalid_content_length() -> None:
    request = make_request(headers=[(b"content-length", b"abc")], chunks=[])
    with pytest.raises(ApiError) as error:
        await enforce_upload_body_limit(request)
    assert error.value.status_code == 400
    assert error.value.code == CODE_UPLOAD_MALFORMED


@pytest.mark.anyio
async def test_body_limit_counts_stream_without_content_length() -> None:
    chunk = b"x" * (1024 * 1024)
    chunks = [chunk] * ((MAX_UPLOAD_REQUEST_BYTES // len(chunk)) + 2)
    request = make_request(headers=[], chunks=chunks)

    await enforce_upload_body_limit(request)
    consumed = 0
    with pytest.raises(ApiError) as error:
        while True:
            message = await request._receive()
            consumed += len(message["body"])
            if not message["more_body"]:
                break
    assert error.value.status_code == 413
    assert error.value.code == CODE_DOCUMENT_TOO_LARGE
    # 流式计数应在超过上限时就触发，而不是等整个正文收完。
    assert consumed <= MAX_UPLOAD_REQUEST_BYTES + len(chunk)


# --- 路由契约：不得让 FastAPI 预解析 multipart ---------------------------------


def test_upload_route_has_no_fastapi_body_params() -> None:
    # 同一路径下另有 GET 列表端点；只取 POST 上传端点，检验的才是真实 upload 路由。
    routes = [
        route
        for route in router.routes
        if getattr(route, "path", "").endswith("/documents")
        and "POST" in (getattr(route, "methods", None) or ())
    ]
    assert len(routes) == 1
    dependant = getattr(routes[0], "dependant")
    # 有 body_params 时 FastAPI 会在 solve_dependencies 之前 await request.form()，
    # 未授权请求的正文会先落盘；这里必须为空，正文由处理器在鉴权后自行读取。
    assert dependant.body_params == []


def test_upload_openapi_documents_multipart_body() -> None:
    app = FastAPI()
    app.include_router(router)
    schema = app.openapi()
    operation = schema["paths"]["/api/v1/knowledge-bases/{kb_id}/documents"]["post"]
    media = operation["requestBody"]["content"]["multipart/form-data"]["schema"]
    assert set(media["required"]) == {"title", "file"}


# --- 领域错误映射 --------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (DocumentTooLarge("x"), 413, CODE_DOCUMENT_TOO_LARGE),
        (DocumentEmpty("x"), 422, CODE_DOCUMENT_EMPTY),
        (DocumentNotText("x"), 422, CODE_DOCUMENT_NOT_TEXT),
        (DocumentNotPdf("x"), 422, CODE_DOCUMENT_NOT_PDF),
        (UnsupportedDocumentType("x"), 422, CODE_UNSUPPORTED_DOCUMENT_TYPE),
        (TitleInvalid("x"), 422, CODE_DOCUMENT_TITLE_INVALID),
        (IdempotencyKeyInvalid("x"), 422, CODE_IDEMPOTENCY_KEY_INVALID),
        (IdempotencyConflict("x"), 409, CODE_IDEMPOTENCY_KEY_REUSED),
    ],
)
def test_ingestion_errors_map_to_named_status(
    error: IngestionError, status: int, code: str
) -> None:
    mapped = _ingestion_error(error)
    assert mapped.status_code == status
    assert mapped.code == code


def test_service_declares_expected_status_constants() -> None:
    assert ingestion_service.JOB_STATUS_QUEUED == "QUEUED"
    assert ingestion_service.OUTBOX_STATUS_PENDING == "PENDING"
    assert ingestion_service.DOCUMENT_LIFECYCLE_CREATED == "CREATED"
    assert ingestion_service.VERSION_STATUS_PENDING == "PENDING"
    assert ingestion_service.SOURCE_TYPE_MARKDOWN == "markdown"


@pytest.mark.anyio
async def test_insert_upload_registers_parser_implementation_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新建上传把解析器真实版本写入 version，并把 job 绑定到登记的 profile。"""

    added: list[object] = []
    profile_id = uuid.uuid4()

    class _RecordingSession:
        def add(self, instance: object) -> None:
            added.append(instance)

        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            return None

    async def fake_profile(session: AsyncSession) -> uuid.UUID:
        return profile_id

    monkeypatch.setattr(ingestion_service, "ensure_default_index_profile", fake_profile)

    outcome = await ingestion_service._insert_upload(
        cast(AsyncSession, _RecordingSession()),
        kb_id=uuid.uuid4(),
        title="t",
        file_ref="ref",
        file_hash="hash",
        dedupe_key="key",
        source_type=ingestion_service.SOURCE_TYPE_MARKDOWN,
        media_type=MARKDOWN_MEDIA_TYPE,
        parser_version=parsing.MARKDOWN_PARSER_VERSION,
    )

    assert outcome.reused is False
    versions = [item for item in added if isinstance(item, DocumentVersion)]
    assert len(versions) == 1
    assert versions[0].parser_version == "markdown-it-py-4.2.0-v1"
    assert versions[0].parser_version == parsing.MARKDOWN_PARSER_VERSION
    # 同一事务写入的 job 显式绑定刚登记/复用的 profile 行。
    jobs = [item for item in added if isinstance(item, IngestJob)]
    assert len(jobs) == 1
    assert jobs[0].profile_id == profile_id


@pytest.mark.anyio
async def test_idempotent_replay_reuses_old_job_without_rewriting_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """回放已有任务只复用旧 job，不重新插入，也不就地升级其 parser_version。

    旧行的 ``markdown-v1`` 属于历史事实，接口回放不读取也不改写 version 行，因此不能
    据此宣称旧数据已按新解析器处理完成。
    """

    content = b"# hi"
    existing = ingestion_service._ExistingJob(
        job_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        title="t",
        file_hash=content_hash(content),
        document_deleted=False,
        expected_active_version_id=None,
    )
    write_calls: list[object] = []

    async def load_existing(
        session: AsyncSession, *, dedupe_key: str, kb_id: uuid.UUID
    ) -> ingestion_service._ExistingJob:
        return existing

    async def fail_insert(session: AsyncSession, **kwargs: object) -> object:
        write_calls.append(kwargs)
        raise AssertionError("幂等回放不应重新插入入库事实")

    monkeypatch.setattr(ingestion_service, "_load_existing_job", load_existing)
    monkeypatch.setattr(ingestion_service, "_insert_upload", fail_insert)

    warm_calls: list[object] = []

    def record_warm() -> str:
        warm_calls.append(object())
        return "analyzer"

    precheck_calls: list[object] = []

    async def record_precheck(session: AsyncSession) -> None:
        precheck_calls.append(object())

    monkeypatch.setattr(
        ingestion_service, "current_keyword_analyzer_version", record_warm
    )
    monkeypatch.setattr(
        ingestion_service, "precheck_default_index_profile", record_precheck
    )

    outcome = await ingestion_service.create_markdown_document(
        cast(AsyncSession, object()),
        DocumentBlobStore(tmp_path),
        kb_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        title="t",
        content=content,
        idempotency_key="k",
    )

    assert outcome.reused is True
    assert outcome.document_id == existing.document_id
    assert outcome.version_id == existing.version_id
    assert outcome.job_id == existing.job_id
    assert write_calls == []
    # 回放只复用旧 job：不预热分析器、不预检 profile、不登记 profile、不改写旧行。
    assert warm_calls == []
    assert precheck_calls == []


@pytest.mark.anyio
async def test_new_upload_warms_analyzer_in_threadpool_before_publishing_blob(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """fsync 等阻塞 I/O 与 jieba 预热都必须在线程池执行，且预热先于 blob 发布。"""

    event_loop_thread = threading.get_ident()
    events: list[tuple[str, int]] = []

    def record_warm() -> str:
        events.append(("warm", threading.get_ident()))
        return "analyzer"

    real_publish = DocumentBlobStore.publish

    def recording_publish(
        self: DocumentBlobStore, kb_id: uuid.UUID, file_hash: str, content: bytes
    ) -> str:
        events.append(("publish", threading.get_ident()))
        return real_publish(self, kb_id, file_hash, content)

    async def no_existing_job(
        session: AsyncSession, *, dedupe_key: str, kb_id: uuid.UUID
    ) -> None:
        return None

    async def fake_insert(
        session: AsyncSession, **kwargs: object
    ) -> ingestion_service.UploadOutcome:
        return ingestion_service.UploadOutcome(
            document_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            job_id=uuid.uuid4(),
            reused=False,
        )

    async def record_precheck(session: AsyncSession) -> None:
        events.append(("precheck", threading.get_ident()))

    class _RollbackRecordingSession:
        rollback_calls = 0

        async def rollback(self) -> None:
            self.rollback_calls += 1

    session = _RollbackRecordingSession()
    monkeypatch.setattr(ingestion_service, "current_keyword_analyzer_version", record_warm)
    monkeypatch.setattr(ingestion_service, "precheck_default_index_profile", record_precheck)
    monkeypatch.setattr(DocumentBlobStore, "publish", recording_publish)
    monkeypatch.setattr(ingestion_service, "_load_existing_job", no_existing_job)
    monkeypatch.setattr(ingestion_service, "_insert_upload", fake_insert)

    await ingestion_service.create_markdown_document(
        cast(AsyncSession, session),
        DocumentBlobStore(tmp_path),
        kb_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        title="t",
        content=b"# hi",
        idempotency_key="k",
    )

    labels = [label for label, _ in events]
    assert labels == ["warm", "precheck", "publish"]
    # 预热与发布都不在事件循环线程执行；幂等只读事务与预检只读事务各被释放一次。
    assert all(
        thread != event_loop_thread for label, thread in events if label != "precheck"
    )
    assert session.rollback_calls == 2


@pytest.mark.anyio
async def test_warmup_failure_leaves_no_blob_or_db_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """jieba 预热失败必须 fail closed：不 publish blob、不进写事务。"""

    publish_calls: list[object] = []

    def fail_warm() -> str:
        raise KeywordAnalyzerError("关键词分析器临时缓存目录不可用")

    def recording_publish(
        self: DocumentBlobStore, kb_id: uuid.UUID, file_hash: str, content: bytes
    ) -> str:
        publish_calls.append((kb_id, file_hash))
        return "unused"

    async def no_existing_job(
        session: AsyncSession, *, dedupe_key: str, kb_id: uuid.UUID
    ) -> None:
        return None

    async def fail_insert(
        session: AsyncSession, **kwargs: object
    ) -> ingestion_service.UploadOutcome:
        raise AssertionError("预热失败不应进入写事务")

    class _RollbackOnlySession:
        rollback_calls = 0

        async def rollback(self) -> None:
            self.rollback_calls += 1

    session = _RollbackOnlySession()
    monkeypatch.setattr(ingestion_service, "_load_existing_job", no_existing_job)
    monkeypatch.setattr(ingestion_service, "current_keyword_analyzer_version", fail_warm)
    monkeypatch.setattr(DocumentBlobStore, "publish", recording_publish)
    monkeypatch.setattr(ingestion_service, "_insert_upload", fail_insert)

    with pytest.raises(KeywordAnalyzerError):
        await ingestion_service.create_markdown_document(
            cast(AsyncSession, session),
            DocumentBlobStore(tmp_path),
            kb_id=uuid.uuid4(),
            organization_id=uuid.uuid4(),
            title="t",
            content=b"# hi",
            idempotency_key="k",
        )

    assert publish_calls == []
    assert not list(tmp_path.rglob("*"))


# --- 写入事务的异常范围收窄与孤儿 blob 窗口 ------------------------------------


class _FakeDiagnostic:
    """psycopg ``diag`` 替身：只提供 ``constraint_name``。"""

    def __init__(self, constraint_name: str | None) -> None:
        self.constraint_name = constraint_name


class _FakePgError(Exception):
    """psycopg 错误替身：只提供 ``sqlstate`` 与 ``diag``。"""

    def __init__(self, *, sqlstate: str, constraint_name: str | None) -> None:
        super().__init__("synthetic psycopg error")
        self.sqlstate = sqlstate
        self.diag = (
            _FakeDiagnostic(constraint_name) if constraint_name is not None else None
        )


class _IntegritySession:
    """四表写入在首次 flush 即抛预设 ``IntegrityError`` 的 session 替身。"""

    def __init__(self, error: IntegrityError) -> None:
        self._error = error
        self.rollback_calls = 0

    def add(self, instance: object) -> None:
        return None

    async def flush(self) -> None:
        raise self._error

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        self.rollback_calls += 1


def _synthetic_integrity_error(
    *, sqlstate: str, constraint_name: str | None
) -> IntegrityError:
    return IntegrityError(
        "INSERT INTO ingest_job (...) VALUES (...)",
        {},
        _FakePgError(sqlstate=sqlstate, constraint_name=constraint_name),
    )


@pytest.mark.anyio
async def test_profile_precheck_conflict_fails_before_publish(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """同 config_hash 字段篡改时，冲突必须在 publish 之前发生：存储零调用。"""

    publish_calls: list[object] = []

    def recording_publish(
        self: DocumentBlobStore, kb_id: uuid.UUID, file_hash: str, content: bytes
    ) -> str:
        publish_calls.append((kb_id, file_hash))
        return "unused"

    async def no_existing_job(
        session: AsyncSession, *, dedupe_key: str, kb_id: uuid.UUID
    ) -> None:
        return None

    async def conflict_precheck(session: AsyncSession) -> None:
        raise IndexProfileConflictError("同 config_hash 的行字段不一致：embedding_model")

    async def fail_insert(
        session: AsyncSession, **kwargs: object
    ) -> ingestion_service.UploadOutcome:
        raise AssertionError("预检失败不应进入写事务")

    class _RollbackOnlySession:
        rollback_calls = 0

        async def rollback(self) -> None:
            self.rollback_calls += 1

    session = _RollbackOnlySession()
    monkeypatch.setattr(ingestion_service, "_load_existing_job", no_existing_job)
    monkeypatch.setattr(
        ingestion_service, "precheck_default_index_profile", conflict_precheck
    )
    monkeypatch.setattr(DocumentBlobStore, "publish", recording_publish)
    monkeypatch.setattr(ingestion_service, "_insert_upload", fail_insert)

    with pytest.raises(IndexProfileConflictError):
        await ingestion_service.create_markdown_document(
            cast(AsyncSession, session),
            DocumentBlobStore(tmp_path),
            kb_id=uuid.uuid4(),
            organization_id=uuid.uuid4(),
            title="t",
            content=b"# hi",
            idempotency_key="k",
        )

    assert publish_calls == []
    assert not list(tmp_path.rglob("*"))
    # 一次释放幂等只读事务，一次在预检 finally 释放；冲突不进入写事务。
    assert session.rollback_calls == 2


@pytest.mark.anyio
async def test_insert_upload_reuses_job_on_dedupe_key_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只有 ``uq_ingest_job_dedupe_key`` 的 23505 冲突才回滚并复用现有 job。"""

    existing = ingestion_service._ExistingJob(
        job_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        title="t",
        file_hash="hash",
        document_deleted=False,
        expected_active_version_id=None,
    )
    load_calls: list[str] = []

    async def load_existing(
        session: AsyncSession, *, dedupe_key: str, kb_id: uuid.UUID
    ) -> ingestion_service._ExistingJob:
        load_calls.append(dedupe_key)
        return existing

    async def fake_profile(session: AsyncSession) -> uuid.UUID:
        return uuid.uuid4()

    monkeypatch.setattr(ingestion_service, "_load_existing_job", load_existing)
    monkeypatch.setattr(ingestion_service, "ensure_default_index_profile", fake_profile)

    session = _IntegritySession(
        _synthetic_integrity_error(
            sqlstate="23505", constraint_name="uq_ingest_job_dedupe_key"
        )
    )
    outcome = await ingestion_service._insert_upload(
        cast(AsyncSession, session),
        kb_id=uuid.uuid4(),
        title="t",
        file_ref="ref",
        file_hash="hash",
        dedupe_key="key",
        source_type=ingestion_service.SOURCE_TYPE_MARKDOWN,
        media_type=MARKDOWN_MEDIA_TYPE,
        parser_version=parsing.MARKDOWN_PARSER_VERSION,
    )

    assert outcome.reused is True
    assert outcome.job_id == existing.job_id
    assert session.rollback_calls == 1
    assert load_calls == ["key"]


@pytest.mark.parametrize(
    ("sqlstate", "constraint_name"),
    [
        ("23503", "uq_ingest_job_dedupe_key"),
        ("23505", "uq_other_constraint"),
        ("42000", None),
    ],
)
@pytest.mark.anyio
async def test_insert_upload_reraises_non_dedupe_integrity_error(
    monkeypatch: pytest.MonkeyPatch, sqlstate: str, constraint_name: str | None
) -> None:
    """其它约束或非 23505 的完整性错误必须原样重抛，不得回读现有 job。"""

    load_calls: list[object] = []

    async def load_existing(
        session: AsyncSession, *, dedupe_key: str, kb_id: uuid.UUID
    ) -> None:
        load_calls.append(object())

    async def fake_profile(session: AsyncSession) -> uuid.UUID:
        return uuid.uuid4()

    monkeypatch.setattr(ingestion_service, "_load_existing_job", load_existing)
    monkeypatch.setattr(ingestion_service, "ensure_default_index_profile", fake_profile)

    error = _synthetic_integrity_error(
        sqlstate=sqlstate, constraint_name=constraint_name
    )
    session = _IntegritySession(error)
    with pytest.raises(IntegrityError) as raised:
        await ingestion_service._insert_upload(
            cast(AsyncSession, session),
            kb_id=uuid.uuid4(),
            title="t",
            file_ref="ref",
            file_hash="hash",
            dedupe_key="key",
            source_type=ingestion_service.SOURCE_TYPE_MARKDOWN,
            media_type=MARKDOWN_MEDIA_TYPE,
            parser_version=parsing.MARKDOWN_PARSER_VERSION,
        )

    assert raised.value is error
    assert session.rollback_calls == 1
    assert load_calls == []


@pytest.mark.anyio
async def test_published_blob_survives_db_failure_without_unlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """blob 已 publish 后写事务失败：不尝试删除（可能被并发共享），只留孤儿窗口。"""

    content = b"# orphan"
    kb_id = uuid.uuid4()
    digest = content_hash(content)
    target = tmp_path / f"{kb_id}/{digest}"

    async def no_existing_job(
        session: AsyncSession, *, dedupe_key: str, kb_id: uuid.UUID
    ) -> None:
        return None

    async def precheck_ok(session: AsyncSession) -> None:
        return None

    async def fake_profile(session: AsyncSession) -> uuid.UUID:
        return uuid.uuid4()

    monkeypatch.setattr(ingestion_service, "_load_existing_job", no_existing_job)
    monkeypatch.setattr(ingestion_service, "precheck_default_index_profile", precheck_ok)
    monkeypatch.setattr(ingestion_service, "ensure_default_index_profile", fake_profile)

    session = _IntegritySession(
        _synthetic_integrity_error(sqlstate="23503", constraint_name="fk_other")
    )
    with pytest.raises(IntegrityError):
        await ingestion_service.create_markdown_document(
            cast(AsyncSession, session),
            DocumentBlobStore(tmp_path),
            kb_id=kb_id,
            organization_id=uuid.uuid4(),
            title="t",
            content=content,
            idempotency_key="k",
        )

    assert target.read_bytes() == content
    assert not list(tmp_path.rglob("*.tmp"))


# --- 实际 ASGI 请求：multipart 解析错误映射 ------------------------------------

UPLOAD_ORIGIN = "http://127.0.0.1"
UPLOAD_KB_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
UPLOAD_DOCUMENT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
UPLOAD_BOUNDARY = "----unitupload"


def upload_settings() -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "session_cookie_secure": False,
        "allowed_origins": UPLOAD_ORIGIN,
        "csrf_secret": "unit-upload-csrf-secret",
    }
    return Settings(**values)


def build_upload_app() -> Any:
    app = create_app(upload_settings())

    async def fake_editor() -> KbAccess:
        return KbAccess(
            kb_id=UPLOAD_KB_ID,
            name="unit",
            role=KbRole.EDITOR,
            organization_id=DEFAULT_ORGANIZATION_ID,
            acl_revision=1,
            kb_revision=1,
        )

    async def fake_csrf() -> None:
        return None

    async def fake_session() -> AsyncIterator[None]:
        yield None

    app.dependency_overrides[require_editor] = fake_editor
    app.dependency_overrides[require_csrf] = fake_csrf
    app.dependency_overrides[get_database_session] = fake_session
    return app


def build_version_app() -> Any:
    app = create_app(upload_settings())

    async def fake_editor() -> DocumentAccess:
        return DocumentAccess(
            document_id=UPLOAD_DOCUMENT_ID,
            kb_id=UPLOAD_KB_ID,
            organization_id=DEFAULT_ORGANIZATION_ID,
            role=KbRole.EDITOR,
        )

    async def fake_csrf() -> None:
        return None

    async def fake_session() -> AsyncIterator[None]:
        yield None

    app.dependency_overrides[require_document_editor] = fake_editor
    app.dependency_overrides[require_csrf] = fake_csrf
    app.dependency_overrides[get_database_session] = fake_session
    return app


def multipart_part(name: str, *, filename: str | None = None, data: bytes = b"") -> bytes:
    disposition = f'form-data; name="{name}"'
    if filename is not None:
        disposition += f'; filename="{filename}"'
    header = (
        f"--{UPLOAD_BOUNDARY}\r\nContent-Disposition: {disposition}\r\n\r\n"
    ).encode()
    return header + data + b"\r\n"


def multipart_body(*parts: bytes) -> bytes:
    return b"".join(parts) + f"--{UPLOAD_BOUNDARY}--\r\n".encode()


async def post_upload(app: Any, *, content_type: str, body: bytes) -> Any:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=UPLOAD_ORIGIN
    ) as client:
        return await client.post(
            f"/api/v1/knowledge-bases/{UPLOAD_KB_ID}/documents",
            content=body,
            headers={
                "Content-Type": content_type,
                "Origin": UPLOAD_ORIGIN,
                "Idempotency-Key": "unit-upload-key",
            },
        )


@pytest.mark.anyio
async def test_upload_maps_malformed_multipart_body_to_422() -> None:
    """错 boundary / 畸形正文：python-multipart 抛解析错误，不得变成 500。"""

    app = build_upload_app()
    response = await post_upload(
        app,
        content_type=f"multipart/form-data; boundary={UPLOAD_BOUNDARY}",
        body=b"not a multipart body at all",
    )

    assert response.status_code == 422
    payload = response.json()
    assert payload["code"] == CODE_UPLOAD_MALFORMED
    assert payload["message"] == UPLOAD_MALFORMED_MESSAGE
    assert "Expected boundary" not in response.text


@pytest.mark.anyio
async def test_upload_maps_missing_multipart_boundary_to_422() -> None:
    """缺少 boundary 参数：Starlette 会包成 400 HTTPException，应映射为 422。"""

    app = build_upload_app()
    response = await post_upload(
        app,
        content_type="multipart/form-data",
        body=multipart_body(multipart_part("title", data=b"t")),
    )

    assert response.status_code == 422
    payload = response.json()
    assert payload["code"] == CODE_UPLOAD_MALFORMED
    assert payload["message"] == UPLOAD_MALFORMED_MESSAGE
    assert "Missing boundary" not in response.text


@pytest.mark.anyio
async def test_upload_maps_extra_file_part_to_422() -> None:
    """超过 max_files=1 的第二个 file part 映射为 422，且不回显解析器消息。"""

    app = build_upload_app()
    body = multipart_body(
        multipart_part("title", data=b"t"),
        multipart_part("file", filename="a.md", data=b"# a"),
        multipart_part("file", filename="b.md", data=b"# b"),
    )
    response = await post_upload(
        app,
        content_type=f"multipart/form-data; boundary={UPLOAD_BOUNDARY}",
        body=body,
    )

    assert response.status_code == 422
    payload = response.json()
    assert payload["code"] == CODE_UPLOAD_MALFORMED
    assert payload["message"] == UPLOAD_MALFORMED_MESSAGE
    assert "Too many files" not in response.text


@pytest.mark.anyio
async def test_upload_maps_oversize_title_field_to_413() -> None:
    """非文件 title 字段超过 max_part_size 映射为 413，且不回显解析器消息。"""

    app = build_upload_app()
    body = multipart_body(
        multipart_part("title", data=b"a" * (MAX_MARKDOWN_BYTES + 1))
    )
    response = await post_upload(
        app,
        content_type=f"multipart/form-data; boundary={UPLOAD_BOUNDARY}",
        body=body,
    )

    assert response.status_code == 413
    payload = response.json()
    assert payload["code"] == CODE_DOCUMENT_TOO_LARGE
    assert payload["message"] == UPLOAD_TOO_LARGE_MESSAGE
    assert "19531" not in response.text


@pytest.mark.anyio
async def test_upload_profile_conflict_fails_closed_with_static_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """profile 登记失败必须 fail closed 返回静态 500，不回显内部字段或值。"""

    async def fail_service(*args: object, **kwargs: object) -> object:
        raise IndexProfileConflictError(
            "同 config_hash 的 index_profile 行与默认契约字段不一致：embedding_model"
        )

    monkeypatch.setattr(ingestion_service, "create_markdown_document", fail_service)

    app = build_upload_app()
    body = multipart_body(
        multipart_part("title", data="标题".encode()),
        multipart_part("file", filename="a.md", data=b"# a"),
    )
    # 应用内 500 由 ServerErrorMiddleware 处理后会重新抛出，需关闭 re-raise 才能断言错误体。
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url=UPLOAD_ORIGIN,
    ) as client:
        response = await client.post(
            f"/api/v1/knowledge-bases/{UPLOAD_KB_ID}/documents",
            content=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={UPLOAD_BOUNDARY}",
                "Origin": UPLOAD_ORIGIN,
                "Idempotency-Key": "unit-upload-key",
            },
        )

    assert response.status_code == 500
    payload = response.json()
    assert payload["code"] == CODE_INTERNAL_ERROR
    assert payload["message"] == "服务器内部错误"
    assert "embedding_model" not in response.text
    assert "config_hash" not in response.text


@pytest.mark.anyio
async def test_upload_pdf_dispatches_to_pdf_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """.pdf 后缀分派到 create_pdf_document，而不是 Markdown 路径。"""

    calls: list[str] = []

    async def fake_pdf(*args: object, **kwargs: object) -> object:
        calls.append("pdf")
        return ingestion_service.UploadOutcome(
            document_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            job_id=uuid.uuid4(),
            reused=False,
        )

    async def forbidden_markdown(*args: object, **kwargs: object) -> object:
        raise AssertionError("PDF 上传不得走 Markdown 分支")

    monkeypatch.setattr(ingestion_service, "create_pdf_document", fake_pdf)
    monkeypatch.setattr(ingestion_service, "create_markdown_document", forbidden_markdown)

    app = build_upload_app()
    body = multipart_body(
        multipart_part("title", data="报告".encode()),
        multipart_part("file", filename="report.pdf", data=b"%PDF-1.4 minimal"),
    )
    response = await post_upload(
        app,
        content_type=f"multipart/form-data; boundary={UPLOAD_BOUNDARY}",
        body=body,
    )

    assert response.status_code == 202
    assert calls == ["pdf"]


@pytest.mark.anyio
async def test_upload_docx_dispatches_to_docx_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """.docx 后缀分派到 create_docx_document，而不是 Markdown/PDF 路径。"""

    from docx_samples import positive_simple_table

    calls: list[str] = []

    async def fake_docx(*args: object, **kwargs: object) -> object:
        calls.append("docx")
        return ingestion_service.UploadOutcome(
            document_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            job_id=uuid.uuid4(),
            reused=False,
        )

    async def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("DOCX 上传不得走 Markdown/PDF 分支")

    monkeypatch.setattr(ingestion_service, "create_docx_document", fake_docx)
    monkeypatch.setattr(ingestion_service, "create_markdown_document", forbidden)
    monkeypatch.setattr(ingestion_service, "create_pdf_document", forbidden)

    app = build_upload_app()
    body = multipart_body(
        multipart_part("title", data="指南".encode()),
        multipart_part("file", filename="guide.docx", data=positive_simple_table()),
    )
    response = await post_upload(
        app,
        content_type=f"multipart/form-data; boundary={UPLOAD_BOUNDARY}",
        body=body,
    )

    assert response.status_code == 202, response.text
    assert calls == ["docx"]


@pytest.mark.anyio
async def test_docx_version_dispatches_to_docx_version_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """.docx 新版本分派到 create_docx_version，而不是 Markdown/PDF 分支。"""

    from docx_samples import positive_simple_table

    calls: list[str] = []

    async def fake_docx_version(*args: object, **kwargs: object) -> object:
        calls.append("docx_version")
        return ingestion_service.UploadOutcome(
            document_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            job_id=uuid.uuid4(),
            reused=False,
        )

    async def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("DOCX 新版本不得走 Markdown/PDF 分支")

    monkeypatch.setattr(ingestion_service, "create_docx_version", fake_docx_version)
    monkeypatch.setattr(ingestion_service, "create_markdown_version", forbidden)
    monkeypatch.setattr(ingestion_service, "create_pdf_version", forbidden)

    app = build_version_app()
    body = multipart_body(
        multipart_part("title", data="指南".encode()),
        multipart_part("expectedVersionId", data=str(UPLOAD_DOCUMENT_ID).encode()),
        multipart_part("file", filename="guide.docx", data=positive_simple_table()),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=UPLOAD_ORIGIN
    ) as client:
        response = await client.post(
            f"/api/v1/documents/{UPLOAD_DOCUMENT_ID}/versions",
            content=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={UPLOAD_BOUNDARY}",
                "Origin": UPLOAD_ORIGIN,
                "Idempotency-Key": "unit-version-key",
            },
        )

    assert response.status_code == 202, response.text
    assert calls == ["docx_version"]


@pytest.mark.anyio
async def test_upload_pdf_without_magic_is_rejected_with_422() -> None:
    """声明 .pdf 但内容无 PDF 魔数：受理期 fail closed，返回 422。"""

    app = build_upload_app()
    body = multipart_body(
        multipart_part("title", data="报告".encode()),
        multipart_part("file", filename="report.pdf", data=b"plain text not pdf"),
    )
    response = await post_upload(
        app,
        content_type=f"multipart/form-data; boundary={UPLOAD_BOUNDARY}",
        body=body,
    )

    assert response.status_code == 422
    assert response.json()["code"] == CODE_DOCUMENT_NOT_PDF
