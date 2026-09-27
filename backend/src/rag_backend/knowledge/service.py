"""KB 成员用例：读取可访问 KB、创建 KB、读取与全量替换成员。

授权事实全部来自数据库：列表按 ``kb_member`` 有效成员与 ``knowledge_base`` 组织 join，
创建与替换在应用事务内完成，替换时对 ``knowledge_base`` 行加 ``FOR UPDATE`` 锁序列化
并发。本模块不缓存任何结果，也不接受客户端提交的 organization_id。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rag_backend.knowledge.roles import (
    KbMemberOwnerDisabled,
    KbMemberUserInvalid,
    KbRole,
    KnowledgeBaseNotFound,
    LastOwnerRequired,
    MemberReplacement,
    validate_member_replacements,
)
from rag_backend.models.identity import KbMember, UserAccount
from rag_backend.models.knowledge import Document, KnowledgeBase


@dataclass(frozen=True)
class KbAccess:
    """当前用户在某个 KB 上的有效角色快照，仅在本请求内有效。"""

    kb_id: uuid.UUID
    name: str
    role: KbRole
    organization_id: uuid.UUID
    acl_revision: int
    kb_revision: int


@dataclass(frozen=True)
class ValidMember:
    """KB 的一名有效成员；``revoked_at`` 为空的行不返回。"""

    user_id: uuid.UUID
    username: str
    role: KbRole


@dataclass(frozen=True)
class DocumentAccess:
    """当前用户在某个文档上的有效 KB 角色快照，仅在本请求内有效。"""

    document_id: uuid.UUID
    kb_id: uuid.UUID
    organization_id: uuid.UUID
    role: KbRole


async def resolve_document_access(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> DocumentAccess | None:
    """把文档解析到其 KB 的有效成员角色；不存在、跨组织、已撤销都返回 None。

    与 ``resolve_knowledge_base_access`` 一样不缓存；它**不过滤** ``deleted_at``，使已删除
    文档仍可由有权限的调用方得到确定状态（更新返回冲突、删除返回幂等），而不暴露给无权限者。
    """

    statement = (
        select(
            Document.id,
            Document.kb_id,
            KnowledgeBase.organization_id,
            KbMember.role,
        )
        .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
        .join(KbMember, KbMember.kb_id == Document.kb_id)
        .where(
            Document.id == document_id,
            KnowledgeBase.organization_id == organization_id,
            KbMember.user_id == user_id,
            KbMember.revoked_at.is_(None),
        )
    )
    row = (await session.execute(statement)).first()
    if row is None:
        return None
    return DocumentAccess(
        document_id=row[0],
        kb_id=row[1],
        organization_id=row[2],
        role=KbRole(row[3]),
    )


async def list_accessible_knowledge_bases(
    session: AsyncSession, *, user_id: uuid.UUID, organization_id: uuid.UUID
) -> list[KbAccess]:
    """列出当前用户在同组织内拥有有效成员关系的 KB。"""

    statement = (
        select(
            KnowledgeBase.id,
            KnowledgeBase.name,
            KnowledgeBase.acl_revision,
            KnowledgeBase.kb_revision,
            KbMember.role,
        )
        .join(KbMember, KbMember.kb_id == KnowledgeBase.id)
        .where(
            KbMember.user_id == user_id,
            KbMember.revoked_at.is_(None),
            KnowledgeBase.organization_id == organization_id,
        )
        .order_by(KnowledgeBase.created_at, KnowledgeBase.id)
    )
    rows = (await session.execute(statement)).all()
    return [
        KbAccess(
            kb_id=kb_id,
            name=name,
            role=KbRole(role),
            organization_id=organization_id,
            acl_revision=acl_revision,
            kb_revision=kb_revision,
        )
        for kb_id, name, acl_revision, kb_revision, role in rows
    ]


async def resolve_knowledge_base_access(
    session: AsyncSession,
    *,
    kb_id: uuid.UUID,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> KbAccess | None:
    """返回当前用户在该 KB 的有效访问；不存在、跨组织、已撤销或无成员关系都返回 None。

    调用方必须把 None 与角色不足统一处理为不暴露存在性的 404。
    """

    statement = (
        select(
            KnowledgeBase.id,
            KnowledgeBase.name,
            KnowledgeBase.acl_revision,
            KnowledgeBase.kb_revision,
            KnowledgeBase.organization_id,
            KbMember.role,
        )
        .join(KbMember, KbMember.kb_id == KnowledgeBase.id)
        .where(
            KnowledgeBase.id == kb_id,
            KnowledgeBase.organization_id == organization_id,
            KbMember.user_id == user_id,
            KbMember.revoked_at.is_(None),
        )
    )
    row = (await session.execute(statement)).first()
    if row is None:
        return None
    access_kb_id, name, acl_revision, kb_revision, access_org_id, role = row
    return KbAccess(
        kb_id=access_kb_id,
        name=name,
        role=KbRole(role),
        organization_id=access_org_id,
        acl_revision=acl_revision,
        kb_revision=kb_revision,
    )


async def create_knowledge_base(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    name: str,
    owner_user_id: uuid.UUID,
) -> KbAccess:
    """在单个事务内创建 KB 与创建者的 OWNER 成员行。

    ``organization_id`` 只来自服务端授权上下文；创建者必须在同组织且已启用，
    失败时回滚，绝不留下只有其一的部分事务。
    """

    owner_row = (
        await session.execute(
            select(UserAccount.organization_id, UserAccount.enabled).where(
                UserAccount.id == owner_user_id
            )
        )
    ).first()
    if owner_row is None:
        raise KbMemberUserInvalid("创建者必须是启用中的同组织用户")
    owner_organization_id, owner_enabled = owner_row
    if owner_organization_id != organization_id or not owner_enabled:
        raise KbMemberUserInvalid("创建者必须是启用中的同组织用户")

    kb_id = uuid.uuid4()
    knowledge_base = KnowledgeBase(id=kb_id, organization_id=organization_id, name=name)
    member = KbMember(
        id=uuid.uuid4(),
        kb_id=kb_id,
        user_id=owner_user_id,
        role=KbRole.OWNER.value,
    )
    try:
        # 无 ORM 关系时插入顺序不保证；先 flush KB，确保 kb_member 外键已经可见。
        session.add(knowledge_base)
        await session.flush()
        session.add(member)
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return KbAccess(
        kb_id=kb_id,
        name=name,
        role=KbRole.OWNER,
        organization_id=organization_id,
        acl_revision=0,
        kb_revision=0,
    )


async def list_valid_members(
    session: AsyncSession, *, kb_id: uuid.UUID, organization_id: uuid.UUID
) -> list[ValidMember]:
    """列出 KB 的有效成员，只返回同组织用户，用于响应与替换后回读。"""

    statement = (
        select(KbMember.user_id, UserAccount.username, KbMember.role)
        .join(UserAccount, UserAccount.id == KbMember.user_id)
        .where(
            KbMember.kb_id == kb_id,
            KbMember.revoked_at.is_(None),
            UserAccount.organization_id == organization_id,
        )
        .order_by(UserAccount.username, KbMember.user_id)
    )
    rows = (await session.execute(statement)).all()
    return [
        ValidMember(user_id=user_id, username=username, role=KbRole(role))
        for user_id, username, role in rows
    ]


async def replace_knowledge_base_members(
    session: AsyncSession,
    *,
    kb_id: uuid.UUID,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    members: Sequence[MemberReplacement],
) -> tuple[list[ValidMember], int]:
    """全量替换 KB 成员，返回替换后的有效成员与 ``acl_revision``。

    事务内先锁定 ``knowledge_base`` 行使并发替换串行化，并在锁内复核调用者仍是该 KB
    的有效 OWNER（避免角色依赖校验与写入之间的 TOCTOU）；随后按当前有效成员计算差量：
    缺席者软撤销、重加入者清空 ``revoked_at`` 并更新角色；只有实际变化才递增
    ``acl_revision``。替换结果必须至少保留一名**启用中**的 OWNER：新加入或提升一名
    已禁用用户为 OWNER 会被拒绝，保留既有已禁用 OWNER 但不搭配任何启用 OWNER 也会被
    拒绝。空、重复、缺少 OWNER 或跨组织用户在写入前被拒绝并回滚，因此不会产生部分事务；
    响应中的成员快照与 ``acl_revision`` 在同一事务、同一行锁内读出，二者一致。
    """

    try:
        validate_member_replacements(members)
        knowledge_base = (
            await session.execute(
                select(KnowledgeBase).where(KnowledgeBase.id == kb_id).with_for_update()
            )
        ).scalar_one_or_none()
        if knowledge_base is None or knowledge_base.organization_id != organization_id:
            raise KnowledgeBaseNotFound("知识库不存在")

        actor_role = (
            await session.execute(
                select(KbMember.role).where(
                    KbMember.kb_id == kb_id,
                    KbMember.user_id == actor_user_id,
                    KbMember.revoked_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if actor_role != KbRole.OWNER.value:
            raise KnowledgeBaseNotFound("知识库不存在")

        current_members = (
            (await session.execute(select(KbMember).where(KbMember.kb_id == kb_id)))
            .scalars()
            .all()
        )
        requested_ids = {member.user_id for member in members}
        current_valid_owner_ids = {
            current.user_id
            for current in current_members
            if current.revoked_at is None and current.role == KbRole.OWNER.value
        }

        user_rows = (
            await session.execute(
                select(
                    UserAccount.id,
                    UserAccount.organization_id,
                    UserAccount.enabled,
                ).where(UserAccount.id.in_(requested_ids))
            )
        ).all()
        organization_by_user = {
            user_id: (org_id, enabled) for user_id, org_id, enabled in user_rows
        }
        for member in members:
            record = organization_by_user.get(member.user_id)
            if record is None or record[0] != organization_id:
                raise KbMemberUserInvalid("成员必须是当前组织的用户")
            if (
                member.role is KbRole.OWNER
                and not record[1]
                and member.user_id not in current_valid_owner_ids
            ):
                raise KbMemberOwnerDisabled("不得新加入或提升已禁用用户为 OWNER")
        if not any(
            member.role is KbRole.OWNER and organization_by_user[member.user_id][1]
            for member in members
        ):
            raise LastOwnerRequired("必须至少保留一名启用中的 OWNER")

        changed = False
        current_by_user = {member.user_id: member for member in current_members}
        for member in members:
            existing = current_by_user.get(member.user_id)
            if existing is None:
                session.add(
                    KbMember(
                        id=uuid.uuid4(),
                        kb_id=kb_id,
                        user_id=member.user_id,
                        role=member.role.value,
                    )
                )
                changed = True
            elif existing.revoked_at is not None or existing.role != member.role.value:
                existing.revoked_at = None
                existing.role = member.role.value
                changed = True

        revoked_at = datetime.now(UTC)
        for current in current_members:
            if current.revoked_at is None and current.user_id not in requested_ids:
                current.revoked_at = revoked_at
                changed = True

        if changed:
            knowledge_base.acl_revision = knowledge_base.acl_revision + 1
        acl_revision = knowledge_base.acl_revision
        # 在同一事务与 KB 行锁内 flush 并读取成员快照，保证响应体与 acl_revision 一致；
        # 若留到 commit 之后再查，可能的并发替换会让 revision 与成员列表不匹配。
        await session.flush()
        valid_members = await list_valid_members(
            session, kb_id=kb_id, organization_id=organization_id
        )
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    return valid_members, acl_revision
