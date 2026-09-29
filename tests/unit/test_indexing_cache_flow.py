"""入库管线增量缓存编排的纯逻辑测试：不连接数据库、Redis、inference 或模型资产。

用假 session/假编码器驱动 ``process_ingest_event``，验证缓存命中不编码、部分命中只编码 miss、
重复 hash 只编码一次并 fan-out、缓存查询失败回退全量编码，以及畸形/维度错误向量按 miss 处理。
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, cast

import pytest
from rag_backend.database import SyncSessionFactory
from rag_backend.ingestion import indexing_worker as iw
from rag_backend.ingestion.chunking import Chunk
from rag_backend.ingestion.identity_preflight import ProfileIdentityDecision
from rag_backend.ingestion.parsing import MARKDOWN_PARSER_VERSION, ParsedDocument
from rag_backend.ingestion.storage import DocumentBlobStore
from rag_backend.models.profile_contract import IndexProfileContract
from sqlalchemy.exc import SQLAlchemyError

MARKDOWN_TEXT = "# 标题\n\n正文段落。\n"
MARKDOWN_SHA256 = hashlib.sha256(MARKDOWN_TEXT.encode("utf-8")).hexdigest()

PROFILE = IndexProfileContract(
    embedding_model="test/model",
    model_revision="test-revision",
    dimension=512,
    normalize=True,
    tokenizer_revision="test-tokenizer",
    chunker_version="heading-pack-v1",
    keyword_analyzer_version="test-analyzer",
)


class FakeCounter:
    def count_tokens(self, text: str) -> int:
        return max(1, len(text))


class FakeAnalyzer:
    def analyze(self, text: str) -> str:
        return " ".join(text.split())


class FakeIdentity:
    profile = PROFILE
    parser_version = MARKDOWN_PARSER_VERSION
    pdf_parser_version = "pypdf-6.19.0+pdfplumber-0.11.10-v1"
    docx_parser_version = "python-docx-1.2.0-v1"
    token_counter = FakeCounter()
    keyword_analyzer = FakeAnalyzer()


class FakeStorage:
    def read_verified_markdown(
        self, kb_id: uuid.UUID, file_ref: str, file_hash: str
    ) -> str:
        return MARKDOWN_TEXT


class RecordingEmbedder:
    """记录每次收到的文本并返回确定性向量；用于断言缓存减少的编码量。"""

    def __init__(self) -> None:
        self.calls = 0
        self.received: list[list[str]] = []
        self.closed = False

    def embed_document_texts(self, texts: Any) -> list[list[float]]:
        self.calls += 1
        self.received.append(list(texts))
        return [[float(index + 1)] * 512 for index in range(len(texts))]

    def close(self) -> None:
        self.closed = True


def make_chunk(
    chunk_index: int, *, model_input_hash: str, heading: tuple[str, ...] = (), text: str = "x"
) -> Chunk:
    return Chunk(
        chunk_index=chunk_index,
        text=text,
        heading_path=heading,
        token_count=1,
        text_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        model_input_hash=model_input_hash,
        source_locator={"locator_version": 1, "source_sha256": MARKDOWN_SHA256},
        parser_version=MARKDOWN_PARSER_VERSION,
        chunker_version="heading-pack-v1",
    )


def claimed_job() -> iw.ClaimedJob:
    return iw.ClaimedJob(
        job_id=uuid.uuid4(),
        lease_token="lease",
        document_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        kb_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        profile_id=uuid.uuid4(),
        parser_version=MARKDOWN_PARSER_VERSION,
        source_type="markdown",
        file_ref="kb/hash",
        file_hash=MARKDOWN_SHA256,
    )


def parsed_document() -> ParsedDocument:
    return ParsedDocument(
        source_sha256=MARKDOWN_SHA256,
        text=MARKDOWN_TEXT,
        blocks=(),
        parser_version=MARKDOWN_PARSER_VERSION,
    )


def run_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    chunks: list[Chunk],
    cache_lookup: iw.CacheLookup,
    embedder: RecordingEmbedder,
) -> dict[str, Any]:
    """驱动一次管线到 READY，返回捕获的 chunks/vectors 与编码器工厂调用次数。"""

    claimed = claimed_job()
    captured: dict[str, Any] = {"factory_calls": 0}

    monkeypatch.setattr(
        iw,
        "claim_ingest_job",
        lambda *a, **k: iw.ClaimResult(iw.PROCESS_STATUS_CLAIMED, claimed),
    )
    monkeypatch.setattr(iw, "advance_ingest_stage", lambda *a, **k: True)
    monkeypatch.setattr(iw, "load_stored_profile", lambda *a, **k: None)
    monkeypatch.setattr(
        iw, "decide_profile_identity", lambda **k: ProfileIdentityDecision.ALLOWED
    )
    monkeypatch.setattr(iw, "chunk_markdown", lambda parsed, counter: list(chunks))

    def fake_stage(*args: Any, vectors: Any, **kwargs: Any) -> uuid.UUID:
        captured["vectors"] = list(vectors)
        return uuid.uuid4()

    monkeypatch.setattr(iw, "create_staging_generation", fake_stage)
    monkeypatch.setattr(
        iw, "publish_ingest_generation", lambda *a, **k: iw.PublishOutcome.PUBLISHED
    )

    def factory(counter: Any) -> RecordingEmbedder:
        captured["factory_calls"] += 1
        return embedder

    dependencies = iw.PipelineDependencies(
        session_factory=cast(SyncSessionFactory, lambda: None),
        storage=cast(DocumentBlobStore, FakeStorage()),
        identity_provider=lambda: FakeIdentity(),
        embedder_factory=factory,
        parse_document=lambda data: parsed_document(),
        cache_lookup=cache_lookup,
    )
    captured["status"] = iw.process_ingest_event(
        dependencies, job_id=claimed.job_id, event_id="e"
    )
    return captured


def _constant_cache(mapping: dict[str, list[float]]) -> iw.CacheLookup:
    def lookup(session_factory: Any, **kwargs: Any) -> dict[str, list[float]]:
        return mapping

    return lookup


def test_full_cache_hit_never_calls_encoder(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = [
        make_chunk(0, model_input_hash="h1", text="one"),
        make_chunk(1, model_input_hash="h2", text="two"),
    ]
    v1 = [0.1] * 512
    v2 = [0.2] * 512
    embedder = RecordingEmbedder()

    captured = run_pipeline(
        monkeypatch,
        chunks=chunks,
        cache_lookup=_constant_cache({"h1": v1, "h2": v2}),
        embedder=embedder,
    )

    assert captured["status"] == iw.PROCESS_STATUS_READY
    assert captured["factory_calls"] == 0
    assert embedder.calls == 0
    assert captured["vectors"] == [v1, v2]


def test_partial_cache_hit_encodes_only_missing_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = [
        make_chunk(0, model_input_hash="h1", text="one"),
        make_chunk(1, model_input_hash="h2", text="two"),
    ]
    cached = [0.1] * 512
    embedder = RecordingEmbedder()

    captured = run_pipeline(
        monkeypatch,
        chunks=chunks,
        cache_lookup=_constant_cache({"h1": cached}),
        embedder=embedder,
    )

    assert captured["status"] == iw.PROCESS_STATUS_READY
    assert captured["factory_calls"] == 1
    assert embedder.calls == 1
    assert embedder.received == [["two"]]
    assert captured["vectors"][0] == cached
    assert captured["vectors"][1] == [1.0] * 512


def test_duplicate_hash_is_encoded_once_and_fanned_out(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = [
        make_chunk(0, model_input_hash="dup", text="first"),
        make_chunk(1, model_input_hash="dup", text="second"),
        make_chunk(2, model_input_hash="other", text="third"),
    ]
    embedder = RecordingEmbedder()

    captured = run_pipeline(
        monkeypatch,
        chunks=chunks,
        cache_lookup=_constant_cache({}),
        embedder=embedder,
    )

    assert captured["status"] == iw.PROCESS_STATUS_READY
    assert embedder.calls == 1
    # 每个唯一 hash 只编码一次；重复 chunk 复用同一向量对象并按 chunks 顺序 fan-out。
    assert embedder.received == [["first", "third"]]
    vectors = captured["vectors"]
    assert vectors[0] is vectors[1]
    assert vectors[2] == [2.0] * 512


def test_cache_query_failure_falls_back_to_full_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = [
        make_chunk(0, model_input_hash="h1", text="one"),
        make_chunk(1, model_input_hash="h2", text="two"),
    ]

    def failing_cache(session_factory: Any, **kwargs: Any) -> dict[str, list[float]]:
        raise SQLAlchemyError("cache down")

    embedder = RecordingEmbedder()

    captured = run_pipeline(
        monkeypatch,
        chunks=chunks,
        cache_lookup=failing_cache,
        embedder=embedder,
    )

    assert captured["status"] == iw.PROCESS_STATUS_READY
    assert captured["factory_calls"] == 1
    assert embedder.calls == 1
    assert embedder.received == [["one", "two"]]


@pytest.mark.parametrize(
    "malformed",
    [
        [0.0] * 511,
        [float("nan")] * 512,
        [float("inf")] * 512,
        ["x"] * 512,
    ],
    ids=["dimension", "nan", "inf", "non-numeric"],
)
def test_malformed_cached_vector_is_treated_as_miss(
    monkeypatch: pytest.MonkeyPatch, malformed: list[Any]
) -> None:
    chunks = [
        make_chunk(0, model_input_hash="bad", text="one"),
        make_chunk(1, model_input_hash="good", text="two"),
    ]
    cached = [0.5] * 512
    embedder = RecordingEmbedder()

    captured = run_pipeline(
        monkeypatch,
        chunks=chunks,
        cache_lookup=_constant_cache({"bad": cast(Any, malformed), "good": cached}),
        embedder=embedder,
    )

    assert captured["status"] == iw.PROCESS_STATUS_READY
    # 畸形 hash 回退编码，合法缓存仍复用。
    assert embedder.calls == 1
    assert embedder.received == [["one"]]
    assert captured["vectors"][0] == [1.0] * 512
    assert captured["vectors"][1] == cached
