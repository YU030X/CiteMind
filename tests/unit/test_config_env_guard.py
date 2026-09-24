"""裸环境变量重命名的启动契约测试：全部使用显式 ``_env_file``，不读取仓库根 .env。"""

from pathlib import Path
from typing import Any

import pytest
from rag_backend.config import Settings

DATABASE_URL = "postgresql+psycopg://citemind_app:strong-password@127.0.0.1:5432/citemind"
LEGACY_DATABASE_URL = "postgresql+psycopg://legacy-user:legacy-secret@127.0.0.1:5432/citemind"
DOTENV_DATABASE_URL = "postgresql+psycopg://dotenv-user:dotenv-secret@127.0.0.1:5432/citemind"


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
