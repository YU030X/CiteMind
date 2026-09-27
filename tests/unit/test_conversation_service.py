"""问答主流程用例单测：假仓储 + 假生成器，不连接数据库、不调用 provider。

覆盖：引用服务端映射、无证据直接拒答（不调模型）、模型拒答、非法响应、provider 失败、
版本变化重检索一次与持续变化的静态失败、撤权历史的排除、所有者隔离，以及每次真实尝试
都追加 ``llm_usage`` 事实。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from rag_backend.conversation.errors import (
    ConversationNotFound,
    ConversationQuestionTooLong,
    ConversationSourcesChanged,
    GenerationFailed,
    GenerationInvalidResponse,
)
from rag_backend.conversation.repository import (
    CitationRecord,
    ConversationRow,
    LlmUsageRecord,
    MessageRecord,
    QueryRunRecord,
    StoredCitation,
    StoredMessage,
)
from rag_backend.conversation.service import (
    REFUSAL_ANSWER,
    answer_question,
)
from rag_backend.generation.context_budget import ContextBudget
from rag_backend.generation.deepseek_client import (
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    GenerationOutcome,
)
from rag_backend.generation.deepseek_prompt import (
    USER_SP_TOKEN,
    ChatMessage,
    PromptEncodingError,
)
from rag_backend.generation.query_rewrite import REWRITE_STAGE, REWRITE_SYSTEM_PROMPT
from rag_backend.retrieval.fusion import FusedCandidate
from rag_backend.retrieval.repository import ChunkSourceState, EvidenceChunkRow
from rag_backend.retrieval.service import RetrievalResult

USER_ID = uuid.uuid4()
ORG_ID = uuid.uuid4()
CONVERSATION_ID = uuid.uuid4()
KB_ID = uuid.uuid4()
DOC_ID = uuid.uuid4()
VERSION_ID = uuid.uuid4()
CHUNK_ID = uuid.uuid4()
PROFILE_ID = uuid.uuid4()

BUDGET = ContextBudget(input_token_budget=100_000, output_token_budget=800)


def _conversation() -> ConversationRow:
    now = datetime.now(UTC)
    return ConversationRow(
        id=CONVERSATION_ID,
        organization_id=ORG_ID,
        owner_id=USER_ID,
        kb_scope=(KB_ID,),
        created_at=now,
        updated_at=now,
    )


def _evidence_row(
    *,
    text: str = "制度原文。",
    locator: dict[str, Any] | None = None,
    chunk_id: uuid.UUID = CHUNK_ID,
    document_id: uuid.UUID = DOC_ID,
) -> EvidenceChunkRow:
    return EvidenceChunkRow(
        chunk_id=chunk_id,
        document_id=document_id,
        kb_id=KB_ID,
        version_id=VERSION_ID,
        version_no=2,
        document_title="制度文档",
        text=text,
        source_locator=locator if locator is not None else {"page": 1},
    )


def _state(
    chunk_id: uuid.UUID,
    *,
    version_id: uuid.UUID,
    active_version_id: uuid.UUID | None = None,
    deleted: bool = False,
    member_active: bool = True,
    in_organization: bool = True,
) -> ChunkSourceState:
    return ChunkSourceState(
        chunk_id=chunk_id,
        document_id=DOC_ID,
        version_id=version_id,
        active_version_id=version_id if active_version_id is None else active_version_id,
        deleted=deleted,
        member_active=member_active,
        in_organization=in_organization,
    )


class FakeConversationRepository:
    def __init__(
        self,
        conversation: ConversationRow | None = None,
        *,
        messages: Sequence[StoredMessage] = (),
        citations: Sequence[StoredCitation] = (),
    ) -> None:
        self.conversation = conversation
        self.messages = list(messages)
        self.citations = list(citations)
        self.query_runs: list[QueryRunRecord] = []
        self.usage: list[LlmUsageRecord] = []
        self.commits = 0
        self._sequence = max((message.sequence for message in self.messages), default=0)

    async def load_conversation(
        self, *, conversation_id: uuid.UUID, owner_id: uuid.UUID, organization_id: uuid.UUID
    ) -> ConversationRow | None:
        conversation = self.conversation
        if conversation is None:
            return None
        if (
            conversation.id != conversation_id
            or conversation.owner_id != owner_id
            or conversation.organization_id != organization_id
        ):
            return None
        return conversation

    async def insert_conversation(
        self,
        *,
        conversation_id: uuid.UUID,
        organization_id: uuid.UUID,
        owner_id: uuid.UUID,
        kb_scope: Sequence[uuid.UUID],
    ) -> datetime:
        raise NotImplementedError

    async def list_messages(self, *, conversation_id: uuid.UUID) -> list[StoredMessage]:
        return sorted(self.messages, key=lambda message: message.sequence)

    async def list_citations(self, *, message_ids: Sequence[uuid.UUID]) -> list[StoredCitation]:
        wanted = set(message_ids)
        return [citation for citation in self.citations if citation.message_id in wanted]

    async def load_citation_for_owner(
        self, *, citation_id: uuid.UUID, owner_id: uuid.UUID, organization_id: uuid.UUID
    ) -> StoredCitation | None:
        for citation in self.citations:
            if citation.id == citation_id:
                return citation
        return None

    async def next_message_sequence(self, *, conversation_id: uuid.UUID) -> int:
        self._sequence += 1
        return self._sequence

    async def insert_query_run(self, record: QueryRunRecord) -> None:
        self.query_runs.append(record)

    async def insert_message(self, record: MessageRecord) -> None:
        self.messages.append(
            StoredMessage(
                id=record.id,
                sequence=record.sequence,
                role=record.role,
                content=record.content,
                query_run_id=record.query_run_id,
                created_at=datetime.now(UTC),
            )
        )

    async def insert_citation(self, record: CitationRecord) -> None:
        self.citations.append(
            StoredCitation(
                id=record.id,
                message_id=record.message_id,
                display_label=record.display_label,
                chunk_id=record.chunk_id,
                version_id=record.version_id,
                document_id=DOC_ID,
                document_title="制度文档",
                version_no=2,
                locator=record.locator,
                quote=record.quote,
                quote_hash=record.quote_hash,
            )
        )

    async def insert_llm_usage(self, record: LlmUsageRecord) -> None:
        self.usage.append(record)

    async def commit(self) -> None:
        self.commits += 1

    async def release(self) -> None:
        return None


class FakeEvidenceRepository:
    def __init__(
        self,
        rows: Sequence[EvidenceChunkRow],
        *,
        states: Sequence[ChunkSourceState] = (),
        empty_load_numbers: Sequence[int] = (),
    ) -> None:
        self.rows = list(rows)
        self.states = {state.chunk_id: state for state in states}
        self.empty_load_numbers = set(empty_load_numbers)
        self.loads = 0
        self.state_loads = 0
        self.releases = 0

    async def load_evidence_chunks(
        self, *, user_id: uuid.UUID, organization_id: uuid.UUID, chunk_ids: Sequence[uuid.UUID]
    ) -> list[EvidenceChunkRow]:
        self.loads += 1
        if self.loads in self.empty_load_numbers:
            return []
        wanted = set(chunk_ids)
        return [row for row in self.rows if row.chunk_id in wanted]

    async def load_chunk_source_states(
        self, *, user_id: uuid.UUID, organization_id: uuid.UUID, chunk_ids: Sequence[uuid.UUID]
    ) -> list[ChunkSourceState]:
        self.state_loads += 1
        return [self.states[cid] for cid in chunk_ids if cid in self.states]

    async def release(self) -> None:
        self.releases += 1


class RevokingEvidenceRepository(FakeEvidenceRepository):
    """在第 ``revoke_after`` 次之后的历史来源查询上返回已撤权，复现轮间的权限变化。"""

    def __init__(
        self,
        rows: Sequence[EvidenceChunkRow],
        *,
        states: Sequence[ChunkSourceState] = (),
        empty_load_numbers: Sequence[int] = (),
        revoke_after: int,
    ) -> None:
        super().__init__(
            rows, states=states, empty_load_numbers=empty_load_numbers
        )
        self.revoke_after = revoke_after

    async def load_chunk_source_states(
        self, *, user_id: uuid.UUID, organization_id: uuid.UUID, chunk_ids: Sequence[uuid.UUID]
    ) -> list[ChunkSourceState]:
        states = await super().load_chunk_source_states(
            user_id=user_id, organization_id=organization_id, chunk_ids=chunk_ids
        )
        if self.state_loads > self.revoke_after:
            return [replace(state, member_active=False) for state in states]
        return states


class FakeRetrieval:
    def __init__(
        self,
        candidates: Sequence[Sequence[FusedCandidate]],
        *,
        kb_ids: Sequence[uuid.UUID] = (KB_ID,),
    ) -> None:
        self.candidates = [list(item) for item in candidates]
        self.kb_ids = tuple(kb_ids)
        self.calls = 0
        self.queries: list[str] = []

    async def __call__(
        self, *, kb_ids: Sequence[uuid.UUID], query: str
    ) -> RetrievalResult:
        index = min(self.calls, len(self.candidates) - 1)
        self.calls += 1
        self.queries.append(query)
        return RetrievalResult(
            kb_ids=self.kb_ids, candidates=tuple(self.candidates[index])
        )


class RecordingEstimator:
    def __init__(self) -> None:
        self.contents: list[str] = []

    def estimate_chat_tokens(self, messages: Sequence[ChatMessage]) -> int:
        self.contents.extend(message.content for message in messages)
        return sum(1 + len(message.content) for message in messages)


class ScaffoldRejectingEstimator(RecordingEstimator):
    """模拟本地渲染器：正文含结构 token 时抛具名 ``PromptEncodingError``。"""

    def estimate_chat_tokens(self, messages: Sequence[ChatMessage]) -> int:
        self.contents.extend(message.content for message in messages)
        for message in messages:
            if USER_SP_TOKEN in message.content:
                raise PromptEncodingError("消息正文包含提示结构 token")
        return sum(1 + len(message.content) for message in messages)


class FakeGenerator:
    """按系统提示区分改写与回答两类调用，不联网；分别记录消息与调用次数。"""

    def __init__(
        self,
        outcomes: Sequence[GenerationOutcome],
        *,
        rewrite_outcomes: Sequence[GenerationOutcome] | None = None,
    ) -> None:
        self.outcomes = list(outcomes)
        self.rewrite_outcomes = list(
            rewrite_outcomes
            if rewrite_outcomes is not None
            else [_outcome(content=_rewrite_json(REWRITE_STANDALONE))]
        )
        self.calls = 0
        self.answer_calls = 0
        self.rewrite_calls = 0
        self.messages: list[tuple[ChatMessage, ...]] = []
        self.answer_messages: list[tuple[ChatMessage, ...]] = []
        self.rewrite_messages: list[tuple[ChatMessage, ...]] = []

    def generate(
        self, messages: Sequence[ChatMessage], *, max_output_tokens: int
    ) -> GenerationOutcome:
        # 捕获每次真实调用进入提示的消息，用于断言历史/证据/改写实际入参。
        captured = tuple(messages)
        self.messages.append(captured)
        self.calls += 1
        if captured[0].content == REWRITE_SYSTEM_PROMPT:
            self.rewrite_calls += 1
            self.rewrite_messages.append(captured)
            pool = self.rewrite_outcomes
            index = min(self.rewrite_calls - 1, len(pool) - 1)
        else:
            self.answer_calls += 1
            self.answer_messages.append(captured)
            pool = self.outcomes
            index = min(self.answer_calls - 1, len(pool) - 1)
        return pool[index]

    def close(self) -> None:
        return None


def _outcome(
    *,
    status: str = STATUS_SUCCEEDED,
    content: str | None = None,
    error_code: str | None = None,
) -> GenerationOutcome:
    return GenerationOutcome(
        status=status,
        error_code=error_code,
        usage_source="PROVIDER_REPORTED" if status == STATUS_SUCCEEDED else "UNKNOWN",
        prompt_tokens=11 if status == STATUS_SUCCEEDED else None,
        completion_tokens=3 if status == STATUS_SUCCEEDED else None,
        prompt_cache_hit_tokens=None,
        prompt_cache_miss_tokens=None,
        latency_ms=42,
        content=content,
    )


def _candidate(
    chunk_id: uuid.UUID = CHUNK_ID, document_id: uuid.UUID = DOC_ID
) -> FusedCandidate:
    return FusedCandidate(
        chunk_id=chunk_id,
        document_id=document_id,
        kb_id=KB_ID,
        version_id=VERSION_ID,
        vector_rank=1,
        vector_score=0.9,
        keyword_rank=1,
        keyword_score=0.5,
        fusion_rank=1,
        fusion_score=0.03,
    )


def _answer_json(citation_ids: Sequence[str] = ("E1",)) -> str:
    return json.dumps(
        {
            "sentences": [{"text": "制度规定。", "citationIds": list(citation_ids)}],
            "insufficientEvidence": False,
            "followUp": None,
        },
        ensure_ascii=False,
    )


REWRITE_STANDALONE = "制度适用范围是什么？"


def _rewrite_json(standalone: str = REWRITE_STANDALONE) -> str:
    return json.dumps({"standaloneQuestion": standalone}, ensure_ascii=False)


async def _run(
    *,
    repository: FakeConversationRepository | None = None,
    evidence: FakeEvidenceRepository | None = None,
    retrieval: FakeRetrieval | None = None,
    estimator: RecordingEstimator | None = None,
    generator: FakeGenerator | None = None,
    question: str = "制度怎么规定？",
    budget: ContextBudget = BUDGET,
) -> tuple[Any, FakeConversationRepository, FakeEvidenceRepository, FakeGenerator]:
    resolved_repository = repository or FakeConversationRepository(_conversation())
    resolved_evidence = evidence or FakeEvidenceRepository([_evidence_row()])
    resolved_retrieval = retrieval or FakeRetrieval([[_candidate()]])
    resolved_generator = generator or FakeGenerator([_outcome(content=_answer_json())])
    result = await answer_question(
        resolved_repository,
        resolved_evidence,
        conversation_id=CONVERSATION_ID,
        user_id=USER_ID,
        organization_id=ORG_ID,
        question=question,
        request_id="req-1",
        retrieve=resolved_retrieval,
        estimator=estimator or RecordingEstimator(),
        generator=resolved_generator,
        budget=budget,
        model="deepseek-flash",
    )
    return result, resolved_repository, resolved_evidence, resolved_generator


# --- 正例 -------------------------------------------------------------------


@pytest.mark.anyio
async def test_happy_path_maps_server_side_citation_and_records_usage() -> None:
    estimator = RecordingEstimator()
    generator = FakeGenerator([_outcome(content=_answer_json())])
    repository = FakeConversationRepository(_conversation())
    evidence = FakeEvidenceRepository(
        [_evidence_row(text="制度原文很长。", locator={"page": 3, "start_line": 5})]
    )

    result, repository, evidence, generator = await _run(
        repository=repository, evidence=evidence, estimator=estimator, generator=generator
    )

    assert result.answer == "制度规定。"
    assert result.insufficient_evidence is False
    assert len(result.citations) == 1
    citation = result.citations[0]
    assert citation.display_label == "E1"
    assert citation.locator == {"page": 3, "start_line": 5}
    assert citation.quote == "制度原文很长。"
    assert citation.version == 2
    assert generator.calls == 1
    # 每次真实尝试恰好一行 provider 事实，且 model 来自配置。
    assert len(repository.usage) == 1
    assert repository.usage[0].model == "deepseek-flash"
    assert repository.usage[0].prompt_tokens == 11
    assert repository.usage[0].completion_tokens == 3
    # 本地估算与 provider 用量分开保存。
    assert result.usage.local_input_tokens is not None
    assert result.usage.provider_prompt_tokens == 11
    assert result.usage.provider_completion_tokens == 3
    assert len(repository.query_runs) == 1
    run = repository.query_runs[0]
    assert run.status == "SUCCEEDED"
    assert run.insufficient_evidence is False
    assert run.estimated_input_tokens == result.usage.local_input_tokens
    assert run.provider_prompt_tokens == 11
    assert run.scope_snapshot == (KB_ID,)
    assert run.evidence_count == 1
    # 正常问答没有异常降级；预算/top-k 裁剪不算故障。
    assert run.degraded_stages == ()
    # 证据文本确实进入了发送给模型的消息。
    sent = "\n".join(message.content for message in generator.messages[0])
    assert "制度原文很长。" in sent
    assert [message.role for message in repository.messages] == ["user", "assistant"]
    assert repository.messages[0].content == "制度怎么规定？"
    assert repository.messages[1].content == "制度规定。"
    assert len(repository.citations) == 1


@pytest.mark.anyio
async def test_no_evidence_refuses_without_calling_model() -> None:
    generator = FakeGenerator([_outcome(content=_answer_json())])
    repository = FakeConversationRepository(_conversation())
    evidence = FakeEvidenceRepository([])

    result, repository, evidence, generator = await _run(
        repository=repository,
        evidence=evidence,
        # NULL active profile 的 KB 不可检索：实际解析后范围为 0。
        retrieval=FakeRetrieval([[]], kb_ids=()),
        generator=generator,
    )

    assert generator.calls == 0
    assert result.insufficient_evidence is True
    assert result.answer == REFUSAL_ANSWER
    assert result.citations == ()
    assert result.degraded_stages == ()
    assert repository.usage == []
    assert repository.query_runs[0].status == "REFUSED"
    assert repository.query_runs[0].llm_usage_id is None
    assert repository.query_runs[0].scope_snapshot == ()
    assert repository.query_runs[0].evidence_count == 0


@pytest.mark.anyio
async def test_model_refusal_records_usage_and_persists_refusal() -> None:
    refusal = json.dumps(
        {"sentences": [], "insufficientEvidence": True, "followUp": None},
        ensure_ascii=False,
    )
    generator = FakeGenerator([_outcome(content=refusal)])
    repository = FakeConversationRepository(_conversation())

    result, repository, evidence, generator = await _run(repository=repository, generator=generator)

    assert generator.calls == 1
    assert result.insufficient_evidence is True
    assert result.answer == REFUSAL_ANSWER
    assert result.citations == ()
    assert len(repository.usage) == 1
    assert repository.query_runs[0].llm_usage_id is not None


@pytest.mark.anyio
async def test_unknown_citation_id_is_an_invalid_response_after_usage() -> None:
    generator = FakeGenerator([_outcome(content=_answer_json(("E9",)))])
    repository = FakeConversationRepository(_conversation())

    with pytest.raises(GenerationInvalidResponse):
        await _run(repository=repository, generator=generator)

    assert len(repository.usage) == 1
    assert repository.usage[0].status == "SUCCEEDED"
    # 非法响应不落任何消息或引用，避免把无引用回答静默交付。
    assert repository.query_runs == []
    assert repository.messages == []
    assert repository.citations == []


@pytest.mark.anyio
async def test_provider_failure_is_a_failed_fact_and_error() -> None:
    generator = FakeGenerator(
        [_outcome(status=STATUS_FAILED, error_code="HTTP_500")]
    )
    repository = FakeConversationRepository(_conversation())

    with pytest.raises(GenerationFailed):
        await _run(repository=repository, generator=generator)

    assert len(repository.usage) == 1
    assert repository.usage[0].status == "FAILED"
    assert repository.usage[0].error_code == "HTTP_500"
    assert repository.usage[0].prompt_tokens is None
    assert repository.query_runs == []


@pytest.mark.anyio
async def test_question_too_long_maps_to_budget_error() -> None:
    repository = FakeConversationRepository(_conversation())
    with pytest.raises(ConversationQuestionTooLong):
        await _run(repository=repository, budget=ContextBudget(input_token_budget=5))


# --- 版本/权限变化 -----------------------------------------------------------


@pytest.mark.anyio
async def test_stale_evidence_triggers_one_retrieval_then_succeeds() -> None:
    repository = FakeConversationRepository(_conversation())
    # 第 1 次证据读取为空（模拟竞态），第 2 次恢复。
    evidence = FakeEvidenceRepository([_evidence_row()], empty_load_numbers=[1])
    retrieval = FakeRetrieval([[_candidate()], [_candidate()]])

    result, repository, evidence, generator = await _run(
        repository=repository, evidence=evidence, retrieval=retrieval
    )

    assert result.insufficient_evidence is False
    assert retrieval.calls == 2
    assert repository.usage and repository.query_runs


@pytest.mark.anyio
async def test_persistent_stale_evidence_returns_retryable_failure() -> None:
    repository = FakeConversationRepository(_conversation())
    evidence = FakeEvidenceRepository([_evidence_row()], empty_load_numbers=[1, 2])

    with pytest.raises(ConversationSourcesChanged):
        await _run(repository=repository, evidence=evidence)

    assert repository.query_runs == []


@pytest.mark.anyio
async def test_version_change_after_model_retrieves_once_then_succeeds() -> None:
    repository = FakeConversationRepository(_conversation())
    # 第 1 次（模型前）正常，第 2 次（交付前）为空，触发一次重检索。
    evidence = FakeEvidenceRepository([_evidence_row()], empty_load_numbers=[2])
    retrieval = FakeRetrieval([[_candidate()], [_candidate()]])
    generator = FakeGenerator([_outcome(content=_answer_json())])

    result, repository, evidence, generator = await _run(
        repository=repository, evidence=evidence, retrieval=retrieval, generator=generator
    )

    assert result.insufficient_evidence is False
    assert retrieval.calls == 2
    # 每次真实尝试都落账：初次与重检索后各一次。
    assert len(repository.usage) == 2
    # 来源变化触发的重检索是真实异常，需要记入静态阶段标识。
    assert repository.query_runs[0].degraded_stages == ("source_retry",)


@pytest.mark.anyio
async def test_version_change_after_model_twice_returns_retryable_failure() -> None:
    repository = FakeConversationRepository(_conversation())
    evidence = FakeEvidenceRepository([_evidence_row()], empty_load_numbers=[2, 4])

    with pytest.raises(ConversationSourcesChanged):
        await _run(repository=repository, evidence=evidence)

    # 两次 provider attempt 都已落账，且没有持久化消息。
    assert len(repository.usage) == 2
    assert repository.query_runs == []


# --- 历史与所有者隔离 --------------------------------------------------------


def _prior_turn(answer: str) -> tuple[StoredMessage, StoredMessage, StoredCitation]:
    run_id = uuid.uuid4()
    user = StoredMessage(
        id=uuid.uuid4(),
        sequence=1,
        role="user",
        content="上一问",
        query_run_id=run_id,
        created_at=datetime.now(UTC),
    )
    assistant = StoredMessage(
        id=uuid.uuid4(),
        sequence=2,
        role="assistant",
        content=answer,
        query_run_id=run_id,
        created_at=datetime.now(UTC),
    )
    citation = StoredCitation(
        id=uuid.uuid4(),
        message_id=assistant.id,
        display_label="E1",
        chunk_id=CHUNK_ID,
        version_id=VERSION_ID,
        document_id=DOC_ID,
        document_title="制度文档",
        version_no=1,
        locator={"page": 1},
        quote="旧引文",
        quote_hash="h",
    )
    return user, assistant, citation


@pytest.mark.anyio
async def test_revoked_history_turn_is_excluded_from_model_context() -> None:
    user, assistant, citation = _prior_turn("来自已撤权来源的旧回答")
    repository = FakeConversationRepository(
        _conversation(), messages=[user, assistant], citations=[citation]
    )
    # 历史引用的来源已撤权；当前证据的来源仍受权。
    evidence = FakeEvidenceRepository(
        [_evidence_row()],
        states=[
            _state(CHUNK_ID, version_id=VERSION_ID, member_active=False),
        ],
    )
    estimator = RecordingEstimator()

    result, repository, evidence, generator = await _run(
        repository=repository, evidence=evidence, estimator=estimator
    )

    assert result.insufficient_evidence is False
    assert all("旧回答" not in content for content in estimator.contents)


@pytest.mark.anyio
async def test_authorized_history_turn_enters_model_context() -> None:
    user, assistant, citation = _prior_turn("来自仍受权来源的旧回答")
    repository = FakeConversationRepository(
        _conversation(), messages=[user, assistant], citations=[citation]
    )
    evidence = FakeEvidenceRepository(
        [_evidence_row()],
        states=[_state(CHUNK_ID, version_id=VERSION_ID)],
    )
    estimator = RecordingEstimator()

    result, repository, evidence, generator = await _run(
        repository=repository, evidence=evidence, estimator=estimator
    )

    assert result.insufficient_evidence is False
    assert any("旧回答" in content for content in estimator.contents)
    # 存在合法历史时先调用一次改写；改写提示只含历史问题，不含历史助手回答。
    assert generator.rewrite_calls == 1
    assert generator.answer_calls == 1
    rewrite_prompt = "\n".join(
        message.content for message in generator.rewrite_messages[0]
    )
    assert "上一问" in rewrite_prompt
    assert "旧回答" not in rewrite_prompt


@pytest.mark.anyio
async def test_other_owner_cannot_answer_conversation() -> None:
    conversation = ConversationRow(
        id=CONVERSATION_ID,
        organization_id=ORG_ID,
        owner_id=uuid.uuid4(),
        kb_scope=(KB_ID,),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    repository = FakeConversationRepository(conversation)

    with pytest.raises(ConversationNotFound):
        await _run(repository=repository)


# --- 实际范围、降级阶段与轮间历史复核 ----------------------------------------


@pytest.mark.anyio
async def test_scope_snapshot_records_resolved_searchable_subset() -> None:
    """名义会话范围包含不可检索 KB 时，快照只记实际解析出的可检索子集。"""

    other_kb = uuid.uuid4()
    repository = FakeConversationRepository(
        replace(_conversation(), kb_scope=(KB_ID, other_kb))
    )

    _result, repository, _evidence, _generator = await _run(
        repository=repository,
        retrieval=FakeRetrieval([[_candidate()]], kb_ids=(KB_ID,)),
    )

    run = repository.query_runs[0]
    assert run.scope_snapshot == (KB_ID,)
    assert other_kb not in run.scope_snapshot
    assert run.evidence_count == 1


@pytest.mark.anyio
async def test_unsupported_evidence_text_is_recorded_as_degraded_on_refusal() -> None:
    """候选正文被本地渲染器拒绝而剔除属真实异常，需记入静态阶段标识。"""

    repository = FakeConversationRepository(_conversation())
    generator = FakeGenerator([_outcome(content=_answer_json())])
    evidence = FakeEvidenceRepository([_evidence_row(text=f"坏正文{USER_SP_TOKEN}注入")])

    result, repository, _evidence, generator = await _run(
        repository=repository,
        evidence=evidence,
        generator=generator,
        estimator=ScaffoldRejectingEstimator(),
    )

    assert generator.calls == 0
    assert result.insufficient_evidence is True
    assert result.degraded_stages == ("unsupported_text",)
    assert repository.query_runs[0].status == "REFUSED"
    assert repository.query_runs[0].degraded_stages == ("unsupported_text",)


@pytest.mark.anyio
async def test_unsupported_evidence_is_skipped_while_valid_evidence_answers() -> None:
    """单条低信任正文被拒绝时只跳过该候选，其余合法证据继续参与。"""

    other_chunk = uuid.uuid4()
    repository = FakeConversationRepository(_conversation())
    evidence = FakeEvidenceRepository(
        [
            _evidence_row(text=f"坏{USER_SP_TOKEN}", chunk_id=CHUNK_ID),
            _evidence_row(text="合法证据。", chunk_id=other_chunk),
        ]
    )
    retrieval = FakeRetrieval([[_candidate(CHUNK_ID), _candidate(other_chunk)]])
    generator = FakeGenerator([_outcome(content=_answer_json(("E2",)))])

    result, repository, _evidence, _generator = await _run(
        repository=repository,
        evidence=evidence,
        retrieval=retrieval,
        generator=generator,
        estimator=ScaffoldRejectingEstimator(),
    )

    assert result.insufficient_evidence is False
    assert result.citations[0].display_label == "E2"
    assert result.citations[0].quote == "合法证据。"
    assert result.degraded_stages == ("unsupported_text",)
    assert repository.query_runs[0].evidence_count == 1


@pytest.mark.anyio
async def test_normal_top_k_exclusion_is_not_degraded() -> None:
    """正常的 max_evidence 限量不是故障，不得机械地当作 degraded。"""

    other_chunk = uuid.uuid4()
    repository = FakeConversationRepository(_conversation())
    evidence = FakeEvidenceRepository(
        [
            _evidence_row(chunk_id=CHUNK_ID),
            _evidence_row(text="第二段。", chunk_id=other_chunk),
        ]
    )
    retrieval = FakeRetrieval([[_candidate(CHUNK_ID), _candidate(other_chunk)]])
    generator = FakeGenerator([_outcome(content=_answer_json(("E1",)))])

    result, repository, _evidence, _generator = await _run(
        repository=repository,
        evidence=evidence,
        retrieval=retrieval,
        generator=generator,
        budget=ContextBudget(
            input_token_budget=100_000, output_token_budget=800, max_evidence=1
        ),
    )

    assert result.insufficient_evidence is False
    assert result.degraded_stages == ()
    assert repository.query_runs[0].degraded_stages == ()
    assert repository.query_runs[0].evidence_count == 1


@pytest.mark.anyio
async def test_history_revoked_before_retry_fails_without_second_retrieval() -> None:
    """首次生成后、版本变化重检索前历史被撤权：派生问题不得再驱动第二次检索。"""

    user, assistant, citation = _prior_turn("来自会撤权来源的旧回答")
    repository = FakeConversationRepository(
        _conversation(), messages=[user, assistant], citations=[citation]
    )
    evidence = RevokingEvidenceRepository(
        [_evidence_row()],
        states=[_state(CHUNK_ID, version_id=VERSION_ID)],
        # 第 2 次证据读取（交付前复核）为空，触发一次重检索。
        empty_load_numbers=[2],
        # 第 4 次历史核查（交付前复核）仍受权；重检索前的第 5 次核查发现已撤权。
        revoke_after=4,
    )
    retrieval = FakeRetrieval([[_candidate()], [_candidate()]])
    generator = FakeGenerator([_outcome(content=_answer_json())])

    with pytest.raises(ConversationSourcesChanged):
        await _run(
            repository=repository,
            evidence=evidence,
            retrieval=retrieval,
            generator=generator,
            question="它的适用范围呢？",
        )

    # 改写一次、首次回答一次；撤权在重检索前被发现，因此只剩 1 次检索。
    assert generator.rewrite_calls == 1
    assert generator.answer_calls == 1
    assert retrieval.calls == 1
    assert retrieval.queries == [REWRITE_STANDALONE]
    assert [row.stage for row in repository.usage] == [REWRITE_STAGE, "qa_answer"]
    # 持续变化不落查询运行、消息或引用，也不持久化派生问题。
    assert repository.query_runs == []
    assert all(message.content != REWRITE_STANDALONE for message in repository.messages)


# --- 追问改写 ---------------------------------------------------------------


def _history_repository(answer: str = "来自仍受权来源的旧回答") -> FakeConversationRepository:
    user, assistant, citation = _prior_turn(answer)
    return FakeConversationRepository(
        _conversation(), messages=[user, assistant], citations=[citation]
    )


def _authorized_history_evidence() -> FakeEvidenceRepository:
    return FakeEvidenceRepository(
        [_evidence_row()],
        states=[_state(CHUNK_ID, version_id=VERSION_ID)],
    )


@pytest.mark.anyio
async def test_first_round_skips_rewrite_entirely() -> None:
    """首轮没有合法历史：不发额外请求，独立问题就是原问题。"""

    repository = FakeConversationRepository(_conversation())
    retrieval = FakeRetrieval([[_candidate()]])
    generator = FakeGenerator([_outcome(content=_answer_json())])

    result, repository, _evidence, generator = await _run(
        repository=repository, retrieval=retrieval, generator=generator
    )

    assert result.insufficient_evidence is False
    assert generator.calls == 1
    assert generator.rewrite_calls == 0
    assert generator.answer_calls == 1
    assert retrieval.queries == ["制度怎么规定？"]
    assert repository.usage[0].stage == "qa_answer"
    run = repository.query_runs[0]
    assert run.question == "制度怎么规定？"
    assert run.standalone_question == "制度怎么规定？"


@pytest.mark.anyio
async def test_follow_up_rewrite_is_used_for_retrieval_and_persisted() -> None:
    """有合法历史的追问：改写一次、检索用独立问题、原始问题仍进回答提示。"""

    repository = _history_repository()
    evidence = _authorized_history_evidence()
    retrieval = FakeRetrieval([[_candidate()]])
    generator = FakeGenerator([_outcome(content=_answer_json())])

    result, repository, _evidence, generator = await _run(
        repository=repository,
        evidence=evidence,
        retrieval=retrieval,
        generator=generator,
        question="它的适用范围呢？",
    )

    assert result.insufficient_evidence is False
    assert generator.rewrite_calls == 1
    assert generator.answer_calls == 1
    # 检索用的是独立问题，而不是原始追问。
    assert retrieval.queries == [REWRITE_STANDALONE]
    run = repository.query_runs[0]
    assert run.question == "它的适用范围呢？"
    assert run.standalone_question == REWRITE_STANDALONE
    # 回答提示仍保留原始问题，独立问题只作为检索上下文。
    answer_prompt = "\n".join(
        message.content for message in generator.answer_messages[0]
    )
    assert "它的适用范围呢？" in answer_prompt
    # 改写与回答分别记账，stage 分开。
    assert [row.stage for row in repository.usage] == [REWRITE_STAGE, "qa_answer"]
    assert repository.usage[0].prompt_tokens == 11


@pytest.mark.anyio
async def test_rewrite_refusal_with_no_legal_history_skips_rewrite() -> None:
    """历史存在但全部撤权：视为没有合法历史，不额外调用改写。"""

    repository = _history_repository("来自已撤权来源的旧回答")
    evidence = FakeEvidenceRepository(
        [_evidence_row()],
        states=[_state(CHUNK_ID, version_id=VERSION_ID, member_active=False)],
    )
    retrieval = FakeRetrieval([[_candidate()]])
    generator = FakeGenerator([_outcome(content=_answer_json())])

    _result, repository, _evidence, generator = await _run(
        repository=repository,
        evidence=evidence,
        retrieval=retrieval,
        generator=generator,
        question="它的适用范围呢？",
    )

    assert generator.rewrite_calls == 0
    assert retrieval.queries == ["它的适用范围呢？"]
    assert repository.query_runs[0].standalone_question == "它的适用范围呢？"


@pytest.mark.anyio
async def test_rewrite_result_trying_to_widen_scope_is_rejected() -> None:
    """模型试图提交 KB 范围（额外字段）：严格拒绝，不捏造范围也不静默退回原问题。"""

    repository = _history_repository()
    retrieval = FakeRetrieval([[_candidate()]])
    bad = json.dumps(
        {
            "standaloneQuestion": "制度适用范围",
            "kbIds": [str(uuid.uuid4())],
        },
        ensure_ascii=False,
    )
    generator = FakeGenerator(
        [_outcome(content=_answer_json())],
        rewrite_outcomes=[_outcome(content=bad)],
    )

    with pytest.raises(GenerationInvalidResponse):
        await _run(
            repository=repository,
            evidence=_authorized_history_evidence(),
            retrieval=retrieval,
            generator=generator,
            question="它的适用范围呢？",
        )

    # 改写响应非法：不检索、不生成回答、不为本轮持久化任何消息。
    assert retrieval.calls == 0
    assert generator.answer_calls == 0
    assert repository.query_runs == []
    assert "它的适用范围呢？" not in [message.content for message in repository.messages]
    assert [row.stage for row in repository.usage] == [REWRITE_STAGE]


@pytest.mark.anyio
async def test_overlong_rewrite_result_is_rejected() -> None:
    repository = _history_repository()
    retrieval = FakeRetrieval([[_candidate()]])
    overlong = _rewrite_json("问" * 9000)
    generator = FakeGenerator(
        [_outcome(content=_answer_json())],
        rewrite_outcomes=[_outcome(content=overlong)],
    )

    with pytest.raises(GenerationInvalidResponse):
        await _run(
            repository=repository,
            evidence=_authorized_history_evidence(),
            retrieval=retrieval,
            generator=generator,
            question="它的适用范围呢？",
        )

    assert retrieval.calls == 0
    assert repository.query_runs == []


@pytest.mark.anyio
async def test_rewrite_provider_failure_is_recorded_and_not_retried() -> None:
    """改写 provider 失败/超时：单独落失败事实并整轮失败，不重试、不退回原问题。"""

    repository = _history_repository()
    retrieval = FakeRetrieval([[_candidate()]])
    generator = FakeGenerator(
        [_outcome(content=_answer_json())],
        rewrite_outcomes=[_outcome(status="TIMEOUT", error_code="TIMEOUT")],
    )

    with pytest.raises(GenerationFailed) as error:
        await _run(
            repository=repository,
            evidence=_authorized_history_evidence(),
            retrieval=retrieval,
            generator=generator,
            question="它的适用范围呢？",
        )

    assert error.value.error_code == "TIMEOUT"
    assert generator.rewrite_calls == 1
    assert generator.answer_calls == 0
    assert retrieval.calls == 0
    assert len(repository.usage) == 1
    assert repository.usage[0].stage == REWRITE_STAGE
    assert repository.usage[0].status == "TIMEOUT"
    assert repository.query_runs == []


@pytest.mark.anyio
async def test_history_revoked_during_rewrite_fails_before_retrieval() -> None:
    """改写期间历史被撤权：派生问题不得驱动检索，立即整轮静态失败。"""

    repository = _history_repository("来自会撤权来源的旧回答")
    evidence = RevokingEvidenceRepository(
        [_evidence_row()],
        states=[_state(CHUNK_ID, version_id=VERSION_ID)],
        # 改写前的历史核查仍受权；改写后、首次检索前的核查发现已撤权。
        revoke_after=1,
    )
    retrieval = FakeRetrieval([[_candidate()]])
    generator = FakeGenerator([_outcome(content=_answer_json())])

    with pytest.raises(ConversationSourcesChanged):
        await _run(
            repository=repository,
            evidence=evidence,
            retrieval=retrieval,
            generator=generator,
            question="它的适用范围呢？",
        )

    # 改写确实发生并单独落账，但绝不发检索/编码/回答，也不落派生问题。
    assert generator.rewrite_calls == 1
    assert generator.answer_calls == 0
    assert retrieval.calls == 0
    assert [row.stage for row in repository.usage] == [REWRITE_STAGE]
    assert repository.query_runs == []
    assert all(message.content != REWRITE_STANDALONE for message in repository.messages)


@pytest.mark.anyio
async def test_history_revoked_before_delivery_fails_and_persists_nothing() -> None:
    """生成完成后、交付前历史被撤权：不得交付回答，也不落派生问题。"""

    repository = _history_repository("来自会撤权来源的旧回答")
    evidence = RevokingEvidenceRepository(
        [_evidence_row()],
        states=[_state(CHUNK_ID, version_id=VERSION_ID)],
        # 前 3 次历史核查（改写前、检索前、提示前）仍受权；交付前第 4 次发现已撤权。
        revoke_after=3,
    )
    retrieval = FakeRetrieval([[_candidate()]])
    generator = FakeGenerator([_outcome(content=_answer_json())])

    with pytest.raises(ConversationSourcesChanged):
        await _run(
            repository=repository,
            evidence=evidence,
            retrieval=retrieval,
            generator=generator,
            question="它的适用范围呢？",
        )

    # 改写与回答各发生一次，但交付前被拦下：无查询运行、消息或引用被持久化。
    assert generator.rewrite_calls == 1
    assert generator.answer_calls == 1
    assert retrieval.calls == 1
    assert [row.stage for row in repository.usage] == [REWRITE_STAGE, "qa_answer"]
    assert repository.query_runs == []
    assert all(message.content != REWRITE_STANDALONE for message in repository.messages)


@pytest.mark.anyio
async def test_version_retry_reuses_single_rewrite() -> None:
    """版本变化触发重检索时不重复改写，也不替换已确定的独立问题。"""

    repository = _history_repository()
    evidence = FakeEvidenceRepository(
        [_evidence_row()],
        states=[_state(CHUNK_ID, version_id=VERSION_ID)],
        # 第 2 次证据读取（交付前复核）为空，触发一次重检索。
        empty_load_numbers=[2],
    )
    retrieval = FakeRetrieval([[_candidate()], [_candidate()]])
    generator = FakeGenerator([_outcome(content=_answer_json())])

    result, repository, _evidence, generator = await _run(
        repository=repository,
        evidence=evidence,
        retrieval=retrieval,
        generator=generator,
        question="它的适用范围呢？",
    )

    assert result.insufficient_evidence is False
    assert generator.rewrite_calls == 1
    assert generator.answer_calls == 2
    assert retrieval.queries == [REWRITE_STANDALONE, REWRITE_STANDALONE]
    assert [row.stage for row in repository.usage] == [
        REWRITE_STAGE,
        "qa_answer",
        "qa_answer",
    ]
    assert repository.query_runs[0].degraded_stages == ("source_retry",)
    assert repository.query_runs[0].standalone_question == REWRITE_STANDALONE
