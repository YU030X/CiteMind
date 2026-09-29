"""Phase 1 开发评估集最小结果 producer（编排核心与 CLI）。

职责边界：

- 只读取既有 ``tests/evaluation/dev-questions.json`` 与 ``corpus/manifest.json``；不改题、
  不改 gold。
- 用**真实 API** 按每题 ``scope.role``/``scope.kbIds`` 与 ``history`` 执行会话：多轮题先按真实顺序
  回放历史**用户**轮次，再提当前问题，因此追问改写与失败都计入模型请求预算。
- 引用 UUID 先由只读数据库查询映射到 ``document_version``，再由本次运行登记的
  ``version UUID -> (逻辑 KB, 逻辑文档, 逻辑版本)`` 映射回逻辑标识；不按标题或版本号猜测，
  也不扩大公开的 ``Citation`` 字段。语料准备状态来自清单，**与 gold 完全分离**。
- 结果只有恰好覆盖题集全部 id 时才写出；任何 API 错误、超时、未 READY 或无法映射的引用都让
  运行不完整，CLI 退出非零且不写结果文件，绝不编造拒答填充 40 题。
- 默认 dry-run：不联网、不写库、不调用模型，只打印计划与预算估算。真实调用必须显式
  ``--allow-real-llm`` 并给出正的 ``--max-model-requests`` 硬上限；对 ``datasetKind=holdout``
  的真实运行还必须显式 ``--confirm-holdout``，dry-run 与离线结构校验不要求。

本模块不建评估平台、不建新数据库、不用 LLM 裁判、不改业务 API、不引入新框架。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic.alias_generators import to_camel

from rag_backend.evaluation.dataset import (
    CorpusManifest,
    DatasetValidationError,
    EvaluationDataset,
    EvaluationQuestion,
    load_dataset_bundle,
    validate_dataset,
)
from rag_backend.evaluation.metrics import (
    EvaluationResults,
    QuestionResult,
    ResultCitation,
)

# ---------------------------------------------------------------------------
# 配置模型

_DB_NAME_SUFFIX = "_test"


class _ConfigModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class RoleCredential(_ConfigModel):
    """合成账号；密码用 ``SecretStr``，repr/日志不打印明文。"""

    username: str = Field(min_length=1)
    password: SecretStr


class EnvironmentDescriptor(_ConfigModel):
    """隔离环境的明示连接信息：逻辑 KB -> 真实 UUID、角色账号与准备账号。

    上传模式下 ``seedRole`` 对应的账号必须对全部语料 KB 有 EDITOR，且对存在删除文档的 KB 有
    OWNER；资产映射模式无上传，不要求该账号存在。该账号只用于上传/删除语料，不参与答题。
    """

    knowledge_bases: dict[str, uuid.UUID] = Field(min_length=1)
    roles: dict[str, RoleCredential] = Field(min_length=1)
    seed_role: str = Field(min_length=1)

    def credential(self, role: str) -> RoleCredential:
        if role not in self.roles:
            raise DatasetRunError(f"环境描述缺少角色账号：{role}")
        return self.roles[role]


# ---------------------------------------------------------------------------
# 错误类型


class RunnerError(Exception):
    """runner 前置条件、环境或执行失败。"""


class DatasetRunError(RunnerError):
    """题集/语料/描述文件相关问题。"""


class BudgetExceeded(RunnerError):
    """模型请求预算耗尽；在发起下一次调用前抛出。"""


class SeedError(RunnerError):
    """语料准备失败（上传/删除/未 READY）。"""


class EnvironmentMismatch(RunnerError):
    """实际可访问 KB 与题集清单不一致。"""


class ConversationDenied(RunnerError):
    """创建会话被拒（KB 不可访问）；按真实拒答记录，不作为错误吞并。"""


class BackendError(RunnerError):
    """真实 API 调用失败（HTTP 状态、业务码或传输错误）。"""


class QuestionExecutionError(RunnerError):
    """单题执行失败：API 错误、超时或引用无法映射。"""

    def __init__(self, question_id: str, reason: str) -> None:
        super().__init__(f"[{question_id}] {reason}")
        self.question_id = question_id
        self.reason = reason


# ---------------------------------------------------------------------------
# 逻辑来源标识


@dataclass(frozen=True)
class LogicalRef:
    """逻辑来源标识：逻辑 KB、逻辑文档与清单内的逻辑版本号。"""

    kb_id: str
    document_id: str
    version: int


# ---------------------------------------------------------------------------
# 语料准备计划（只依赖清单，不读 gold）

@dataclass(frozen=True)
class SeedVersion:
    version: int
    file: str
    source_type: str
    status: str


@dataclass(frozen=True)
class SeedDocument:
    kb_id: str
    document_id: str
    versions: tuple[SeedVersion, ...]
    deleted: bool


@dataclass(frozen=True)
class SeedPlan:
    documents: tuple[SeedDocument, ...]

    @property
    def version_count(self) -> int:
        return sum(len(document.versions) for document in self.documents)


def build_seed_plan(manifest: CorpusManifest) -> SeedPlan:
    """按清单构造准备计划：版本升序上传，``currentVersion`` 为空即逻辑删除。"""

    documents: list[SeedDocument] = []
    for kb_id in sorted(manifest.knowledge_bases):
        config = manifest.knowledge_bases[kb_id]
        for document_id in sorted(config.documents):
            document = config.documents[document_id]
            versions = tuple(
                SeedVersion(entry.version, entry.file, entry.source_type, entry.status)
                for entry in sorted(document.versions, key=lambda item: item.version)
            )
            documents.append(
                SeedDocument(
                    kb_id=kb_id,
                    document_id=document_id,
                    versions=versions,
                    deleted=document.current_version is None,
                )
            )
    return SeedPlan(tuple(documents))


# ---------------------------------------------------------------------------
# 资产登记表

class AssetRegistry:
    """把真实 UUID 映射回逻辑标识；只在语料准备/资产映射阶段写入。"""

    def __init__(self) -> None:
        self._kb_uuids: dict[str, uuid.UUID] = {}
        self._document_refs: dict[uuid.UUID, tuple[str, str]] = {}
        self._version_refs: dict[uuid.UUID, LogicalRef] = {}

    def register_knowledge_base(self, logical_id: str, kb_uuid: uuid.UUID) -> None:
        self._kb_uuids[logical_id] = kb_uuid

    def register_document(
        self, *, logical_kb: str, logical_document: str, document_uuid: uuid.UUID
    ) -> None:
        self._document_refs[document_uuid] = (logical_kb, logical_document)

    def register_version(self, *, ref: LogicalRef, version_uuid: uuid.UUID) -> None:
        self._version_refs[version_uuid] = ref

    def kb_uuid(self, logical_id: str) -> uuid.UUID:
        try:
            return self._kb_uuids[logical_id]
        except KeyError as error:
            raise DatasetRunError(f"环境描述缺少 knowledgeBase：{logical_id}") from error

    def document_uuid(self, *, logical_kb: str, logical_document: str) -> uuid.UUID:
        for document_uuid, (kb_id, document_id) in self._document_refs.items():
            if (kb_id, document_id) == (logical_kb, logical_document):
                return document_uuid
        raise DatasetRunError(f"资产登记缺少文档：{logical_kb}/{logical_document}")

    def version_ref(self, version_uuid: uuid.UUID) -> LogicalRef:
        try:
            return self._version_refs[version_uuid]
        except KeyError as error:
            raise DatasetRunError(f"引用指向未登记的版本：{version_uuid}") from error

    def version_uuid(self, ref: LogicalRef) -> uuid.UUID:
        for version_uuid, registered in self._version_refs.items():
            if registered == ref:
                return version_uuid
        raise DatasetRunError(
            f"资产登记缺少版本：{ref.kb_id}/{ref.document_id}@{ref.version}"
        )


# ---------------------------------------------------------------------------
# 依赖协议（真实实现见 ``runner_adapters``）

@dataclass(frozen=True)
class UploadReceipt:
    document_uuid: uuid.UUID
    version_uuid: uuid.UUID


@dataclass(frozen=True)
class AskOutcome:
    refused: bool
    citation_ids: tuple[uuid.UUID, ...]
    answer_text: str


class CorpusUploader(Protocol):
    def existing_document_ids(self, *, kb_uuid: uuid.UUID) -> Sequence[uuid.UUID]: ...

    def upload_new_document(
        self,
        *,
        kb_uuid: uuid.UUID,
        title: str,
        filename: str,
        content: bytes,
        idempotency_key: str,
    ) -> UploadReceipt: ...

    def upload_new_version(
        self,
        *,
        document_uuid: uuid.UUID,
        expected_version_uuid: uuid.UUID,
        title: str,
        filename: str,
        content: bytes,
        idempotency_key: str,
    ) -> UploadReceipt: ...

    def delete_document(self, *, document_uuid: uuid.UUID) -> None: ...


class ReadinessProbe(Protocol):
    def wait_active(
        self, *, document_uuid: uuid.UUID, version_uuid: uuid.UUID, timeout_seconds: float
    ) -> None: ...

    def wait_deleted(self, *, document_uuid: uuid.UUID, timeout_seconds: float) -> None: ...


class RoleSession(Protocol):
    def create_conversation(self, kb_ids: Sequence[uuid.UUID]) -> uuid.UUID: ...

    def ask(self, conversation_id: uuid.UUID, question: str) -> AskOutcome: ...


class QuestionBackend(Protocol):
    def session_for(self, role: str) -> RoleSession: ...


class CitationLookup(Protocol):
    def version_ids_for(
        self, citation_ids: Sequence[uuid.UUID]
    ) -> Mapping[uuid.UUID, uuid.UUID]: ...


# ---------------------------------------------------------------------------
# 模型请求预算

class ModelRequestBudget:
    """本地硬上限：每次提问保守预留最多两次回答请求，已有历史再预留一次改写请求。

    服务端在一次提问内可因证据来源变化最多重试一次生成（共两次回答请求），改写是否真正发生
    也由服务端决定；这里按**最坏情况**在调用前预留，因此预留数是成本上界而非实际计费次数。
    失败的提问同样占用额度，不会因失败而绕过上限。
    """

    # 一次提问最多两次回答请求；存在前序用户轮次时再加一次改写请求。
    BASE_COST = 2
    REWRITE_COST = 1

    def __init__(self, limit: int) -> None:
        if limit < 0:
            raise ValueError("模型请求上限不能为负数")
        self._limit = limit
        self._spent = 0

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def spent(self) -> int:
        return self._spent

    @property
    def remaining(self) -> int:
        return self._limit - self._spent

    def cost_of(self, *, rewrite: bool) -> int:
        return self.BASE_COST + (self.REWRITE_COST if rewrite else 0)

    def spend(self, *, rewrite: bool) -> None:
        cost = self.cost_of(rewrite=rewrite)
        if self._spent + cost > self._limit:
            raise BudgetExceeded(
                f"模型请求预算不足以发起下一次调用：已用 {self._spent}/{self._limit}，"
                f"本次需要 {cost}"
            )
        self._spent += cost


# ---------------------------------------------------------------------------
# 轮询

def poll_until(
    check: Callable[[], bool],
    *,
    timeout_seconds: float,
    interval_seconds: float = 0.5,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """在超时前轮询 ``check``；超时返回 ``False``，至少执行一次检查。"""

    deadline = clock() + timeout_seconds
    while True:
        if check():
            return True
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleep(min(interval_seconds, remaining))


# ---------------------------------------------------------------------------
# 语料准备

def _read_corpus_bytes(corpus_dir: Path, filename: str) -> bytes:
    candidate = (corpus_dir / filename).resolve()
    root = corpus_dir.resolve()
    if root not in candidate.parents:
        raise DatasetRunError(f"语料文件名越界：{filename}")
    try:
        return candidate.read_bytes()
    except OSError as error:
        raise DatasetRunError(f"语料文件不可读：{filename}") from error


def seed_corpus(
    plan: SeedPlan,
    *,
    corpus_dir: Path,
    uploader: CorpusUploader,
    readiness: ReadinessProbe,
    registry: AssetRegistry,
    run_id: str,
    title_prefix: str = "eval:",
    timeout_seconds: float = 300.0,
) -> None:
    """按版本升序上传每个文档，逐版等待 READY，最后删除应删除的文档。

    开始任何上传前先核对每个语料 KB 当前没有未删除文档，否则静态失败，不自动清理既有资料；
    每一步都等待当前版本成为 ``active`` 才继续，保证新版本的 ``expectedVersionId`` 始终有效，
    并且不会把 superseded/deleted 状态提前施加到后续题目的 gold 上。

    边界：``existing_document_ids`` 只看到未删除文档，因此只包含逻辑删除文档的 KB 会被判为空；
    可复现运行应使用全新专用隔离 KB。
    """

    _assert_corpus_kbs_empty(plan, uploader=uploader, registry=registry)
    for document in plan.documents:
        kb_uuid = registry.kb_uuid(document.kb_id)
        document_uuid: uuid.UUID | None = None
        previous_version_uuid: uuid.UUID | None = None
        title = f"{title_prefix}{document.document_id}"
        for version in document.versions:
            content = _read_corpus_bytes(corpus_dir, version.file)
            key = f"{run_id}:{document.kb_id}:{document.document_id}:{version.version}"
            if previous_version_uuid is None:
                receipt = uploader.upload_new_document(
                    kb_uuid=kb_uuid,
                    title=title,
                    filename=version.file,
                    content=content,
                    idempotency_key=key,
                )
            else:
                if document_uuid is None:
                    raise SeedError("内部状态错误：缺少文档 UUID")
                receipt = uploader.upload_new_version(
                    document_uuid=document_uuid,
                    expected_version_uuid=previous_version_uuid,
                    title=title,
                    filename=version.file,
                    content=content,
                    idempotency_key=key,
                )
                if receipt.document_uuid != document_uuid:
                    raise SeedError("新版本上传返回了不同的 documentId")
            document_uuid = receipt.document_uuid
            registry.register_document(
                logical_kb=document.kb_id,
                logical_document=document.document_id,
                document_uuid=receipt.document_uuid,
            )
            registry.register_version(
                ref=LogicalRef(document.kb_id, document.document_id, version.version),
                version_uuid=receipt.version_uuid,
            )
            readiness.wait_active(
                document_uuid=receipt.document_uuid,
                version_uuid=receipt.version_uuid,
                timeout_seconds=timeout_seconds,
            )
            previous_version_uuid = receipt.version_uuid
        if document.deleted:
            if document_uuid is None:
                raise SeedError("内部状态错误：删除文档缺少文档 UUID")
            uploader.delete_document(document_uuid=document_uuid)
            readiness.wait_deleted(document_uuid=document_uuid, timeout_seconds=timeout_seconds)


def _assert_corpus_kbs_empty(
    plan: SeedPlan,
    *,
    uploader: CorpusUploader,
    registry: AssetRegistry,
) -> None:
    """上传前核对每个语料 KB 没有未删除文档；不自动删除既有资料。"""

    checked: set[uuid.UUID] = set()
    for document in plan.documents:
        kb_uuid = registry.kb_uuid(document.kb_id)
        if kb_uuid in checked:
            continue
        checked.add(kb_uuid)
        existing = uploader.existing_document_ids(kb_uuid=kb_uuid)
        if existing:
            raise SeedError(
                f"语料 KB {document.kb_id} 已有 {len(existing)} 个未删除文档；"
                "请使用全新专用隔离 KB，runner 不会自动清理既有资料。"
            )


def verify_ready(
    plan: SeedPlan,
    *,
    readiness: ReadinessProbe,
    registry: AssetRegistry,
) -> None:
    """资产映射模式下核对全部文档已处于期望状态；不等待，未就绪立即失败。"""

    for document in plan.documents:
        document_uuid = registry.document_uuid(
            logical_kb=document.kb_id, logical_document=document.document_id
        )
        if document.deleted:
            readiness.wait_deleted(document_uuid=document_uuid, timeout_seconds=0.0)
            continue
        active = registry.version_uuid(
            LogicalRef(document.kb_id, document.document_id, document.versions[-1].version)
        )
        readiness.wait_active(
            document_uuid=document_uuid, version_uuid=active, timeout_seconds=0.0
        )


# ---------------------------------------------------------------------------
# 逐题执行

@dataclass(frozen=True)
class QuestionDiagnostic:
    question_id: str
    outcome: str
    detail: str = ""


@dataclass(frozen=True)
class RunOutcome:
    results: EvaluationResults | None
    diagnostics: tuple[QuestionDiagnostic, ...]
    budget_spent: int

    @property
    def complete(self) -> bool:
        return self.results is not None


def run_questions(
    dataset: EvaluationDataset,
    *,
    registry: AssetRegistry,
    backend: QuestionBackend,
    citations: CitationLookup,
    budget: ModelRequestBudget,
) -> RunOutcome:
    """按题集顺序执行全部题目；只有完整覆盖时 ``results`` 非空。"""

    results: list[QuestionResult] = []
    diagnostics: list[QuestionDiagnostic] = []
    for question in dataset.questions:
        try:
            result = _run_question(
                question, registry=registry, backend=backend, citations=citations, budget=budget
            )
        except ConversationDenied as error:
            results.append(
                QuestionResult(
                    question_id=question.id, behavior="refuse", citations=[], answer_text=""
                )
            )
            diagnostics.append(QuestionDiagnostic(question.id, "conversation_denied", str(error)))
            continue
        except QuestionExecutionError as error:
            diagnostics.append(QuestionDiagnostic(question.id, "error", error.reason))
            return RunOutcome(None, tuple(diagnostics), budget.spent)
        results.append(result)
        diagnostics.append(QuestionDiagnostic(question.id, result.behavior))

    complete = _covers_exactly(dataset, results)
    return RunOutcome(
        EvaluationResults(
            dataset_kind=dataset.dataset_kind,
            dataset_version=dataset.dataset_version,
            results=results,
        )
        if complete
        else None,
        tuple(diagnostics),
        budget.spent,
    )


def _run_question(
    question: EvaluationQuestion,
    *,
    registry: AssetRegistry,
    backend: QuestionBackend,
    citations: CitationLookup,
    budget: ModelRequestBudget,
) -> QuestionResult:
    try:
        session = backend.session_for(question.scope.role)
    except BackendError as error:
        raise QuestionExecutionError(question.id, f"角色登录失败：{error}") from error
    kb_ids = [registry.kb_uuid(kb_id) for kb_id in question.scope.kb_ids]
    try:
        conversation_id = session.create_conversation(kb_ids)
    except ConversationDenied:
        raise
    except BackendError as error:
        raise QuestionExecutionError(question.id, str(error)) from error

    prior_asks = 0
    for turn in question.history:
        if turn.role != "user":
            continue
        _spend(question, budget=budget, rewrite=prior_asks > 0)
        try:
            session.ask(conversation_id, turn.text)
        except BackendError as error:
            raise QuestionExecutionError(question.id, f"历史轮次回放失败：{error}") from error
        prior_asks += 1

    _spend(question, budget=budget, rewrite=prior_asks > 0)
    try:
        outcome = session.ask(conversation_id, question.question)
    except BackendError as error:
        raise QuestionExecutionError(question.id, str(error)) from error

    if outcome.refused:
        return QuestionResult(
            question_id=question.id,
            behavior="refuse",
            citations=[],
            answer_text=outcome.answer_text,
        )
    if not outcome.citation_ids:
        raise QuestionExecutionError(question.id, "回答缺少引用，无法作为可作答结果")
    resolved = citations.version_ids_for(outcome.citation_ids)
    result_citations: list[ResultCitation] = []
    for citation_id in outcome.citation_ids:
        version_uuid = resolved.get(citation_id)
        if version_uuid is None:
            raise QuestionExecutionError(question.id, f"引用 {citation_id} 无法映射到版本")
        ref = registry.version_ref(version_uuid)
        result_citations.append(
            ResultCitation(kb_id=ref.kb_id, document_id=ref.document_id, version=ref.version)
        )
    return QuestionResult(
        question_id=question.id,
        behavior="answer",
        citations=result_citations,
        answer_text=outcome.answer_text,
    )


def _spend(question: EvaluationQuestion, *, budget: ModelRequestBudget, rewrite: bool) -> None:
    """在发起下一次调用前预留额度；不足时带上题目 id 安全失败。"""

    try:
        budget.spend(rewrite=rewrite)
    except BudgetExceeded as error:
        raise QuestionExecutionError(question.id, str(error)) from error


def _covers_exactly(dataset: EvaluationDataset, results: Sequence[QuestionResult]) -> bool:
    expected = [question.id for question in dataset.questions]
    actual = [result.question_id for result in results]
    return sorted(actual) == sorted(expected) and len(actual) == len(expected)


# ---------------------------------------------------------------------------
# 环境核对

_SEED_ROLE_RANK: dict[str, int] = {"READER": 1, "EDITOR": 2, "OWNER": 3}


def verify_environment(
    dataset: EvaluationDataset,
    manifest: CorpusManifest,
    *,
    descriptor: EnvironmentDescriptor,
    registry: AssetRegistry,
    knowledge_base_roles: Callable[[str], Mapping[uuid.UUID, str]],
    require_seed_write: bool = True,
) -> None:
    """核对各角色真实 KB 角色与题集清单一致，并（可选）确认准备角色具备所需写权限。

    题集角色：清单声明可访问的 KB 必须在且清单声明不可访问的 KB 不得出现。
    准备角色（仅上传模式）：每个语料 KB 至少 EDITOR，含删除文档的 KB 必须 OWNER。
    资产映射模式无上传，``require_seed_write=False`` 时不要求准备角色凭据。
    """

    for role in sorted({question.scope.role for question in dataset.questions}):
        descriptor.credential(role)
        roles = knowledge_base_roles(role)
        accessible = frozenset(roles)
        for kb_id in manifest.roles[role].knowledge_bases:
            kb_uuid = registry.kb_uuid(kb_id)
            if kb_uuid not in accessible:
                raise EnvironmentMismatch(
                    f"角色 {role} 在环境中无法访问清单声明的 KB {kb_id}"
                )
        for kb_id in sorted(manifest.knowledge_bases):
            if manifest.role_can_access_kb(role, kb_id):
                continue
            if registry.kb_uuid(kb_id) in accessible:
                raise EnvironmentMismatch(
                    f"角色 {role} 在环境中能访问清单声明不可访问的 KB {kb_id}"
                )

    if not require_seed_write:
        return
    descriptor.credential(descriptor.seed_role)
    seed_roles = knowledge_base_roles(descriptor.seed_role)
    for kb_id in sorted(manifest.knowledge_bases):
        kb_uuid = registry.kb_uuid(kb_id)
        actual = seed_roles.get(kb_uuid)
        if actual is None:
            raise EnvironmentMismatch(
                f"准备角色 {descriptor.seed_role} 在环境中无法访问语料 KB {kb_id}"
            )
        required = "OWNER" if _kb_has_deleted_document(manifest, kb_id) else "EDITOR"
        if _SEED_ROLE_RANK.get(actual, 0) < _SEED_ROLE_RANK[required]:
            raise EnvironmentMismatch(
                f"准备角色 {descriptor.seed_role} 对 KB {kb_id} 的角色为 {actual}，"
                f"需要至少 {required}"
            )


def _kb_has_deleted_document(manifest: CorpusManifest, kb_id: str) -> bool:
    documents = manifest.knowledge_bases[kb_id].documents
    return any(document.current_version is None for document in documents.values())


def validate_static_configuration(
    dataset: EvaluationDataset,
    plan: SeedPlan,
    *,
    descriptor: EnvironmentDescriptor,
    registry: AssetRegistry,
    require_seed_write: bool = True,
) -> None:
    """dry-run 前不联网就能判定的配置校验：角色凭据与 KB 映射必须完整。

    资产映射模式无上传，``require_seed_write=False`` 时不要求准备角色凭据。
    """

    if require_seed_write:
        descriptor.credential(descriptor.seed_role)
    for role in sorted({question.scope.role for question in dataset.questions}):
        descriptor.credential(role)
    for document in plan.documents:
        registry.kb_uuid(document.kb_id)


# ---------------------------------------------------------------------------
# 资产映射文件

def load_asset_map(path: Path, *, registry: AssetRegistry) -> None:
    """读取显式资产映射并填充分辨表；字段缺失或类型不符立即失败。"""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DatasetRunError(f"资产映射不可读：{path.name}") from error
    if not isinstance(payload, dict):
        raise DatasetRunError("资产映射必须是 JSON 对象")
    knowledge_bases = payload.get("knowledgeBases")
    documents = payload.get("documents")
    if not isinstance(knowledge_bases, dict) or not isinstance(documents, dict):
        raise DatasetRunError("资产映射缺少 knowledgeBases/documents")
    for logical_id, raw_uuid in knowledge_bases.items():
        registry.register_knowledge_base(str(logical_id), _as_uuid(raw_uuid, "knowledgeBase"))
    for logical_document, raw_document in documents.items():
        if not isinstance(raw_document, dict):
            raise DatasetRunError(f"文档 {logical_document} 的映射必须是对象")
        logical_kb = raw_document.get("kbId")
        document_uuid = _as_uuid(raw_document.get("documentId"), "documentId")
        if not isinstance(logical_kb, str) or not logical_kb:
            raise DatasetRunError(f"文档 {logical_document} 缺少 kbId")
        registry.register_document(
            logical_kb=logical_kb,
            logical_document=str(logical_document),
            document_uuid=document_uuid,
        )
        raw_versions = raw_document.get("versions")
        if not isinstance(raw_versions, dict):
            raise DatasetRunError(f"文档 {logical_document} 缺少 versions")
        for raw_version, raw_version_uuid in raw_versions.items():
            try:
                version_no = int(raw_version)
            except (TypeError, ValueError) as error:
                raise DatasetRunError(f"文档 {logical_document} 的版本号非法") from error
            registry.register_version(
                ref=LogicalRef(logical_kb, str(logical_document), version_no),
                version_uuid=_as_uuid(raw_version_uuid, "versionId"),
            )


def _as_uuid(value: object, label: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    if not isinstance(value, str):
        raise DatasetRunError(f"{label} 必须是 UUID 字符串")
    try:
        return uuid.UUID(value)
    except ValueError as error:
        raise DatasetRunError(f"{label} 不是合法 UUID") from error


# ---------------------------------------------------------------------------
# CLI

def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m rag_backend.evaluation.runner",
        description="Phase 1 开发题集最小结果 producer（默认 dry-run，不联网、不调用模型）。",
    )
    parser.add_argument("--dataset", type=Path, default=_default_dataset_path())
    parser.add_argument("--descriptor", type=Path, required=True, help="隔离环境描述 JSON")
    parser.add_argument("--asset-map", type=Path, default=None, help="可选：显式资产映射 JSON")
    parser.add_argument("--api-base-url", default=None, help="真实运行必填；API 基址")
    parser.add_argument(
        "--database-url-env",
        default="EVAL_DATABASE_URL",
        help="保存只读数据库 DSN 的环境变量名（默认 EVAL_DATABASE_URL）",
    )
    parser.add_argument(
        "--allow-database-name",
        default=None,
        help="数据库名不以 _test 结尾时，显式重申其数据库名以确认不是开发库",
    )
    parser.add_argument("--results-out", type=Path, default=None, help="真实运行的结果文件路径")
    parser.add_argument("--diagnostics-out", type=Path, default=None, help="可选诊断文件路径")
    parser.add_argument(
        "--allow-non-loopback-api",
        action="store_true",
        help="允许 api-base-url 指向非回环地址；默认只接受回环地址",
    )
    parser.add_argument(
        "--allow-real-llm",
        action="store_true",
        help="显式开启真实 API/模型调用；默认 dry-run 不联网",
    )
    parser.add_argument(
        "--confirm-holdout",
        action="store_true",
        help="真实运行 holdout 题集时必须显式确认的护栏；dry-run 不要求",
    )
    parser.add_argument(
        "--max-model-requests",
        type=int,
        default=0,
        help="真实运行的模型请求硬上限（含改写与失败），必须为正",
    )
    parser.add_argument("--http-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--ready-timeout-seconds", type=float, default=300.0)
    return parser.parse_args(argv)


def _is_loopback_url(base_url: str) -> bool:
    from urllib.parse import urlparse

    return (urlparse(base_url).hostname or "") in {"127.0.0.1", "localhost", "::1"}


def _default_dataset_path() -> Path:
    return Path(__file__).resolve().parents[4] / "tests" / "evaluation" / "dev-questions.json"


def _load_descriptor(path: Path) -> EnvironmentDescriptor:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise DatasetRunError(f"环境描述不可读：{path.name}") from error
    try:
        return EnvironmentDescriptor.model_validate_json(text)
    except ValueError as error:
        # 不回显原始输入，避免验证错误体带出凭据。
        raise DatasetRunError(f"环境描述非法：{path.name}") from error


def _register_descriptor_kbs(descriptor: EnvironmentDescriptor, registry: AssetRegistry) -> None:
    for logical_id, kb_uuid in descriptor.knowledge_bases.items():
        registry.register_knowledge_base(logical_id, kb_uuid)


def _sanitize(message: str, descriptor: EnvironmentDescriptor | None) -> str:
    """从错误消息中移除任何已知密码，避免凭据写入诊断或标准错误。"""

    if descriptor is None:
        return message
    for credential in descriptor.roles.values():
        secret = credential.password.get_secret_value()
        if secret:
            message = message.replace(secret, "***")
    return message


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    descriptor: EnvironmentDescriptor | None = None
    try:
        dataset, manifest, corpus_dir = load_dataset_bundle(args.dataset)
        validate_dataset(dataset, manifest, corpus_dir)
        if (
            args.allow_real_llm
            and dataset.dataset_kind == "holdout"
            and not args.confirm_holdout
        ):
            print("真实运行 holdout 题集必须显式加 --confirm-holdout。", file=sys.stderr)
            return 1
        descriptor = _load_descriptor(args.descriptor)
        registry = AssetRegistry()
        _register_descriptor_kbs(descriptor, registry)
        plan = build_seed_plan(manifest)
    except (DatasetRunError, DatasetValidationError, ValueError) as error:
        print(f"准备失败：{error}", file=sys.stderr)
        return 1

    print(
        f"开发题集：kind={dataset.dataset_kind} version={dataset.dataset_version} "
        f"total={len(dataset.questions)}"
    )
    print(
        f"语料计划：documents={len(plan.documents)} versions={plan.version_count} "
        f"deleted={sum(1 for document in plan.documents if document.deleted)}"
    )
    estimated = _estimate_requests(dataset)
    print(f"模型请求保守预留（成本上界，非实际计费次数，含改写与生成重试最坏情况）：{estimated}")

    asset_map = args.asset_map
    asset_mode = asset_map is not None
    if asset_mode:
        try:
            load_asset_map(asset_map, registry=registry)
        except DatasetRunError as error:
            print(f"准备失败：{error}", file=sys.stderr)
            return 1
        print("资产映射模式：跳过上传，仅核对 READY 状态")
    else:
        print("语料准备模式：按清单上传全部版本并逐版等待 READY")

    try:
        validate_static_configuration(
            dataset,
            plan,
            descriptor=descriptor,
            registry=registry,
            require_seed_write=not asset_mode,
        )
    except DatasetRunError as error:
        print(f"配置校验失败：{error}", file=sys.stderr)
        return 1

    if not args.allow_real_llm:
        print("dry-run：未开启 --allow-real-llm，不联网、不写库、不调用模型、不写结果文件。")
        return 0

    if args.max_model_requests <= 0:
        print("真实运行必须给出正的 --max-model-requests 硬上限。", file=sys.stderr)
        return 1
    if args.api_base_url is None:
        print("真实运行必须提供 --api-base-url。", file=sys.stderr)
        return 1
    if not _is_loopback_url(args.api_base_url) and not args.allow_non_loopback_api:
        print(
            "api-base-url 不是回环地址；如确为隔离栈，请显式加 --allow-non-loopback-api。",
            file=sys.stderr,
        )
        return 1
    if args.results_out is None:
        print("真实运行必须提供 --results-out。", file=sys.stderr)
        return 1
    database_url = _read_database_url(args.database_url_env, args.allow_database_name)
    if database_url is None:
        return 1

    return _run_real(
        args,
        dataset=dataset,
        manifest=manifest,
        plan=plan,
        corpus_dir=corpus_dir,
        descriptor=descriptor,
        registry=registry,
        database_url=database_url,
        asset_mode=asset_mode,
    )


def _read_database_url(env_name: str, allowed_name: str | None) -> str | None:
    database_url = os.environ.get(env_name)
    if not database_url:
        print(f"真实运行需要只读数据库 DSN（环境变量 {env_name}）。", file=sys.stderr)
        return None
    try:
        database_name = _database_name(database_url)
    except DatasetRunError as error:
        print(f"数据库 DSN 非法：{error}", file=sys.stderr)
        return None
    if not database_name.endswith(_DB_NAME_SUFFIX) and database_name != allowed_name:
        print(
            "数据库名不以 _test 结尾；如确认它不是开发库，请用 --allow-database-name "
            f"{database_name} 重申。",
            file=sys.stderr,
        )
        return None
    return database_url


def _database_name(database_url: str) -> str:
    from sqlalchemy.engine import make_url

    try:
        url = make_url(database_url)
    except ValueError as error:
        raise DatasetRunError("数据库 DSN 不是合法 URL") from error
    return url.database or ""


def _estimate_requests(dataset: EvaluationDataset) -> int:
    """按题数据估算保守预留额度：首问两次回答，后续每问两次回答 + 一次改写。"""

    total = 0
    for question in dataset.questions:
        replay = sum(1 for turn in question.history if turn.role == "user")
        asks = replay + 1
        total += ModelRequestBudget.BASE_COST  # 首问最多两次回答请求
        total += (ModelRequestBudget.BASE_COST + ModelRequestBudget.REWRITE_COST) * (asks - 1)
    return total


def _run_real(
    args: argparse.Namespace,
    *,
    dataset: EvaluationDataset,
    manifest: CorpusManifest,
    plan: SeedPlan,
    corpus_dir: Path,
    descriptor: EnvironmentDescriptor,
    registry: AssetRegistry,
    database_url: str,
    asset_mode: bool,
) -> int:
    from rag_backend.evaluation.runner_adapters import HttpBackend, SqlEvaluationDatabase

    run_id = uuid.uuid4().hex
    database = SqlEvaluationDatabase(database_url)
    backend: HttpBackend | None = None
    diagnostics: tuple[QuestionDiagnostic, ...] = ()
    try:
        backend = HttpBackend(
            base_url=args.api_base_url,
            descriptor=descriptor,
            timeout_seconds=args.http_timeout_seconds,
        )
        verify_environment(
            dataset,
            manifest,
            descriptor=descriptor,
            registry=registry,
            knowledge_base_roles=lambda role: backend.session_for(role).knowledge_base_roles(),
            require_seed_write=not asset_mode,
        )
        # 直接证据：只读数据库必须包含 API 目标的同一批语料 KB（同 host 不算证明）。
        kb_uuids = [registry.kb_uuid(kb_id) for kb_id in sorted(manifest.knowledge_bases)]
        if database.missing_knowledge_base_ids(kb_uuids):
            raise EnvironmentMismatch(
                "只读数据库不包含 API 目标的语料 KB；API 与数据库可能不是同一隔离栈"
            )
        if asset_mode:
            verify_ready(plan, readiness=database, registry=registry)
        else:
            seed_corpus(
                plan,
                corpus_dir=corpus_dir,
                uploader=backend.seed_session(),
                readiness=database,
                registry=registry,
                run_id=run_id,
                timeout_seconds=args.ready_timeout_seconds,
            )
        budget = ModelRequestBudget(args.max_model_requests)
        outcome = run_questions(
            dataset,
            registry=registry,
            backend=backend,
            citations=database,
            budget=budget,
        )
        diagnostics = outcome.diagnostics
        if not outcome.complete or outcome.results is None:
            print("运行不完整，未写出结果文件。", file=sys.stderr)
            _print_diagnostics(diagnostics)
            _write_diagnostics(args.diagnostics_out, diagnostics)
            return 1
        args.results_out.write_text(
            outcome.results.model_dump_json(by_alias=True, indent=2) + "\n", encoding="utf-8"
        )
        print(f"结果已写出：{args.results_out}（共 {len(outcome.results.results)} 题）")
        _write_diagnostics(args.diagnostics_out, diagnostics)
        return 0
    except RunnerError as error:
        print(f"运行失败：{_sanitize(str(error), descriptor)}", file=sys.stderr)
        _write_diagnostics(args.diagnostics_out, diagnostics)
        return 1
    finally:
        if backend is not None:
            backend.close()
        database.close()


def _print_diagnostics(diagnostics: Sequence[QuestionDiagnostic]) -> None:
    for diagnostic in diagnostics:
        line = f"{diagnostic.question_id}: {diagnostic.outcome}"
        if diagnostic.detail:
            line += f" ({diagnostic.detail})"
        print(line, file=sys.stderr)


def _write_diagnostics(
    path: Path | None, diagnostics: Sequence[QuestionDiagnostic]
) -> None:
    if path is None:
        return
    payload = {
        "diagnostics": [
            {"questionId": item.question_id, "outcome": item.outcome, "detail": item.detail}
            for item in diagnostics
        ]
    }
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        print(f"诊断文件写入失败：{error}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
