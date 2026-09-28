"""开发评估 runner 的聚焦单测（不联网、不读 .env、不调用真实模型）。

覆盖：语料准备顺序与状态等待、引用 UUID -> 逻辑标识映射、多轮历史回放与模型请求预算、
越权会话按拒答记录、后端错误/引用缺失导致不完整、40 题完整性、环境 KB 可访问性核对、
``poll_until`` 超时，以及 HTTP 适配器在合成 ``MockTransport`` 上的请求形状与错误分类。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from rag_backend.auth.tokens import CSRF_HEADER_NAME
from rag_backend.evaluation.dataset import EvaluationDataset, load_dataset_bundle
from rag_backend.evaluation.runner import (
    AskOutcome,
    AssetRegistry,
    BackendError,
    ConversationDenied,
    DatasetRunError,
    EnvironmentDescriptor,
    EnvironmentMismatch,
    LogicalRef,
    ModelRequestBudget,
    QuestionExecutionError,
    RoleCredential,
    RunnerError,
    RunOutcome,
    SeedError,
    UploadReceipt,
    build_seed_plan,
    poll_until,
    run_questions,
    seed_corpus,
    validate_static_configuration,
    verify_environment,
)
from rag_backend.evaluation.runner_adapters import HttpBackend, SqlEvaluationDatabase

_EVALUATION_DIR = Path(__file__).resolve().parents[1] / "evaluation"
_DATASET_PATH = _EVALUATION_DIR / "dev-questions.json"
_CORPUS_DIR = _EVALUATION_DIR / "corpus"
_KB_HANDBOOK = "kb-handbook"
_KB_RESTRICTED = "kb-restricted"
_HANDBOOK_V3 = LogicalRef(_KB_HANDBOOK, "handbook", 3)


def _bundle() -> tuple[EvaluationDataset, Any, Path]:
    return load_dataset_bundle(_DATASET_PATH)


# ---------------------------------------------------------------------------
# 语料准备

class FakeUploader:
    def __init__(self, *, existing: Sequence[uuid.UUID] = ()) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self._version_by_document: dict[uuid.UUID, uuid.UUID] = {}
        self._existing = tuple(existing)

    def existing_document_ids(self, *, kb_uuid: uuid.UUID) -> Sequence[uuid.UUID]:
        self.calls.append(("list", kb_uuid))
        return self._existing

    def upload_new_document(
        self,
        *,
        kb_uuid: uuid.UUID,
        title: str,
        filename: str,
        content: bytes,
        idempotency_key: str,
    ) -> UploadReceipt:
        document_uuid = uuid.uuid4()
        version_uuid = uuid.uuid4()
        self._version_by_document[document_uuid] = version_uuid
        self.calls.append(("new", kb_uuid, filename, content, idempotency_key))
        return UploadReceipt(document_uuid=document_uuid, version_uuid=version_uuid)

    def upload_new_version(
        self,
        *,
        document_uuid: uuid.UUID,
        expected_version_uuid: uuid.UUID,
        title: str,
        filename: str,
        content: bytes,
        idempotency_key: str,
    ) -> UploadReceipt:
        assert self._version_by_document[document_uuid] == expected_version_uuid
        version_uuid = uuid.uuid4()
        self._version_by_document[document_uuid] = version_uuid
        self.calls.append(("version", filename, expected_version_uuid, idempotency_key))
        return UploadReceipt(document_uuid=document_uuid, version_uuid=version_uuid)

    def delete_document(self, *, document_uuid: uuid.UUID) -> None:
        self.calls.append(("delete", document_uuid))


class FakeReadiness:
    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []

    def wait_active(
        self, *, document_uuid: uuid.UUID, version_uuid: uuid.UUID, timeout_seconds: float
    ) -> None:
        self.events.append(("active", document_uuid, version_uuid))

    def wait_deleted(self, *, document_uuid: uuid.UUID, timeout_seconds: float) -> None:
        self.events.append(("deleted", document_uuid))


def _seeded_registry() -> tuple[AssetRegistry, FakeUploader, FakeReadiness]:
    _, manifest, _ = _bundle()
    plan = build_seed_plan(manifest)
    registry = AssetRegistry()
    registry.register_knowledge_base(_KB_HANDBOOK, uuid.uuid4())
    registry.register_knowledge_base(_KB_RESTRICTED, uuid.uuid4())
    uploader = FakeUploader()
    readiness = FakeReadiness()
    seed_corpus(
        plan,
        corpus_dir=_CORPUS_DIR,
        uploader=uploader,
        readiness=readiness,
        registry=registry,
        run_id="run-1",
    )
    return registry, uploader, readiness


def test_seed_corpus_uploads_versions_in_order_and_waits() -> None:
    registry, uploader, readiness = _seeded_registry()
    kinds = [call[0] for call in uploader.calls]
    # 先按 KB 列表确认专用隔离 KB 为空；handbook 有两个逻辑版本，legacy-bonus 上传后被删除。
    assert kinds.count("list") == 2
    assert kinds.count("new") == 7
    assert kinds.count("version") == 2  # handbook 与 leave-policy 各一个第二版
    assert kinds.count("delete") == 1
    # 每个版本上传后都立即等待 active；删除后再等 deleted。
    active_events = [event for event in readiness.events if event[0] == "active"]
    deleted_events = [event for event in readiness.events if event[0] == "deleted"]
    assert len(active_events) == 9
    assert len(deleted_events) == 1
    # 逻辑版本映射：handbook 的第一版登记为清单版本 2，第二版为 3。
    assert registry.version_ref(registry.version_uuid(_HANDBOOK_V3)) == _HANDBOOK_V3
    assert registry.version_uuid(LogicalRef(_KB_HANDBOOK, "handbook", 2)) is not None


def test_seed_corpus_refuses_non_empty_kb_before_upload() -> None:
    _, manifest, _ = _bundle()
    plan = build_seed_plan(manifest)
    registry = AssetRegistry()
    registry.register_knowledge_base(_KB_HANDBOOK, uuid.uuid4())
    registry.register_knowledge_base(_KB_RESTRICTED, uuid.uuid4())
    uploader = FakeUploader(existing=(uuid.uuid4(),))
    with pytest.raises(SeedError):
        seed_corpus(
            plan,
            corpus_dir=_CORPUS_DIR,
            uploader=uploader,
            readiness=FakeReadiness(),
            registry=registry,
            run_id="run-1",
        )
    assert [call[0] for call in uploader.calls] == ["list"]


def test_seed_corpus_orders_versions_ascending() -> None:
    _, uploader, _ = _seeded_registry()
    # handbook-v2.md 必须在 handbook-v3.md 之前上传。
    filenames = [
        call[2] if call[0] == "new" else call[1]
        for call in uploader.calls
        if call[0] in ("new", "version")
    ]
    assert filenames.index("handbook-v2.md") < filenames.index("handbook-v3.md")


def _role_descriptor(handbook_uuid: uuid.UUID, restricted_uuid: uuid.UUID) -> EnvironmentDescriptor:
    return EnvironmentDescriptor(
        knowledge_bases={_KB_HANDBOOK: handbook_uuid, _KB_RESTRICTED: restricted_uuid},
        roles={
            "staff": RoleCredential(username="s", password=SecretStr("p")),
            "seed": RoleCredential(username="seed", password=SecretStr("p")),
        },
        seed_role="seed",
    )


def test_verify_environment_checks_read_direction_and_seed_role() -> None:
    dataset, manifest, _ = _bundle()
    registry = AssetRegistry()
    handbook_uuid = uuid.uuid4()
    restricted_uuid = uuid.uuid4()
    registry.register_knowledge_base(_KB_HANDBOOK, handbook_uuid)
    registry.register_knowledge_base(_KB_RESTRICTED, restricted_uuid)
    descriptor = _role_descriptor(handbook_uuid, restricted_uuid)
    # handbook 含逻辑删除文档，因此准备角色必须是 OWNER；kb-restricted 至少 EDITOR。
    good: Mapping[str, Mapping[uuid.UUID, str]] = {
        "staff": {handbook_uuid: "READER"},
        "seed": {handbook_uuid: "OWNER", restricted_uuid: "EDITOR"},
    }
    verify_environment(
        dataset,
        manifest,
        descriptor=descriptor,
        registry=registry,
        knowledge_base_roles=lambda role: good[role],
    )

    leaky: Mapping[str, Mapping[uuid.UUID, str]] = {
        "staff": {handbook_uuid: "READER", restricted_uuid: "READER"},
        "seed": {handbook_uuid: "OWNER", restricted_uuid: "EDITOR"},
    }
    with pytest.raises(EnvironmentMismatch):
        verify_environment(
            dataset,
            manifest,
            descriptor=descriptor,
            registry=registry,
            knowledge_base_roles=lambda role: leaky[role],
        )

    weak_seed: Mapping[str, Mapping[uuid.UUID, str]] = {
        "staff": {handbook_uuid: "READER"},
        "seed": {handbook_uuid: "EDITOR", restricted_uuid: "EDITOR"},
    }
    with pytest.raises(EnvironmentMismatch):
        verify_environment(
            dataset,
            manifest,
            descriptor=descriptor,
            registry=registry,
            knowledge_base_roles=lambda role: weak_seed[role],
        )
    # 资产映射模式无上传，不要求准备角色写权限；题集角色方向仍必须一致。
    verify_environment(
        dataset,
        manifest,
        descriptor=descriptor,
        registry=registry,
        knowledge_base_roles=lambda role: weak_seed[role],
        require_seed_write=False,
    )


def test_static_configuration_requires_roles_and_kb_mapping() -> None:
    dataset, manifest, _ = _bundle()
    plan = build_seed_plan(manifest)
    registry = AssetRegistry()
    registry.register_knowledge_base(_KB_HANDBOOK, uuid.uuid4())
    # kb-restricted 未登记且缺少角色账号：dry-run 前静态校验必须失败。
    descriptor = EnvironmentDescriptor(
        knowledge_bases={_KB_HANDBOOK: registry.kb_uuid(_KB_HANDBOOK)},
        roles={"staff": RoleCredential(username="s", password=SecretStr("p"))},
        seed_role="staff",
    )
    with pytest.raises(DatasetRunError):
        validate_static_configuration(dataset, plan, descriptor=descriptor, registry=registry)


def test_asset_mode_does_not_require_seed_credential() -> None:
    dataset, manifest, _ = _bundle()
    plan = build_seed_plan(manifest)
    registry = AssetRegistry()
    registry.register_knowledge_base(_KB_HANDBOOK, uuid.uuid4())
    registry.register_knowledge_base(_KB_RESTRICTED, uuid.uuid4())
    # 资产映射模式无上传：准备角色不在描述中也应通过静态校验与题集角色核对。
    descriptor = EnvironmentDescriptor(
        knowledge_bases={
            _KB_HANDBOOK: registry.kb_uuid(_KB_HANDBOOK),
            _KB_RESTRICTED: registry.kb_uuid(_KB_RESTRICTED),
        },
        roles={"staff": RoleCredential(username="s", password=SecretStr("p"))},
        seed_role="seed",
    )
    validate_static_configuration(
        dataset, plan, descriptor=descriptor, registry=registry, require_seed_write=False
    )
    verify_environment(
        dataset,
        manifest,
        descriptor=descriptor,
        registry=registry,
        knowledge_base_roles=lambda role: {"staff": {registry.kb_uuid(_KB_HANDBOOK): "READER"}}[
            role
        ],
        require_seed_write=False,
    )
    # 上传模式仍必须提供准备角色凭据。
    with pytest.raises(DatasetRunError):
        validate_static_configuration(dataset, plan, descriptor=descriptor, registry=registry)


# ---------------------------------------------------------------------------
# 逐题执行

class FakeSession:
    def __init__(
        self,
        *,
        citation_uuid: uuid.UUID | None,
        refuse: bool = False,
        deny: bool = False,
        fail: bool = False,
    ) -> None:
        self._citation_uuid = citation_uuid
        self._refuse = refuse
        self._deny = deny
        self._fail = fail
        self.asks: list[tuple[uuid.UUID, str]] = []
        self.conversations: list[tuple[uuid.UUID, ...]] = []

    def create_conversation(self, kb_ids: Sequence[uuid.UUID]) -> uuid.UUID:
        if self._deny:
            raise ConversationDenied("目标 KB 对当前角色不可访问")
        conversation_id = uuid.uuid4()
        self.conversations.append(tuple(kb_ids))
        return conversation_id

    def ask(self, conversation_id: uuid.UUID, question: str) -> AskOutcome:
        if self._fail:
            raise BackendError("提问失败（HTTP 502/GENERATION_FAILED）")
        self.asks.append((conversation_id, question))
        if self._refuse:
            return AskOutcome(refused=True, citation_ids=(), answer_text="无法回答。")
        assert self._citation_uuid is not None
        return AskOutcome(
            refused=False, citation_ids=(self._citation_uuid,), answer_text="回答[1]。"
        )


class FakeQuestionBackend:
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    def session_for(self, role: str) -> FakeSession:
        return self.session


class IdentityCitationLookup:
    def version_ids_for(
        self, citation_ids: Sequence[uuid.UUID]
    ) -> Mapping[uuid.UUID, uuid.UUID]:
        return {citation_id: citation_id for citation_id in citation_ids}


def _multi_turn_dataset() -> EvaluationDataset:
    return EvaluationDataset.model_validate(
        {
            "datasetKind": "dev",
            "datasetVersion": "test-1",
            "corpusManifest": "corpus/manifest.json",
            "questions": [
                {
                    "id": "q-single",
                    "category": "single_document",
                    "scope": {"role": "staff", "kbIds": [_KB_HANDBOOK]},
                    "question": "当前问题",
                    "expectedBehavior": "answer",
                    "goldAnswerPoints": ["要点"],
                    "goldSourceSpans": [
                        {
                            "kbId": _KB_HANDBOOK,
                            "documentId": "handbook",
                            "version": 3,
                            "quote": "引用",
                            "locator": {
                                "sourceType": "markdown",
                                "parserVersion": "v",
                                "headingPath": ["a"],
                                "startLine": 1,
                                "endLine": 1,
                            },
                        }
                    ],
                },
                {
                    "id": "q-multi",
                    "category": "single_document",
                    "scope": {"role": "staff", "kbIds": [_KB_HANDBOOK]},
                    "question": "追问问题",
                    "standaloneQuestion": "独立问题",
                    "history": [
                        {"role": "user", "text": "第一问"},
                        {"role": "assistant", "text": "第一答"},
                    ],
                    "expectedBehavior": "answer",
                    "goldAnswerPoints": ["要点"],
                    "goldSourceSpans": [
                        {
                            "kbId": _KB_HANDBOOK,
                            "documentId": "handbook",
                            "version": 3,
                            "quote": "引用",
                            "locator": {
                                "sourceType": "markdown",
                                "parserVersion": "v",
                                "headingPath": ["a"],
                                "startLine": 1,
                                "endLine": 1,
                            },
                        }
                    ],
                },
            ],
        }
    )


def _question_registry(citation_uuid: uuid.UUID) -> AssetRegistry:
    registry = AssetRegistry()
    registry.register_knowledge_base(_KB_HANDBOOK, uuid.uuid4())
    registry.register_version(ref=_HANDBOOK_V3, version_uuid=citation_uuid)
    return registry


def test_run_questions_replays_history_and_counts_budget() -> None:
    citation_uuid = uuid.uuid4()
    session = FakeSession(citation_uuid=citation_uuid)
    dataset = _multi_turn_dataset()
    budget = ModelRequestBudget(10)
    outcome = run_questions(
        dataset,
        registry=_question_registry(citation_uuid),
        backend=FakeQuestionBackend(session),
        citations=IdentityCitationLookup(),
        budget=budget,
    )
    assert outcome.complete
    assert outcome.results is not None
    assert [result.question_id for result in outcome.results.results] == ["q-single", "q-multi"]
    # 单题最多 2 次回答；多轮题 1 次历史回放(2) + 1 次回答(2) + 1 次改写(1) = 5。
    assert budget.spent == 7
    # 多轮题先回放用户轮次，再问当前问题；助手轮次不重放。
    multi_conversation = session.asks[-2][0]
    assert session.asks[-2] == (multi_conversation, "第一问")
    assert session.asks[-1] == (multi_conversation, "追问问题")


def test_run_questions_uses_actual_behavior_not_gold() -> None:
    citation_uuid = uuid.uuid4()
    # 题目预期 answer，但真实 API 返回拒答：结果必须记录真实拒答。
    session = FakeSession(citation_uuid=citation_uuid, refuse=True)
    outcome = run_questions(
        _multi_turn_dataset(),
        registry=_question_registry(citation_uuid),
        backend=FakeQuestionBackend(session),
        citations=IdentityCitationLookup(),
        budget=ModelRequestBudget(10),
    )
    assert outcome.complete
    assert outcome.results is not None
    assert [result.behavior for result in outcome.results.results] == ["refuse", "refuse"]


def test_run_questions_records_conversation_denied_as_refusal() -> None:
    session = FakeSession(citation_uuid=None, deny=True)
    outcome = run_questions(
        _multi_turn_dataset(),
        registry=_question_registry(uuid.uuid4()),
        backend=FakeQuestionBackend(session),
        citations=IdentityCitationLookup(),
        budget=ModelRequestBudget(10),
    )
    assert outcome.complete
    assert outcome.results is not None
    assert [result.behavior for result in outcome.results.results] == ["refuse", "refuse"]
    assert all(diagnostic.outcome == "conversation_denied" for diagnostic in outcome.diagnostics)


def test_run_questions_backend_error_yields_incomplete_run() -> None:
    session = FakeSession(citation_uuid=None, fail=True)
    outcome = run_questions(
        _multi_turn_dataset(),
        registry=_question_registry(uuid.uuid4()),
        backend=FakeQuestionBackend(session),
        citations=IdentityCitationLookup(),
        budget=ModelRequestBudget(10),
    )
    assert not outcome.complete
    assert outcome.results is None
    assert outcome.diagnostics[-1].question_id == "q-single"
    assert outcome.diagnostics[-1].outcome == "error"


def test_run_questions_missing_citation_is_incomplete() -> None:
    citation_uuid = uuid.uuid4()
    session = FakeSession(citation_uuid=citation_uuid)

    class EmptyLookup:
        def version_ids_for(
            self, citation_ids: Sequence[uuid.UUID]
        ) -> Mapping[uuid.UUID, uuid.UUID]:
            return {}

    outcome = run_questions(
        _multi_turn_dataset(),
        registry=_question_registry(citation_uuid),
        backend=FakeQuestionBackend(session),
        citations=EmptyLookup(),
        budget=ModelRequestBudget(10),
    )
    assert not outcome.complete
    assert outcome.results is None


def test_budget_exceeded_yields_question_diagnostic_without_results() -> None:
    citation_uuid = uuid.uuid4()
    session = FakeSession(citation_uuid=citation_uuid)
    # 限额 2：首题消耗 2，多轮题历史回放需要 2 而余额为 0，应在调用前失败。
    outcome = run_questions(
        _multi_turn_dataset(),
        registry=_question_registry(citation_uuid),
        backend=FakeQuestionBackend(session),
        citations=IdentityCitationLookup(),
        budget=ModelRequestBudget(2),
    )
    assert not outcome.complete
    assert outcome.results is None
    assert outcome.diagnostics[-1].question_id == "q-multi"
    assert outcome.diagnostics[-1].outcome == "error"
    assert "预算" in outcome.diagnostics[-1].detail
    # 预算不足时不得发出该次提问。
    assert all(question != "追问问题" for _conversation, question in session.asks)


def test_run_questions_covers_all_40_dev_questions() -> None:
    dataset, _, _ = _bundle()
    registry, _, _ = _seeded_registry()
    citation_uuid = registry.version_uuid(_HANDBOOK_V3)
    session = FakeSession(citation_uuid=citation_uuid)
    outcome = run_questions(
        dataset,
        registry=registry,
        backend=FakeQuestionBackend(session),
        citations=IdentityCitationLookup(),
        budget=ModelRequestBudget(1000),
    )
    assert outcome.complete
    assert outcome.results is not None
    assert len(outcome.results.results) == 40
    assert len(outcome.diagnostics) == 40


def test_run_questions_login_failure_is_incomplete_with_question_id() -> None:
    class LoginFailingBackend:
        def session_for(self, role: str) -> FakeSession:
            raise BackendError("登录失败（HTTP 401/AUTH_INVALID_CREDENTIALS）")

    outcome = run_questions(
        _multi_turn_dataset(),
        registry=_question_registry(uuid.uuid4()),
        backend=LoginFailingBackend(),
        citations=IdentityCitationLookup(),
        budget=ModelRequestBudget(10),
    )
    assert not outcome.complete
    assert outcome.results is None
    assert outcome.diagnostics[-1].question_id == "q-single"
    assert outcome.diagnostics[-1].outcome == "error"
    assert "登录" in outcome.diagnostics[-1].detail


def test_budget_cost_matches_service_retry_worst_case() -> None:
    budget = ModelRequestBudget(10)
    # 服务端一次提问可因来源变化重试一次生成：最多 2 次回答；有历史再加 1 次改写。
    assert budget.cost_of(rewrite=False) == 2
    assert budget.cost_of(rewrite=True) == 3


def test_estimate_requests_uses_real_question_data() -> None:
    from rag_backend.evaluation.runner import _estimate_requests

    dataset, _, _ = _bundle()
    # 38 题无历史（各 2），2 题多轮（各 2 + 3）= 76 + 10 = 86。
    assert _estimate_requests(dataset) == 86


def test_report_complete_only_for_all_ids() -> None:
    # 保底：RunOutcome.complete 只由 results 是否为 None 决定。
    assert not RunOutcome(None, (), 0).complete
    assert RunOutcome(None, (), 0).results is None


# ---------------------------------------------------------------------------
# 轮询与数据库映射纯逻辑

def test_poll_until_times_out_without_waiting() -> None:
    now = 0.0
    sleeps: list[float] = []

    def clock() -> float:
        return now

    def sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    assert poll_until(lambda: False, timeout_seconds=2.0, clock=clock, sleep=sleep) is False
    assert sum(sleeps) >= 2.0

    assert poll_until(lambda: True, timeout_seconds=0.0, clock=clock, sleep=sleep) is True


def test_sql_database_rejects_non_psycopg_driver() -> None:
    with pytest.raises(RunnerError):
        SqlEvaluationDatabase("sqlite:///eval.db")


# ---------------------------------------------------------------------------
# HTTP 适配器（合成 MockTransport）

def _descriptor(handler_kb: uuid.UUID) -> EnvironmentDescriptor:
    return EnvironmentDescriptor(
        knowledge_bases={_KB_HANDBOOK: handler_kb},
        roles={"staff": RoleCredential(username="staff-user", password=SecretStr("pw-staff"))},
        seed_role="staff",
    )


def _backend(handler: Any, *, kb_uuid: uuid.UUID) -> HttpBackend:
    transport = httpx.MockTransport(handler)
    return HttpBackend(
        base_url="http://127.0.0.1:58080",
        descriptor=_descriptor(kb_uuid),
        transport=transport,
    )


def test_http_backend_login_me_conversation_and_ask() -> None:
    kb_uuid = uuid.uuid4()
    citation_uuid = uuid.uuid4()
    conversation_uuid = uuid.uuid4()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/v1/auth/login":
            return httpx.Response(200, json={"csrfToken": "csrf-value"})
        if request.url.path == "/api/v1/me":
            return httpx.Response(
                200, json={"knowledgeBases": [{"id": str(kb_uuid), "role": "EDITOR"}]}
            )
        if request.url.path == "/api/v1/conversations":
            return httpx.Response(201, json={"conversationId": str(conversation_uuid)})
        if request.url.path.endswith("/messages"):
            return httpx.Response(
                200,
                json={
                    "insufficientEvidence": False,
                    "answer": "回答[1]。",
                    "citations": [{"citationId": str(citation_uuid)}],
                },
            )
        raise AssertionError(request.url.path)

    backend = _backend(handler, kb_uuid=kb_uuid)
    try:
        session = backend.session_for("staff")
        assert session.knowledge_base_roles() == {kb_uuid: "EDITOR"}
        conversation_id = session.create_conversation([kb_uuid])
        assert conversation_id == conversation_uuid
        outcome = session.ask(conversation_id, "问题")
        assert outcome.refused is False
        assert outcome.citation_ids == (citation_uuid,)
        assert outcome.answer_text == "回答[1]。"
    finally:
        backend.close()

    login = next(request for request in seen if request.url.path == "/api/v1/auth/login")
    assert login.headers["Origin"] == "http://127.0.0.1:58080"
    create = next(request for request in seen if request.url.path == "/api/v1/conversations")
    assert create.headers[CSRF_HEADER_NAME] == "csrf-value"


def test_http_backend_lists_existing_documents() -> None:
    kb_uuid = uuid.uuid4()
    document_uuid = uuid.uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/login":
            return httpx.Response(200, json={"csrfToken": "c"})
        if request.url.path.endswith("/documents"):
            return httpx.Response(200, json={"documents": [{"id": str(document_uuid)}]})
        raise AssertionError(request.url.path)

    backend = _backend(handler, kb_uuid=kb_uuid)
    try:
        session = backend.seed_session()
        assert list(session.existing_document_ids(kb_uuid=kb_uuid)) == [document_uuid]
    finally:
        backend.close()


def test_http_backend_maps_conversation_404_to_denied() -> None:
    kb_uuid = uuid.uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/login":
            return httpx.Response(200, json={"csrfToken": "c"})
        if request.url.path == "/api/v1/conversations":
            return httpx.Response(404, json={"code": "KNOWLEDGE_BASE_NOT_FOUND"})
        raise AssertionError(request.url.path)

    backend = _backend(handler, kb_uuid=kb_uuid)
    try:
        with pytest.raises(ConversationDenied):
            backend.session_for("staff").create_conversation([kb_uuid])
    finally:
        backend.close()


def test_http_backend_ask_server_error_is_backend_error() -> None:
    kb_uuid = uuid.uuid4()
    conversation_uuid = uuid.uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/login":
            return httpx.Response(200, json={"csrfToken": "c"})
        return httpx.Response(502, json={"code": "GENERATION_FAILED"})

    backend = _backend(handler, kb_uuid=kb_uuid)
    try:
        with pytest.raises(BackendError):
            backend.session_for("staff").ask(conversation_uuid, "问题")
    finally:
        backend.close()


def test_http_backend_upload_and_delete_use_headers() -> None:
    kb_uuid = uuid.uuid4()
    document_uuid = uuid.uuid4()
    version_uuid = uuid.uuid4()
    seen: list[httpx.Request] = []

    class Handler:
        def __call__(self, request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/api/v1/auth/login":
                return httpx.Response(200, json={"csrfToken": "c"})
            if request.method == "POST" and request.url.path.endswith("/documents"):
                return httpx.Response(
                    202,
                    json={
                        "documentId": str(document_uuid),
                        "versionId": str(version_uuid),
                        "jobId": str(uuid.uuid4()),
                    },
                )
            if request.method == "DELETE":
                return httpx.Response(204)
            raise AssertionError(request.url.path)

    backend = _backend(Handler(), kb_uuid=kb_uuid)
    try:
        session = backend.seed_session()
        receipt = session.upload_new_document(
            kb_uuid=kb_uuid,
            title="eval:handbook",
            filename="handbook-v2.md",
            content=b"# t\n",
            idempotency_key="key-1",
        )
        assert receipt == UploadReceipt(document_uuid=document_uuid, version_uuid=version_uuid)
        session.delete_document(document_uuid=document_uuid)
    finally:
        backend.close()

    upload = next(
        request
        for request in seen
        if request.method == "POST" and "documents" in str(request.url)
    )
    assert upload.headers["Idempotency-Key"] == "key-1"
    assert upload.headers[CSRF_HEADER_NAME] == "c"
    delete_request = next(request for request in seen if request.method == "DELETE")
    assert delete_request.headers[CSRF_HEADER_NAME] == "c"


def test_asset_map_loads_explicit_mapping(tmp_path: Path) -> None:
    from rag_backend.evaluation.runner import load_asset_map

    kb_uuid = uuid.uuid4()
    document_uuid = uuid.uuid4()
    version_uuid = uuid.uuid4()
    payload = {
        "knowledgeBases": {_KB_HANDBOOK: str(kb_uuid)},
        "documents": {
            "handbook": {
                "kbId": _KB_HANDBOOK,
                "documentId": str(document_uuid),
                "versions": {"3": str(version_uuid)},
            }
        },
    }
    path = tmp_path / "assets.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    registry = AssetRegistry()
    load_asset_map(path, registry=registry)
    assert registry.kb_uuid(_KB_HANDBOOK) == kb_uuid
    assert (
        registry.document_uuid(logical_kb=_KB_HANDBOOK, logical_document="handbook")
        == document_uuid
    )
    assert registry.version_ref(version_uuid) == _HANDBOOK_V3


def test_question_execution_error_requires_question_id() -> None:
    error = QuestionExecutionError("q-1", "引用缺失")
    assert error.question_id == "q-1"
    assert error.reason == "引用缺失"
