"""真实入库管线的纯逻辑测试：不连接数据库、Redis、inference 或模型资产。"""

from __future__ import annotations

import time
import uuid
from typing import Any, cast

import pytest
from rag_backend.ingestion import indexing_worker as iw
from rag_backend.ingestion.embedding_client import (
    EmbeddingBusyError,
    EmbeddingPermanentError,
    EmbeddingTransportError,
)
from rag_backend.ingestion.identity_preflight import ProfileIdentityDecision


def claim_facts(**overrides: Any) -> iw.ClaimFacts:
    values: dict[str, Any] = {
        "status": iw.JOB_STATUS_QUEUED,
        "document_deleted": False,
        "version_matches_document": True,
        "receive_marker_present": False,
        "existing_error_code": None,
        "profile_bound": True,
        "parser_version": "markdown-it-py-4.2.0-v1",
        "expected_parser_version": "markdown-it-py-4.2.0-v1",
        "version_no": 1,
        "document_active_version_id": None,
        "ready_generation_present": False,
        "expected_active_version_id": None,
        "next_run_due": True,
    }
    values.update(overrides)
    return iw.ClaimFacts(**values)


ACTIVE_VERSION = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
STALE_VERSION = uuid.UUID("00000000-0000-0000-0000-0000000000bb")


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        (claim_facts(), iw.ClaimAction.CLAIM),
        (
            claim_facts(
                parser_version="pypdf-6.19.0-v1",
                expected_parser_version="pypdf-6.19.0-v1",
            ),
            iw.ClaimAction.CLAIM,
        ),
        (claim_facts(status="READY"), iw.ClaimAction.NOT_QUEUED),
        (claim_facts(status="PARSING"), iw.ClaimAction.NOT_QUEUED),
        (claim_facts(status="FAILED"), iw.ClaimAction.NOT_QUEUED),
        (claim_facts(document_deleted=True), iw.ClaimAction.DELETED),
        (claim_facts(version_matches_document=False), iw.ClaimAction.VERSION_MISMATCH),
        (claim_facts(receive_marker_present=True), iw.ClaimAction.ALREADY_RECEIVED),
        (
            claim_facts(existing_error_code="DELIVERY_UNCONFIRMED"),
            iw.ClaimAction.EXISTING_DIAGNOSTIC,
        ),
        (claim_facts(profile_bound=False), iw.ClaimAction.LEGACY_UNSUPPORTED),
        (claim_facts(parser_version="markdown-v1"), iw.ClaimAction.LEGACY_UNSUPPORTED),
        (claim_facts(expected_parser_version=None), iw.ClaimAction.LEGACY_UNSUPPORTED),
        (claim_facts(version_no=2), iw.ClaimAction.UNSUPPORTED_UPDATE),
        (
            claim_facts(
                version_no=2,
                document_active_version_id=ACTIVE_VERSION,
                expected_active_version_id=ACTIVE_VERSION,
            ),
            iw.ClaimAction.CLAIM,
        ),
        (
            claim_facts(
                version_no=2,
                document_active_version_id=ACTIVE_VERSION,
                expected_active_version_id=STALE_VERSION,
            ),
            iw.ClaimAction.STALE_EXPECTED,
        ),
        (
            claim_facts(document_active_version_id=ACTIVE_VERSION),
            iw.ClaimAction.UNSUPPORTED_UPDATE,
        ),
        (claim_facts(ready_generation_present=True), iw.ClaimAction.UNSUPPORTED_UPDATE),
        (claim_facts(next_run_due=False), iw.ClaimAction.NOT_DUE),
    ],
    ids=[
        "claimable",
        "claimable-pdf",
        "ready",
        "active",
        "failed",
        "deleted",
        "version-mismatch",
        "marker",
        "diagnostic",
        "unbound-profile",
        "legacy-parser",
        "unsupported-source-none-version",
        "second-version",
        "second-version-update",
        "second-version-stale-expected",
        "already-active-version",
        "already-ready-generation",
        "backoff-not-due",
    ],
)
def test_decide_claim_action_priority(facts: iw.ClaimFacts, expected: iw.ClaimAction) -> None:
    assert iw.decide_claim_action(facts) is expected


