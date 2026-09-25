"""上传片 schema fixture 清理路径的纯逻辑检查：不连接数据库。

用假 engine 与 monkeypatch 驱动真实的 schema 生成器 ``open_upload_schema``，证明两条
清理安全性质：

1. 前置条件不干净（public schema 已有业务表）时不声明所有权，绝不做任何 downgrade；
2. 已声明所有权后升级失败时，仍会尝试降回 base，且 engine 始终被 dispose。

真实 PostgreSQL 上的升级/降级语义由 ``tests/integration/test_document_upload_flow.py``
覆盖。假 DSN 只用显式不可达的 ``127.0.0.1:1`` 且不含密码，本文件不建立任何连接。
"""

import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# 集成 fixture 与单元测试同属 tests 但不同目录且都不是包，显式加入 sys.path 后再导入。
sys.path.insert(0, str(Path(__file__).parents[1] / "integration"))

import test_document_upload_flow as upload_flow  # noqa: E402

TEST_DATABASE_NAME = "citemind_test"
ALEMBIC_CONFIG_SENTINEL = object()


class FakeEngine:
    """只实现清理路径需要的最小 engine 接口。"""

    def __init__(self, *, database_name: str) -> None:
        self.database_name = database_name
        self.disposed = False

    def connect(self) -> "FakeConnection":
        return FakeConnection(self)

    def dispose(self) -> None:
        self.disposed = True


class FakeConnection:
    def __init__(self, engine: FakeEngine) -> None:
        self._engine = engine

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def scalar(self, statement: Any, parameters: Any = None) -> Any:
        # 只有 current_database() 会直接走 engine；其余查询由被替换的 helper 处理。
        assert "current_database" in str(statement), f"意外的直接查询: {statement}"
        return self._engine.database_name


class CommandRecorder:
    """替代 alembic ``command``：记录 upgrade/downgrade 顺序，可选让 upgrade 失败。"""

    def __init__(self, *, upgrade_error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self._upgrade_error = upgrade_error

    def upgrade(self, config: Any, revision: str) -> None:
        self.calls.append(f"upgrade:{revision}")
        if self._upgrade_error is not None:
            raise self._upgrade_error

    def downgrade(self, config: Any, revision: str) -> None:
        self.calls.append(f"downgrade:{revision}")


def fake_target() -> Any:
    """生成器只读取 ``url`` 与 ``database_name``；DSN 显式不可达且不含密码。"""

    return SimpleNamespace(
        url=f"postgresql+psycopg://citemind_upload@127.0.0.1:1/{TEST_DATABASE_NAME}",
        database_name=TEST_DATABASE_NAME,
    )


def patch_schema_environment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    engine: FakeEngine,
    recorder: CommandRecorder,
    revision: Callable[[Any], str | None],
    tables: Callable[[Any], set[str]],
) -> None:
    monkeypatch.setattr(upload_flow, "create_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(upload_flow, "alembic_config", lambda _url: ALEMBIC_CONFIG_SENTINEL)
    monkeypatch.setattr(upload_flow, "command", recorder)
    monkeypatch.setattr(upload_flow, "alembic_revision", revision)
    monkeypatch.setattr(upload_flow, "business_tables", tables)


def test_dirty_precondition_never_downgrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """库中已有业务表时前置断言失败，拒不声明所有权，也不发出任何 downgrade。"""

    engine = FakeEngine(database_name=TEST_DATABASE_NAME)
    recorder = CommandRecorder()
    patch_schema_environment(
        monkeypatch,
        engine=engine,
        recorder=recorder,
        revision=lambda connection: None,
        tables=lambda connection: {"leftover_business_table"},
    )

    generator = upload_flow.open_upload_schema(fake_target())
    with pytest.raises(AssertionError):
        next(generator)

    assert recorder.calls == []
    assert engine.disposed is True


def test_owned_schema_failure_still_downgrades_to_base_and_disposes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已声明所有权后升级失败，仍降回 base（best-effort）并 dispose。"""

    engine = FakeEngine(database_name=TEST_DATABASE_NAME)
    recorder = CommandRecorder(upgrade_error=RuntimeError("升级中断"))
    patch_schema_environment(
        monkeypatch,
        engine=engine,
        recorder=recorder,
        revision=lambda connection: None,
        tables=lambda connection: set(),
    )

    generator = upload_flow.open_upload_schema(fake_target())
    with pytest.raises(RuntimeError, match="升级中断"):
        next(generator)

    assert recorder.calls == [
        f"upgrade:{upload_flow.SCHEMA_REVISION}",
        "downgrade:base",
    ]
    assert engine.disposed is True