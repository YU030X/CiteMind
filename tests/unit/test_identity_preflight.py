"""身份预检纯模块单测：全类别矩阵、逐字段变异、hash 篡改与导入图约束。

本文件不连数据库、不连 Redis/Celery、不做业务文件 IO、不联网、不加载真实 tokenizer。分类器
自身从不构造默认 profile：``default_index_profile()`` 只在测试显式调用一次以取得真实冻结契约，
子进程用例另行证明调用分类器不会引入 jieba/markdown_it 等依赖。
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
import uuid
from typing import Any

import pytest
from rag_backend.ingestion.docx_parsing import DOCX_PARSER_VERSION
from rag_backend.ingestion.identity_preflight import (
    SOURCE_TYPE_DOCX,
    SOURCE_TYPE_MARKDOWN,
    SOURCE_TYPE_PDF,
    SUPPORTED_SOURCE_TYPES,
    ProfileIdentityDecision,
    StoredIndexProfile,
    decide_profile_identity,
    static_reason,
)
from rag_backend.ingestion.parsing import MARKDOWN_PARSER_VERSION
from rag_backend.ingestion.pdf_parsing import PDF_PARSER_VERSION
from rag_backend.ingestion.service import SOURCE_TYPE_DOCX as SERVICE_SOURCE_TYPE_DOCX
from rag_backend.ingestion.service import SOURCE_TYPE_MARKDOWN as SERVICE_SOURCE_TYPE
from rag_backend.ingestion.service import SOURCE_TYPE_PDF as SERVICE_SOURCE_TYPE_PDF
from rag_backend.models.profile_contract import (
    IndexProfileContract,
    ProfileContractError,
    default_index_profile,
)

PARSER_VERSION = MARKDOWN_PARSER_VERSION
LEGACY_PARSER_VERSION = "markdown-v1"
UNSUPPORTED_SOURCE = "html"
PROFILE_ID_A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
PROFILE_ID_B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")

_VALID_FIELD_CHANGES: list[tuple[str, Any]] = [
    ("embedding_model", "BAAI/other-model"),
    ("model_revision", "0" * 40),
    ("tokenizer_revision", "other-revision"),
    ("chunker_version", "heading-pack-v2"),
    ("keyword_analyzer_version", "jieba-0.42.1-search-v2"),
]

_INVALID_FIELD_VALUES: list[tuple[str, Any]] = [
    ("embedding_model", ""),
    ("embedding_model", None),
    ("model_revision", 123),
    ("dimension", 768),
    ("dimension", "512"),
    ("dimension", True),
    ("dimension", 0),
    ("normalize", False),
    ("normalize", 1),
    ("normalize", "true"),
    ("tokenizer_revision", ""),
    ("chunker_version", None),
    ("keyword_analyzer_version", ""),
]


@pytest.fixture(scope="module")
def expected() -> IndexProfileContract:
    """真实的冻结默认契约；构造一次并显式承担 jieba 身份构造开销。"""

    return default_index_profile()


def _stored(
    contract: IndexProfileContract,
    *,
    profile_id: uuid.UUID = PROFILE_ID_A,
    config_hash: str | None = None,
    **changes: Any,
) -> StoredIndexProfile:
    """把一个契约映射成行 DTO；``changes`` 用于逐字段变异。"""

    values: dict[str, Any] = {
        "profile_id": profile_id,
        "config_hash": config_hash if config_hash is not None else contract.config_hash(),
    }
    for field in dataclasses.fields(contract):
        values[field.name] = getattr(contract, field.name)
    values.update(changes)
    return StoredIndexProfile(**values)


def _decide(
    expected: IndexProfileContract,
    stored: StoredIndexProfile | None,
    *,
    job_profile_id: uuid.UUID | None = None,
    unbound: bool = False,
    source_type: str = SOURCE_TYPE_MARKDOWN,
    parser_version: str = PARSER_VERSION,
    expected_parser_version: str = PARSER_VERSION,
) -> ProfileIdentityDecision:
    if unbound:
        resolved: uuid.UUID | None = None
    elif job_profile_id is not None:
        resolved = job_profile_id
    else:
        resolved = stored.profile_id if stored is not None else None
    return decide_profile_identity(
        job_profile_id=resolved,
        source_type=source_type,
        parser_version=parser_version,
        stored_profile=stored,
        expected=expected,
        expected_parser_version=expected_parser_version,
    )


# ---------------------------------------------------------------------------
# 全类别矩阵与优先级
# ---------------------------------------------------------------------------


def test_allowed_only_after_profile_and_hash_match(expected: IndexProfileContract) -> None:
    assert _decide(expected, _stored(expected)) is ProfileIdentityDecision.ALLOWED


def test_decision_categories_are_complete_and_mutually_exclusive(
    expected: IndexProfileContract,
) -> None:
    cases: dict[ProfileIdentityDecision, ProfileIdentityDecision] = {
        ProfileIdentityDecision.ALLOWED: _decide(expected, _stored(expected)),
        ProfileIdentityDecision.PROFILE_UNBOUND: _decide(
            expected, _stored(expected), unbound=True
        ),
        ProfileIdentityDecision.PROFILE_MISSING: _decide(
            expected, None, job_profile_id=PROFILE_ID_B
        ),
        ProfileIdentityDecision.PROFILE_ID_MISMATCH: _decide(
            expected, _stored(expected), job_profile_id=PROFILE_ID_B
        ),
        ProfileIdentityDecision.SOURCE_UNSUPPORTED: _decide(
            expected, _stored(expected), source_type=UNSUPPORTED_SOURCE
        ),
        ProfileIdentityDecision.PARSER_UNSUPPORTED: _decide(
            expected, _stored(expected), parser_version=LEGACY_PARSER_VERSION
        ),
        ProfileIdentityDecision.CONTRACT_MISMATCH: _decide(
            expected, _stored(expected, normalize=False)
        ),
        ProfileIdentityDecision.HASH_MISMATCH: _decide(
            expected, _stored(expected, config_hash="0" * 64)
        ),
    }

    assert set(cases) == set(ProfileIdentityDecision)
    for expected_decision, actual in cases.items():
        assert actual is expected_decision
    assert len({decision.value for decision in cases.values()}) == len(ProfileIdentityDecision)


def test_unbound_wins_over_missing_source_and_parser(expected: IndexProfileContract) -> None:
    assert (
        _decide(
            expected,
            None,
            unbound=True,
            source_type=UNSUPPORTED_SOURCE,
            parser_version=LEGACY_PARSER_VERSION,
        )
        is ProfileIdentityDecision.PROFILE_UNBOUND
    )


def test_missing_row_wins_over_source_and_parser(expected: IndexProfileContract) -> None:
    assert (
        _decide(
            expected,
            None,
            job_profile_id=PROFILE_ID_B,
            source_type=UNSUPPORTED_SOURCE,
            parser_version=LEGACY_PARSER_VERSION,
        )
        is ProfileIdentityDecision.PROFILE_MISSING
    )


def test_id_mismatch_wins_over_source_and_parser(expected: IndexProfileContract) -> None:
    assert (
        _decide(
            expected,
            _stored(expected),
            job_profile_id=PROFILE_ID_B,
            source_type=UNSUPPORTED_SOURCE,
            parser_version=LEGACY_PARSER_VERSION,
        )
        is ProfileIdentityDecision.PROFILE_ID_MISMATCH
    )


def test_source_is_checked_before_parser(expected: IndexProfileContract) -> None:
    assert (
        _decide(
            expected,
            _stored(expected),
            source_type=UNSUPPORTED_SOURCE,
            parser_version=LEGACY_PARSER_VERSION,
        )
        is ProfileIdentityDecision.SOURCE_UNSUPPORTED
    )


def test_parser_is_checked_before_profile_contract(expected: IndexProfileContract) -> None:
    stored = _stored(expected, normalize=False)

    assert (
        _decide(expected, stored, parser_version=LEGACY_PARSER_VERSION)
        is ProfileIdentityDecision.PARSER_UNSUPPORTED
    )


# ---------------------------------------------------------------------------
# 逐字段变异与 hash 篡改
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"), _VALID_FIELD_CHANGES, ids=[name for name, _ in _VALID_FIELD_CHANGES]
)
def test_each_valid_contract_field_change_returns_contract_mismatch(
    expected: IndexProfileContract, field: str, value: Any
) -> None:
    mutated = dataclasses.replace(expected, **{field: value})
    stored = _stored(mutated)

    # 行自身自洽（字段与 config_hash 一致），但与 expected 契约不同。
    assert stored.config_hash == mutated.config_hash()
    assert _decide(expected, stored) is ProfileIdentityDecision.CONTRACT_MISMATCH


@pytest.mark.parametrize(
    ("field", "value"), _INVALID_FIELD_VALUES, ids=[name for name, _ in _INVALID_FIELD_VALUES]
)
def test_invalid_contract_field_value_returns_contract_mismatch(
    expected: IndexProfileContract, field: str, value: Any
) -> None:
    # 保留 expected 的 config_hash，行字段校验在构造契约时即失败。
    stored = _stored(expected, **{field: value})

    assert _decide(expected, stored) is ProfileIdentityDecision.CONTRACT_MISMATCH


def test_field_change_without_hash_update_returns_hash_mismatch(
    expected: IndexProfileContract,
) -> None:
    # 行字段被改写但 config_hash 仍是 expected 的，说明行自身不一致，先命中 HASH_MISMATCH。
    stored = _stored(expected, chunker_version="heading-pack-v2")

    assert _decide(expected, stored) is ProfileIdentityDecision.HASH_MISMATCH


def test_tampered_config_hash_returns_hash_mismatch(expected: IndexProfileContract) -> None:
    stored = _stored(expected, config_hash="f" * 64)

    assert _decide(expected, stored) is ProfileIdentityDecision.HASH_MISMATCH


def test_non_hex_config_hash_returns_hash_mismatch(expected: IndexProfileContract) -> None:
    stored = _stored(expected, config_hash="not-a-hash")

    assert _decide(expected, stored) is ProfileIdentityDecision.HASH_MISMATCH


def test_self_consistent_other_profile_returns_contract_mismatch(
    expected: IndexProfileContract,
) -> None:
    other = dataclasses.replace(expected, chunker_version="heading-pack-v2")
    stored = _stored(other)

    assert stored.config_hash == other.config_hash()
    assert _decide(expected, stored) is ProfileIdentityDecision.CONTRACT_MISMATCH


# ---------------------------------------------------------------------------
# 静态原因与契约防漂移
# ---------------------------------------------------------------------------


def test_static_reason_never_echoes_sensitive_input() -> None:
    sensitive = [
        str(PROFILE_ID_A),
        "0" * 64,
        PARSER_VERSION,
        LEGACY_PARSER_VERSION,
        UNSUPPORTED_SOURCE,
        "/var/lib/citemind/documents",
        "postgresql://citemind:secret@postgres:5432/citemind",
    ]
    reasons = [static_reason(decision) for decision in ProfileIdentityDecision]

    assert all(reason for reason in reasons)
    assert len(set(reasons)) == len(ProfileIdentityDecision)
    for reason in reasons:
        for token in sensitive:
            assert token not in reason


def test_stored_profile_fields_match_contract_fields() -> None:
    dto_fields = {
        field.name for field in dataclasses.fields(StoredIndexProfile)
    } - {"profile_id", "config_hash"}
    contract_fields = {field.name for field in dataclasses.fields(IndexProfileContract)}

    assert dto_fields == contract_fields


def test_profile_contract_error_is_a_value_error() -> None:
    # 分类器不导入模型包，因此用 ValueError 捕获 ProfileContractError；此断言固定该前提。
    assert issubclass(ProfileContractError, ValueError)


def test_supported_source_types_cover_markdown_pdf_and_docx() -> None:
    assert SUPPORTED_SOURCE_TYPES == frozenset(
        {SOURCE_TYPE_MARKDOWN, SOURCE_TYPE_PDF, SOURCE_TYPE_DOCX}
    )
    assert SOURCE_TYPE_MARKDOWN == SERVICE_SOURCE_TYPE
    assert SOURCE_TYPE_PDF == SERVICE_SOURCE_TYPE_PDF
    assert SOURCE_TYPE_DOCX == SERVICE_SOURCE_TYPE_DOCX
    assert SOURCE_TYPE_PDF == "pdf"
    assert SOURCE_TYPE_DOCX == "docx"


def test_docx_source_with_docx_parser_is_allowed(expected: IndexProfileContract) -> None:
    assert (
        _decide(
            expected,
            _stored(expected),
            source_type=SOURCE_TYPE_DOCX,
            parser_version=DOCX_PARSER_VERSION,
            expected_parser_version=DOCX_PARSER_VERSION,
        )
        is ProfileIdentityDecision.ALLOWED
    )


def test_docx_source_with_markdown_parser_is_rejected(
    expected: IndexProfileContract,
) -> None:
    assert (
        _decide(
            expected,
            _stored(expected),
            source_type=SOURCE_TYPE_DOCX,
            parser_version=MARKDOWN_PARSER_VERSION,
            expected_parser_version=DOCX_PARSER_VERSION,
        )
        is ProfileIdentityDecision.PARSER_UNSUPPORTED
    )


def test_pdf_source_with_pdf_parser_is_allowed(expected: IndexProfileContract) -> None:
    assert (
        _decide(
            expected,
            _stored(expected),
            source_type=SOURCE_TYPE_PDF,
            parser_version=PDF_PARSER_VERSION,
            expected_parser_version=PDF_PARSER_VERSION,
        )
        is ProfileIdentityDecision.ALLOWED
    )


# ---------------------------------------------------------------------------
# 导入图与无 IO 约束
# ---------------------------------------------------------------------------


def test_importing_module_pulls_no_heavy_or_database_dependencies() -> None:
    code = (
        "import sys; import rag_backend.ingestion.identity_preflight; "
        "forbidden = {'tokenizers', 'jieba', 'torch', 'transformers', 'markdown_it', "
        "'sqlalchemy', 'pgvector', 'celery', 'redis', 'psycopg', "
        "'rag_backend.database', 'rag_backend.models'}; "
        "loaded = forbidden & set(sys.modules); "
        "assert not loaded, sorted(loaded)"
    )

    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


def test_classifier_call_does_not_import_jieba_or_markdown_parser() -> None:
    code = (
        "import sys, uuid\n"
        "from rag_backend.ingestion.identity_preflight import ("
        "ProfileIdentityDecision, StoredIndexProfile, decide_profile_identity)\n"
        "from rag_backend.models.profile_contract import IndexProfileContract\n"
        "expected = IndexProfileContract(embedding_model='m', model_revision='r', "
        "dimension=512, normalize=True, tokenizer_revision='t', "
        "chunker_version='c', keyword_analyzer_version='k')\n"
        "stored = StoredIndexProfile(profile_id=uuid.uuid4(), "
        "config_hash=expected.config_hash(), embedding_model='m', model_revision='r', "
        "dimension=512, normalize=True, tokenizer_revision='t', "
        "chunker_version='c', keyword_analyzer_version='k')\n"
        "decision = decide_profile_identity(job_profile_id=stored.profile_id, "
        "source_type='markdown', parser_version='p', stored_profile=stored, "
        "expected=expected, expected_parser_version='p')\n"
        "assert decision is ProfileIdentityDecision.ALLOWED, decision\n"
        "forbidden = {'jieba', 'tokenizers', 'torch', 'transformers', 'markdown_it'}\n"
        "loaded = forbidden & set(sys.modules)\n"
        "assert not loaded, sorted(loaded)\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


def test_classifier_does_not_construct_default_profile(
    expected: IndexProfileContract, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rag_backend.models import profile_contract

    calls: list[str] = []
    monkeypatch.setattr(
        profile_contract, "default_index_profile", lambda: calls.append("default")
    )
    monkeypatch.setattr(
        profile_contract, "current_keyword_analyzer_version", lambda: calls.append("jieba")
    )

    assert _decide(expected, _stored(expected)) is ProfileIdentityDecision.ALLOWED
    assert calls == []
