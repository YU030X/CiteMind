"""数据库异常后的终态收敛测试：一次性故障、commit 结果未知、数据库持续不可用。

用假 session factory 与假依赖驱动，不连接数据库、Redis、inference 或模型资产。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from typing import Any, cast

import pytest
from rag_backend.database import SyncSessionFactory
from rag_backend.ingestion import indexing_worker as iw
from rag_backend.ingestion.identity_preflight import ProfileIdentityDecision
from rag_backend.ingestion.parse_subprocess import (
    ParseSubprocessError,
    ParseSubprocessTimeout,
    PdfEncryptedSubprocessError,
    PdfInvalidSubprocessError,
    PdfTooManyPagesSubprocessError,
)
from rag_backend.ingestion.parsing import ParsedDocument, parse_markdown
from rag_backend.ingestion.storage import DocumentBlobStore
from rag_backend.models.profile_contract import IndexProfileContract
from sqlalchemy.exc import OperationalError

PROFILE = IndexProfileContract(
    embedding_model="test/model",
    model_revision="test-revision",
    dimension=512,
    normalize=True,
    tokenizer_revision="test-tokenizer",
    chunker_version="heading-pack-v1",
    keyword_analyzer_version="test-analyzer",
)


def db_error() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception("db down"))


class FakeCounter:
    def count_tokens(self, text: str) -> int:
        return max(1, len(text))


class FakeAnalyzer:
    def analyze(self, text: str) -> str:
        return " ".join(text.split())


class FakeIdentity:
    profile = PROFILE
    parser_version = "markdown-it-py-4.2.0-v1"
    pdf_parser_version = "pypdf-6.19.0-v1"
    docx_parser_version = "python-docx-1.2.0-v1"
    token_counter = FakeCounter()
    keyword_analyzer = FakeAnalyzer()


class FakeEmbedder:
    def embed_document_texts(self, texts: Any) -> list[list[float]]:
        return [[0.0] * 512 for _ in texts]

    def close(self) -> None:
        return None


class FakeStorage:
    def read_verified_markdown(
        self, kb_id: uuid.UUID, file_ref: str, file_hash: str
    ) -> str:
        return MARKDOWN_TEXT

    def read_verified_pdf(
        self, kb_id: uuid.UUID, file_ref: str, file_hash: str
    ) -> bytes:
        return b"%PDF-1.4"


MARKDOWN_TEXT = "# 标题\n\n正文段落。\n"
MARKDOWN_SHA256 = hashlib.sha256(MARKDOWN_TEXT.encode("utf-8")).hexdigest()
PDF_TEXT = "PDF page text\n"
PDF_SHA256 = hashlib.sha256(PDF_TEXT.encode("utf-8")).hexdigest()
PDF_PARSER_VERSION = "pypdf-6.19.0-v1"


def dummy_session_factory() -> Any:
    raise AssertionError("本测试不应真正打开业务会话")


def build_dependencies(
    parse_document: iw.ParseDocument | None = None,
    parse_pdf_document: iw.ParseDocument | None = None,
) -> iw.PipelineDependencies:
    resolved_parse = parse_document or (lambda data: parse_markdown(data))
    return iw.PipelineDependencies(
        session_factory=cast(SyncSessionFactory, dummy_session_factory),
        storage=cast(DocumentBlobStore, FakeStorage()),
        identity_provider=lambda: FakeIdentity(),
        embedder_factory=lambda counter: FakeEmbedder(),
        parse_document=resolved_parse,
        parse_pdf_document=parse_pdf_document or resolved_parse,
    )


def claimed_job(*, source_type: str = "markdown") -> iw.ClaimedJob:
    if source_type == "pdf":
        parser_version = PDF_PARSER_VERSION
        file_hash = PDF_SHA256
    else:
        parser_version = "markdown-it-py-4.2.0-v1"
        file_hash = MARKDOWN_SHA256
    return iw.ClaimedJob(
        job_id=uuid.uuid4(),
        lease_token="lease",
        document_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        kb_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        profile_id=uuid.uuid4(),
        parser_version=parser_version,
        source_type=source_type,
        file_ref="kb/hash",
        file_hash=file_hash,
    )


def patch_pipeline(monkeypatch: pytest.MonkeyPatch, claimed: iw.ClaimedJob) -> None:
    monkeypatch.setattr(
        iw, "claim_ingest_job", lambda *a, **k: iw.ClaimResult(iw.PROCESS_STATUS_CLAIMED, claimed)
    )
    monkeypatch.setattr(iw, "advance_ingest_stage", lambda *a, **k: True)
    monkeypatch.setattr(iw, "load_stored_profile", lambda *a, **k: None)
    monkeypatch.setattr(
        iw, "decide_profile_identity", lambda **k: ProfileIdentityDecision.ALLOWED
    )


def test_process_fails_statically_when_staging_db_error_and_terminal_state_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed = claimed_job()
    patch_pipeline(monkeypatch, claimed)

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise db_error()

    monkeypatch.setattr(iw, "create_staging_generation", explode)
    monkeypatch.setattr(
        iw, "resolve_after_db_error", lambda *a, **k: iw.ResolveOutcome.FAILED_MARKED
    )

    status = iw.process_ingest_event(build_dependencies(), job_id=claimed.job_id, event_id="e")

    assert status == iw.PROCESS_STATUS_FAILED


def test_process_keeps_ready_when_commit_outcome_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    claimed = claimed_job()
    patch_pipeline(monkeypatch, claimed)
    monkeypatch.setattr(iw, "create_staging_generation", lambda *a, **k: uuid.uuid4())

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise db_error()

    monkeypatch.setattr(iw, "publish_ingest_generation", explode)
    monkeypatch.setattr(iw, "resolve_after_db_error", lambda *a, **k: iw.ResolveOutcome.READY)

    status = iw.process_ingest_event(build_dependencies(), job_id=claimed.job_id, event_id="e")

    assert status == iw.PROCESS_STATUS_READY


def test_process_reports_persist_unconfirmed_when_db_stays_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed = claimed_job()
    patch_pipeline(monkeypatch, claimed)

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise db_error()

    monkeypatch.setattr(iw, "create_staging_generation", explode)
    monkeypatch.setattr(iw, "resolve_after_db_error", explode)

    status = iw.process_ingest_event(build_dependencies(), job_id=claimed.job_id, event_id="e")

    assert status == iw.PROCESS_STATUS_PERSIST_UNCONFIRMED


class _MappingResult:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def mappings(self) -> _MappingResult:
        return self

    def first(self) -> dict[str, Any] | None:
        return self._row


class _StateSession:
    def __init__(self, row: dict[str, Any] | None, *, raises: bool) -> None:
        self._row = row
        self._raises = raises
        self.rolled_back = False

    def execute(self, statement: Any, parameters: Any = None) -> _MappingResult:
        if self._raises:
            raise db_error()
        return _MappingResult(self._row)

    def rollback(self) -> None:
        self.rolled_back = True

    def __enter__(self) -> _StateSession:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def state_factory(
    row: dict[str, Any] | None, *, raises: bool = False
) -> SyncSessionFactory:
    return cast(SyncSessionFactory, lambda: _StateSession(row, raises=raises))


def _record_fail(called: list[str], *args: Any, **kwargs: Any) -> bool:
    called.append("fail")
    return True


def test_resolve_after_db_error_ready_is_not_downgraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []
    monkeypatch.setattr(
        iw, "fail_ingest_job", lambda *a, **k: _record_fail(called, *a, **k)
    )

    outcome = iw.resolve_after_db_error(
        state_factory({"status": "READY", "lease_token": "lease", "lease_valid": True}),
        job_id=uuid.uuid4(),
        lease_token="lease",
    )

    assert outcome is iw.ResolveOutcome.READY
    assert called == []


def test_resolve_after_db_error_marks_failed_only_while_holding_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(iw, "fail_ingest_job", lambda *a, **k: True)

    outcome = iw.resolve_after_db_error(
        state_factory({"status": "INDEXING", "lease_token": "lease", "lease_valid": True}),
        job_id=uuid.uuid4(),
        lease_token="lease",
    )

    assert outcome is iw.ResolveOutcome.FAILED_MARKED


@pytest.mark.parametrize(
    "row",
    [
        {"status": "INDEXING", "lease_token": "other", "lease_valid": True},
        {"status": "INDEXING", "lease_token": "lease", "lease_valid": False},
        {"status": "FAILED", "lease_token": "lease", "lease_valid": True},
    ],
    ids=["other-lease", "expired", "not-active"],
)
def test_resolve_after_db_error_does_not_touch_other_owner(
    monkeypatch: pytest.MonkeyPatch, row: dict[str, Any]
) -> None:
    called: list[str] = []
    monkeypatch.setattr(
        iw, "fail_ingest_job", lambda *a, **k: _record_fail(called, *a, **k)
    )

    outcome = iw.resolve_after_db_error(
        state_factory(row), job_id=uuid.uuid4(), lease_token="lease"
    )

    assert outcome is iw.ResolveOutcome.LEASE_LOST
    assert called == []


def test_resolve_after_db_error_missing_row_is_unknown() -> None:
    outcome = iw.resolve_after_db_error(
        state_factory(None), job_id=uuid.uuid4(), lease_token="lease"
    )
    assert outcome is iw.ResolveOutcome.UNKNOWN


def test_resolve_after_db_error_propagates_persistent_failure() -> None:
    with pytest.raises(OperationalError):
        iw.resolve_after_db_error(
            state_factory(None, raises=True), job_id=uuid.uuid4(), lease_token="lease"
        )


class _LostHeartbeat:
    lost = True


def test_recover_from_db_error_returns_lease_lost_when_heartbeat_lost() -> None:
    status = iw.recover_from_db_error(
        state_factory(None),
        cast(iw.LeaseHeartbeat, _LostHeartbeat()),
        job_id=uuid.uuid4(),
        lease_token="lease",
    )
    assert status == iw.PROCESS_STATUS_LEASE_LOST


def run_parse_failure(
    monkeypatch: pytest.MonkeyPatch,
    parse_document: iw.ParseDocument,
    *,
    expected_code: str,
) -> None:
    """驱动管线在解析阶段失败，断言静态错误码且绝不进入暂存/发布。"""

    claimed = claimed_job()
    patch_pipeline(monkeypatch, claimed)
    captured: list[str] = []

    def record_fail(
        session_factory: Any,
        *,
        job_id: Any,
        lease_token: Any,
        error_code: str,
        generation_id: Any = None,
    ) -> bool:
        captured.append(error_code)
        return True

    monkeypatch.setattr(iw, "fail_ingest_job", record_fail)

    def not_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("解析失败不得 embed/stage/publish")

    monkeypatch.setattr(iw, "create_staging_generation", not_called)
    monkeypatch.setattr(iw, "publish_ingest_generation", not_called)

    status = iw.process_ingest_event(
        build_dependencies(parse_document=parse_document),
        job_id=claimed.job_id,
        event_id="e",
    )

    assert status == iw.PROCESS_STATUS_FAILED
    assert captured == [expected_code]


def test_pipeline_parse_timeout_maps_to_static_failure_without_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(data: bytes) -> Any:
        raise ParseSubprocessTimeout("timeout")

    run_parse_failure(
        monkeypatch, timeout, expected_code=iw.ERROR_PIPELINE_PARSE_TIMEOUT
    )


def test_pipeline_parse_error_maps_to_static_failure_without_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failure(data: bytes) -> Any:
        raise ParseSubprocessError("bad")

    run_parse_failure(
        monkeypatch, failure, expected_code=iw.ERROR_PIPELINE_PARSE_FAILED
    )


def test_pipeline_parser_version_mismatch_fails_before_chunking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mismatched(data: bytes) -> Any:
        return replace(parse_markdown(data), parser_version="markdown-it-py-9.9.9-v0")

    run_parse_failure(
        monkeypatch, mismatched, expected_code=iw.ERROR_PIPELINE_PARSE_FAILED
    )


def _run_pdf_failure(
    monkeypatch: pytest.MonkeyPatch,
    parse_pdf_document: iw.ParseDocument,
    *,
    expected_code: str,
) -> None:
    """驱动 PDF 管线在解析阶段失败，断言具名静态错误码且不进入暂存/发布。"""

    claimed = claimed_job(source_type="pdf")
    patch_pipeline(monkeypatch, claimed)
    captured: list[str] = []

    def record_fail(
        session_factory: Any,
        *,
        job_id: Any,
        lease_token: Any,
        error_code: str,
        generation_id: Any = None,
    ) -> bool:
        captured.append(error_code)
        return True

    monkeypatch.setattr(iw, "fail_ingest_job", record_fail)

    def not_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("PDF 解析失败不得 embed/stage/publish")

    monkeypatch.setattr(iw, "create_staging_generation", not_called)
    monkeypatch.setattr(iw, "publish_ingest_generation", not_called)

    status = iw.process_ingest_event(
        build_dependencies(parse_pdf_document=parse_pdf_document),
        job_id=claimed.job_id,
        event_id="e",
    )

    assert status == iw.PROCESS_STATUS_FAILED
    assert captured == [expected_code]


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (PdfEncryptedSubprocessError("x"), iw.ERROR_PIPELINE_PDF_ENCRYPTED),
        (PdfTooManyPagesSubprocessError("x"), iw.ERROR_PIPELINE_PDF_TOO_MANY_PAGES),
        (PdfInvalidSubprocessError("x"), iw.ERROR_PIPELINE_PDF_INVALID),
    ],
)
def test_pipeline_pdf_named_parse_errors_map_to_static_codes(
    monkeypatch: pytest.MonkeyPatch, error: Exception, expected_code: str
) -> None:
    def failure(data: bytes) -> Any:
        raise error

    _run_pdf_failure(monkeypatch, failure, expected_code=expected_code)


def test_pipeline_pdf_source_uses_pdf_parser_version_and_blob_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed = claimed_job(source_type="pdf")
    patch_pipeline(monkeypatch, claimed)
    captured: list[str] = []

    def record_fail(*a: Any, error_code: str, **k: Any) -> bool:
        captured.append(error_code)
        return True

    def record_needs_ocr(*a: Any, **k: Any) -> bool:
        captured.append(iw.ERROR_PIPELINE_NEEDS_OCR)
        return True

    monkeypatch.setattr(iw, "fail_ingest_job", record_fail)
    monkeypatch.setattr(iw, "mark_ingest_needs_ocr", record_needs_ocr)

    def empty_pdf(data: bytes) -> ParsedDocument:
        return ParsedDocument(
            source_sha256=PDF_SHA256,
            text="",
            blocks=(),
            parser_version=PDF_PARSER_VERSION,
            source_type="pdf",
        )

    status = iw.process_ingest_event(
        build_dependencies(parse_pdf_document=empty_pdf),
        job_id=claimed.job_id,
        event_id="e",
    )

    # 零可提取文本（扫描件）走 NEEDS_OCR，而不是 Markdown 的 CONTENT_EMPTY。
    assert status == iw.PROCESS_STATUS_FAILED
    assert captured == [iw.ERROR_PIPELINE_NEEDS_OCR]
