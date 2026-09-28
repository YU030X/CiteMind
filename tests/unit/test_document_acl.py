"""文档 ACL 的纯规则与进程内 HTTP 契约测试；不连数据库。

覆盖：``INHERIT`` 携带名单/重复用户被拒；``acl_read_clause`` 同时引用 ``acl_mode`` 与
``document_acl``；ACL 路由要求 OWNER、Origin 与 CSRF，且服务端领域错误映射为具名 422/404。
真实数据库上的权限、revision 与锁序由 ``tests/integration/test_document_acl_flow.py`` 承担。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from rag_backend.api.errors import (
    CODE_DOCUMENT_ACL_INVALID,
    CODE_DOCUMENT_ACL_MEMBER_INVALID,
    CODE_DOCUMENT_NOT_FOUND,
    CODE_ORIGIN_NOT_ALLOWED,
)
from rag_backend.app import create_app
from rag_backend.auth.context import AuthContext
from rag_backend.auth.dependencies import get_auth_context
from rag_backend.auth.tokens import CSRF_HEADER_NAME
from rag_backend.config import Settings
from rag_backend.database import get_database_session
from rag_backend.knowledge.document_acl import (
    DocumentAclDocumentNotFound,
    DocumentAclInvalid,
    DocumentAclMember,
    DocumentAclMemberInvalid,
    DocumentAclMode,
    DocumentAclView,
    acl_read_clause,
    validate_document_acl_request,
)
from rag_backend.knowledge.roles import KbRole
from rag_backend.knowledge.service import DocumentAccess

ORIGIN = "http://127.0.0.1"
USER_ID = uuid.uuid4()
ORG_ID = uuid.uuid4()
KB_ID = uuid.uuid4()
DOC_ID = uuid.uuid4()
CSRF_TOKEN = "unit-acl-csrf"


def _settings() -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "session_cookie_secure": False,
        "allowed_origins": ORIGIN,
        "csrf_secret": "unit-acl-secret",
    }
    return Settings(**values)


def _context() -> AuthContext:
    return AuthContext(
        user_id=USER_ID,
        organization_id=ORG_ID,
        username="owner",
        is_admin=False,
        session_id=uuid.uuid4(),
        csrf_token=CSRF_TOKEN,
    )


class FakeSession:
    def __init__(self) -> None:
        self.rollbacks = 0

    async def rollback(self) -> None:
        self.rollbacks += 1


def _app(monkeypatch: pytest.MonkeyPatch, service: Any) -> Any:
    app = create_app(_settings())
    app.dependency_overrides[get_auth_context] = _context
    app.dependency_overrides[get_database_session] = lambda: FakeSession()

    async def fake_resolve(*_args: Any, **_kwargs: Any) -> DocumentAccess:
        return DocumentAccess(
            document_id=DOC_ID, kb_id=KB_ID, organization_id=ORG_ID, role=KbRole.OWNER
        )

    monkeypatch.setattr(
        "rag_backend.auth.dependencies.resolve_document_access", fake_resolve
    )
    monkeypatch.setattr("rag_backend.api.documents.replace_document_acl", service)
    return app


def _client(app: Any) -> AsyncClient:
    transport = ASGITransport(app=app, client=("10.20.0.1", 12345))
    return AsyncClient(transport=transport, base_url=ORIGIN)


async def _put_acl(app: Any, payload: dict[str, Any], headers: dict[str, str]) -> Any:
    async with _client(app) as client:
        return await client.put(
            f"/api/v1/documents/{DOC_ID}/acl", json=payload, headers=headers
        )


# --- 纯规则 -------------------------------------------------------------------


def test_inherit_mode_with_members_is_invalid() -> None:
    with pytest.raises(DocumentAclInvalid):
        validate_document_acl_request(
            DocumentAclMode.INHERIT, [DocumentAclMember(user_id=uuid.uuid4())]
        )


def test_duplicate_members_are_invalid() -> None:
    member = DocumentAclMember(user_id=uuid.uuid4())
    with pytest.raises(DocumentAclInvalid):
        validate_document_acl_request(DocumentAclMode.RESTRICTED, [member, member])


def test_restricted_with_unique_members_is_valid() -> None:
    validate_document_acl_request(
        DocumentAclMode.RESTRICTED,
        [DocumentAclMember(user_id=uuid.uuid4()), DocumentAclMember(user_id=uuid.uuid4())],
    )


def test_acl_read_clause_references_mode_and_allow_list() -> None:
    clause = acl_read_clause(USER_ID)
    compiled = str(clause.compile(compile_kwargs={"literal_binds": True}))
    assert "acl_mode" in compiled
    assert "document_acl" in compiled
    assert "principal_type = 'USER'" in compiled
    assert "permission = 'READ'" in compiled


# --- HTTP 契约 -----------------------------------------------------------------


@pytest.mark.anyio
async def test_replace_acl_returns_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    member_id = uuid.uuid4()
    calls: dict[str, Any] = {}

    async def fake_replace(session: Any, **kwargs: Any) -> DocumentAclView:
        calls.update(kwargs)
        return DocumentAclView(
            document_id=DOC_ID,
            mode=DocumentAclMode.RESTRICTED,
            member_ids=(member_id,),
            acl_revision=3,
        )

    app = _app(monkeypatch, fake_replace)
    response = await _put_acl(
        app,
        {"mode": "RESTRICTED", "members": [{"userId": str(member_id)}]},
        {"Origin": ORIGIN, CSRF_HEADER_NAME: CSRF_TOKEN},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "documentId": str(DOC_ID),
        "mode": "RESTRICTED",
        "members": [{"userId": str(member_id)}],
        "aclRevision": 3,
    }
    assert calls["mode"] is DocumentAclMode.RESTRICTED
    assert calls["organization_id"] == ORG_ID
    assert calls["actor_user_id"] == USER_ID
    assert [member.user_id for member in calls["members"]] == [member_id]


@pytest.mark.anyio
async def test_replace_acl_requires_csrf(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_replace(*_args: Any, **_kwargs: Any) -> DocumentAclView:
        raise AssertionError("缺少 CSRF 时不应调用服务")

    app = _app(monkeypatch, fake_replace)
    response = await _put_acl(
        app, {"mode": "INHERIT", "members": []}, {"Origin": ORIGIN}
    )

    assert response.status_code == 403


@pytest.mark.anyio
async def test_replace_acl_rejects_foreign_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_replace(*_args: Any, **_kwargs: Any) -> DocumentAclView:
        raise AssertionError("非法 Origin 时不应调用服务")

    app = _app(monkeypatch, fake_replace)
    response = await _put_acl(
        app,
        {"mode": "INHERIT", "members": []},
        {"Origin": "http://evil.example", CSRF_HEADER_NAME: CSRF_TOKEN},
    )

    assert response.status_code == 403
    assert response.json()["code"] == CODE_ORIGIN_NOT_ALLOWED


@pytest.mark.anyio
async def test_replace_acl_maps_invalid_shape_to_422(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_replace(*_args: Any, **_kwargs: Any) -> DocumentAclView:
        raise DocumentAclInvalid("INHERIT 模式不能携带成员名单")

    app = _app(monkeypatch, fake_replace)
    response = await _put_acl(
        app,
        {"mode": "INHERIT", "members": [{"userId": str(uuid.uuid4())}]},
        {"Origin": ORIGIN, CSRF_HEADER_NAME: CSRF_TOKEN},
    )

    assert response.status_code == 422
    assert response.json()["code"] == CODE_DOCUMENT_ACL_INVALID


@pytest.mark.anyio
async def test_replace_acl_maps_invalid_member_to_422(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_replace(*_args: Any, **_kwargs: Any) -> DocumentAclView:
        raise DocumentAclMemberInvalid("成员必须是当前组织内该知识库的有效用户")

    app = _app(monkeypatch, fake_replace)
    response = await _put_acl(
        app,
        {"mode": "RESTRICTED", "members": [{"userId": str(uuid.uuid4())}]},
        {"Origin": ORIGIN, CSRF_HEADER_NAME: CSRF_TOKEN},
    )

    assert response.status_code == 422
    assert response.json()["code"] == CODE_DOCUMENT_ACL_MEMBER_INVALID


@pytest.mark.anyio
async def test_replace_acl_maps_missing_document_to_404(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_replace(*_args: Any, **_kwargs: Any) -> DocumentAclView:
        raise DocumentAclDocumentNotFound("文档不存在或无权访问")

    app = _app(monkeypatch, fake_replace)
    response = await _put_acl(
        app,
        {"mode": "INHERIT", "members": []},
        {"Origin": ORIGIN, CSRF_HEADER_NAME: CSRF_TOKEN},
    )

    assert response.status_code == 404
    assert response.json()["code"] == CODE_DOCUMENT_NOT_FOUND


@pytest.mark.anyio
async def test_replace_acl_denies_non_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(_settings())
    app.dependency_overrides[get_auth_context] = _context
    app.dependency_overrides[get_database_session] = lambda: FakeSession()

    async def deny(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr("rag_backend.auth.dependencies.resolve_document_access", deny)

    response = await _put_acl(
        app,
        {"mode": "INHERIT", "members": []},
        {"Origin": ORIGIN, CSRF_HEADER_NAME: CSRF_TOKEN},
    )

    assert response.status_code == 404
    assert response.json()["code"] == CODE_DOCUMENT_NOT_FOUND