def test_marker_and_diagnostic_take_priority_over_legacy_and_update() -> None:
    # 已有 marker 优先于诊断与 legacy；已有诊断优先于 legacy。
    assert iw.decide_claim_action(
        claim_facts(receive_marker_present=True, profile_bound=False)
    ) is iw.ClaimAction.ALREADY_RECEIVED
    assert iw.decide_claim_action(
        claim_facts(existing_error_code="X", profile_bound=False, version_no=2)
    ) is iw.ClaimAction.EXISTING_DIAGNOSTIC


@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        (ProfileIdentityDecision.ALLOWED, None),
        (ProfileIdentityDecision.PROFILE_UNBOUND, "LEGACY_JOB_UNSUPPORTED"),
        (ProfileIdentityDecision.PARSER_UNSUPPORTED, "LEGACY_JOB_UNSUPPORTED"),
        (
            ProfileIdentityDecision.SOURCE_UNSUPPORTED,
            iw.ERROR_PIPELINE_SOURCE_UNSUPPORTED,
        ),
        (
            ProfileIdentityDecision.PROFILE_MISSING,
            iw.ERROR_PIPELINE_PROFILE_MISMATCH,
        ),
        (
            ProfileIdentityDecision.PROFILE_ID_MISMATCH,
            iw.ERROR_PIPELINE_PROFILE_MISMATCH,
        ),
        (
            ProfileIdentityDecision.CONTRACT_MISMATCH,
            iw.ERROR_PIPELINE_PROFILE_MISMATCH,
        ),
        (ProfileIdentityDecision.HASH_MISMATCH, iw.ERROR_PIPELINE_PROFILE_MISMATCH),
    ],
)
def test_classify_identity_decision(
    decision: ProfileIdentityDecision, expected: str | None
) -> None:
    assert iw.classify_identity_decision(decision) == expected


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (EmbeddingPermanentError("x"), iw.ERROR_PIPELINE_EMBEDDING_REJECTED),
        (EmbeddingBusyError("x"), iw.ERROR_PIPELINE_EMBEDDING_FAILED),
        (EmbeddingTransportError("x"), iw.ERROR_PIPELINE_EMBEDDING_FAILED),
    ],
)
def test_classify_embedding_error(error: Any, expected: str) -> None:
    assert iw.classify_embedding_error(error) == expected


class _FakeEmbedder:
    """按预置结果序列返回或抛出的假编码器；记录调用与关闭。"""

    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0
        self.closed = False

    def embed_document_texts(self, texts: Any) -> list[list[float]]:
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return cast(list[list[float]], outcome)

    def close(self) -> None:
        self.closed = True


def test_embed_chunk_inputs_retries_only_transient_failures() -> None:
    vectors = [[0.1] * 512]
    embedder = _FakeEmbedder(
        [EmbeddingBusyError("busy"), EmbeddingTransportError("net"), vectors]
    )
    sleeps: list[float] = []

    result = iw.embed_chunk_inputs(embedder, ["text"], max_attempts=3, sleep=sleeps.append)

    assert result == vectors
    assert embedder.calls == 3
    assert len(sleeps) == 2


def test_embed_chunk_inputs_does_not_retry_permanent_failure() -> None:
    embedder = _FakeEmbedder([EmbeddingPermanentError("rejected")])
    sleeps: list[float] = []

    with pytest.raises(EmbeddingPermanentError):
        iw.embed_chunk_inputs(embedder, ["text"], max_attempts=3, sleep=sleeps.append)

    assert embedder.calls == 1
    assert sleeps == []


