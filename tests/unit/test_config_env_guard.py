"""裸环境变量重命名的启动契约测试：全部使用显式 ``_env_file``，不读取仓库根 .env。"""

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from rag_backend.config import Settings
from sqlalchemy.engine import make_url

DATABASE_URL = "postgresql+psycopg://citemind_app:strong-password@127.0.0.1:5432/citemind"
LEGACY_DATABASE_URL = "postgresql+psycopg://legacy-user:legacy-secret@127.0.0.1:5432/citemind"
DOTENV_DATABASE_URL = "postgresql+psycopg://dotenv-user:dotenv-secret@127.0.0.1:5432/citemind"

# 合成高熵哨兵，只用于证明诊断输出不泄露 DSN 凭据，不代表任何真实令牌。
REPR_DB_SENTINEL = "SENTINEL_REPR_DB_p8Qw3Zk7Lm2Tx9Rb"
REPR_REDIS_SENTINEL = "SENTINEL_REPR_REDIS_v4Hn6Yc1Ws8Jd5Fg"
REPR_DATABASE_URL = f"postgresql+psycopg://repr_user:{REPR_DB_SENTINEL}@127.0.0.1:5432/citemind"
REPR_REDIS_URL = f"redis://:{REPR_REDIS_SENTINEL}@127.0.0.1:56379/0"


def make_settings(**overrides: Any) -> Settings:
    """构造不读取仓库根 .env 的配置；``_env_file`` 用 kwargs 字典传，避免 mypy 误报。"""

    values: dict[str, Any] = {"_env_file": None}
    values.update(overrides)
    return Settings(**values)


def test_rejects_legacy_prefixed_process_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CITEMIND_DATABASE_URL", LEGACY_DATABASE_URL)

    with pytest.raises(ValueError, match="CITEMIND_DATABASE_URL") as error:
        make_settings()

    # 守卫只暴露键名，绝不回显旧值。
    assert "legacy-secret" not in str(error.value)


def test_rejects_legacy_prefixed_dotenv_key(tmp_path: Path) -> None:
    env_file = tmp_path / "legacy.env"
    env_file.write_text(
        "CITEMIND_REDIS_URL=redis://:legacy-secret@127.0.0.1:56379/0\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="CITEMIND_REDIS_URL") as error:
        make_settings(_env_file=env_file)

    assert "legacy-secret" not in str(error.value)


def test_lists_every_legacy_key_without_reading_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CITEMIND_ALLOW_LLM_PROBE", "1")
    monkeypatch.setenv("CITEMIND_LLM_API_KEY", "sk-legacy-secret")

    with pytest.raises(ValueError) as error:
        make_settings()

    message = str(error.value)
    assert "CITEMIND_ALLOW_LLM_PROBE" in message
    assert "CITEMIND_LLM_API_KEY" in message
    assert "sk-legacy-secret" not in message


def test_bare_process_env_key_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)

    resolved = make_settings()

    assert resolved.database_url == DATABASE_URL


def test_bare_dotenv_key_is_read(tmp_path: Path) -> None:
    env_file = tmp_path / "new.env"
    env_file.write_text(f"DATABASE_URL={DATABASE_URL}\n", encoding="utf-8")

    resolved = make_settings(_env_file=env_file)

    assert resolved.database_url == DATABASE_URL


def test_bare_process_env_overrides_dotenv_for_same_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同名裸键时进程环境优先于自制 dotenv，避免旧文件值静默覆盖运行时配置。"""

    env_file = tmp_path / "override.env"
    env_file.write_text(f"DATABASE_URL={DOTENV_DATABASE_URL}\n", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)

    resolved = make_settings(_env_file=env_file)

    assert resolved.database_url == DATABASE_URL


def test_unrelated_third_party_variables_are_not_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "dev-only-jev-key")
    monkeypatch.setenv("POSTGRES_PASSWORD", "compose-secret")
    monkeypatch.setenv("REDISCLI_AUTH", "compose-secret")

    resolved = make_settings()

    assert resolved.llm_api_key is None
    assert "dev-only-jev-key" not in repr(resolved)


def test_settings_repr_and_str_hide_database_and_redis_credentials() -> None:
    """诊断用的 repr/str 不得回显 DSN 明文密码（安全任务 #118）。"""

    resolved = make_settings(
        environment="test",
        database_url=REPR_DATABASE_URL,
        redis_url=REPR_REDIS_URL,
    )

    rendered = repr(resolved) + str(resolved)

    assert REPR_DB_SENTINEL not in rendered
    assert REPR_REDIS_SENTINEL not in rendered


def test_hidden_repr_keeps_resolved_values_and_connection_password_intact() -> None:
    """屏蔽 repr 只影响诊断显示，字段原值、model_dump 与连接密码必须保持不变。"""

    resolved = make_settings(
        environment="test",
        database_url=REPR_DATABASE_URL,
        redis_url=REPR_REDIS_URL,
    )

    assert resolved.database_url == REPR_DATABASE_URL
    assert resolved.redis_url == REPR_REDIS_URL

    dumped = resolved.model_dump()
    assert dumped["database_url"] == REPR_DATABASE_URL
    assert dumped["redis_url"] == REPR_REDIS_URL

    # 仅解析 URL 确认连接所需密码仍在，不建立任何数据库连接。
    assert make_url(resolved.database_url).password == REPR_DB_SENTINEL


def test_pytest_showlocals_failure_report_does_not_leak_dsn(tmp_path: Path) -> None:
    """pytest --showlocals 打印失败用例 locals 时不得把合成 DSN 凭据带进报告。"""

    module = tmp_path / "test_repr_diagnostic.py"
    module.write_text(
        "\n".join(
            [
                "from rag_backend.config import Settings",
                f"DB_URL = {REPR_DATABASE_URL!r}",
                f"REDIS_URL = {REPR_REDIS_URL!r}",
                "",
                "def test_diagnostic():",
                "    settings = Settings(",
                '        _env_file=None, environment="test",',
                "        database_url=DB_URL, redis_url=REDIS_URL,",
                "    )",
                '    assert False, "intentional diagnostic failure"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        # ``-vv`` 关闭 pytest 对 locals 的截断，否则 ``...`` 会掩盖部分明文，
        # 使断言在没有修复时也能通过。
        [sys.executable, "-m", "pytest", "--showlocals", "-p", "no:cacheprovider", "-vv"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    report = completed.stdout + completed.stderr
    # 合成用例本来就会失败；这里验证的是报告内容，而不是退出码本身。
    assert completed.returncode != 0
    assert "settings" in report, "--showlocals 未打印 settings，断言前提不成立"
    assert REPR_DB_SENTINEL not in report
    assert REPR_REDIS_SENTINEL not in report
