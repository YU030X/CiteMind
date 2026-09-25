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
    require_editor,
    router,
)
from rag_backend.api.errors import (
    CODE_DOCUMENT_EMPTY,
    CODE_DOCUMENT_NOT_TEXT,
    CODE_DOCUMENT_TITLE_INVALID,
    CODE_DOCUMENT_TOO_LARGE,
    CODE_IDEMPOTENCY_KEY_INVALID,
    CODE_IDEMPOTENCY_KEY_REUSED,
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
    DocumentNotText,
    DocumentTooLarge,
    IdempotencyConflict,
    IdempotencyKeyInvalid,
    IngestionError,
    TitleInvalid,
    UnsupportedDocumentType,
)
from rag_backend.ingestion.storage import (
    DocumentBlobStore,
    InvalidBlobReference,
    content_hash,
)
from rag_backend.ingestion.validation import (
    MAX_IDEMPOTENCY_KEY_LENGTH,
    MAX_MARKDOWN_BYTES,
    build_dedupe_key,
    decode_markdown_content,
    normalize_idempotency_key,
    normalize_title,
    validate_markdown_filename,
)
from rag_backend.knowledge.roles import KbRole
from rag_backend.knowledge.service import KbAccess
from rag_backend.models.knowledge import DocumentVersion
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
    routes = [
        route
        for route in router.routes
        if getattr(route, "path", "").endswith("/documents")
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
async def test_insert_upload_registers_parser_implementation_version() -> None:
    """新建上传把 ``document_version.parser_version`` 写成解析器真实版本。"""

    added: list[object] = []

    class _RecordingSession:
        def add(self, instance: object) -> None:
            added.append(instance)

        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            return None

    outcome = await ingestion_service._insert_upload(
        cast(AsyncSession, _RecordingSession()),
        kb_id=uuid.uuid4(),
        title="t",
        file_ref="ref",
        file_hash="hash",
        dedupe_key="key",
    )

    assert outcome.reused is False
    versions = [item for item in added if isinstance(item, DocumentVersion)]
    assert len(versions) == 1
    assert versions[0].parser_version == "markdown-it-py-4.2.0-v1"
    assert versions[0].parser_version == parsing.MARKDOWN_PARSER_VERSION


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


@pytest.mark.anyio
async def test_blob_publish_runs_off_the_event_loop_thread(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """fsync 等阻塞 I/O 必须在线程池执行，不能阻塞异步路由的事件循环线程。"""

    event_loop_thread = threading.get_ident()
    publish_threads: list[int] = []
    real_publish = DocumentBlobStore.publish

    def recording_publish(
        self: DocumentBlobStore, kb_id: uuid.UUID, file_hash: str, content: bytes
    ) -> str:
        publish_threads.append(threading.get_ident())
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

    monkeypatch.setattr(DocumentBlobStore, "publish", recording_publish)
    monkeypatch.setattr(ingestion_service, "_load_existing_job", no_existing_job)
    monkeypatch.setattr(ingestion_service, "_insert_upload", fake_insert)

    await ingestion_service.create_markdown_document(
        cast(AsyncSession, object()),
        DocumentBlobStore(tmp_path),
        kb_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        title="t",
        content=b"# hi",
        idempotency_key="k",
    )

    assert publish_threads
    assert publish_threads[0] != event_loop_thread


# --- 实际 ASGI 请求：multipart 解析错误映射 ------------------------------------

UPLOAD_ORIGIN = "http://127.0.0.1"
UPLOAD_KB_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
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
