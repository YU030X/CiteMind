"""只读探针真实适配器的聚焦单测：只测纯解析/校验函数，不建 engine、不连数据库、不联网。"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

import pytest
from rag_backend.evaluation.dataset import GoldLocator
from rag_backend.evaluation.probe import CandidateFacts
from rag_backend.evaluation.probe_adapters import (
    LocatorCandidateMapper,
    ProbeAccountRow,
    ProbeAdapterError,
    ProbeProfileRow,
    build_role_identities,
    parse_evidence_locator,
    resolve_single_organization,
    resolve_single_profile,
    validate_probe_database_url,
)
from rag_backend.evaluation.runner import LogicalRef
from rag_backend.retrieval.repository import EvidenceChunkRow

ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
OTHER_ORG = uuid.UUID("00000000-0000-0000-0000-000000000002")
CHUNK = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
VERSION = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
PROFILE = uuid.UUID("00000000-0000-0000-0000-0000000000cc")


# ---------------------------------------------------------------------------
# DSN 护栏


def test_dsn_accepts_psycopg_test_database() -> None:
    url = "postgresql+psycopg://user:pw@localhost:5432/rag_test"
    assert validate_probe_database_url(url, allow_database_name=None) == url


def test_dsn_rejects_other_driver() -> None:
    with pytest.raises(ProbeAdapterError):
        validate_probe_database_url(
            "postgresql://user:pw@localhost:5432/rag_test", allow_database_name=None
        )


def test_dsn_rejects_non_test_database_without_allow() -> None:
    with pytest.raises(ProbeAdapterError):
        validate_probe_database_url(
            "postgresql+psycopg://user:pw@localhost:5432/production",
            allow_database_name=None,
        )


def test_dsn_accepts_explicit_allow_name() -> None:
    url = "postgresql+psycopg://user:pw@localhost:5432/production"
    assert (
        validate_probe_database_url(url, allow_database_name="production") == url
    )


def test_dsn_rejects_mismatched_allow_name() -> None:
    with pytest.raises(ProbeAdapterError):
        validate_probe_database_url(
            "postgresql+psycopg://user:pw@localhost:5432/production",
            allow_database_name="another",
        )


def test_dsn_error_does_not_echo_secret() -> None:
    secret = "supersecretvalue"
    with pytest.raises(ProbeAdapterError) as captured:
        validate_probe_database_url(
            f"mysql://user:{secret}@localhost/production", allow_database_name=None
        )
    assert secret not in str(captured.value)


# ---------------------------------------------------------------------------
# locator 解析


def test_parse_markdown_locator_with_heading_path() -> None:
    locator = parse_evidence_locator(
        {
            "source_type": "markdown",
            "parser_version": "md-v1",
            "start_line": 3,
            "end_line": 5,
            "heading_path": ["A", "B"],
        }
    )
    assert locator == GoldLocator(
        source_type="markdown",
        parser_version="md-v1",
        heading_path=["A", "B"],
        start_line=3,
        end_line=5,
    )


def test_parse_markdown_locator_defaults_heading_path_empty() -> None:
    locator = parse_evidence_locator(
        {
            "source_type": "markdown",
            "parser_version": "md-v1",
            "start_line": 1,
            "end_line": 1,
        }
    )
    assert locator.heading_path == []


@pytest.mark.parametrize(
    "locator",
    [
        {
            "source_type": "markdown",
            "parser_version": "md-v1",
            "start_line": 0,
            "end_line": 1,
        },
        {
            "source_type": "markdown",
            "parser_version": "md-v1",
            "start_line": True,
            "end_line": 1,
        },
        {
            "source_type": "markdown",
            "parser_version": "md-v1",
            "start_line": 2,
            "end_line": 1,
        },
        {
            "source_type": "markdown",
            "parser_version": "md-v1",
            "start_line": 1,
            "end_line": 1,
            "heading_path": "A",
        },
        {
            "source_type": "markdown",
            "parser_version": "md-v1",
            "start_line": 1,
            "end_line": 1,
            "heading_path": [1],
        },
    ],
)
def test_parse_markdown_locator_rejects_malformed(
    locator: Mapping[str, object],
) -> None:
    with pytest.raises(ProbeAdapterError):
        parse_evidence_locator(locator)


def test_parse_pdf_locator_single_page() -> None:
    locator = parse_evidence_locator(
        {"source_type": "pdf", "parser_version": "pdf-v1", "pages": [2]}
    )
    assert locator == GoldLocator(source_type="pdf", parser_version="pdf-v1", page=2)


@pytest.mark.parametrize("pages", [[], [1, 2], [0], ["1"], [True]])
def test_parse_pdf_locator_rejects_malformed(pages: list[object]) -> None:
    with pytest.raises(ProbeAdapterError):
        parse_evidence_locator(
            {"source_type": "pdf", "parser_version": "pdf-v1", "pages": pages}
        )


@pytest.mark.parametrize("source_type", ["web", "docx", None])
def test_parse_locator_rejects_unsupported_source(source_type: object) -> None:
    with pytest.raises(ProbeAdapterError):
        parse_evidence_locator(
            {"source_type": source_type, "parser_version": "x-v1"}
        )


def test_parse_locator_requires_parser_version() -> None:
    with pytest.raises(ProbeAdapterError):
        parse_evidence_locator({"source_type": "markdown", "start_line": 1, "end_line": 1})


# ---------------------------------------------------------------------------
# 组织 / 角色 / profile 逻辑


def test_resolve_single_organization_requires_uniform_org() -> None:
    kb_a, kb_b = uuid.uuid4(), uuid.uuid4()
    assert resolve_single_organization([kb_a, kb_b], {kb_a: ORG, kb_b: ORG}) == ORG


def test_resolve_single_organization_rejects_mixed_orgs() -> None:
    kb_a, kb_b = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(ProbeAdapterError):
        resolve_single_organization([kb_a, kb_b], {kb_a: ORG, kb_b: OTHER_ORG})


def test_resolve_single_organization_rejects_missing_kb() -> None:
    kb_a, kb_b = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(ProbeAdapterError):
        resolve_single_organization([kb_a, kb_b], {kb_a: ORG})


def test_build_role_identities_requires_unique_account() -> None:
    accounts = [ProbeAccountRow(username="reader-user", user_id=uuid.uuid4())]
    identities = build_role_identities(
        organization_id=ORG, roles={"reader": "reader-user"}, accounts=accounts
    )
    assert identities["reader"].user_id == accounts[0].user_id
    assert identities["reader"].organization_id == ORG


def test_build_role_identities_rejects_missing_account() -> None:
    with pytest.raises(ProbeAdapterError):
        build_role_identities(
            organization_id=ORG, roles={"reader": "reader-user"}, accounts=[]
        )


def test_build_role_identities_rejects_duplicate_account_without_echo() -> None:
    accounts = [
        ProbeAccountRow(username="reader-user", user_id=uuid.uuid4()),
        ProbeAccountRow(username="reader-user", user_id=uuid.uuid4()),
    ]
    with pytest.raises(ProbeAdapterError) as captured:
        build_role_identities(
            organization_id=ORG, roles={"reader": "reader-user"}, accounts=accounts
        )
    assert "reader-user" not in str(captured.value)


def _profile_row(kb: uuid.UUID, *, revision: str = "rev-1") -> ProbeProfileRow:
    return ProbeProfileRow(
        kb_id=kb,
        profile_id=PROFILE,
        embedding_model="bge",
        model_revision=revision,
        dimension=512,
        normalize=True,
        tokenizer_revision="tokenizer-1",
        chunker_version="chunker-1",
        keyword_analyzer_version="analyzer-1",
        config_hash="config-hash-1",
    )


def test_resolve_single_profile_accepts_identical_profiles() -> None:
    kb_a, kb_b = uuid.uuid4(), uuid.uuid4()
    identity = resolve_single_profile(
        [kb_a, kb_b],
        [_profile_row(kb_a), _profile_row(kb_b)],
        query_contract="bge-zh-query-v1",
    )
    assert identity.profile_id == PROFILE
    assert identity.query_contract == "bge-zh-query-v1"
    assert identity.scalars()["tokenizerRevision"] == "tokenizer-1"
    assert identity.scalars()["chunkerVersion"] == "chunker-1"
    assert identity.scalars()["configHash"] == "config-hash-1"


def test_resolve_single_profile_rejects_inconsistent_profiles() -> None:
    kb_a, kb_b = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(ProbeAdapterError):
        resolve_single_profile(
            [kb_a, kb_b],
            [_profile_row(kb_a), _profile_row(kb_b, revision="rev-2")],
            query_contract="bge-zh-query-v1",
        )


def test_resolve_single_profile_rejects_missing_profile() -> None:
    kb_a, kb_b = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(ProbeAdapterError):
        resolve_single_profile(
            [kb_a, kb_b], [_profile_row(kb_a)], query_contract="bge-zh-query-v1"
        )


# ---------------------------------------------------------------------------
# 候选映射


def test_locator_mapper_preserves_facts_and_locator() -> None:
    row = EvidenceChunkRow(
        chunk_id=CHUNK,
        document_id=uuid.uuid4(),
        kb_id=uuid.uuid4(),
        version_id=VERSION,
        version_no=1,
        document_title="标题",
        text="正文",
        source_locator={
            "source_type": "markdown",
            "parser_version": "md-v1",
            "start_line": 3,
            "end_line": 5,
            "heading_path": ["A"],
        },
    )
    facts = CandidateFacts(
        chunk_id=CHUNK,
        version_id=VERSION,
        rank=2,
        vector_rank=1,
        vector_score=0.9,
        keyword_rank=4,
        keyword_score=0.3,
        fusion_rank=2,
        fusion_score=0.5,
        rerank_score=0.7,
    )
    candidate = LocatorCandidateMapper().map(
        row, facts, LogicalRef("kb-a", "doc-1", 3)
    )
    assert candidate.candidate_id == str(CHUNK)
    assert (candidate.kb_id, candidate.document_id, candidate.version) == (
        "kb-a",
        "doc-1",
        3,
    )
    assert candidate.locator == GoldLocator(
        source_type="markdown",
        parser_version="md-v1",
        heading_path=["A"],
        start_line=3,
        end_line=5,
    )
    assert candidate.rank == 2
    assert candidate.vector_rank == 1
    assert candidate.vector_score == 0.9
    assert candidate.keyword_rank == 4
    assert candidate.keyword_score == 0.3
    assert candidate.fusion_rank == 2
    assert candidate.fusion_score == 0.5
    assert candidate.rerank_score == 0.7


def test_locator_mapper_rejects_unsupported_locator() -> None:
    row = EvidenceChunkRow(
        chunk_id=CHUNK,
        document_id=uuid.uuid4(),
        kb_id=uuid.uuid4(),
        version_id=VERSION,
        version_no=1,
        document_title="标题",
        text="正文",
        source_locator={"source_type": "docx", "parser_version": "docx-v1"},
    )
    facts = CandidateFacts(chunk_id=CHUNK, version_id=VERSION, rank=1)
    with pytest.raises(ProbeAdapterError):
        LocatorCandidateMapper().map(row, facts, LogicalRef("kb-a", "doc-1", 1))
