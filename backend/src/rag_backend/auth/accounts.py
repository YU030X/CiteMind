"""账号开户：由运维显式创建登录主体。

只在运维入口使用；不接受任何 HTTP 路径调用。密码在此哈希后落库，明文不外传、不打印。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rag_backend.auth.passwords import hash_password
from rag_backend.models.identity import UserAccount

# 迁移 20260923_0005 中组织内用户名的唯一约束名；并发同名插入时用它区分冲突。
USERNAME_UNIQUE_CONSTRAINT = "uq_user_account_organization_id_username"

# 与登录 `LoginRequest.username` 相同的长度契约；两处必须同步，否则会建出无法登录的账号。
MAX_USERNAME_LENGTH = 255


class AccountExistsError(RuntimeError):
    """同一组织内用户名已存在。"""

    def __init__(self, username: str) -> None:
        super().__init__(f"账号已存在: {username}")
        self.username = username


class InvalidUsernameError(ValueError):
    """用户名不符合登录契约（空、首尾空白、超长或含控制字符）。"""


def validate_username(username: str) -> str:
    """校验用户名与登录 schema 一致；不合法时抛 ``InvalidUsernameError``。

    建号是用户名的唯一入口，因此这里比登录更严格：拒绝首尾空白与不可打印控制字符，
    避免数据库里存在一个登录校验永远匹配不到的账号，也避免 CLI 日志被换行注入。
    """

    if not username:
        raise InvalidUsernameError("用户名不能为空")
    if username != username.strip():
        raise InvalidUsernameError("用户名不能包含首尾空白")
    if len(username) > MAX_USERNAME_LENGTH:
        raise InvalidUsernameError(f"用户名不能超过 {MAX_USERNAME_LENGTH} 字符")
    if not username.isprintable():
        raise InvalidUsernameError("用户名不能包含控制字符")
    return username


def is_username_unique_violation(error: IntegrityError) -> bool:
    """判断 IntegrityError 是否由组织内用户名唯一约束触发（并发竞态）。"""

    original = error.orig
    sqlstate = getattr(original, "sqlstate", None)
    diag = getattr(original, "diag", None)
    constraint_name = getattr(diag, "constraint_name", None)
    return sqlstate == "23505" and constraint_name == USERNAME_UNIQUE_CONSTRAINT


def create_account(
    session: Session,
    *,
    organization_id: uuid.UUID,
    username: str,
    password: str,
    is_admin: bool = False,
) -> UserAccount:
    """创建启用状态账号。

    用户名不满足契约时抛 ``InvalidUsernameError``，同组织重名时抛 ``AccountExistsError``。
    """

    validate_username(username)

    existing = session.scalar(
        select(UserAccount).where(
            UserAccount.organization_id == organization_id,
            UserAccount.username == username,
        )
    )
    if existing is not None:
        raise AccountExistsError(username)

    account = UserAccount(
        id=uuid.uuid4(),
        organization_id=organization_id,
        username=username,
        password_hash=hash_password(password),
        enabled=True,
        is_admin=is_admin,
    )
    try:
        session.add(account)
        session.commit()
    except IntegrityError as error:
        # 预先 SELECT 无法与并发插入互斥；提交时的唯一冲突同样归为“账号已存在”，
        # 而不是让 CLI 误报为数据库故障。
        session.rollback()
        if is_username_unique_violation(error):
            raise AccountExistsError(username) from error
        raise
    return account
