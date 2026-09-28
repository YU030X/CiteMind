"""授权混合检索的参数化 SQL 仓储。

两条候选路复用同一授权 JOIN：组织来自会话、``kb_member`` 必须未撤销、KB 必须在请求
范围内、``document`` 未删除、``document.active_version_id`` 指向的版本、``index_generation``
为 ``READY`` 且 ``profile_id`` 等于 ``knowledge_base.active_index_profile_id``。向量路与
关键词路都要求 ``chunk_embedding.profile_id`` 与该 active profile 一致，因此检索候选必然
带有匹配 profile 的向量。

归属链以权威关系为准：``chunk -> index_generation -> document_version -> document ->
knowledge_base``，``organization_id``/``kb_id`` 取自 ``knowledge_base``/``document``，不使用
``chunk`` 上冗余的 ``organization_id``/``kb_id``/``document_id``/``version_id`` 列，避免
跨租户脏数据借冗余列泄漏。

SQL 全部使用绑定参数（含列表展开与 pgvector 字面量参数），不做字符串拼接。``LIMIT``
由调用方传入冻结的每路 top-k。仓储只读；``release`` 用 ``rollback`` 结束只读事务，
把连接交还连接池，使调用方能在不持连接的情况下调用模型。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from rag_backend.retrieval.fusion import PATH_TOP_K, RankedChunk

_KB_SCOPE_SQL = text(
    """
    SELECT
        kb.id AS kb_id,
        kb.active_index_profile_id AS profile_id,
        p.model_revision AS model_revision,
        p.dimension AS dimension,
        p.normalize AS normalize,
        p.keyword_analyzer_version AS keyword_analyzer_version
    FROM knowledge_base AS kb
    JOIN kb_member AS m ON m.kb_id = kb.id
    LEFT JOIN index_profile AS p ON p.id = kb.active_index_profile_id
    WHERE m.user_id = :user_id
      AND m.revoked_at IS NULL
      AND kb.organization_id = :organization_id
      AND kb.id IN :kb_ids
    """
).bindparams(bindparam("kb_ids", expanding=True))

# 文档 ACL 只收紧读取：``INHERIT`` 沿用 KB 成员权限；``RESTRICTED`` 时只有 ``document_acl``
# 里显式登记为 ``USER``/``READ`` 的用户可见。两个候选路、证据正文与来源状态复核都用这同
# 一子句，且都绑定 ``:user_id``，不做字符串拼接。
_ACL_ALLOWED_SQL = (
    "("
    "d.acl_mode = 'INHERIT' OR EXISTS ("
    "SELECT 1 FROM document_acl AS a "
    "WHERE a.document_id = d.id "
    "AND a.principal_type = 'USER' "
    "AND a.principal_id = :user_id "
    "AND a.permission = 'READ'"
    "))"
)

_VECTOR_SQL = text(
    f"""
    SELECT
        c.id AS chunk_id,
        d.id AS document_id,
        d.kb_id AS kb_id,
        dv.id AS version_id,
        ce.embedding <=> CAST(:query_vector AS vector) AS distance
    FROM chunk AS c
    JOIN chunk_embedding AS ce ON ce.chunk_id = c.id
    JOIN index_generation AS g ON g.id = c.generation_id
    JOIN document_version AS dv ON dv.id = g.version_id
    JOIN document AS d ON d.id = dv.document_id
    JOIN knowledge_base AS kb ON kb.id = d.kb_id
    JOIN kb_member AS m ON m.kb_id = kb.id
    WHERE m.user_id = :user_id
      AND m.revoked_at IS NULL
      AND kb.organization_id = :organization_id
      AND kb.id IN :kb_ids
      AND kb.active_index_profile_id = :profile_id
      AND g.profile_id = :profile_id
      AND g.status = 'READY'
      AND ce.profile_id = :profile_id
      AND d.deleted_at IS NULL
      AND d.active_version_id = dv.id
      AND dv.status = 'READY'
      AND {_ACL_ALLOWED_SQL}
    ORDER BY distance ASC, c.id ASC
    LIMIT :limit
    """
).bindparams(bindparam("kb_ids", expanding=True))

_KEYWORD_SQL = text(
    f"""
    SELECT
        c.id AS chunk_id,
        d.id AS document_id,
        d.kb_id AS kb_id,
        dv.id AS version_id,
        ts_rank_cd(c.fts, plainto_tsquery('simple', :query_terms)) AS score
    FROM chunk AS c
    JOIN chunk_embedding AS ce ON ce.chunk_id = c.id
    JOIN index_generation AS g ON g.id = c.generation_id
    JOIN document_version AS dv ON dv.id = g.version_id
    JOIN document AS d ON d.id = dv.document_id
    JOIN knowledge_base AS kb ON kb.id = d.kb_id
    JOIN kb_member AS m ON m.kb_id = kb.id
    WHERE m.user_id = :user_id
      AND m.revoked_at IS NULL
      AND kb.organization_id = :organization_id
      AND kb.id IN :kb_ids
      AND kb.active_index_profile_id = :profile_id
      AND g.profile_id = :profile_id
      AND g.status = 'READY'
      AND ce.profile_id = :profile_id
      AND d.deleted_at IS NULL
      AND d.active_version_id = dv.id
      AND dv.status = 'READY'
      AND {_ACL_ALLOWED_SQL}
      AND c.fts @@ plainto_tsquery('simple', :query_terms)
    ORDER BY score DESC, c.id ASC
    LIMIT :limit
    """
).bindparams(bindparam("kb_ids", expanding=True))

# 证据正文读取：与两条候选路复用同一授权 JOIN，按 chunk id 取回原文、版本号与 locator。
_EVIDENCE_SQL = text(
    f"""
    SELECT
        c.id AS chunk_id,
        d.id AS document_id,
        d.kb_id AS kb_id,
        dv.id AS version_id,
        dv.version_no AS version_no,
        d.title AS document_title,
        c.text AS text,
        c.source_locator AS source_locator
    FROM chunk AS c
    JOIN chunk_embedding AS ce ON ce.chunk_id = c.id
    JOIN index_generation AS g ON g.id = c.generation_id
    JOIN document_version AS dv ON dv.id = g.version_id
    JOIN document AS d ON d.id = dv.document_id
    JOIN knowledge_base AS kb ON kb.id = d.kb_id
    JOIN kb_member AS m ON m.kb_id = kb.id
    WHERE m.user_id = :user_id
      AND m.revoked_at IS NULL
      AND kb.organization_id = :organization_id
      AND kb.active_index_profile_id = g.profile_id
      AND g.status = 'READY'
      AND ce.profile_id = g.profile_id
      AND d.deleted_at IS NULL
      AND d.active_version_id = dv.id
      AND dv.status = 'READY'
      AND {_ACL_ALLOWED_SQL}
      AND c.id IN :chunk_ids
    """
).bindparams(bindparam("chunk_ids", expanding=True))

# 来源状态复核：不过滤 active version，供「历史引用是否仍被授权」与「证据版本是否变更」使用。
# ``kb_member`` 必须用 LEFT JOIN：无成员行时 ``revoked_at`` 也是 NULL，若不显式判定行是否存在，
# 缺失成员会被误判成有效成员（撤权/删除成员行后旧引用仍被当作可交付）。ACL 不参与行过滤，
# 只作为一个布尔列返回，避免把 LEFT JOIN 出来的行过滤掉而误判成「已授权」。
_CHUNK_STATE_SQL = text(
    f"""
    SELECT
        c.id AS chunk_id,
        d.id AS document_id,
        dv.id AS version_id,
        d.active_version_id AS active_version_id,
        d.deleted_at AS deleted_at,
        kb.id AS kb_id,
        kb.organization_id AS kb_organization_id,
        (m.user_id IS NOT NULL AND m.revoked_at IS NULL) AS member_active,
        {_ACL_ALLOWED_SQL} AS acl_allowed
    FROM chunk AS c
    JOIN index_generation AS g ON g.id = c.generation_id
    JOIN document_version AS dv ON dv.id = g.version_id
    JOIN document AS d ON d.id = dv.document_id
    JOIN knowledge_base AS kb ON kb.id = d.kb_id
    LEFT JOIN kb_member AS m ON m.kb_id = kb.id AND m.user_id = :user_id
    WHERE c.id IN :chunk_ids
    """
).bindparams(bindparam("chunk_ids", expanding=True))


@dataclass(frozen=True, slots=True, kw_only=True)
class KbScopeRow:
    """KB 授权范围内的一个 KB 及其 active index profile 只读事实。"""

    kb_id: uuid.UUID
    profile_id: uuid.UUID | None
    model_revision: str | None
    dimension: int | None
    normalize: bool | None
    keyword_analyzer_version: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceChunkRow:
    """一条已按权威链受权的证据原文；只用于服务端构造引用与模型上下文。"""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    kb_id: uuid.UUID
    version_id: uuid.UUID
    version_no: int
    document_title: str
    text: str
    source_locator: dict[str, Any]


@dataclass(frozen=True, slots=True, kw_only=True)
class ChunkSourceState:
    """一个引用来源的当前授权与版本状态；调用方据此判定是否仍可交付。

    ``member_active`` 必须是「存在未撤销成员行」，而不是「``revoked_at`` 为 NULL」：成员行被删除
    时 LEFT JOIN 出来同样是 NULL，两者语义不同。
    """

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    active_version_id: uuid.UUID | None
    deleted: bool
    member_active: bool
    in_organization: bool
    acl_allowed: bool

    def is_authorized(self) -> bool:
        """来源是否仍可交付（成员有效、同组织、文档未删除、ACL 放行；允许历史版本）。"""

        return (
            self.member_active
            and self.in_organization
            and not self.deleted
            and self.acl_allowed
        )

    def is_current(self) -> bool:
        """来源是否仍是生成时的 active version（用于版本变化检测）。"""

        return self.is_authorized() and self.active_version_id == self.version_id


def vector_literal(vector: Sequence[float]) -> str:
    """把查询向量渲染成 pgvector 可解析的 ``[v0,v1,...]`` 参数值；仅用于绑定。"""

    return json.dumps([float(value) for value in vector], allow_nan=False)


class RetrievalRepository(Protocol):
    """检索读取接口；``release`` 必须结束当前只读事务并交还连接。"""

    async def load_kb_scope(
        self,
        *,
        user_id: uuid.UUID,
        organization_id: uuid.UUID,
        kb_ids: Sequence[uuid.UUID],
    ) -> list[KbScopeRow]: ...

    async def release(self) -> None: ...

    async def fetch_vector_candidates(
        self,
        *,
        user_id: uuid.UUID,
        organization_id: uuid.UUID,
        kb_ids: Sequence[uuid.UUID],
        profile_id: uuid.UUID,
        query_vector: Sequence[float],
    ) -> list[RankedChunk]: ...

    async def fetch_keyword_candidates(
        self,
        *,
        user_id: uuid.UUID,
        organization_id: uuid.UUID,
        kb_ids: Sequence[uuid.UUID],
        profile_id: uuid.UUID,
        query_terms: str,
    ) -> list[RankedChunk]: ...


class EvidenceRepository(Protocol):
    """证据读取与来源复核接口；与检索共用同一权威授权链。"""

    async def load_evidence_chunks(
        self,
        *,
        user_id: uuid.UUID,
        organization_id: uuid.UUID,
        chunk_ids: Sequence[uuid.UUID],
    ) -> list[EvidenceChunkRow]: ...

    async def load_chunk_source_states(
        self,
        *,
        user_id: uuid.UUID,
        organization_id: uuid.UUID,
        chunk_ids: Sequence[uuid.UUID],
    ) -> list[ChunkSourceState]: ...

    async def release(self) -> None: ...


class SqlRetrievalRepository:
    """基于调用方 ``AsyncSession`` 的只读实现；不提交、不写库。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_kb_scope(
        self,
        *,
        user_id: uuid.UUID,
        organization_id: uuid.UUID,
        kb_ids: Sequence[uuid.UUID],
    ) -> list[KbScopeRow]:
        result = await self._session.execute(
            _KB_SCOPE_SQL,
            {
                "user_id": user_id,
                "organization_id": organization_id,
                "kb_ids": list(kb_ids),
            },
        )
        return [
            KbScopeRow(
                kb_id=row["kb_id"],
                profile_id=row["profile_id"],
                model_revision=row["model_revision"],
                dimension=row["dimension"],
                normalize=row["normalize"],
                keyword_analyzer_version=row["keyword_analyzer_version"],
            )
            for row in result.mappings().all()
        ]

    async def release(self) -> None:
        """结束只读事务、交还连接；不改变任何业务状态。"""

        await self._session.rollback()

    async def fetch_vector_candidates(
        self,
        *,
        user_id: uuid.UUID,
        organization_id: uuid.UUID,
        kb_ids: Sequence[uuid.UUID],
        profile_id: uuid.UUID,
        query_vector: Sequence[float],
    ) -> list[RankedChunk]:
        result = await self._session.execute(
            _VECTOR_SQL,
            {
                "user_id": user_id,
                "organization_id": organization_id,
                "kb_ids": list(kb_ids),
                "profile_id": profile_id,
                "query_vector": vector_literal(query_vector),
                "limit": PATH_TOP_K,
            },
        )
        candidates: list[RankedChunk] = []
        for row in result.mappings().all():
            # pgvector ``<=>`` 是余弦距离；对外统一为越高越好的相似度。
            candidates.append(self._to_ranked(row, score=1.0 - float(row["distance"])))
        return candidates

    async def fetch_keyword_candidates(
        self,
        *,
        user_id: uuid.UUID,
        organization_id: uuid.UUID,
        kb_ids: Sequence[uuid.UUID],
        profile_id: uuid.UUID,
        query_terms: str,
    ) -> list[RankedChunk]:
        result = await self._session.execute(
            _KEYWORD_SQL,
            {
                "user_id": user_id,
                "organization_id": organization_id,
                "kb_ids": list(kb_ids),
                "profile_id": profile_id,
                "query_terms": query_terms,
                "limit": PATH_TOP_K,
            },
        )
        return [self._to_ranked(row, score=float(row["score"])) for row in result.mappings().all()]

    @staticmethod
    def _to_ranked(row: Any, *, score: float) -> RankedChunk:
        return RankedChunk(
            chunk_id=row["chunk_id"],
            document_id=row["document_id"],
            kb_id=row["kb_id"],
            version_id=row["version_id"],
            score=score,
        )

    async def load_evidence_chunks(
        self,
        *,
        user_id: uuid.UUID,
        organization_id: uuid.UUID,
        chunk_ids: Sequence[uuid.UUID],
    ) -> list[EvidenceChunkRow]:
        if not chunk_ids:
            return []
        result = await self._session.execute(
            _EVIDENCE_SQL,
            {
                "user_id": user_id,
                "organization_id": organization_id,
                "chunk_ids": list(chunk_ids),
            },
        )
        return [
            EvidenceChunkRow(
                chunk_id=row["chunk_id"],
                document_id=row["document_id"],
                kb_id=row["kb_id"],
                version_id=row["version_id"],
                version_no=int(row["version_no"]),
                document_title=str(row["document_title"]),
                text=str(row["text"]),
                source_locator=dict(row["source_locator"]),
            )
            for row in result.mappings().all()
        ]

    async def load_chunk_source_states(
        self,
        *,
        user_id: uuid.UUID,
        organization_id: uuid.UUID,
        chunk_ids: Sequence[uuid.UUID],
    ) -> list[ChunkSourceState]:
        if not chunk_ids:
            return []
        result = await self._session.execute(
            _CHUNK_STATE_SQL,
            {"user_id": user_id, "chunk_ids": list(chunk_ids)},
        )
        return [
            ChunkSourceState(
                chunk_id=row["chunk_id"],
                document_id=row["document_id"],
                version_id=row["version_id"],
                active_version_id=row["active_version_id"],
                deleted=row["deleted_at"] is not None,
                # 无成员行与已撤销成员一样都是未授权；不能只看 ``revoked_at IS NULL``。
                member_active=bool(row["member_active"]),
                in_organization=row["kb_organization_id"] == organization_id,
                acl_allowed=bool(row["acl_allowed"]),
            )
            for row in result.mappings().all()
        ]


__all__ = [
    "ChunkSourceState",
    "EvidenceChunkRow",
    "EvidenceRepository",
    "KbScopeRow",
    "RetrievalRepository",
    "SqlRetrievalRepository",
    "vector_literal",
]
