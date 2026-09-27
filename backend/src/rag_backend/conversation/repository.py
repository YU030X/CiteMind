"""问答会话的参数化 SQL 仓储。

持有调用方 ``AsyncSession``，不自行创建连接；``commit`` 与 ``release`` 由用例显式调用，
使模型调用期间不持有事务。所有者隔离在 SQL 内完成（``owner_id`` 与 ``organization_id``），
引用读取还必须经 ``message → conversation`` 回到所有者，避免跨会话读取引用。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, cast

from sqlalchemy import bindparam, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

_LIST_CONVERSATIONS_SQL = text(
    """
    SELECT c.id,
           c.kb_scope,
           c.title,
           c.pinned_at,
           c.created_at,
           (SELECT max(m.created_at) FROM message AS m
            WHERE m.conversation_id = c.id) AS last_message_at
    FROM conversation AS c
    WHERE c.owner_id = :owner_id
      AND c.organization_id = :organization_id
      AND c.deleted_at IS NULL
    """
)

_LOAD_CONVERSATION_SQL = text(
    """
    SELECT id, organization_id, owner_id, kb_scope, title, pinned_at, created_at,
           updated_at, deleted_at
    FROM conversation
    WHERE id = :conversation_id
      AND owner_id = :owner_id
      AND organization_id = :organization_id
      AND deleted_at IS NULL
    """
)

# 单会话摘要读取：PATCH 写完后按同一所有者隔离返回最新标题/置顶与最近消息时间。
_LOAD_CONVERSATION_SUMMARY_SQL = text(
    """
    SELECT c.id,
           c.kb_scope,
           c.title,
           c.pinned_at,
           c.created_at,
           (SELECT max(m.created_at) FROM message AS m
            WHERE m.conversation_id = c.id) AS last_message_at
    FROM conversation AS c
    WHERE c.id = :conversation_id
      AND c.owner_id = :owner_id
      AND c.organization_id = :organization_id
      AND c.deleted_at IS NULL
    """
)

# 标题与置顶都按列级 UPDATE 授权；未提交的 None 表示不改动该字段。
_UPDATE_CONVERSATION_SQL = text(
    """
    UPDATE conversation
    SET title = COALESCE(CAST(:title AS text), title),
        pinned_at = CASE
            WHEN CAST(:pinned AS boolean) IS NULL THEN pinned_at
            WHEN CAST(:pinned AS boolean) THEN COALESCE(pinned_at, now())
            ELSE NULL
        END,
        updated_at = now()
    WHERE id = :conversation_id
      AND owner_id = :owner_id
      AND organization_id = :organization_id
      AND deleted_at IS NULL
    """
)

# 软删：只写 deleted_at/updated_at；二次删除（rowcount = 0）由调用方再判是否存在。
_SOFT_DELETE_CONVERSATION_SQL = text(
    """
    UPDATE conversation
    SET deleted_at = now(),
        updated_at = now()
    WHERE id = :conversation_id
      AND owner_id = :owner_id
      AND organization_id = :organization_id
      AND deleted_at IS NULL
    """
)

# 所有者隔离的存在性检查：不过滤 deleted_at，用于让重复删除保持幂等 204。
_CONVERSATION_EXISTS_SQL = text(
    """
    SELECT 1 FROM conversation
    WHERE id = :conversation_id
      AND owner_id = :owner_id
      AND organization_id = :organization_id
    """
)

# 首轮提问派生标题：只在尚未有标题时写入，不覆盖用户后来的改名。
_SET_TITLE_IF_EMPTY_SQL = text(
    """
    UPDATE conversation
    SET title = CAST(:title AS text),
        updated_at = now()
    WHERE id = :conversation_id
      AND title IS NULL
      AND deleted_at IS NULL
    """
)

_INSERT_CONVERSATION_SQL = text(
    """
    INSERT INTO conversation (id, organization_id, owner_id, kb_scope)
    VALUES (:id, :organization_id, :owner_id, CAST(:kb_scope AS jsonb))
    RETURNING created_at
    """
)

_LIST_MESSAGES_SQL = text(
    """
    SELECT id, sequence, role, content, query_run_id, created_at
    FROM message
    WHERE conversation_id = :conversation_id
    ORDER BY sequence ASC
    """
)

_CITATION_COLUMNS = (
    "ci.id AS citation_id, ci.message_id, ci.display_label, ci.chunk_id, ci.version_id, "
    "ci.locator_snapshot, ci.quote, ci.quote_hash, dv.version_no AS version_no, "
    "d.id AS document_id, d.title AS document_title, "
    # 是否仍是文档当前版本：以权威 ``document.active_version_id`` 对比引用快照的 ``version_id``，
    # 两者都为 NULL 时才可能相等，显式要求 active 非 NULL，避免把已清空指针当成匹配。
    "(d.active_version_id IS NOT NULL AND d.active_version_id = ci.version_id) "
    "AS is_current_version"
)

_LIST_CITATIONS_SQL = text(
    f"""
    SELECT {_CITATION_COLUMNS}
    FROM citation AS ci
    JOIN document_version AS dv ON dv.id = ci.version_id
    JOIN document AS d ON d.id = dv.document_id
    WHERE ci.message_id IN :message_ids
    ORDER BY ci.display_label ASC
    """
).bindparams(bindparam("message_ids", expanding=True))

_LOAD_CITATION_FOR_OWNER_SQL = text(
    f"""
    SELECT {_CITATION_COLUMNS}
    FROM citation AS ci
    JOIN message AS m ON m.id = ci.message_id
    JOIN conversation AS cv ON cv.id = m.conversation_id
    JOIN document_version AS dv ON dv.id = ci.version_id
    JOIN document AS d ON d.id = dv.document_id
    WHERE ci.id = :citation_id
      AND cv.owner_id = :owner_id
      AND cv.organization_id = :organization_id
      AND cv.deleted_at IS NULL
    """
)

# 会话级 advisory 事务锁：序列化同一会话的并发序号分配，且不需要给 api 角色任何 UPDATE
# 权限（``SELECT ... FOR UPDATE`` 会额外要求 UPDATE 权限，而会话表在应用语义上是只写一次）。
_LOCK_CONVERSATION_SQL = text(
    "SELECT pg_advisory_xact_lock(hashtextextended(cast(:conversation_id AS text), 0))"
)

_NEXT_SEQUENCE_SQL = text(
    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM message WHERE conversation_id = :conversation_id"
)

_INSERT_QUERY_RUN_SQL = text(
    """
    INSERT INTO query_run (
        id, conversation_id, question, standalone_question, request_id,
        scope_snapshot, input_token_budget, output_token_budget,
        estimated_input_tokens, evidence_count, status, insufficient_evidence,
        degraded_stages, llm_usage_id, provider_prompt_tokens, provider_completion_tokens,
        generation_options
    ) VALUES (
        :id, :conversation_id, :question, :standalone_question, :request_id,
        CAST(:scope_snapshot AS jsonb), :input_token_budget, :output_token_budget,
        :estimated_input_tokens, :evidence_count, :status, :insufficient_evidence,
        CAST(:degraded_stages AS jsonb), :llm_usage_id, :provider_prompt_tokens,
        :provider_completion_tokens, CAST(:generation_options AS jsonb)
    )
    """
)

_INSERT_MESSAGE_SQL = text(
    """
    INSERT INTO message (id, conversation_id, sequence, role, content, query_run_id)
    VALUES (:id, :conversation_id, :sequence, :role, :content, :query_run_id)
    """
)

_INSERT_CITATION_SQL = text(
    """
    INSERT INTO citation (
        id, message_id, query_run_id, chunk_id, version_id, display_label,
        locator_snapshot, quote, quote_hash
    ) VALUES (
        :id, :message_id, :query_run_id, :chunk_id, :version_id, :display_label,
        CAST(:locator_snapshot AS jsonb), :quote, :quote_hash
    )
    """
)

# 与一次性探针使用同一张 append-only 账本；这里只写 provider 实际上报的事实。
_INSERT_LLM_USAGE_SQL = text(
    """
    INSERT INTO llm_usage (
        id, provider, model, stage, status, error_code, usage_source, attempt,
        prompt_tokens, completion_tokens, prompt_cache_hit_tokens,
        prompt_cache_miss_tokens, latency_ms
    ) VALUES (
        :id, :provider, :model, :stage, :status, :error_code, :usage_source, :attempt,
        :prompt_tokens, :completion_tokens, :prompt_cache_hit_tokens,
        :prompt_cache_miss_tokens, :latency_ms
    )
    """
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationRow:
    """一次会话的持久事实。"""

    id: uuid.UUID
    organization_id: uuid.UUID
    owner_id: uuid.UUID
    kb_scope: tuple[uuid.UUID, ...]
    created_at: datetime
    updated_at: datetime
    title: str | None = None
    pinned_at: datetime | None = None
    deleted_at: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationSummaryRow:
    """会话列表的一行；``last_message_at`` 为空表示尚无消息，不含正文。"""

    id: uuid.UUID
    kb_scope: tuple[uuid.UUID, ...]
    created_at: datetime
    last_message_at: datetime | None
    title: str | None = None
    pinned_at: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredMessage:
    """会话内一条按 ``sequence`` 排序的消息。"""

    id: uuid.UUID
    sequence: int
    role: str
    content: str
    query_run_id: uuid.UUID | None
    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredCitation:
    """一条引用快照及其来源版本信息。"""

    id: uuid.UUID
    message_id: uuid.UUID
    display_label: str
    chunk_id: uuid.UUID
    version_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    version_no: int
    locator: dict[str, Any]
    quote: str
    quote_hash: str
    is_current_version: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class LlmUsageRecord:
    """一次 provider attempt 的账本事实；token 缺失时必须为 None，不得伪造。"""

    id: uuid.UUID
    provider: str
    model: str
    stage: str
    status: str
    error_code: str | None
    usage_source: str
    attempt: int
    prompt_tokens: int | None
    completion_tokens: int | None
    prompt_cache_hit_tokens: int | None
    prompt_cache_miss_tokens: int | None
    latency_ms: int


@dataclass(frozen=True, slots=True, kw_only=True)
class QueryRunRecord:
    """一次问答运行的落库事实。"""

    id: uuid.UUID
    conversation_id: uuid.UUID
    question: str
    standalone_question: str
    request_id: str | None
    scope_snapshot: tuple[uuid.UUID, ...]
    input_token_budget: int
    output_token_budget: int
    estimated_input_tokens: int | None
    evidence_count: int
    status: str
    insufficient_evidence: bool
    degraded_stages: tuple[str, ...]
    llm_usage_id: uuid.UUID | None
    provider_prompt_tokens: int | None
    provider_completion_tokens: int | None
    # 本轮实际使用的生成选项（模型/思考开关/强度）；每轮独立快照，不被后续选择改写。
    generation_options: dict[str, Any]


@dataclass(frozen=True, slots=True, kw_only=True)
class MessageRecord:
    """一条待写入的消息；``sequence`` 由仓储在会话行锁内分配。"""

    id: uuid.UUID
    conversation_id: uuid.UUID
    sequence: int
    role: str
    content: str
    query_run_id: uuid.UUID | None


@dataclass(frozen=True, slots=True, kw_only=True)
class CitationRecord:
    """一条待写入的引用快照；locator 与 quote 都来自服务端读取的 chunk。"""

    id: uuid.UUID
    message_id: uuid.UUID
    query_run_id: uuid.UUID
    chunk_id: uuid.UUID
    version_id: uuid.UUID
    display_label: str
    locator: dict[str, Any]
    quote: str
    quote_hash: str


class ConversationRepository(Protocol):
    """问答会话读写接口；``commit``/``release`` 由调用方显式控制事务边界。"""

    async def load_conversation(
        self,
        *,
        conversation_id: uuid.UUID,
        owner_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> ConversationRow | None: ...

    async def insert_conversation(
        self,
        *,
        conversation_id: uuid.UUID,
        organization_id: uuid.UUID,
        owner_id: uuid.UUID,
        kb_scope: Sequence[uuid.UUID],
    ) -> datetime: ...

    async def load_conversation_summary(
        self,
        *,
        conversation_id: uuid.UUID,
        owner_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> ConversationSummaryRow | None: ...

    async def update_conversation_metadata(
        self,
        *,
        conversation_id: uuid.UUID,
        owner_id: uuid.UUID,
        organization_id: uuid.UUID,
        title: str | None,
        pinned: bool | None,
    ) -> bool: ...

    async def soft_delete_conversation(
        self,
        *,
        conversation_id: uuid.UUID,
        owner_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> bool: ...

    async def set_conversation_title_if_empty(
        self, *, conversation_id: uuid.UUID, title: str
    ) -> None: ...

    async def list_messages(self, *, conversation_id: uuid.UUID) -> list[StoredMessage]: ...

    async def list_citations(
        self, *, message_ids: Sequence[uuid.UUID]
    ) -> list[StoredCitation]: ...

    async def load_citation_for_owner(
        self,
        *,
        citation_id: uuid.UUID,
        owner_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> StoredCitation | None: ...

    async def next_message_sequence(self, *, conversation_id: uuid.UUID) -> int: ...

    async def insert_query_run(self, record: QueryRunRecord) -> None: ...

    async def insert_message(self, record: MessageRecord) -> None: ...

    async def insert_citation(self, record: CitationRecord) -> None: ...

    async def insert_llm_usage(self, record: LlmUsageRecord) -> None: ...

    async def commit(self) -> None: ...

    async def release(self) -> None: ...


class SqlConversationRepository:
    """基于调用方 ``AsyncSession`` 的实现；不自行提交或回滚，除非调用方要求。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_conversation(
        self,
        *,
        conversation_id: uuid.UUID,
        owner_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> ConversationRow | None:
        row = (
            await self._session.execute(
                _LOAD_CONVERSATION_SQL,
                {
                    "conversation_id": conversation_id,
                    "owner_id": owner_id,
                    "organization_id": organization_id,
                },
            )
        ).mappings().first()
        if row is None:
            return None
        return ConversationRow(
            id=row["id"],
            organization_id=row["organization_id"],
            owner_id=row["owner_id"],
            kb_scope=tuple(uuid.UUID(str(item)) for item in row["kb_scope"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            title=row["title"],
            pinned_at=row["pinned_at"],
            deleted_at=row["deleted_at"],
        )

    async def list_conversations(
        self, *, owner_id: uuid.UUID, organization_id: uuid.UUID
    ) -> list[ConversationSummaryRow]:
        raw_rows = (
            await self._session.execute(
                _LIST_CONVERSATIONS_SQL,
                {"owner_id": owner_id, "organization_id": organization_id},
            )
        ).mappings().all()
        return [self._to_summary(row) for row in raw_rows]

    async def load_conversation_summary(
        self,
        *,
        conversation_id: uuid.UUID,
        owner_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> ConversationSummaryRow | None:
        row = (
            await self._session.execute(
                _LOAD_CONVERSATION_SUMMARY_SQL,
                {
                    "conversation_id": conversation_id,
                    "owner_id": owner_id,
                    "organization_id": organization_id,
                },
            )
        ).mappings().first()
        return None if row is None else self._to_summary(row)

    async def update_conversation_metadata(
        self,
        *,
        conversation_id: uuid.UUID,
        owner_id: uuid.UUID,
        organization_id: uuid.UUID,
        title: str | None,
        pinned: bool | None,
    ) -> bool:
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                _UPDATE_CONVERSATION_SQL,
                {
                    "conversation_id": conversation_id,
                    "owner_id": owner_id,
                    "organization_id": organization_id,
                    "title": title,
                    "pinned": pinned,
                },
            ),
        )
        return result.rowcount == 1

    async def soft_delete_conversation(
        self,
        *,
        conversation_id: uuid.UUID,
        owner_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> bool:
        # 与追加追问的序号分配共用同一会话级 advisory 锁：删除与「查删除 + 插入」互相串行，
        # 让提交前的重查能确定看到已提交的删除，避免回答在删除后复活。
        await self._session.execute(
            _LOCK_CONVERSATION_SQL, {"conversation_id": conversation_id}
        )
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                _SOFT_DELETE_CONVERSATION_SQL,
                {
                    "conversation_id": conversation_id,
                    "owner_id": owner_id,
                    "organization_id": organization_id,
                },
            ),
        )
        if result.rowcount == 1:
            return True
        # 已删除（重复删除）仍返回 True 让路由保持幂等 204；越权/不存在返回 False。
        exists = await self._session.scalar(
            _CONVERSATION_EXISTS_SQL,
            {
                "conversation_id": conversation_id,
                "owner_id": owner_id,
                "organization_id": organization_id,
            },
        )
        return exists is not None

    async def set_conversation_title_if_empty(
        self, *, conversation_id: uuid.UUID, title: str
    ) -> None:
        await self._session.execute(
            _SET_TITLE_IF_EMPTY_SQL,
            {"conversation_id": conversation_id, "title": title},
        )

    async def insert_conversation(
        self,
        *,
        conversation_id: uuid.UUID,
        organization_id: uuid.UUID,
        owner_id: uuid.UUID,
        kb_scope: Sequence[uuid.UUID],
    ) -> datetime:
        created_at = await self._session.scalar(
            _INSERT_CONVERSATION_SQL,
            {
                "id": conversation_id,
                "organization_id": organization_id,
                "owner_id": owner_id,
                "kb_scope": json.dumps([str(item) for item in kb_scope]),
            },
        )
        assert isinstance(created_at, datetime)
        return created_at

    async def list_messages(self, *, conversation_id: uuid.UUID) -> list[StoredMessage]:
        rows = (
            await self._session.execute(
                _LIST_MESSAGES_SQL, {"conversation_id": conversation_id}
            )
        ).mappings().all()
        return [
            StoredMessage(
                id=row["id"],
                sequence=int(row["sequence"]),
                role=str(row["role"]),
                content=str(row["content"]),
                query_run_id=row["query_run_id"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def list_citations(
        self, *, message_ids: Sequence[uuid.UUID]
    ) -> list[StoredCitation]:
        if not message_ids:
            return []
        rows = (
            await self._session.execute(
                _LIST_CITATIONS_SQL, {"message_ids": list(message_ids)}
            )
        ).mappings().all()
        return [self._to_citation(row) for row in rows]

    async def load_citation_for_owner(
        self,
        *,
        citation_id: uuid.UUID,
        owner_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> StoredCitation | None:
        row = (
            await self._session.execute(
                _LOAD_CITATION_FOR_OWNER_SQL,
                {
                    "citation_id": citation_id,
                    "owner_id": owner_id,
                    "organization_id": organization_id,
                },
            )
        ).mappings().first()
        return None if row is None else self._to_citation(row)

    async def next_message_sequence(self, *, conversation_id: uuid.UUID) -> int:
        # 会话行锁把同一会话的并发追问串行化，避免 (conversation_id, sequence) 冲突。
        await self._session.execute(
            _LOCK_CONVERSATION_SQL, {"conversation_id": conversation_id}
        )
        value = await self._session.scalar(
            _NEXT_SEQUENCE_SQL, {"conversation_id": conversation_id}
        )
        return int(value or 1)

    async def insert_query_run(self, record: QueryRunRecord) -> None:
        await self._session.execute(
            _INSERT_QUERY_RUN_SQL,
            {
                "id": record.id,
                "conversation_id": record.conversation_id,
                "question": record.question,
                "standalone_question": record.standalone_question,
                "request_id": record.request_id,
                "scope_snapshot": json.dumps([str(item) for item in record.scope_snapshot]),
                "input_token_budget": record.input_token_budget,
                "output_token_budget": record.output_token_budget,
                "estimated_input_tokens": record.estimated_input_tokens,
                "evidence_count": record.evidence_count,
                "status": record.status,
                "insufficient_evidence": record.insufficient_evidence,
                "degraded_stages": json.dumps(list(record.degraded_stages)),
                "llm_usage_id": record.llm_usage_id,
                "provider_prompt_tokens": record.provider_prompt_tokens,
                "provider_completion_tokens": record.provider_completion_tokens,
                "generation_options": json.dumps(record.generation_options),
            },
        )

    async def insert_message(self, record: MessageRecord) -> None:
        await self._session.execute(
            _INSERT_MESSAGE_SQL,
            {
                "id": record.id,
                "conversation_id": record.conversation_id,
                "sequence": record.sequence,
                "role": record.role,
                "content": record.content,
                "query_run_id": record.query_run_id,
            },
        )

    async def insert_citation(self, record: CitationRecord) -> None:
        await self._session.execute(
            _INSERT_CITATION_SQL,
            {
                "id": record.id,
                "message_id": record.message_id,
                "query_run_id": record.query_run_id,
                "chunk_id": record.chunk_id,
                "version_id": record.version_id,
                "display_label": record.display_label,
                "locator_snapshot": json.dumps(record.locator),
                "quote": record.quote,
                "quote_hash": record.quote_hash,
            },
        )

    async def insert_llm_usage(self, record: LlmUsageRecord) -> None:
        await self._session.execute(
            _INSERT_LLM_USAGE_SQL,
            {
                "id": record.id,
                "provider": record.provider,
                "model": record.model,
                "stage": record.stage,
                "status": record.status,
                "error_code": record.error_code,
                "usage_source": record.usage_source,
                "attempt": record.attempt,
                "prompt_tokens": record.prompt_tokens,
                "completion_tokens": record.completion_tokens,
                "prompt_cache_hit_tokens": record.prompt_cache_hit_tokens,
                "prompt_cache_miss_tokens": record.prompt_cache_miss_tokens,
                "latency_ms": record.latency_ms,
            },
        )

    async def commit(self) -> None:
        await self._session.commit()

    async def release(self) -> None:
        await self._session.rollback()

    @staticmethod
    def _to_summary(row: Any) -> ConversationSummaryRow:
        return ConversationSummaryRow(
            id=row["id"],
            kb_scope=tuple(uuid.UUID(str(item)) for item in row["kb_scope"]),
            created_at=row["created_at"],
            last_message_at=row["last_message_at"],
            title=row["title"],
            pinned_at=row["pinned_at"],
        )

    @staticmethod
    def _to_citation(row: Any) -> StoredCitation:
        return StoredCitation(
            id=row["citation_id"],
            message_id=row["message_id"],
            display_label=str(row["display_label"]),
            chunk_id=row["chunk_id"],
            version_id=row["version_id"],
            document_id=row["document_id"],
            document_title=str(row["document_title"]),
            version_no=int(row["version_no"]),
            locator=dict(row["locator_snapshot"]),
            quote=str(row["quote"]),
            quote_hash=str(row["quote_hash"]),
            is_current_version=bool(row["is_current_version"]),
        )


__all__ = [
    "CitationRecord",
    "ConversationRepository",
    "ConversationRow",
    "ConversationSummaryRow",
    "LlmUsageRecord",
    "MessageRecord",
    "QueryRunRecord",
    "SqlConversationRepository",
    "StoredCitation",
    "StoredMessage",
]