def test_embed_chunk_inputs_stops_at_total_budget() -> None:
    embedder = _FakeEmbedder([EmbeddingBusyError("busy"), EmbeddingBusyError("busy")])
    sleeps: list[float] = []

    with pytest.raises(EmbeddingBusyError):
        iw.embed_chunk_inputs(embedder, ["text"], max_attempts=2, sleep=sleeps.append)

    assert embedder.calls == 2
    assert len(sleeps) == 1


class _FakeResult:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    def first(self) -> tuple[Any, ...] | None:
        return self._row


class _FakeSession:
    """心跳只用到的最小 Session 接口。"""

    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row
        self.executions = 0

    def execute(self, statement: Any, parameters: Any = None) -> _FakeResult:
        self.executions += 1
        return _FakeResult(self._row)

    def begin(self) -> _FakeSession:
        return self

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_lease_heartbeat_renews_and_stops_cleanly() -> None:
    sessions: list[_FakeSession] = []

    def factory() -> _FakeSession:
        session = _FakeSession((uuid.uuid4(),))
        sessions.append(session)
        return session

    heartbeat = iw.LeaseHeartbeat(
        factory,  # type: ignore[arg-type]
        job_id=uuid.uuid4(),
        lease_token="t",
        lease_seconds=10,
        interval_seconds=0.01,
    )
    heartbeat.start()
    deadline = time.monotonic() + 2.0
    while not sessions and time.monotonic() < deadline:
        time.sleep(0.005)
    heartbeat.stop()
    heartbeat.stop()  # 幂等

    assert heartbeat.lost is False
    assert sessions and all(session.executions >= 1 for session in sessions)


def test_lease_heartbeat_marks_lost_when_update_misses() -> None:
    def factory() -> _FakeSession:
        return _FakeSession(None)

    heartbeat = iw.LeaseHeartbeat(
        factory,  # type: ignore[arg-type]
        job_id=uuid.uuid4(),
        lease_token="t",
        lease_seconds=10,
        interval_seconds=0.01,
    )
    heartbeat.start()
    deadline = time.monotonic() + 2.0
    while not heartbeat.lost and time.monotonic() < deadline:
        time.sleep(0.005)
    heartbeat.stop()

    assert heartbeat.lost is True


def test_lease_heartbeat_marks_lost_on_connection_error() -> None:
    def factory() -> Any:
        raise RuntimeError("db down")

    heartbeat = iw.LeaseHeartbeat(
        factory,  # type: ignore[arg-type]
        job_id=uuid.uuid4(),
        lease_token="t",
        lease_seconds=10,
        interval_seconds=0.01,
    )
    heartbeat.start()
    deadline = time.monotonic() + 2.0
    while not heartbeat.lost and time.monotonic() < deadline:
        time.sleep(0.005)
    heartbeat.stop()

    assert heartbeat.lost is True


def test_process_status_constants_are_distinct() -> None:
    statuses = {
        iw.PROCESS_STATUS_READY,
        iw.PROCESS_STATUS_ALREADY_RECEIVED,
        iw.PROCESS_STATUS_NOT_QUEUED,
        iw.PROCESS_STATUS_DELETED,
        iw.PROCESS_STATUS_VERSION_MISMATCH,
        iw.PROCESS_STATUS_LEGACY_UNSUPPORTED,
        iw.PROCESS_STATUS_EXISTING_DIAGNOSTIC,
        iw.PROCESS_STATUS_UNSUPPORTED_UPDATE,
        iw.PROCESS_STATUS_STALE_EXPECTED,
        iw.PROCESS_STATUS_NOT_DUE,
        iw.PROCESS_STATUS_FAILED,
        iw.PROCESS_STATUS_LEASE_LOST,
        iw.PROCESS_STATUS_CLAIMED,
        iw.PROCESS_STATUS_PERSIST_UNCONFIRMED,
    }
    assert len(statuses) == 14
