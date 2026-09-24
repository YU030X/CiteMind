"""外部 JSON schema 的 camelCase 契约测试。"""

import uuid

import pytest
from pydantic import ValidationError
from rag_backend.auth.accounts import MAX_USERNAME_LENGTH
from rag_backend.knowledge.roles import KbRole
from rag_backend.schemas.auth import (
    KbRoleSummary,
    LoginRequest,
    MeOverviewResponse,
    MeResponse,
    UserSummary,
)
from rag_backend.schemas.errors import ErrorResponse

ORGANIZATION_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def test_login_request_requires_username_and_password() -> None:
    parsed = LoginRequest(username="alice", password="secret")

    assert parsed.username == "alice"
    assert parsed.password == "secret"

    with pytest.raises(ValidationError):
        LoginRequest(username="", password="secret")


def test_login_request_username_length_matches_account_creation_contract() -> None:
    max_length_username = "u" * MAX_USERNAME_LENGTH

    assert LoginRequest(username=max_length_username, password="secret").username == (
        max_length_username
    )
    with pytest.raises(ValidationError):
        LoginRequest(username="u" * (MAX_USERNAME_LENGTH + 1), password="secret")


def test_me_response_serialises_camel_case_fields() -> None:
    response = MeResponse(
        user=UserSummary(
            id=uuid.uuid4(),
            username="alice",
            is_admin=True,
            organization_id=ORGANIZATION_ID,
        ),
        csrf_token="csrf-token",
    )

    payload = response.model_dump(by_alias=True)

    assert set(payload) == {"user", "csrfToken"}
    assert payload["csrfToken"] == "csrf-token"
    assert set(payload["user"]) == {"id", "username", "isAdmin", "organizationId"}
    assert payload["user"]["isAdmin"] is True


def test_me_overview_includes_knowledge_base_roles() -> None:
    kb_id = uuid.uuid4()
    payload = MeOverviewResponse(
        user=UserSummary(
            id=uuid.uuid4(),
            username="alice",
            is_admin=False,
            organization_id=ORGANIZATION_ID,
        ),
        csrf_token="csrf-token",
        knowledge_bases=[KbRoleSummary(id=kb_id, name="kb", role=KbRole.OWNER)],
    ).model_dump(mode="json", by_alias=True)

    assert set(payload) == {"user", "csrfToken", "knowledgeBases"}
    assert payload["knowledgeBases"] == [
        {"id": str(kb_id), "name": "kb", "role": "OWNER"}
    ]


def test_error_response_has_stable_camel_case_shape() -> None:
    payload = ErrorResponse(
        code="AUTH_REQUIRED",
        message="需要登录",
        request_id="req-1",
        details={"retryAfter": 3},
    ).model_dump(by_alias=True)

    assert payload == {
        "code": "AUTH_REQUIRED",
        "message": "需要登录",
        "requestId": "req-1",
        "details": {"retryAfter": 3},
    }
