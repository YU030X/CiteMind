"""Phase 3 只读探针的真实适配器：PostgreSQL 只读读取、身份/profile 解析与候选 locator 映射。

职责边界：

- 只读数据库：全部 SQL 都是参数化 ``SELECT``，不写库、不建表、不做迁移；每条仓储调用创建并
  追踪一个 ``AsyncSession``，运行结束统一 ``close`` 所有 session 并 ``dispose`` engine。
- DSN 护栏：必须是 ``postgresql+psycopg``，数据库名必须以 ``_test`` 结尾，除非 CLI 用
  ``--allow-database-name`` 精确重申其数据库名；错误消息静态，不回显 DSN 或数据库名。
- 身份与 profile：题集实际角色的 ``username`` 必须在同一组织内唯一命中 ``user_account``；
  所有 scope KB 必须属于同一组织，且 active ``index_profile`` 身份完全一致。artifact schema 只
  允许全局 scalar，因此多 profile 不一致时静态失败，而不是任选其一。
- 候选 locator：只从 ``EvidenceChunkRow.source_locator`` 解析当前评估支持的 ``markdown``/``pdf``；
  其它来源或畸形 locator 静态失败。本模块不猜测来源、不伪造定位。

错误消息不回显 username、查询文本、token 或候选 UUID；调用方负责把错误收敛为静态 CLI 输出。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import bindparam, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from rag_backend.evaluation.dataset import EvaluationQuestion, GoldLocator
from rag_backend.evaluation.probe import (
    CandidateFacts,
    CandidateMapper,
    ProbeIdentity,
    RepositoryFactory,
)
from rag_backend.evaluation.ranking_metrics import RankingCandidate
from rag_backend.evaluation.runner import LogicalRef
from rag_backend.retrieval.keyword_analyzer import get_keyword_analyzer
from rag_backend.retrieval.query_embedding_client import (
    QueryEmbeddingClient,
)
from rag_backend.retrieval.repository import (
    EvidenceChunkRow,
    SqlRetrievalRepository,
)
from rag_backend.retrieval.rerank_client import RerankClient
from rag_backend.retrieval.service import KeywordAnalyzerLike, QueryEmbedder, Reranker

_DATABASE_NAME_SUFFIX = "_test"
_REQUIRED_DRIVER = "postgresql+psycopg"

_KB_ORGANIZATION_SQL = text(
    "SELECT id AS kb_id, organization_id FROM knowledge_base WHERE id IN :kb_ids"
).bindparams(bindparam("kb_ids", expanding=True))

_PROFILE_SQL = text(
    """
    SELECT
        kb.id AS kb_id,
        p.id AS profile_id,
        p.embedding_model AS embedding_model,
        p.model_revision AS model_revision,
        p.dimension AS dimension,
        p.normalize AS normalize,
        p.tokenizer_revision AS tokenizer_revision,
        p.chunker_version AS chunker_version,
        p.keyword_analyzer_version AS keyword_analyzer_version,
        p.config_hash AS config_hash
    FROM knowledge_base AS kb
    JOIN index_profile AS p ON p.id = kb.active_index_profile_id
    WHERE kb.id IN :kb_ids
    """
).bindparams(bindparam("kb_ids", expanding=True))

_ACCOUNT_SQL = text(
    """
    SELECT id AS user_id, username
    FROM user_account
    WHERE organization_id = :organization_id
      AND username IN :usernames
    """
).bindparams(bindparam("usernames", expanding=True))


class ProbeAdapterError(Exception):
    """适配器前置条件或解析失败；消息静态，不回显凭据、UUID 或原始 locator。"""


# ---------------------------------------------------------------------------
# DSN 护栏


def validate_probe_database_url(
    database_url: str, *, allow_database_name: str | None
) -> str:
    """校验只读探针 DSN；返回原值，失败抛静态 :class:`ProbeAdapterError`。

    只接受 ``postgresql+psycopg`` 驱动；数据库名必须以 ``_test`` 结尾，除非
    ``allow_database_name`` 精确等于该数据库名。错误消息不回显 DSN 或数据库名。
    """

    try:
        url = make_url(database_url)
    except (ValueError, ValidationError) as error:
        raise ProbeAdapterError("数据库 DSN 不是合法 URL") from error
    if url.drivername != _REQUIRED_DRIVER:
        raise ProbeAdapterError("数据库 DSN 必须使用 postgresql+psycopg 驱动")
    database_name = url.database or ""
    if not database_name:
        raise ProbeAdapterError("数据库 DSN 缺少数据库名")
    if not database_name.endswith(_DATABASE_NAME_SUFFIX) and database_name != (
        allow_database_name
    ):
        raise ProbeAdapterError(
            "数据库名不以 _test 结尾；如确认不是开发库，请用 --allow-database-name "
            "精确重申其数据库名"
        )
    return database_url


# ---------------------------------------------------------------------------
# 身份与 profile 的只读事实


@dataclass(frozen=True, slots=True)
class ProbeAccountRow:
    """``user_account`` 的只读事实：组织内 username 与用户 id。"""

    username: str
    user_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class ProbeProfileRow:
    """一个 KB 的 active ``index_profile`` 只读事实；``kb_id`` 只用于覆盖校验。"""

    kb_id: uuid.UUID
    profile_id: uuid.UUID
    embedding_model: str
    model_revision: str
    dimension: int
    normalize: bool
    tokenizer_revision: str
    chunker_version: str
    keyword_analyzer_version: str
    config_hash: str

    def identity(self, *, query_contract: str) -> ProbeProfileIdentity:
        return ProbeProfileIdentity(
            profile_id=self.profile_id,
            embedding_model=self.embedding_model,
            model_revision=self.model_revision,
            dimension=self.dimension,
            normalize=self.normalize,
            tokenizer_revision=self.tokenizer_revision,
            chunker_version=self.chunker_version,
            query_contract=query_contract,
            keyword_analyzer_version=self.keyword_analyzer_version,
            config_hash=self.config_hash,
        )


@dataclass(frozen=True, slots=True)
class ProbeProfileIdentity:
    """单次探针全局唯一的 index profile 身份；可转成 artifact 的 scalar 映射。"""

    profile_id: uuid.UUID
    embedding_model: str
    model_revision: str
    dimension: int
    normalize: bool
    tokenizer_revision: str
    chunker_version: str
    query_contract: str
    keyword_analyzer_version: str
    config_hash: str

    def scalars(self) -> dict[str, str | int | float | bool]:
        return {
            "profileId": str(self.profile_id),
            "embeddingModel": self.embedding_model,
            "modelRevision": self.model_revision,
            "dimension": self.dimension,
            "normalize": self.normalize,
            "tokenizerRevision": self.tokenizer_revision,
            "chunkerVersion": self.chunker_version,
            "queryContract": self.query_contract,
            "keywordAnalyzerVersion": self.keyword_analyzer_version,
            "configHash": self.config_hash,
        }


def resolve_single_organization(
    kb_ids: Sequence[uuid.UUID],
    organizations: Mapping[uuid.UUID, uuid.UUID],
) -> uuid.UUID:
    """要求全部 scope KB 都存在且属于同一组织；否则静态失败。"""

    expected = set(kb_ids)
    if not expected:
        raise ProbeAdapterError("题集未覆盖任何 knowledgeBase")
    if set(organizations) != expected:
        raise ProbeAdapterError("scope knowledgeBase 在只读数据库中不完整")
    distinct = set(organizations.values())
    if len(distinct) != 1:
        raise ProbeAdapterError("scope knowledgeBase 不属于同一组织")
    return next(iter(distinct))


def build_role_identities(
    *,
    organization_id: uuid.UUID,
    roles: Mapping[str, str],
    accounts: Sequence[ProbeAccountRow],
) -> dict[str, ProbeIdentity]:
    """把角色 -> username 映射解析为唯一身份；缺失或重复命中都静态失败。"""

    by_username: dict[str, list[uuid.UUID]] = {}
    for row in accounts:
        by_username.setdefault(row.username, []).append(row.user_id)
    identities: dict[str, ProbeIdentity] = {}
    for role, username in roles.items():
        user_ids = by_username.get(username, [])
        if len(user_ids) != 1:
            raise ProbeAdapterError("角色账号在环境中不存在或不唯一")
        identities[role] = ProbeIdentity(
            user_id=user_ids[0], organization_id=organization_id
        )
    return identities


def resolve_single_profile(
    kb_ids: Sequence[uuid.UUID],
    rows: Sequence[ProbeProfileRow],
    *,
    query_contract: str,
) -> ProbeProfileIdentity:
    """要求全部 scope KB 都有 active profile 且身份完全一致；否则静态失败。"""

    expected = set(kb_ids)
    if {row.kb_id for row in rows} != expected:
        raise ProbeAdapterError("scope knowledgeBase 缺少 active index profile")
    identities = {row.identity(query_contract=query_contract) for row in rows}
    if len(identities) != 1:
        raise ProbeAdapterError("scope knowledgeBase 使用了不一致的 index profile")
    return next(iter(identities))


# ---------------------------------------------------------------------------
# locator 解析与候选映射


def _positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ProbeAdapterError("locator 的正整数定位字段非法")
    return value


def _required_str(mapping: Mapping[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ProbeAdapterError("locator 缺少必填字符串字段")
    return value


def parse_evidence_locator(locator: Mapping[str, Any]) -> GoldLocator:
    """把真实 ``source_locator`` 收敛为当前评估支持的 ``GoldLocator``。

    只支持 ``markdown``（1-based ``start_line``/``end_line``，可选字符串数组 ``heading_path``）
    与 ``pdf``（``pages`` 恰好一个 1-based 正整数页）；``web``/``docx`` 或畸形结构静态失败。
    """

    source_type = locator.get("source_type")
    parser_version = _required_str(locator, "parser_version")
    if source_type == "markdown":
        heading_path = locator.get("heading_path", [])
        if not isinstance(heading_path, list) or any(
            not isinstance(item, str) for item in heading_path
        ):
            raise ProbeAdapterError("markdown headingPath 必须是字符串数组")
        start_line = _positive_int(locator.get("start_line"))
        end_line = _positive_int(locator.get("end_line"))
        try:
            return GoldLocator(
                source_type="markdown",
                parser_version=parser_version,
                heading_path=list(heading_path),
                start_line=start_line,
                end_line=end_line,
            )
        except ValidationError as error:
            raise ProbeAdapterError("markdown locator 非法") from error
    if source_type == "pdf":
        pages = locator.get("pages")
        if not isinstance(pages, list) or len(pages) != 1:
            raise ProbeAdapterError("pdf locator 必须恰好包含一个页码")
        page = _positive_int(pages[0])
        try:
            return GoldLocator(
                source_type="pdf", parser_version=parser_version, page=page
            )
        except ValidationError as error:
            raise ProbeAdapterError("pdf locator 非法") from error
    raise ProbeAdapterError("当前评估不支持该来源类型的 locator")


class LocatorCandidateMapper:
    """从真实授权证据行与候选事实构造 ``RankingCandidate``；不伪造来源。"""

    def map(
        self,
        row: EvidenceChunkRow,
        facts: CandidateFacts,
        version_ref: LogicalRef,
    ) -> RankingCandidate:
        locator = parse_evidence_locator(row.source_locator)
        return RankingCandidate(
            candidate_id=str(row.chunk_id),
            kb_id=version_ref.kb_id,
            document_id=version_ref.document_id,
            version=version_ref.version,
            locator=locator,
            rank=facts.rank,
            vector_rank=facts.vector_rank,
            vector_score=facts.vector_score,
            keyword_rank=facts.keyword_rank,
            keyword_score=facts.keyword_score,
            fusion_rank=facts.fusion_rank,
            fusion_score=facts.fusion_score,
            rerank_score=facts.rerank_score,
        )


# ---------------------------------------------------------------------------
# 只读数据库与运行时装配

LoadKbOrganizations = Callable[
    [Sequence[uuid.UUID]], Awaitable[Mapping[uuid.UUID, uuid.UUID]]
]
LoadProfiles = Callable[[Sequence[uuid.UUID]], Awaitable[Sequence[ProbeProfileRow]]]
LoadAccounts = Callable[
    [uuid.UUID, Sequence[str]], Awaitable[Sequence[ProbeAccountRow]]
]
CloseRuntime = Callable[[], Awaitable[None]]


class AsyncProbeDatabase:
    """只读 async SQLAlchemy 管理器：按需创建并追踪 session，统一关闭并 dispose engine。"""

    def __init__(self, database_url: str) -> None:
        try:
            self._engine = create_async_engine(database_url, pool_pre_ping=True)
        except Exception as error:  # noqa: BLE001 - 收敛为静态适配器错误
            raise ProbeAdapterError("只读数据库引擎构造失败") from error
        self._session_factory = async_sessionmaker(
            self._engine, expire_on_commit=False
        )
        self._sessions: list[AsyncSession] = []

    def repository_factory(
        self, role: str, question: EvaluationQuestion
    ) -> SqlRetrievalRepository:
        session = self._session_factory()
        self._sessions.append(session)
        return SqlRetrievalRepository(session)

    async def load_kb_organizations(
        self, kb_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, uuid.UUID]:
        if not kb_ids:
            return {}
        async with self._session_factory() as session:
            result = await session.execute(
                _KB_ORGANIZATION_SQL, {"kb_ids": list(kb_ids)}
            )
            return {
                row["kb_id"]: row["organization_id"] for row in result.mappings()
            }

    async def load_profiles(
        self, kb_ids: Sequence[uuid.UUID]
    ) -> list[ProbeProfileRow]:
        if not kb_ids:
            return []
        async with self._session_factory() as session:
            result = await session.execute(_PROFILE_SQL, {"kb_ids": list(kb_ids)})
            return [
                ProbeProfileRow(
                    kb_id=row["kb_id"],
                    profile_id=row["profile_id"],
                    embedding_model=row["embedding_model"],
                    model_revision=row["model_revision"],
                    dimension=row["dimension"],
                    normalize=row["normalize"],
                    tokenizer_revision=row["tokenizer_revision"],
                    chunker_version=row["chunker_version"],
                    keyword_analyzer_version=row["keyword_analyzer_version"],
                    config_hash=row["config_hash"],
                )
                for row in result.mappings()
            ]

    async def load_accounts(
        self, organization_id: uuid.UUID, usernames: Sequence[str]
    ) -> list[ProbeAccountRow]:
        if not usernames:
            return []
        async with self._session_factory() as session:
            result = await session.execute(
                _ACCOUNT_SQL,
                {"organization_id": organization_id, "usernames": list(usernames)},
            )
            return [
                ProbeAccountRow(username=row["username"], user_id=row["user_id"])
                for row in result.mappings()
            ]

    async def aclose(self) -> None:
        first_error: Exception | None = None
        for session in self._sessions:
            try:
                await session.close()
            except Exception as error:  # noqa: BLE001 - 继续清理其余资源后再报告首错
                if first_error is None:
                    first_error = error
        self._sessions.clear()
        try:
            await self._engine.dispose()
        except Exception as error:  # noqa: BLE001 - 保留首个清理失败
            if first_error is None:
                first_error = error
        if first_error is not None:
            raise ProbeAdapterError("探针数据库资源关闭失败") from first_error


@dataclass
class ProbeRuntime:
    """真实运行所需的适配器集合；``aclose`` 关闭本地推理客户端与数据库。"""

    repository_factory: RepositoryFactory
    mapper: CandidateMapper
    embedder: QueryEmbedder
    analyzer: KeywordAnalyzerLike
    reranker: Reranker
    load_kb_organizations: LoadKbOrganizations
    load_profiles: LoadProfiles
    load_accounts: LoadAccounts
    aclose: CloseRuntime


def build_probe_runtime(
    *,
    database_url: str,
    token: str,
    base_url: str,
    embedding_timeout_seconds: float,
    rerank_timeout_seconds: float,
) -> ProbeRuntime:
    """用显式 token/基址与已校验 DSN 构造真实运行时；构造失败静态收敛。"""

    embedder: QueryEmbeddingClient | None = None
    reranker: RerankClient | None = None
    try:
        # 客户端复用既有 URL 白名单、超时与 token 校验；token 只用显式入参。
        embedder = QueryEmbeddingClient(
            token=token, base_url=base_url, timeout_seconds=embedding_timeout_seconds
        )
        reranker = RerankClient(
            token=token, base_url=base_url, timeout_seconds=rerank_timeout_seconds
        )
        analyzer = get_keyword_analyzer()
    except Exception as error:  # noqa: BLE001 - 收敛为静态适配器错误
        if embedder is not None:
            embedder.close()
        if reranker is not None:
            reranker.close()
        raise ProbeAdapterError("探针运行资源构造失败") from error
    try:
        database = AsyncProbeDatabase(database_url)
    except Exception as error:  # noqa: BLE001 - 收敛为静态适配器错误
        embedder.close()
        reranker.close()
        raise ProbeAdapterError("探针运行资源构造失败") from error

    async def aclose() -> None:
        embedder.close()
        reranker.close()
        await database.aclose()

    return ProbeRuntime(
        repository_factory=database.repository_factory,
        mapper=LocatorCandidateMapper(),
        embedder=embedder,
        analyzer=analyzer,
        reranker=reranker,
        load_kb_organizations=database.load_kb_organizations,
        load_profiles=database.load_profiles,
        load_accounts=database.load_accounts,
        aclose=aclose,
    )


__all__ = [
    "AsyncProbeDatabase",
    "LocatorCandidateMapper",
    "ProbeAccountRow",
    "ProbeAdapterError",
    "ProbeProfileIdentity",
    "ProbeProfileRow",
    "ProbeRuntime",
    "build_probe_runtime",
    "build_role_identities",
    "parse_evidence_locator",
    "resolve_single_organization",
    "resolve_single_profile",
    "validate_probe_database_url",
]
