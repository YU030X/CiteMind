"""KB 角色与成员替换纯规则测试。"""

import uuid

import pytest
from rag_backend.knowledge.roles import (
    KbMembersInvalid,
    KbRole,
    LastOwnerRequired,
    MemberReplacement,
    kb_role_rank,
    validate_member_replacements,
)


def member(role: KbRole, *, user_id: uuid.UUID | None = None) -> MemberReplacement:
    return MemberReplacement(user_id=user_id or uuid.uuid4(), role=role)


def test_role_rank_orders_owner_above_editor_above_reader() -> None:
    assert kb_role_rank(KbRole.OWNER) > kb_role_rank(KbRole.EDITOR) > kb_role_rank(KbRole.READER)


def test_valid_replacement_requires_one_owner() -> None:
    validate_member_replacements(
        [member(KbRole.OWNER), member(KbRole.EDITOR), member(KbRole.READER)]
    )


def test_empty_replacement_is_rejected() -> None:
    with pytest.raises(KbMembersInvalid):
        validate_member_replacements([])


def test_duplicate_user_is_rejected() -> None:
    user_id = uuid.uuid4()
    with pytest.raises(KbMembersInvalid):
        validate_member_replacements(
            [
                MemberReplacement(user_id=user_id, role=KbRole.OWNER),
                MemberReplacement(user_id=user_id, role=KbRole.READER),
            ]
        )


def test_replacement_without_owner_is_rejected() -> None:
    with pytest.raises(LastOwnerRequired):
        validate_member_replacements([member(KbRole.EDITOR), member(KbRole.READER)])
