"""相邻证据查询的 SQL 结构与参数绑定单测：不连接数据库。

只断言 ``load_adjacent_evidence_chunks`` 生成的 SQL 文本与绑定参数形状：邻居必须与直接证据
一样经过完整权威链（组织、未撤销成员、active profile、READY generation、匹配 profile 的
embedding、active version、文档 ACL）重新授权，并且不依赖 ``chunk`` 上的冗余归属列。真实
数据库上的跨 KB/组织/旧版本/ACL 负例由集成用例单独定义，本文件不声称已验证 SQL 行为。
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from rag_backend.retrieval.repository import (
    AdjacentEvidenceChunk,
    EvidenceChunkRow,
    EvidenceRepository,
    SqlRetrievalRepository,
)
from sqlalchemy.ext.asyncio import AsyncSession

USER_ID = uuid.uuid4()
ORGANIZATION_ID = uuid.uuid4()
SEED_ONE = uuid.uuid4()
SEED_TWO = uuid.uuid4()


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _Result:
        return self

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)


class _CapturingSession:
    """记录 ``execute`` 的语句与参数，并按需返回预置 mapping 行。"""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.rollbacks = 0

    async def execute(self, statement: Any, params: dict[str, Any]) -> _Result:
        self.executed.append((str(statement), params))
        return _Result(self.rows)

    async def rollback(self) -> None:
        self.rollbacks += 1


def _repository(session: _CapturingSession) -> SqlRetrievalRepository:
    return SqlRetrievalRepository(cast(AsyncSession, session))


@pytest.mark.anyio
async def test_adjacent_sql_reauthorizes_full_authority_chain_and_acl() -> None:
    session = _CapturingSession()
    await _repository(session).load_adjacent_evidence_chunks(
        user_id=USER_ID, organization_id=ORGANIZATION_ID, chunk_ids=[SEED_ONE, SEED_TWO]
    )

    assert len(session.executed) == 1
    sql, params = session.executed[0]
    # 同 generation 的相邻 index 配对。
    assert "c.generation_id = seed.generation_id" in sql
    assert "seed.chunk_index - 1" in sql
    assert "seed.chunk_index + 1" in sql
    # 与直接证据相同的授权谓词与 ACL。
    assert "m.user_id = :user_id" in sql
    assert "m.revoked_at IS NULL" in sql
    assert "kb.organization_id = :organization_id" in sql
    assert "kb.active_index_profile_id = g.profile_id" in sql
    assert "g.status = 'READY'" in sql
    assert "ce.profile_id = g.profile_id" in sql
    assert "d.deleted_at IS NULL" in sql
    assert "d.active_version_id = dv.id" in sql
    assert "dv.status = 'READY'" in sql
    assert "document_acl" in sql
    # 不使用 chunk 上的冗余归属列判定归属。
    for redundant in ("c.organization_id", "c.kb_id", "c.document_id", "c.version_id"):
        assert redundant not in sql
    # 值全部参数绑定，不把 UUID 或组织拼进 SQL 文本。
    assert params["user_id"] == USER_ID
    assert params["organization_id"] == ORGANIZATION_ID
    assert params["chunk_ids"] == [SEED_ONE, SEED_TWO]
    assert str(USER_ID) not in sql
    assert str(SEED_ONE) not in sql


@pytest.mark.anyio
async def test_adjacent_query_is_skipped_without_seeds() -> None:
    session = _CapturingSession()
    rows = await _repository(session).load_adjacent_evidence_chunks(
        user_id=USER_ID, organization_id=ORGANIZATION_ID, chunk_ids=[]
    )

    assert rows == []
    assert session.executed == []


@pytest.mark.anyio
async def test_adjacent_rows_map_seed_relation_and_evidence_fields() -> None:
    chunk_id = uuid.uuid4()
    document_id = uuid.uuid4()
    kb_id = uuid.uuid4()
    version_id = uuid.uuid4()
    session = _CapturingSession(
        [
            {
                "seed_chunk_id": SEED_ONE,
                "seed_chunk_index": 5,
                "chunk_index": 6,
                "chunk_id": chunk_id,
                "document_id": document_id,
                "kb_id": kb_id,
                "version_id": version_id,
                "version_no": 3,
                "document_title": "制度文档",
                "text": "相邻补充片段。",
                "source_locator": {"page": 6},
            }
        ]
    )

    rows = await _repository(session).load_adjacent_evidence_chunks(
        user_id=USER_ID, organization_id=ORGANIZATION_ID, chunk_ids=[SEED_ONE]
    )

    assert len(rows) == 1
    item = rows[0]
    assert isinstance(item, AdjacentEvidenceChunk)
    assert item.seed_chunk_id == SEED_ONE
    assert item.seed_chunk_index == 5
    assert item.chunk_index == 6
    assert item.chunk == EvidenceChunkRow(
        chunk_id=chunk_id,
        document_id=document_id,
        kb_id=kb_id,
        version_id=version_id,
        version_no=3,
        document_title="制度文档",
        text="相邻补充片段。",
        source_locator={"page": 6},
    )


def test_evidence_repository_declares_adjacent_method() -> None:
    """协议与实现都声明相邻查询；避免 fake 或调用方漏实现后静默通过。"""

    assert "load_adjacent_evidence_chunks" in EvidenceRepository.__dict__
    assert hasattr(SqlRetrievalRepository, "load_adjacent_evidence_chunks")
