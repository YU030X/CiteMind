"""账号开户 CLI 的纯逻辑测试：不连接真实数据库、不进入交互提示。"""

import argparse
import io
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from rag_backend.auth.accounts import (
    MAX_USERNAME_LENGTH,
    AccountExistsError,
    InvalidUsernameError,
    create_account,
    is_username_unique_violation,
)
from rag_backend.auth.cli import (
    EXIT_ACCOUNT_EXISTS,
    EXIT_DATABASE_FAILURE,
    EXIT_DATABASE_ROLE,
    EXIT_OK,
    EXIT_USAGE,
    UnsupportedDatabaseRoleError,
    build_parser,
    main,
    require_api_database_role,
    resolve_password,
)
from rag_backend.auth.tokens import hash_token
from rag_backend.config import Settings
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

PASSWORD = "correct horse battery staple"
ORGANIZATION_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class FakeSyncSession:
    def __init__(self, *, existing: Any = None) -> None:
        self.existing = existing
        self.added: list[Any] = []
        self.commits = 0

    def scalar(self, statement: Any) -> Any:
        return self.existing

    def add(self, instance: Any) -> None:
        self.added.append(instance)

    def commit(self) -> None:
        self.commits += 1


class CommitFailingSession(FakeSyncSession):
    """在 commit 时抛出指定异常，并记录 rollback 调用。"""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error
        self.rollbacks = 0

    def commit(self) -> None:
        raise self.error

    def rollback(self) -> None:
        self.rollbacks += 1


class FakeDiag:
    def __init__(self, constraint_name: str) -> None:
        self.constraint_name = constraint_name


class FakePgError(RuntimeError):
    def __init__(self, *, sqlstate: str, constraint_name: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate
        self.diag = FakeDiag(constraint_name)


def make_integrity_error(
    *,
    sqlstate: str = "23505",
    constraint_name: str = "uq_user_account_organization_id_username",
) -> IntegrityError:
    return IntegrityError(
        "INSERT INTO user_account ...",
        {},
        FakePgError(sqlstate=sqlstate, constraint_name=constraint_name),
    )


class TtyStdin(io.StringIO):
    def isatty(self) -> bool:
        return True


def parse(args: list[str]) -> argparse.Namespace:
    return build_parser().parse_args(args)


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"_env_file": None, "environment": "test"}
    values.update(overrides)
    return Settings(**values)


def prompt_from(values: list[str]) -> Callable[[str], str]:
    iterator = iter(values)

    def prompt(_: str) -> str:
        return next(iterator)

    return prompt


# --- create_account ---------------------------------------------------------


def test_create_account_hashes_password_and_enables_account() -> None:
    session = cast(Session, FakeSyncSession())

    account = create_account(
        session,
        organization_id=ORGANIZATION_ID,
        username="alice",
        password=PASSWORD,
        is_admin=True,
    )

    assert account.password_hash.startswith("$argon2")
    assert PASSWORD not in account.password_hash
    assert account.enabled is True
    assert account.is_admin is True
    assert account.organization_id == ORGANIZATION_ID
    assert hash_token(PASSWORD) != account.password_hash


def test_create_account_rejects_duplicate_username() -> None:
    session = cast(Session, FakeSyncSession(existing=object()))

    with pytest.raises(AccountExistsError):
        create_account(
            session,
            organization_id=ORGANIZATION_ID,
            username="alice",
            password=PASSWORD,
        )


def test_create_account_accepts_max_length_username() -> None:
    username = "u" * MAX_USERNAME_LENGTH

    account = create_account(
        cast(Session, FakeSyncSession()),
        organization_id=ORGANIZATION_ID,
        username=username,
        password=PASSWORD,
    )

    assert account.username == username
    assert len(account.username) == MAX_USERNAME_LENGTH


@pytest.mark.parametrize(
    ("username", "message"),
    [
        ("", "不能为空"),
        ("   ", "首尾空白"),
        (" alice", "首尾空白"),
        ("alice ", "首尾空白"),
        ("u" * (MAX_USERNAME_LENGTH + 1), "255"),
        ("ali\nce", "控制字符"),
        ("ali\x7fce", "控制字符"),
    ],
    ids=[
        "empty",
        "spaces",
        "leading-space",
        "trailing-space",
        "too-long",
        "newline",
        "del",
    ],
)
def test_create_account_rejects_usernames_login_cannot_use(
    username: str, message: str
) -> None:
    with pytest.raises(InvalidUsernameError, match=message):
        create_account(
            cast(Session, FakeSyncSession()),
            organization_id=ORGANIZATION_ID,
            username=username,
            password=PASSWORD,
        )


