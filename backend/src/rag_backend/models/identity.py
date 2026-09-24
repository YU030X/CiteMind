"""身份、会话与 KB 成员模型（Phase 1 首片）。

``user_account`` 是登录主体，``organization_id`` 来自服务端会话、暂不建组织外键，
账号在组织内唯一；``auth_session`` 只保存 token hash 与 CSRF token hash，退出、
过期和禁用通过 ``revoked_at`` 或直接拒绝生效；``kb_member`` 用 ``revoked_at`` 软撤销，
``(kb_id, user_id)`` 唯一。本模块只定义表结构；密码校验、登录/会话与 KB 权限判定的
行为现由 ``rag_backend.auth`` 与 ``rag_backend.knowledge`` 实现。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from rag_backend.models.base import Base, CreatedAtMixin, UpdatedAtMixin


class UserAccount(CreatedAtMixin, UpdatedAtMixin, Base):
    """登录主体；组织由服务端会话确定，账号在组织内唯一。"""

    __tablename__ = "user_account"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "username",
            name="uq_user_account_organization_id_username",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    username: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False)


class AuthSession(CreatedAtMixin, UpdatedAtMixin, Base):
    """服务端会话；库中只存 token/CSRF hash，撤销只写 ``revoked_at``。"""

    __tablename__ = "auth_session"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_auth_session_token_hash"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("user_account.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    csrf_token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class KbMember(CreatedAtMixin, UpdatedAtMixin, Base):
    """KB 成员授权；``revoked_at`` 软撤销，``(kb_id, user_id)`` 唯一。"""

    __tablename__ = "kb_member"
    __table_args__ = (
        CheckConstraint("role IN ('OWNER', 'EDITOR', 'READER')", name="role"),
        UniqueConstraint("kb_id", "user_id", name="uq_kb_member_kb_id_user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    kb_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("knowledge_base.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("user_account.id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
