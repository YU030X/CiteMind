"""账号开户运维命令行入口。

用法（交互式，密码隐藏输入）：

    uv run python -m rag_backend.auth.cli --username alice

非交互式（供部署 fixture/自动化使用；密码不进入命令行历史）：

    uv run python -m rag_backend.auth.cli --username alice --password-env NEW_USER_PASSWORD
    uv run python -m rag_backend.auth.cli --username alice --password-file /run/secrets/alice
    printf '%s\\n' "$PW" | uv run python -m rag_backend.auth.cli --username alice --password-stdin

安全约束：密码只用 getpass/文件/环境/标准输入读取，绝不打印；stdin 非终端且未提供任何
密码来源时快速失败，避免 Agent 或 CI 意外挂在交互提示上。`--organization-id` 必须与
`ORGANIZATION_ID` 一致，否则在读取密码前拒绝。建号前以只读 `SELECT current_user`
预检连接角色，非 `citemind_api` 时显式失败且不写入。命令不注册 HTTP 路由。
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TextIO

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from rag_backend.auth.accounts import (
    MAX_USERNAME_LENGTH,
    AccountExistsError,
    InvalidUsernameError,
    create_account,
    validate_username,
)
from rag_backend.config import Settings, get_settings

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_ACCOUNT_EXISTS = 3
EXIT_DATABASE_FAILURE = 4
EXIT_DATABASE_ROLE = 5

MAX_PASSWORD_BYTES = 1024

# 开户只允许用最小权限的 api 运行角色；迁移/超级用户可写但不应以运维默认身份建号。
API_DATABASE_ROLE = "citemind_api"


class UnsupportedDatabaseRoleError(RuntimeError):
    """数据库连接角色不是允许开户的 api 运行角色。"""

    def __init__(self, role: str) -> None:
        super().__init__(
            f"数据库连接角色必须是 {API_DATABASE_ROLE}，实际为 {role}；拒绝开户且未写入"
        )
        self.role = role


def require_api_database_role(connection: Connection) -> None:
    """建号前用只读查询确认连接角色；非 api 角色直接失败，保证零写入。"""

    role = connection.scalar(text("SELECT current_user"))
    if role != API_DATABASE_ROLE:
        raise UnsupportedDatabaseRoleError(str(role))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m rag_backend.auth.cli",
        description="创建一个登录账号；密码默认隐藏输入，绝不出现在日志或命令行。",
    )
    parser.add_argument(
        "--username",
        required=True,
        help=f"组织内唯一的登录用户名（≤{MAX_USERNAME_LENGTH} 字符，无首尾空白）",
    )
    parser.add_argument(
        "--admin",
        action="store_true",
        help="把账号标记为管理员（默认否）",
    )
    parser.add_argument(
        "--organization-id",
        default=None,
        help="必须与配置的 ORGANIZATION_ID 一致；不一致时拒绝开户",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help=f"覆盖数据库 DSN；默认取 DATABASE_URL，连接角色必须是 {API_DATABASE_ROLE}",
    )
    password_group = parser.add_mutually_exclusive_group()
    password_group.add_argument(
        "--password-env",
        metavar="VAR",
        default=None,
        help="从环境变量读取密码（推荐给自动化 fixture）",
    )
    password_group.add_argument(
        "--password-file",
        metavar="PATH",
        default=None,
        help="从文件首行读取密码",
    )
    password_group.add_argument(
        "--password-stdin",
        action="store_true",
        help="从标准输入读取一行密码",
    )
    return parser


def resolve_password(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str],
    stdin: TextIO,
    prompt: Callable[[str], str],
) -> str:
    """按参数优先级解析密码；仅交互分支读取隐藏输入。"""

    if args.password_env:
        value = environ.get(args.password_env)
        if value is None:
            raise ValueError(f"环境变量 {args.password_env} 未设置")
        password = value
    elif args.password_file:
        password = Path(args.password_file).read_text(encoding="utf-8").splitlines()[0]
    elif args.password_stdin:
        password = stdin.readline().rstrip("\r\n")
    else:
        if not stdin.isatty():
            raise ValueError(
                "标准输入不是终端；请使用 --password-env、--password-file 或 --password-stdin"
            )
        first = prompt("请输入新账号密码: ")
        second = prompt("请再次输入密码: ")
        if first != second:
            raise ValueError("两次输入的密码不一致")
        password = first
        del first, second

    if not password:
        raise ValueError("密码不能为空")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError(f"密码不能超过 {MAX_PASSWORD_BYTES} 字节")
    return password


def provision_account(
    *,
    database_url: str,
    organization_id: uuid.UUID,
    username: str,
    password: str,
    is_admin: bool,
) -> uuid.UUID:
    """在真实数据库上创建账号；返回新账号 id。调用方负责关闭引擎。"""

    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        # 先只读预检连接角色：非 citemind_api 时在此失败，不建立 Session、不写入。
        with engine.connect() as connection:
            require_api_database_role(connection)
        with Session(engine) as session:
            account = create_account(
                session,
                organization_id=organization_id,
                username=username,
                password=password,
                is_admin=is_admin,
            )
            return account.id
    finally:
        engine.dispose()


def main(
    argv: Sequence[str] | None = None,
    *,
    settings_factory: Callable[[], Settings] = get_settings,
) -> int:
    args = build_parser().parse_args(argv)
    settings = settings_factory()

    # 用户名校验先于密码读取与数据库连接；无效输入不 traceback、不建号。
    try:
        validate_username(args.username)
    except InvalidUsernameError as error:
        print(f"无效的 username: {error}", file=sys.stderr)
        return EXIT_USAGE

    database_url = args.database_url or settings.database_url
    organization_id = settings.organization_id
    if args.organization_id:
        try:
            requested_organization_id = uuid.UUID(args.organization_id)
        except ValueError:
            print(f"无效的 organization-id: {args.organization_id}", file=sys.stderr)
            return EXIT_USAGE
        if requested_organization_id != organization_id:
            # 登录只按配置的单组织查账号；允许写入其它组织会得到永远无法登录的账号。
            print(
                f"organization-id {requested_organization_id} 与当前单组织配置 "
                f"{organization_id} 不一致；拒绝开户",
                file=sys.stderr,
            )
            return EXIT_USAGE

    try:
        password = resolve_password(
            args,
            environ=os.environ,
            stdin=sys.stdin,
            prompt=getpass.getpass,
        )
    except (ValueError, OSError, IndexError) as error:
        print(f"读取密码失败: {error}", file=sys.stderr)
        return EXIT_USAGE

    try:
        account_id = provision_account(
            database_url=database_url,
            organization_id=organization_id,
            username=args.username,
            password=password,
            is_admin=args.admin,
        )
    except AccountExistsError as error:
        print(str(error), file=sys.stderr)
        return EXIT_ACCOUNT_EXISTS
    except UnsupportedDatabaseRoleError as error:
        print(str(error), file=sys.stderr)
        return EXIT_DATABASE_ROLE
    except SQLAlchemyError as error:
        print(f"数据库操作失败: {type(error).__name__}", file=sys.stderr)
        return EXIT_DATABASE_FAILURE
    finally:
        del password

    print(
        f"已创建账号 username={args.username} id={account_id} "
        f"organizationId={organization_id} isAdmin={args.admin}"
    )
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - 由命令行触发
    raise SystemExit(main())
