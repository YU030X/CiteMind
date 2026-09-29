"""开发评估 runner 的真实适配器：HTTP API 与只读 PostgreSQL。

- ``HttpBackend`` 用 ``httpx`` 走真实 HTTP（可注入 ``transport`` 供合成测试），按角色维护独立会话
  并完成登录/CSRF；上传/删除用于语料准备，会话用于答题。
- ``SqlEvaluationDatabase`` 只用 ``SELECT``：把引用 UUID 映射到 ``document_version``，并轮询文档
  READY/删除状态。它不写库、不建表、不做迁移。

错误消息只带 HTTP 状态码与业务 ``code``，不回显响应正文、DSN、文件名或凭据。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from pydantic import ValidationError
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError

from rag_backend.api.errors import (
    CODE_KNOWLEDGE_BASE_NOT_FOUND,
)
from rag_backend.auth.tokens import CSRF_HEADER_NAME
from rag_backend.evaluation import runner as core
from rag_backend.evaluation.runner import (
    AskOutcome,
    BackendError,
    ConversationDenied,
    EnvironmentDescriptor,
    RoleCredential,
    SeedError,
    UploadReceipt,
)


def _origin_of(base_url: str) -> str:
    url = httpx.URL(base_url)
    if not url.scheme or not url.host:
        raise core.RunnerError("api-base-url 必须是带 scheme 与 host 的绝对地址")
    port = "" if url.port is None else f":{url.port}"
    return f"{url.scheme}://{url.host}{port}"


def _coerce_uuid(value: Any) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def _required_uuid(payload: Mapping[str, Any], key: str, message: str) -> uuid.UUID:
    """严格解析必填 UUID 字段；缺失或非法一律抛 ``BackendError``。"""

    if key not in payload:
        raise BackendError(message)
    try:
        return _coerce_uuid(payload[key])
    except (ValueError, TypeError, AttributeError) as error:
        raise BackendError(message) from error


def _error_code(response: httpx.Response) -> str | None:
    """只读取错误体的业务 ``code``；不读取/记录消息与正文。"""

    try:
        payload = response.json()
    except ValueError:
        return None
    if isinstance(payload, dict):
        code = payload.get("code")
        if isinstance(code, str):
            return code
    return None


class HttpRoleSession:
    """一个真实角色的 HTTP 会话：登录一次，写操作带 CSRF，删除/上传与会话共用连接。"""

    def __init__(self, client: httpx.Client, *, origin: str) -> None:
        self._client = client
        self._origin = origin
        self._csrf: str | None = None

    def close(self) -> None:
        self._client.close()

    def login(self, credential: RoleCredential) -> None:
        response = self._send(
            lambda: self._client.post(
                "/api/v1/auth/login",
                json={
                    "username": credential.username,
                    "password": credential.password.get_secret_value(),
                },
                headers={"Origin": self._origin},
            ),
            action="登录",
        )
        self._require(response, 200, "登录")
        payload = _json_object(response)
        csrf = payload.get("csrfToken")
        if not isinstance(csrf, str) or not csrf:
            raise BackendError("登录响应缺少 csrfToken")
        self._csrf = csrf

    def knowledge_base_roles(self) -> Mapping[uuid.UUID, str]:
        """读取 ``GET /me`` 的 KB 角色；保留角色名供准备权限校验。"""

        response = self._send(
            lambda: self._client.get("/api/v1/me", headers=self._headers(write=False)),
            action="读取 /me",
        )
        self._require(response, 200, "读取 /me")
        payload = _json_object(response)
        entries = payload.get("knowledgeBases")
        if not isinstance(entries, list):
            raise BackendError("读取 /me 响应缺少 knowledgeBases")
        result: dict[uuid.UUID, str] = {}
        for entry in entries:
            if not isinstance(entry, dict) or "id" not in entry or "role" not in entry:
                raise BackendError("读取 /me 响应结构非法")
            role = entry["role"]
            if not isinstance(role, str):
                raise BackendError("读取 /me 响应结构非法")
            result[_coerce_uuid(entry["id"])] = role
        return result

    def existing_document_ids(self, *, kb_uuid: uuid.UUID) -> Sequence[uuid.UUID]:
        """列出 KB 内**未删除**文档 id；用于上传前确认专用隔离 KB 为空。"""

        response = self._send(
            lambda: self._client.get(
                f"/api/v1/knowledge-bases/{kb_uuid}/documents",
                headers=self._headers(write=False),
            ),
            action="列出 KB 文档",
        )
        self._require(response, 200, "列出 KB 文档")
        payload = _json_object(response)
        documents = payload.get("documents")
        if not isinstance(documents, list):
            raise BackendError("列出 KB 文档响应缺少 documents")
        result: list[uuid.UUID] = []
        for document in documents:
            if not isinstance(document, dict) or "id" not in document:
                raise BackendError("文档列表结构非法")
            result.append(_coerce_uuid(document["id"]))
        return result

    def create_conversation(self, kb_ids: Sequence[uuid.UUID]) -> uuid.UUID:
        response = self._send(
            lambda: self._client.post(
                "/api/v1/conversations",
                json={"kbIds": [str(kb_id) for kb_id in kb_ids]},
                headers=self._headers(write=True),
            ),
            action="创建会话",
        )
        if response.status_code == 404 and _error_code(response) == CODE_KNOWLEDGE_BASE_NOT_FOUND:
            raise ConversationDenied("目标 KB 对当前角色不可访问")
        self._require(response, 201, "创建会话")
        payload = _json_object(response)
        return _coerce_uuid(payload["conversationId"])

    def ask(self, conversation_id: uuid.UUID, question: str) -> AskOutcome:
        response = self._send(
            lambda: self._client.post(
                f"/api/v1/conversations/{conversation_id}/messages",
                json={"question": question},
                headers=self._headers(write=True),
            ),
            action="提问",
        )
        self._require(response, 200, "提问")
        payload = _json_object(response)
        query_run_id = _required_uuid(payload, "queryRunId", "提问响应缺少或非法 queryRunId")
        refused = payload.get("insufficientEvidence")
        if not isinstance(refused, bool):
            raise BackendError("提问响应缺少 insufficientEvidence")
        citations = payload.get("citations")
        if not isinstance(citations, list):
            raise BackendError("提问响应缺少 citations")
        citation_ids: list[uuid.UUID] = []
        for citation in citations:
            if not isinstance(citation, dict) or "citationId" not in citation:
                raise BackendError("引用结构非法")
            citation_ids.append(_coerce_uuid(citation["citationId"]))
        answer = payload.get("answer")
        if not isinstance(answer, str):
            raise BackendError("提问响应缺少 answer")
        return AskOutcome(
            query_run_id=query_run_id,
            refused=refused,
            citation_ids=tuple(citation_ids),
            answer_text=answer,
        )

    # --- 语料准备 ---------------------------------------------------------

    def upload_new_document(
        self,
        *,
        kb_uuid: uuid.UUID,
        title: str,
        filename: str,
        content: bytes,
        idempotency_key: str,
    ) -> UploadReceipt:
        response = self._send(
            lambda: self._client.post(
                f"/api/v1/knowledge-bases/{kb_uuid}/documents",
                files={"file": (filename, content)},
                data={"title": title},
                headers=self._upload_headers(idempotency_key),
            ),
            action="上传文档",
        )
        return self._receipt(response)

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
        response = self._send(
            lambda: self._client.post(
                f"/api/v1/documents/{document_uuid}/versions",
                files={"file": (filename, content)},
                data={"title": title, "expectedVersionId": str(expected_version_uuid)},
                headers=self._upload_headers(idempotency_key),
            ),
            action="上传新版本",
        )
        return self._receipt(response)

    def delete_document(self, *, document_uuid: uuid.UUID) -> None:
        response = self._send(
            lambda: self._client.delete(
                f"/api/v1/documents/{document_uuid}",
                headers=self._headers(write=True),
            ),
            action="删除文档",
        )
        if response.status_code != 204:
            raise SeedError(f"删除文档失败（{_status_detail(response)}）")

    # --- 内部 -------------------------------------------------------------

    def _headers(self, *, write: bool) -> dict[str, str]:
        headers = {"Origin": self._origin}
        if write:
            if self._csrf is None:
                raise BackendError("写操作前必须先登录")
            headers[CSRF_HEADER_NAME] = self._csrf
        return headers

    def _upload_headers(self, idempotency_key: str) -> dict[str, str]:
        headers = self._headers(write=True)
        headers["Idempotency-Key"] = idempotency_key
        return headers

    def _receipt(self, response: httpx.Response) -> UploadReceipt:
        if response.status_code != 202:
            raise SeedError(f"上传失败（{_status_detail(response)}）")
        payload = _json_object(response)
        return UploadReceipt(
            document_uuid=_coerce_uuid(payload["documentId"]),
            version_uuid=_coerce_uuid(payload["versionId"]),
        )

    def _send(
        self, request: Callable[[], httpx.Response], *, action: str
    ) -> httpx.Response:
        try:
            return request()
        except httpx.TimeoutException as error:
            raise BackendError(f"{action}超时") from error
        except httpx.TransportError as error:
            raise BackendError(f"{action}传输失败") from error

    def _require(self, response: httpx.Response, expected: int, action: str) -> None:
        if response.status_code != expected:
            raise BackendError(f"{action}失败（{_status_detail(response)}）")


def _status_detail(response: httpx.Response) -> str:
    code = _error_code(response)
    if code:
        return f"HTTP {response.status_code}/{code}"
    return f"HTTP {response.status_code}"


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as error:
        raise BackendError("响应不是合法 JSON") from error
    if not isinstance(payload, dict):
        raise BackendError("响应结构非法")
    return payload


class HttpBackend:
    """按角色惰性登录的 HTTP 后端；一个角色一个连接。"""

    def __init__(
        self,
        *,
        base_url: str,
        descriptor: EnvironmentDescriptor,
        timeout_seconds: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._origin = _origin_of(base_url)
        self._descriptor = descriptor
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._sessions: dict[str, HttpRoleSession] = {}

    def session_for(self, role: str) -> HttpRoleSession:
        return self._session(role)

    def seed_session(self) -> HttpRoleSession:
        return self._session(self._descriptor.seed_role)

    def close(self) -> None:
        for session in self._sessions.values():
            session.close()
        self._sessions.clear()

    def _session(self, role: str) -> HttpRoleSession:
        session = self._sessions.get(role)
        if session is not None:
            return session
        credential = self._descriptor.credential(role)
        client = httpx.Client(
            base_url=self._base_url,
            timeout=self._timeout_seconds,
            transport=self._transport,
            follow_redirects=False,
        )
        session = HttpRoleSession(client, origin=self._origin)
        try:
            session.login(credential)
        except Exception:
            session.close()
            raise
        self._sessions[role] = session
        return session


@dataclass(frozen=True)
class UsageAttemptRow:
    """``llm_usage`` 中的一条 provider attempt 事实（只读，含 queryRunId 与账本列）。"""

    usage_id: uuid.UUID
    query_run_id: uuid.UUID
    created_at: datetime
    stage: str
    status: str
    provider: str
    model: str
    attempt: int
    error_code: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    prompt_cache_hit_tokens: int | None
    prompt_cache_miss_tokens: int | None
    latency_ms: int | None


@dataclass(frozen=True)
class QueryRunRewriteRow:
    """``query_run`` 中一条已捕获 run 的权威改写文本（只读三列）。"""

    query_run_id: uuid.UUID
    question: str
    standalone_question: str


class SqlEvaluationDatabase:
    """只读评估数据库：引用 UUID -> 版本 UUID、账本行与 READY/删除状态轮询。"""

    _CITATION_QUERY = text(
        "SELECT id, version_id FROM citation WHERE id IN :ids"
    ).bindparams(bindparam("ids", expanding=True))
    _DOCUMENT_QUERY = text(
        "SELECT id, deleted_at, active_version_id FROM document WHERE id = :id"
    )
    _KB_QUERY = text("SELECT id FROM knowledge_base WHERE id IN :ids").bindparams(
        bindparam("ids", expanding=True)
    )
    # 需要已部署迁移 20260929_0016（``llm_usage.query_run_id``）；只 SELECT，复用同一 engine。
    _USAGE_QUERY = text(
        """
        SELECT id, query_run_id, provider, created_at, stage, status, model, attempt, error_code,
               prompt_tokens, completion_tokens, prompt_cache_hit_tokens,
               prompt_cache_miss_tokens, latency_ms
        FROM llm_usage
        WHERE query_run_id IN :ids
        ORDER BY query_run_id, stage, created_at, id
        """
    ).bindparams(bindparam("ids", expanding=True))
    # 追问改写观测：按已捕获 queryRunId 只读回读权威文本；只 SELECT，复用同一 engine。
    _REWRITE_QUERY = text(
        "SELECT id, question, standalone_question FROM query_run WHERE id IN :ids"
    ).bindparams(bindparam("ids", expanding=True))

    def __init__(self, database_url: str) -> None:
        try:
            url = make_url(database_url)
        except (ValueError, ValidationError) as error:
            raise core.DatasetRunError("数据库 DSN 不是合法 URL") from error
        if url.drivername != "postgresql+psycopg":
            raise core.DatasetRunError("数据库 DSN 必须使用 postgresql+psycopg 驱动")
        self._engine: Engine = create_engine(database_url, pool_pre_ping=True)

    def close(self) -> None:
        self._engine.dispose()

    def version_ids_for(
        self, citation_ids: Sequence[uuid.UUID]
    ) -> Mapping[uuid.UUID, uuid.UUID]:
        if not citation_ids:
            return {}
        with self._engine.connect() as connection:
            rows = connection.execute(
                self._CITATION_QUERY, {"ids": list(citation_ids)}
            ).all()
        return {_coerce_uuid(row[0]): _coerce_uuid(row[1]) for row in rows}

    def usage_attempts_for(
        self, query_run_ids: Sequence[uuid.UUID]
    ) -> Sequence[UsageAttemptRow]:
        """按调用前关联键只读返回账本行；空输入不查询。需要已部署迁移 ``20260929_0016``。"""

        if not query_run_ids:
            return ()
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(
                    self._USAGE_QUERY, {"ids": list(query_run_ids)}
                ).all()
        except SQLAlchemyError as error:
            raise core.RunnerError("usage 账本只读查询失败") from error
        return tuple(
            UsageAttemptRow(
                usage_id=_coerce_uuid(row[0]),
                query_run_id=_coerce_uuid(row[1]),
                created_at=row[3],
                stage=row[4],
                status=row[5],
                provider=row[2],
                model=row[6],
                attempt=row[7],
                error_code=row[8],
                prompt_tokens=row[9],
                completion_tokens=row[10],
                prompt_cache_hit_tokens=row[11],
                prompt_cache_miss_tokens=row[12],
                latency_ms=row[13],
            )
            for row in rows
        )

    def rewrite_rows_for(
        self, query_run_ids: Sequence[uuid.UUID]
    ) -> Sequence[QueryRunRewriteRow]:
        """按已捕获 ``queryRunId`` 只读返回 ``query_run`` 权威改写文本；空输入不查询。"""

        if not query_run_ids:
            return ()
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(
                    self._REWRITE_QUERY, {"ids": list(query_run_ids)}
                ).all()
        except SQLAlchemyError as error:
            raise core.RunnerError("query_run 追问改写只读查询失败") from error
        return tuple(
            QueryRunRewriteRow(
                query_run_id=_coerce_uuid(row[0]),
                question=row[1],
                standalone_question=row[2],
            )
            for row in rows
        )

    def missing_knowledge_base_ids(self, kb_ids: Sequence[uuid.UUID]) -> frozenset[uuid.UUID]:
        """返回只读数据库中不存在的 KB UUID；用于确认 API 与数据库同栈。"""

        if not kb_ids:
            return frozenset()
        with self._engine.connect() as connection:
            rows = connection.execute(self._KB_QUERY, {"ids": list(kb_ids)}).all()
        present = {_coerce_uuid(row[0]) for row in rows}
        return frozenset(kb_ids) - present

    def wait_active(
        self, *, document_uuid: uuid.UUID, version_uuid: uuid.UUID, timeout_seconds: float
    ) -> None:
        if not core.poll_until(
            lambda: self._is_active(document_uuid, version_uuid),
            timeout_seconds=timeout_seconds,
        ):
            raise SeedError(f"等待文档 READY 超时：{document_uuid}")

    def wait_deleted(self, *, document_uuid: uuid.UUID, timeout_seconds: float) -> None:
        if not core.poll_until(
            lambda: self._is_deleted(document_uuid),
            timeout_seconds=timeout_seconds,
        ):
            raise SeedError(f"等待文档删除超时：{document_uuid}")

    def _document_row(self, document_uuid: uuid.UUID) -> Any:
        with self._engine.connect() as connection:
            return connection.execute(
                self._DOCUMENT_QUERY, {"id": document_uuid}
            ).first()

    def _is_active(self, document_uuid: uuid.UUID, version_uuid: uuid.UUID) -> bool:
        row = self._document_row(document_uuid)
        if row is None:
            return False
        active = row[2]
        if active is None:
            return False
        return row[1] is None and _coerce_uuid(active) == version_uuid

    def _is_deleted(self, document_uuid: uuid.UUID) -> bool:
        row = self._document_row(document_uuid)
        return row is not None and row[1] is not None


__all__ = [
    "HttpBackend",
    "HttpRoleSession",
    "QueryRunRewriteRow",
    "SqlEvaluationDatabase",
    "UsageAttemptRow",
]
