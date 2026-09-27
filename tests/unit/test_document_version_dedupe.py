"""文档新版本去重键与端点参数解析的纯逻辑测试（不连数据库）。"""

from __future__ import annotations

import uuid

import pytest
from rag_backend.api.documents import (
    _ingestion_error,
    _is_deadlock_error,
    _parse_expected_version,
)
from rag_backend.api.errors import (
    CODE_DOCUMENT_DELETED,
    CODE_DOCUMENT_NOT_FOUND,
    CODE_DOCUMENT_VERSION_CONFLICT,
    ApiError,
)
from rag_backend.ingestion.errors import (
    DocumentDeleted,
    DocumentNotFound,
    ExpectedVersionConflict,
    IdempotencyConflict,
)
from rag_backend.ingestion.service import _existing_from_row
from rag_backend.ingestion.validation import (
    build_dedupe_key,
    build_version_dedupe_key,
    build_version_dedupe_key_prefix,
    parse_version_dedupe_key,
)

ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
KB = uuid.UUID("00000000-0000-0000-0000-000000000002")
DOC = uuid.UUID("00000000-0000-0000-0000-000000000003")
OTHER_DOC = uuid.UUID("00000000-0000-0000-0000-000000000004")
V1 = uuid.UUID("00000000-0000-0000-0000-000000000005")


def test_version_prefix_is_scoped_by_document_and_key() -> None:
    base = build_version_dedupe_key_prefix(ORG, KB, DOC, "key-1")
    assert base == build_version_dedupe_key_prefix(ORG, KB, DOC, "key-1")
    assert base != build_version_dedupe_key_prefix(ORG, KB, OTHER_DOC, "key-1")
    assert base != build_version_dedupe_key_prefix(ORG, KB, DOC, "key-2")
    assert base.startswith("ver1:")
    # 与首次上传的裸 SHA-256 键互不匹配，避免跨操作命中同一去重行。
    assert not base.startswith(build_dedupe_key(ORG, KB, "key-1"))


def test_version_dedupe_key_roundtrip_carries_expected_active() -> None:
    prefix = build_version_dedupe_key_prefix(ORG, KB, DOC, "key-1")
    key = build_version_dedupe_key(prefix, V1)
    assert parse_version_dedupe_key(key) == (DOC, V1)
    assert key.startswith(prefix + ":")


def test_parse_rejects_first_upload_and_malformed_keys() -> None:
    assert parse_version_dedupe_key(build_dedupe_key(ORG, KB, "key-1")) is None
    assert parse_version_dedupe_key("ver1:not-a-uuid:" + "0" * 64 + ":" + str(V1)) is None
    assert parse_version_dedupe_key("ver1:" + str(DOC) + ":zz:" + str(V1)) is None
    assert parse_version_dedupe_key("ver1:" + str(DOC) + ":" + "0" * 64 + ":not-a-uuid") is None


def test_parse_expected_version_accepts_uuid_and_rejects_bad_input() -> None:
    assert _parse_expected_version(str(V1)) == V1
    with pytest.raises(ApiError) as missing:
        _parse_expected_version(None)
    assert missing.value.status_code == 422
    with pytest.raises(ApiError) as invalid:
        _parse_expected_version("not-a-uuid")
    assert invalid.value.status_code == 422
    # 错误体不回显提交值。
    assert "not-a-uuid" not in invalid.value.message


def test_ingestion_error_mapping_for_document_lifecycle() -> None:
    assert _ingestion_error(DocumentNotFound()).status_code == 404
    assert _ingestion_error(DocumentNotFound()).code == CODE_DOCUMENT_NOT_FOUND
    assert _ingestion_error(DocumentDeleted()).status_code == 409
    assert _ingestion_error(DocumentDeleted()).code == CODE_DOCUMENT_DELETED
    assert _ingestion_error(ExpectedVersionConflict()).status_code == 409
    assert _ingestion_error(ExpectedVersionConflict()).code == CODE_DOCUMENT_VERSION_CONFLICT
    # 同键不同内容/标题仍是既有 409。
    assert _ingestion_error(IdempotencyConflict()).code == "IDEMPOTENCY_KEY_REUSED"


class _FakeDbapiError:
    """只需 ``orig.sqlstate`` 的最小替身，用于纯逻辑判定死锁。"""

    def __init__(self, sqlstate: object) -> None:
        self.orig = type("Orig", (), {"sqlstate": sqlstate})()


def test_deadlock_detection_only_accepts_sqlstate_40p01() -> None:
    assert _is_deadlock_error(_FakeDbapiError("40P01")) is True  # type: ignore[arg-type]
    for sqlstate in ("23505", "40001", None, 4001):
        assert _is_deadlock_error(_FakeDbapiError(sqlstate)) is False  # type: ignore[arg-type]
    # ``orig`` 缺失或非标准错误对象时不误判。
    assert _is_deadlock_error(object()) is False  # type: ignore[arg-type]


def test_existing_row_prefers_immutable_request_title_and_falls_back() -> None:
    """幂等标题优先取不可变 ``request_title``；旧行 NULL 时回退到 ``document.title``。"""

    job_id, doc_id, ver_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    key = build_version_dedupe_key(build_version_dedupe_key_prefix(ORG, KB, DOC, "k"), V1)
    digest = "a" * 64

    fresh = _existing_from_row(
        (job_id, doc_id, ver_id, key, "请求标题", "文档当前标题", None, digest)
    )
    assert fresh.title == "请求标题"

    legacy = _existing_from_row(
        (job_id, doc_id, ver_id, key, None, "文档当前标题", None, digest)
    )
    assert legacy.title == "文档当前标题"
