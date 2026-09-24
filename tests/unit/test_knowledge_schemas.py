"""知识库外部 schema 的 camelCase 与角色校验测试。"""

import uuid

import pytest
from pydantic import ValidationError
from rag_backend.knowledge.roles import KbRole
from rag_backend.schemas.knowledge import (
    KbMemberListResponse,
    KbMemberReplaceRequest,
    KbMemberSummary,
    KnowledgeBaseCreateRequest,
    KnowledgeBaseListResponse,
    KnowledgeBaseSummary,
)

KB_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def test_create_request_rejects_empty_and_overlong_name() -> None:
    with pytest.raises(ValidationError):
        KnowledgeBaseCreateRequest(name="")
    with pytest.raises(ValidationError):
        KnowledgeBaseCreateRequest(name="x" * 201)

    assert KnowledgeBaseCreateRequest(name="知识库").name == "知识库"


def test_knowledge_base_list_serialises_camel_case() -> None:
    payload = KnowledgeBaseListResponse(
        knowledge_bases=[
            KnowledgeBaseSummary(id=KB_ID, name="kb", role=KbRole.OWNER, acl_revision=2)
        ]
    ).model_dump(mode="json", by_alias=True)

    assert set(payload) == {"knowledgeBases"}
    assert payload["knowledgeBases"][0] == {
        "id": str(KB_ID),
        "name": "kb",
        "role": "OWNER",
        "aclRevision": 2,
    }


def test_member_replace_request_parses_camel_case_and_role() -> None:
    parsed = KbMemberReplaceRequest.model_validate(
        {"members": [{"userId": str(USER_ID), "role": "EDITOR"}]}
    )

    assert parsed.members[0].user_id == USER_ID
    assert parsed.members[0].role is KbRole.EDITOR


def test_member_replace_request_rejects_unknown_role() -> None:
    with pytest.raises(ValidationError):
        KbMemberReplaceRequest.model_validate(
            {"members": [{"userId": str(USER_ID), "role": "ADMIN"}]}
        )


def test_member_list_serialises_camel_case() -> None:
    payload = KbMemberListResponse(
        members=[KbMemberSummary(user_id=USER_ID, username="alice", role=KbRole.READER)],
        acl_revision=5,
    ).model_dump(mode="json", by_alias=True)

    assert set(payload) == {"members", "aclRevision"}
    assert payload["members"][0] == {
        "userId": str(USER_ID),
        "username": "alice",
        "role": "READER",
    }
    assert payload["aclRevision"] == 5
