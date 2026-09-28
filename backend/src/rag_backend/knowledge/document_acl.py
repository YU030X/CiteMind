"""文档读取收紧（ACL）的纯规则、读取判定与全量替换用例。

``document.acl_mode`` 只有两种取值：

- ``INHERIT``：沿用 KB 成员读权限；此时 ``document_acl`` 必须为空。
- ``RESTRICTED``：读取只允许 ``document_acl`` 中显式登记为 ``USER``/``READ`` 的用户，
  且该用户仍必须是同组织、未撤销的 KB 成员。空名单表示任何人都不能读，连 OWNER 也不能
  （管理权不等于读权）。

ACL 只**收紧**读取，不改变现有管理权：KB ``EDITOR`` 仍可更新、``OWNER`` 仍可删除并管理
ACL。允许名单不是独立授权，组织关系与成员状态始终走权威链
``document → knowledge_base → kb_member``。

替换在单事务内完成，锁序与删除一致：``document`` 行锁 → ``knowledge_base`` 行锁 →
``document_acl`` 写入；不锁定 ``kb_member``（成员替换是 ``knowledge_base → kb_member``，
本模块不与之形成环）。只有 ``acl_mode`` 或名单实际变化才递增 ``knowledge_base.acl_revision``。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import delete, exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from rag_backend.knowledge.roles import KbRole
from rag_backend.models.identity import KbMember, UserAccount
from rag_backend.models.knowledge import Document, DocumentAcl, KnowledgeBase

PRINCIPAL_TYPE_USER = "USER"
PERMISSION_READ = "READ"
DOCUMENT_LIFECYCLE_DELETED = "DELETED"


class DocumentAclMode(StrEnum):
    """文档读取模式；默认沿用 KB 成员权限。"""

    INHERIT = "INHERIT"
    RESTRICTED = "RESTRICTED"


class DocumentAclError(Exception):
    """文档 ACL 用例的领域错误基类；路由层负责映射到具名 HTTP 错误。"""


class DocumentAclInvalid(DocumentAclError):
    """请求形状非法：``INHERIT`` 却提交了非空名单，或名单含重复用户。"""


class DocumentAclMemberInvalid(DocumentAclError):
    """名单引用了不存在、跨组织、已禁用或不是该 KB 有效成员的用户。"""


class DocumentAclDocumentNotFound(DocumentAclError):
    """文档不存在、跨组织、已删除，或调用者不是该知识库的有效 OWNER。"""


@dataclass(frozen=True, slots=True)
class DocumentAclMember:
    """一次替换请求中的名单条目；首片只有用户主体。"""

    user_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class DocumentAclView:
    """替换后的 ACL 快照；``member_ids`` 升序稳定输出。"""

    document_id: uuid.UUID
    mode: DocumentAclMode
    member_ids: tuple[uuid.UUID, ...]
    acl_revision: int


@dataclass(frozen=True, slots=True)
class DocumentReadAccess:
    """一次文档读取判定结果；只在当前请求事务内有效。"""

    document_id: uuid.UUID
    kb_id: uuid.UUID
    organization_id: uuid.UUID
    role: KbRole


def validate_document_acl_request(
    mode: DocumentAclMode, members: Sequence[DocumentAclMember]
) -> None:
    """校验请求形状；不接触数据库，调用方应在加锁与写入前执行。

    ``INHERIT`` 不允许携带名单（名单只对 ``RESTRICTED`` 有意义），重复用户同样拒绝。
    """

    if mode is DocumentAclMode.INHERIT and members:
        raise DocumentAclInvalid("INHERIT 模式不能携带成员名单")
    seen: set[uuid.UUID] = set()
    for member in members:
        if member.user_id in seen:
            raise DocumentAclInvalid("成员名单包含重复用户")
        seen.add(member.user_id)


def acl_read_clause(user_id: uuid.UUID) -> ColumnElement[bool]:
    """构造「该文档对当前用户放行读取」的相关子句。

    放行条件是 ``INHERIT``，或该用户在 ``document_acl`` 中登记了 ``USER``/``READ``。
    供 ORM 读取路径（列表、详情、内容）复用同一判定，避免各处自行拼装。
    """

    return (Document.acl_mode == DocumentAclMode.INHERIT.value) | exists().where(
        DocumentAcl.document_id == Document.id,
        DocumentAcl.principal_type == PRINCIPAL_TYPE_USER,
        DocumentAcl.principal_id == user_id,
        DocumentAcl.permission == PERMISSION_READ,
    )


async def resolve_document_read_access(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> DocumentReadAccess | None:
    """判定当前用户能否**读取**该文档；不能则返回 ``None``（调用方统一 404）。

    要求：文档未删除、属于同组织 KB、用户是未撤销的 KB 成员，且 ACL 放行。已删除文档与
    越权一样返回 ``None``，不泄露存在性。
    """

    statement = (
        select(Document.id, Document.kb_id, KnowledgeBase.organization_id, KbMember.role)
        .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
        .join(KbMember, KbMember.kb_id == Document.kb_id)
        .where(
            Document.id == document_id,
            KnowledgeBase.organization_id == organization_id,
            KbMember.user_id == user_id,
            KbMember.revoked_at.is_(None),
            Document.deleted_at.is_(None),
            Document.lifecycle_status != "DELETED",
            acl_read_clause(user_id),
        )
    )
    row = (await session.execute(statement)).first()
    if row is None:
        return None
    return DocumentReadAccess(
        document_id=row[0],
        kb_id=row[1],
        organization_id=row[2],
        role=KbRole(row[3]),
    )


async def _invalid_member_ids(
    session: AsyncSession,
    *,
    kb_id: uuid.UUID,
    organization_id: uuid.UUID,
    requested_ids: set[uuid.UUID],
) -> set[uuid.UUID]:
    """返回名单里不是「同组织 + 启用 + 该 KB 未撤销成员」的用户 id。"""

    if not requested_ids:
        return set()
    user_rows = (
        await session.execute(
            select(
                UserAccount.id, UserAccount.organization_id, UserAccount.enabled
            ).where(UserAccount.id.in_(list(requested_ids)))
        )
    ).all()
    valid_users = {
        user_id
        for user_id, org_id, enabled in user_rows
        if org_id == organization_id and enabled
    }
    member_rows = (
        await session.execute(
            select(KbMember.user_id).where(
                KbMember.kb_id == kb_id,
                KbMember.user_id.in_(list(requested_ids)),
                KbMember.revoked_at.is_(None),
            )
        )
    ).all()
    active_members = {row[0] for row in member_rows}
    return requested_ids - (valid_users & active_members)


async def replace_document_acl(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    mode: DocumentAclMode,
    members: Sequence[DocumentAclMember],
) -> DocumentAclView:
    """OWNER 全量替换文档读取 ACL，返回替换后的快照。

    锁序：``document`` 行锁 → ``knowledge_base`` 行锁（``acl_revision`` 条件递增）→
    ``document_acl`` 删除/插入。``document`` 行锁与删除事务同序，避免新环；``kb_member``
    只读不锁，且成员替换是 ``knowledge_base → kb_member``，本模块不持有 ``kb_member`` 锁，
    因此不与其冲突。锁内复核调用者仍是该 KB 的有效 OWNER，防止校验与写入之间的 TOCTOU。
    已删除文档（``deleted_at`` 非空或 ``lifecycle_status='DELETED'``）与不存在/越权一样返回
    404，与文档读取/管理边界一致，此时不改 ACL 也不递增 revision。
    """

    validate_document_acl_request(mode, members)

    try:
        document = (
            await session.execute(
                select(Document)
                .where(
                    Document.id == document_id,
                    Document.deleted_at.is_(None),
                    Document.lifecycle_status != DOCUMENT_LIFECYCLE_DELETED,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if document is None:
            raise DocumentAclDocumentNotFound("文档不存在或无权访问")

        kb_row = (
            await session.execute(
                select(KnowledgeBase.id, KnowledgeBase.organization_id).where(
                    KnowledgeBase.id == document.kb_id
                )
            )
        ).first()
        if kb_row is None or kb_row[1] != organization_id:
            raise DocumentAclDocumentNotFound("文档不存在或无权访问")

        actor_role = (
            await session.execute(
                select(KbMember.role).where(
                    KbMember.kb_id == document.kb_id,
                    KbMember.user_id == actor_user_id,
                    KbMember.revoked_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if actor_role != KbRole.OWNER.value:
            raise DocumentAclDocumentNotFound("文档不存在或无权访问")

        requested_ids = {member.user_id for member in members}
        invalid_ids = await _invalid_member_ids(
            session,
            kb_id=document.kb_id,
            organization_id=organization_id,
            requested_ids=requested_ids,
        )
        if invalid_ids:
            raise DocumentAclMemberInvalid(
                "成员必须是当前组织内该知识库的有效用户"
            )

        current_rows = (
            await session.execute(
                select(DocumentAcl.principal_id).where(
                    DocumentAcl.document_id == document_id
                )
            )
        ).all()
        current_ids = {row[0] for row in current_rows}
        current_revision = int(
            (
                await session.execute(
                    select(KnowledgeBase.acl_revision).where(
                        KnowledgeBase.id == document.kb_id
                    )
                )
            ).scalar_one()
        )

        mode_value = mode.value
        mode_changed = document.acl_mode != mode_value
        members_changed = current_ids != requested_ids
        if not mode_changed and not members_changed:
            await session.commit()
            return DocumentAclView(
                document_id=document_id,
                mode=mode,
                member_ids=tuple(sorted(requested_ids, key=str)),
                acl_revision=current_revision,
            )

        document.acl_mode = mode_value
        await session.execute(
            delete(DocumentAcl).where(DocumentAcl.document_id == document_id)
        )
        for user_id in sorted(requested_ids, key=str):
            session.add(
                DocumentAcl(
                    id=uuid.uuid4(),
                    document_id=document_id,
                    principal_type=PRINCIPAL_TYPE_USER,
                    principal_id=user_id,
                    permission=PERMISSION_READ,
                )
            )
        acl_revision = int(
            (
                await session.execute(
                    update(KnowledgeBase)
                    .where(KnowledgeBase.id == document.kb_id)
                    .values(acl_revision=KnowledgeBase.acl_revision + 1)
                    .returning(KnowledgeBase.acl_revision)
                )
            ).scalar_one()
        )
        await session.flush()
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    return DocumentAclView(
        document_id=document_id,
        mode=mode,
        member_ids=tuple(sorted(requested_ids, key=str)),
        acl_revision=acl_revision,
    )


__all__ = [
    "PERMISSION_READ",
    "PRINCIPAL_TYPE_USER",
    "DocumentAclDocumentNotFound",
    "DocumentAclError",
    "DocumentAclInvalid",
    "DocumentAclMember",
    "DocumentAclMemberInvalid",
    "DocumentAclMode",
    "DocumentAclView",
    "DocumentReadAccess",
    "acl_read_clause",
    "replace_document_acl",
    "resolve_document_read_access",
    "validate_document_acl_request",
]
