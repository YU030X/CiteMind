"""KB 角色定义与纯校验规则。

角色只来自服务端数据库的 ``kb_member``；客户端提交的角色声明只是期望值，必须由
服务端在事务内核对。这里只放不依赖数据库的规则，便于单测与在路由层统一映射错误。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class KbRole(StrEnum):
    """KB 成员角色；排名用于 ``require_kb_role`` 的最小角色判定。"""

    OWNER = "OWNER"
    EDITOR = "EDITOR"
    READER = "READER"


KB_ROLE_RANK: dict[KbRole, int] = {
    KbRole.READER: 1,
    KbRole.EDITOR: 2,
    KbRole.OWNER: 3,
}


def kb_role_rank(role: KbRole) -> int:
    """角色数值化；未知角色按最低权限处理，调用方不应依赖它放行。"""

    return KB_ROLE_RANK.get(role, 0)


@dataclass(frozen=True)
class MemberReplacement:
    """经过解析的一次成员替换条目。"""

    user_id: uuid.UUID
    role: KbRole


class KnowledgeBaseError(Exception):
    """KB 用例的领域错误基类；路由层负责映射到具名 HTTP 错误。"""


class KbMembersInvalid(KnowledgeBaseError):
    """成员替换请求为空或包含重复用户。"""


class KbMemberUserInvalid(KnowledgeBaseError):
    """请求引用了不存在或不属于当前组织的用户。"""


class KbMemberOwnerDisabled(KnowledgeBaseError):
    """试图新加入或提升一名已禁用用户为 OWNER。"""


class LastOwnerRequired(KnowledgeBaseError):
    """替换后没有任何有效 OWNER。"""


class KnowledgeBaseNotFound(KnowledgeBaseError):
    """KB 不存在或不属于当前组织（不向客户端区分）。"""


def validate_member_replacements(members: Sequence[MemberReplacement]) -> None:
    """校验全量替换请求本身；空、重复用户或缺少 OWNER 时抛领域错误。

    结果集恰好等于请求集，因此“至少一名有效 OWNER”只需在请求内判定；该函数不接触
    数据库，调用方应在加锁与写入前先执行它，保证失败时不会产生部分事务。
    """

    if not members:
        raise KbMembersInvalid("成员列表不能为空")
    seen: set[uuid.UUID] = set()
    for member in members:
        if member.user_id in seen:
            raise KbMembersInvalid("成员列表包含重复用户")
        seen.add(member.user_id)
    if not any(member.role is KbRole.OWNER for member in members):
        raise LastOwnerRequired("必须至少保留一名 OWNER")
