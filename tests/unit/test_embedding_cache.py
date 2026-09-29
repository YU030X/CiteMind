"""增量 embedding 缓存的纯逻辑测试：不连接数据库、Redis、inference 或模型资产。

覆盖向量严格校验、合法/畸形缓存收敛，以及缓存查询 SQL 的权威链形状（不信任 ``chunk`` 上
冗余的 ``organization_id``/``kb_id``/``document_id``/``version_id``）。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from rag_backend.ingestion import embedding_cache as ec
from sqlalchemy.exc import SQLAlchemyError

VALID_VECTOR = [0.125] * ec.EMBEDDING_DIMENSION


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _Result:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeSession:
    def __init__(self, rows: list[dict[str, Any]], *, raises: bool = False) -> None:
        self._rows = rows
        self._raises = raises
        self.rolled_back = False
        self.executions: list[tuple[Any, Any]] = []

    def execute(self, statement: Any, parameters: Any = None) -> _Result:
        self.executions.append((statement, parameters))
        if self._raises:
            raise SQLAlchemyError("db down")
        return _Result(self._rows)

    def rollback(self) -> None:
        self.rolled_back = True

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def session_factory(session: _FakeSession) -> Any:
    return lambda: session


@pytest.mark.parametrize(
    "value",
    [
        VALID_VECTOR,
        tuple(VALID_VECTOR),
        json.dumps(VALID_VECTOR),
        [0] * ec.EMBEDDING_DIMENSION,
        [-1.5, 2] + [0.0] * (ec.EMBEDDING_DIMENSION - 2),
    ],
    ids=["list", "tuple", "pgvector-text", "zeros", "negatives"],
)
def test_coerce_embedding_vector_accepts_valid_vectors(value: Any) -> None:
    result = ec.coerce_embedding_vector(value)

    assert result is not None
    assert len(result) == ec.EMBEDDING_DIMENSION
    assert all(isinstance(item, float) for item in result)


@pytest.mark.parametrize(
    "value",
    [
        "0.1" * ec.EMBEDDING_DIMENSION,
        b"bytes",
        None,
        123,
        {"a": 1},
        [0.0] * (ec.EMBEDDING_DIMENSION - 1),
        [0.0] * (ec.EMBEDDING_DIMENSION + 1),
        [True] * ec.EMBEDDING_DIMENSION,
        ["x"] * ec.EMBEDDING_DIMENSION,
        [float("nan")] * ec.EMBEDDING_DIMENSION,
        [float("inf")] * ec.EMBEDDING_DIMENSION,
    ],
    ids=[
        "string",
        "bytes",
        "none",
        "int",
        "mapping",
        "too-short",
        "too-long",
        "bool",
        "non-numeric",
        "nan",
        "inf",
    ],
)
def test_coerce_embedding_vector_rejects_invalid(value: Any) -> None:
    assert ec.coerce_embedding_vector(value) is None


def test_as_cache_vectors_drops_malformed_entries() -> None:
    raw = {
        "good": VALID_VECTOR,
        "short": [0.0] * 3,
        "nan": [float("nan")] * ec.EMBEDDING_DIMENSION,
        "text": "not-a-vector",
    }

    assert ec.as_cache_vectors(raw) == {"good": VALID_VECTOR}


def test_load_cached_embeddings_returns_only_valid_vectors_and_ends_transaction() -> None:
    session = _FakeSession(
        [
            {"model_input_hash": "h1", "embedding": VALID_VECTOR},
            {"model_input_hash": "bad", "embedding": [0.0] * 3},
            {"model_input_hash": "nan", "embedding": [float("nan")] * ec.EMBEDDING_DIMENSION},
            {"model_input_hash": 7, "embedding": VALID_VECTOR},
        ]
    )

    vectors = ec.load_cached_embeddings(
        session_factory(session),
        organization_id=uuid.uuid4(),
        profile_id=uuid.uuid4(),
        model_input_hashes=["h1", "h1", "bad", "nan"],
    )

    assert vectors == {"h1": VALID_VECTOR}
    assert session.rolled_back is True
    # 去重后的 hash 列表按首次出现顺序传给查询。
    _, parameters = session.executions[0]
    assert parameters["hashes"] == ["h1", "bad", "nan"]


def test_load_cached_embeddings_empty_hashes_does_not_open_session() -> None:
    opened = False

    def factory() -> _FakeSession:
        nonlocal opened
        opened = True
        return _FakeSession([])

    assert (
        ec.load_cached_embeddings(
            factory,  # type: ignore[arg-type]
            organization_id=uuid.uuid4(),
            profile_id=uuid.uuid4(),
            model_input_hashes=[],
        )
        == {}
    )
    assert opened is False


def test_load_cached_embeddings_propagates_sqlalchemy_error() -> None:
    session = _FakeSession([], raises=True)

    with pytest.raises(SQLAlchemyError):
        ec.load_cached_embeddings(
            session_factory(session),
            organization_id=uuid.uuid4(),
            profile_id=uuid.uuid4(),
            model_input_hashes=["h1"],
        )
    # 查询失败也显式结束只读事务，不把连接留在坏状态。
    assert session.rolled_back is True


def test_cache_sql_uses_the_authoritative_chain_and_never_trusts_redundant_columns() -> None:
    sql = str(ec.SELECT_EMBEDDING_CACHE_SQL)

    assert "FROM chunk AS c" in sql
    assert "JOIN index_generation AS g ON g.id = c.generation_id" in sql
    assert "JOIN document_version AS dv ON dv.id = g.version_id" in sql
    assert "JOIN document AS d ON d.id = dv.document_id" in sql
    assert "JOIN knowledge_base AS kb ON kb.id = d.kb_id" in sql
    assert "c.model_input_hash IN" in sql
    # 同组织、READY generation、generation 与 embedding 双侧 profile 一致。
    assert "kb.organization_id = :organization_id" in sql
    assert "g.status = 'READY'" in sql
    assert "g.profile_id = :profile_id" in sql
    assert "ce.profile_id = :profile_id" in sql
    assert "ce.embedding::text AS embedding" in sql
    # 排除已删除文档（tombstone 与生命周期两处）。
    assert "d.deleted_at IS NULL" in sql
    assert "d.lifecycle_status <> 'DELETED'" in sql
    # 不信任 chunk 上冗余来源列：既不 JOIN 也不过滤这些列。
    for redundant in (
        "c.organization_id",
        "c.kb_id",
        "c.document_id",
        "c.version_id",
    ):
        assert redundant not in sql
