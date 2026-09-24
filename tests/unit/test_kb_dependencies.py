"""``require_kb_role`` 的最小角色判定与统一 404 测试。"""

import uuid
from typing import Any

import pytest
from rag_backend.api.errors import CODE_KNOWLEDGE_BASE_NOT_FOUND, ApiError
from rag_backend.auth.context import AuthContext
from rag_backend.auth.dependencies import require_kb_role
from rag_backend.knowledge.roles import KbRole
from rag_backend.knowledge.service import KbAccess


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_context() -> AuthContext:
    return AuthContext(
        user_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        username="alice",
        is_admin=False,
        session_id=uuid.uuid4(),
        csrf_token="csrf",
    )


def make_access(role: KbRole) -> KbAccess:
    return KbAccess(
        kb_id=uuid.uuid4(),
        name="kb",
        role=role,
        organization_id=uuid.uuid4(),
        acl_revision=1,
        kb_revision=1,
    )


def patch_resolver(
    monkeypatch: pytest.MonkeyPatch, resolved: KbAccess | None
) -> None:
    async def fake_resolve(
        session: Any,
        *,
        kb_id: uuid.UUID,
        user_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> KbAccess | None:
        return resolved

    monkeypatch.setattr(
        "rag_backend.auth.dependencies.resolve_knowledge_base_access", fake_resolve
    )


@pytest.mark.anyio
async def test_require_kb_role_returns_access_when_rank_sufficient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    access = make_access(KbRole.OWNER)
    patch_resolver(monkeypatch, access)
    session: Any = object()

    result = await require_kb_role(KbRole.EDITOR)(
        kb_id=access.kb_id, context=make_context(), session=session
    )

    assert result is access


@pytest.mark.anyio
async def test_require_kb_role_rejects_insufficient_role_with_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    access = make_access(KbRole.READER)
    patch_resolver(monkeypatch, access)

    with pytest.raises(ApiError) as error:
        await require_kb_role(KbRole.OWNER)(
            kb_id=access.kb_id, context=make_context(), session=object()
        )

    assert error.value.status_code == 404
    assert error.value.code == CODE_KNOWLEDGE_BASE_NOT_FOUND


@pytest.mark.anyio
async def test_require_kb_role_returns_404_when_no_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_resolver(monkeypatch, None)

    with pytest.raises(ApiError) as error:
        await require_kb_role(KbRole.READER)(
            kb_id=uuid.uuid4(), context=make_context(), session=object()
        )

    assert error.value.status_code == 404
    assert error.value.code == CODE_KNOWLEDGE_BASE_NOT_FOUND
