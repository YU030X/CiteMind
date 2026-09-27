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
    ModelCapability,
    ThinkingCapability,
    UserSummary,
    generation_capability,
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
        generation=generation_capability(False, "deepseek-flash"),
    )

    payload = response.model_dump(by_alias=True)

    assert set(payload) == {"user", "csrfToken", "generation"}
    assert payload["csrfToken"] == "csrf-token"
    assert payload["generation"] == {
        "enabled": False,
        "defaultModel": "deepseek-flash",
        "defaultThinking": "disabled",
        "models": [
            {
                "id": "deepseek-flash",
                "thinking": {
                    "supported": True,
                    "efforts": ["low", "high", "max"],
                    "defaultEffort": "high",
                },
            }
        ],
    }
    assert set(payload["user"]) == {"id", "username", "isAdmin", "organizationId"}
    assert payload["user"]["isAdmin"] is True


def test_generation_capability_only_exposes_verified_models() -> None:
    """官方存在但未经本仓库 tokenizer/渲染契约验证的模型不得出现在能力列表里。"""

    capability = generation_capability(True, "deepseek-flash")

    assert [model.id for model in capability.models] == ["deepseek-flash"]
    assert all(model.id != "deepseek-v4-pro" for model in capability.models)
    assert capability.default_model == "deepseek-flash"
    assert capability.default_thinking == "disabled"


def test_model_capability_rejects_unknown_thinking_effort() -> None:
    with pytest.raises(ValidationError):
        ModelCapability.model_validate(
            {
                "id": "deepseek-flash",
                "thinking": {
                    "supported": True,
                    "efforts": ["ultra"],
                    "defaultEffort": "high",
                },
            }
        )


def test_thinking_capability_requires_efforts() -> None:
    with pytest.raises(ValidationError):
        ThinkingCapability.model_validate({"supported": True})


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
        generation=generation_capability(True, "deepseek-flash"),
        knowledge_bases=[KbRoleSummary(id=kb_id, name="kb", role=KbRole.OWNER)],
    ).model_dump(mode="json", by_alias=True)

    assert set(payload) == {"user", "csrfToken", "generation", "knowledgeBases"}
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