def test_is_username_unique_violation_matches_only_username_constraint() -> None:
    assert is_username_unique_violation(make_integrity_error())
    assert not is_username_unique_violation(
        make_integrity_error(constraint_name="uq_other")
    )
    assert not is_username_unique_violation(make_integrity_error(sqlstate="23503"))


def test_create_account_maps_unique_race_to_account_exists() -> None:
    session = cast(Session, CommitFailingSession(make_integrity_error()))

    with pytest.raises(AccountExistsError):
        create_account(
            session,
            organization_id=ORGANIZATION_ID,
            username="alice",
            password=PASSWORD,
        )

    assert cast(CommitFailingSession, session).rollbacks == 1


def test_create_account_reraises_unrelated_integrity_errors() -> None:
    session = cast(Session, CommitFailingSession(make_integrity_error(sqlstate="23503")))

    with pytest.raises(IntegrityError):
        create_account(
            session,
            organization_id=ORGANIZATION_ID,
            username="alice",
            password=PASSWORD,
        )

    assert cast(CommitFailingSession, session).rollbacks == 1


# --- resolve_password -------------------------------------------------------


def test_resolve_password_reads_environment() -> None:
    args = parse(["--username", "alice", "--password-env", "PW"])

    password = resolve_password(
        args,
        environ={"PW": PASSWORD},
        stdin=io.StringIO(),
        prompt=prompt_from([]),
    )

    assert password == PASSWORD


def test_resolve_password_missing_environment_variable_fails() -> None:
    args = parse(["--username", "alice", "--password-env", "PW"])

    with pytest.raises(ValueError, match="未设置"):
        resolve_password(
            args, environ={}, stdin=io.StringIO(), prompt=prompt_from([])
        )


def test_resolve_password_reads_first_file_line(tmp_path: Path) -> None:
    password_file = tmp_path / "secret"
    password_file.write_text(PASSWORD + "\nignored\n", encoding="utf-8")
    args = parse(["--username", "alice", "--password-file", str(password_file)])

    assert (
        resolve_password(
            args, environ={}, stdin=io.StringIO(), prompt=prompt_from([])
        )
        == PASSWORD
    )


def test_resolve_password_reads_stdin() -> None:
    args = parse(["--username", "alice", "--password-stdin"])

    assert (
        resolve_password(
            args, environ={}, stdin=io.StringIO(PASSWORD + "\n"), prompt=prompt_from([])
        )
        == PASSWORD
    )


def test_resolve_password_interactive_prompts_twice() -> None:
    args = parse(["--username", "alice"])
    prompts: list[str] = []

    def prompt(text: str) -> str:
        prompts.append(text)
        return PASSWORD

    assert (
        resolve_password(args, environ={}, stdin=TtyStdin(), prompt=prompt) == PASSWORD
    )
    assert len(prompts) == 2


def test_resolve_password_interactive_mismatch_fails() -> None:
    args = parse(["--username", "alice"])

    with pytest.raises(ValueError, match="不一致"):
        resolve_password(
            args,
            environ={},
            stdin=TtyStdin(),
            prompt=prompt_from([PASSWORD, "other"]),
        )


def test_resolve_password_non_tty_without_source_fails_fast() -> None:
    args = parse(["--username", "alice"])

    with pytest.raises(ValueError, match="不是终端"):
        resolve_password(
            args, environ={}, stdin=io.StringIO(), prompt=prompt_from([])
        )


def test_resolve_password_rejects_empty_password() -> None:
    args = parse(["--username", "alice", "--password-env", "PW"])

    with pytest.raises(ValueError, match="不能为空"):
        resolve_password(args, environ={"PW": ""}, stdin=io.StringIO(), prompt=prompt_from([]))


# --- main -------------------------------------------------------------------


def test_parser_requires_username() -> None:
    with pytest.raises(SystemExit):
        parse([])


def test_main_creates_account_without_printing_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    account_id = uuid.uuid4()
    captured: dict[str, Any] = {}

    def fake_provision(**kwargs: Any) -> uuid.UUID:
        captured.update(kwargs)
        return account_id

    monkeypatch.setattr("rag_backend.auth.cli.provision_account", fake_provision)
    monkeypatch.setenv("PW", PASSWORD)

    exit_code = main(
        ["--username", "alice", "--admin", "--password-env", "PW"],
        settings_factory=lambda: make_settings(
            database_url="postgresql+psycopg://citemind_api:citemind@127.0.0.1:55432/citemind"
        ),
    )

    output = capsys.readouterr()
    assert exit_code == EXIT_OK
    assert captured["password"] == PASSWORD
    assert captured["organization_id"] == ORGANIZATION_ID
    assert captured["is_admin"] is True
    assert str(account_id) in output.out
    assert PASSWORD not in output.out
    assert PASSWORD not in output.err


