"""增量 embedding 缓存：从既有 ``chunk_embedding`` 复用同组织、同 profile 的向量。

本模块只做只读批量查询，不新增缓存表、Redis 或进程内 LRU，也不写任何事实。缓存命中必须经
权威链 ``chunk -> index_generation -> document_version -> document -> knowledge_base`` 校验，
不信任 ``chunk`` 上冗余的 ``organization_id``/``kb_id``/``document_id``/``version_id``：

- 同 ``knowledge_base.organization_id``，禁止跨组织复用；
- ``index_generation.status = 'READY'`` 且 ``g.profile_id = :profile_id``；
- ``chunk_embedding.profile_id = :profile_id``；
- ``chunk.model_input_hash`` 命中；
- 排除已删除文档（``deleted_at`` 非空或 ``lifecycle_status = 'DELETED'``）。

允许同组织跨文档与旧版本（发布切换前）复用，不要求缓存来源仍是文档当前 active version；
缓存只复用向量，不复用来源位置，调用方仍为每个 chunk 重建 locator/FTS 并写新 generation。

缓存键边界由不可变 ``index_profile`` 身份与运行期身份预检共同确定：命中行必须与本次目标
``profile_id`` 一致，因此模型 revision、维度、tokenizer/chunker 契约与 normalize 都随 profile
固定；pooling/provider 不是当前可配置自由度，由冻结模型 revision 与 inference 契约约束。
"""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Mapping, Sequence

from sqlalchemy import bindparam, text

from rag_backend.database import SyncSessionFactory

EMBEDDING_DIMENSION = 512

# 缓存命中查询：权威链 + 同组织 + 同 profile（generation 与 embedding 双侧）+ READY + 未删除。
# 每个 model_input_hash 取一条确定性行（最新 created_at / id），同一 hash 的多个历史副本不
# 影响结果；不 JOIN ``chunk`` 冗余来源列，也不要求缓存来源仍是 active version。
SELECT_EMBEDDING_CACHE_SQL = text(
    """
    SELECT DISTINCT ON (c.model_input_hash)
        c.model_input_hash,
        ce.embedding::text AS embedding
    FROM chunk AS c
    JOIN index_generation AS g ON g.id = c.generation_id
    JOIN document_version AS dv ON dv.id = g.version_id
    JOIN document AS d ON d.id = dv.document_id
    JOIN knowledge_base AS kb ON kb.id = d.kb_id
    JOIN chunk_embedding AS ce ON ce.chunk_id = c.id
    WHERE c.model_input_hash IN :hashes
      AND kb.organization_id = :organization_id
      AND g.status = 'READY'
      AND g.profile_id = :profile_id
      AND ce.profile_id = :profile_id
      AND d.deleted_at IS NULL
      AND d.lifecycle_status <> 'DELETED'
    ORDER BY c.model_input_hash, c.created_at DESC, c.id
    """
).bindparams(bindparam("hashes", expanding=True))


def coerce_embedding_vector(value: object) -> list[float] | None:
    """把缓存向量严格校验为 512 维有限 ``float`` 列表；不合法返回 None（按 miss 处理）。

    接受数值序列或 PostgreSQL ``vector::text`` 的 JSON 兼容字面量；字节、维度不为 512、
    含布尔或非数值、含 NaN/Inf 一律拒绝。调用方据此把畸形缓存当作该 hash miss，绝不把
    无效向量写进新 generation。
    """

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return None
    if not isinstance(value, Sequence):
        return None
    if len(value) != EMBEDDING_DIMENSION:
        return None
    vector: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        number = float(item)
        if not math.isfinite(number):
            return None
        vector.append(number)
    return vector


def load_cached_embeddings(
    session_factory: SyncSessionFactory,
    *,
    organization_id: uuid.UUID,
    profile_id: uuid.UUID,
    model_input_hashes: Sequence[str],
) -> dict[str, list[float]]:
    """批量读取可复用的向量；独立短只读事务，显式 ``rollback`` 结束，不写任何状态。

    去重后的每个 hash 最多返回一条向量。查询/连接失败由 ``SQLAlchemyError`` 向上传播，由调用
    方在同一主事务之外回退为全量 miss 编码；单个畸形向量只跳过该 hash 而不整体失败。
    """

    hashes = list(dict.fromkeys(model_input_hashes))
    if not hashes:
        return {}
    with session_factory() as session:
        try:
            rows = (
                session.execute(
                    SELECT_EMBEDDING_CACHE_SQL,
                    {
                        "hashes": hashes,
                        "organization_id": organization_id,
                        "profile_id": profile_id,
                    },
                )
                .mappings()
                .all()
            )
        finally:
            # 独立只读事务显式结束并交还连接；绝不影响调用方的主事务。
            session.rollback()
    vectors: dict[str, list[float]] = {}
    for row in rows:
        model_input_hash = row["model_input_hash"]
        if not isinstance(model_input_hash, str):
            continue
        vector = coerce_embedding_vector(row["embedding"])
        if vector is None:
            continue
        vectors[model_input_hash] = vector
    return vectors


def as_cache_vectors(
    raw: Mapping[str, object],
) -> dict[str, list[float]]:
    """把任意缓存查询结果收敛为只含合法向量的映射；非法向量按 miss 丢弃。"""

    vectors: dict[str, list[float]] = {}
    for model_input_hash, raw_vector in raw.items():
        if not isinstance(model_input_hash, str):
            continue
        vector = coerce_embedding_vector(raw_vector)
        if vector is not None:
            vectors[model_input_hash] = vector
    return vectors


__all__ = [
    "EMBEDDING_DIMENSION",
    "SELECT_EMBEDDING_CACHE_SQL",
    "as_cache_vectors",
    "coerce_embedding_vector",
    "load_cached_embeddings",
]