def test_main_reports_duplicate_account(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_provision(**kwargs: Any) -> uuid.UUID:
        raise AccountExistsError(str(kwargs["username"]))

    monkeypatch.setattr("rag_backend.auth.cli.provision_account", fake_provision)
    monkeypatch.setenv("PW", PASSWORD)

    exit_code = main(
        ["--username", "alice", "--password-env", "PW"],
        settings_factory=lambda: make_settings(),
    )

    assert exit_code == EXIT_ACCOUNT_EXISTS
    assert PASSWORD not in capsys.readouterr().err


def test_main_reports_database_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_provision(**kwargs: Any) -> uuid.UUID:
        raise SQLAlchemyError("boom")

    monkeypatch.setattr("rag_backend.auth.cli.provision_account", fake_provision)
    monkeypatch.setenv("PW", PASSWORD)

    exit_code = main(
        ["--username", "alice", "--password-env", "PW"],
        settings_factory=lambda: make_settings(),
    )

    assert exit_code == EXIT_DATABASE_FAILURE


@pytest.mark.parametrize(
    "username",
    ["", "   ", " alice", "u" * (MAX_USERNAME_LENGTH + 1), "ali\nce"],
    ids=["empty", "spaces", "leading-space", "too-long", "newline"],
)
def test_main_rejects_invalid_username_before_password_or_database(
    username: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("无效用户名不得读取密码或连接数据库")

    monkeypatch.setattr("rag_backend.auth.cli.resolve_password", fail_if_called)
    monkeypatch.setattr("rag_backend.auth.cli.provision_account", fail_if_called)
    monkeypatch.setenv("PW", PASSWORD)

    exit_code = main(
        ["--username", username, "--password-env", "PW"],
        settings_factory=lambda: make_settings(),
    )

    assert exit_code == EXIT_USAGE
    error_text = capsys.readouterr().err
    assert "username" in error_text
    assert PASSWORD not in error_text


def test_main_reports_invalid_organization_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PW", PASSWORD)

    exit_code = main(
        ["--username", "alice", "--password-env", "PW", "--organization-id", "not-a-uuid"],
        settings_factory=lambda: make_settings(),
    )

    assert exit_code == EXIT_USAGE
    assert PASSWORD not in capsys.readouterr().err


def test_main_rejects_mismatched_organization_before_reading_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    other_organization_id = uuid.uuid4()

    def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("组织不一致时不得继续读取密码或建号")

    monkeypatch.setattr("rag_backend.auth.cli.resolve_password", fail_if_called)
    monkeypatch.setattr("rag_backend.auth.cli.provision_account", fail_if_called)

    exit_code = main(
        [
            "--username",
            "alice",
            "--organization-id",
            str(other_organization_id),
            "--password-env",
            "PW",
        ],
        settings_factory=lambda: make_settings(),
    )

    assert exit_code == EXIT_USAGE
    captured = capsys.readouterr()
    assert str(other_organization_id) in captured.err


def test_main_allows_organization_id_matching_the_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_provision(**kwargs: Any) -> uuid.UUID:
        captured.update(kwargs)
        return uuid.uuid4()

    monkeypatch.setattr("rag_backend.auth.cli.provision_account", fake_provision)
    monkeypatch.setenv("PW", PASSWORD)

    exit_code = main(
        [
            "--username",
            "alice",
            "--organization-id",
            str(ORGANIZATION_ID),
            "--password-env",
            "PW",
        ],
        settings_factory=lambda: make_settings(),
    )

    assert exit_code == EXIT_OK
    assert captured["organization_id"] == ORGANIZATION_ID


def test_main_reports_unsupported_database_role(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_provision(**kwargs: Any) -> uuid.UUID:
        raise UnsupportedDatabaseRoleError("citemind_migrator")

    monkeypatch.setattr("rag_backend.auth.cli.provision_account", fake_provision)
    monkeypatch.setenv("PW", PASSWORD)

    exit_code = main(
        ["--username", "alice", "--password-env", "PW"],
        settings_factory=lambda: make_settings(),
    )

    assert exit_code == EXIT_DATABASE_ROLE
    error_text = capsys.readouterr().err
    assert "citemind_api" in error_text
    assert PASSWORD not in error_text


class FakeRoleConnection:
    def __init__(self, role: str) -> None:
        self.role = role

    def scalar(self, statement: Any) -> Any:
        return self.role


def test_require_api_database_role_accepts_only_the_api_role() -> None:
    require_api_database_role(cast(Any, FakeRoleConnection("citemind_api")))

    with pytest.raises(UnsupportedDatabaseRoleError) as error:
        require_api_database_role(cast(Any, FakeRoleConnection("citemind_migrator")))

    assert error.value.role == "citemind_migrator"
