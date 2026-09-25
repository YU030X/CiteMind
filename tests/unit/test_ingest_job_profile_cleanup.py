"""``ingest_job.profile_id`` 迁移 fixture 清理路径的纯逻辑检查：不连接数据库。

用假 engine 与 monkeypatch 驱动真实的 schema 生成器
``open_ingest_job_profile_schema``，证明两条清理安全性质：

1. 前置条件不干净（public schema 已有业务表）时不声明所有权，绝不做任何 downgrade；
2. 降回 0005 后的结构核对抛 ``AssertionError`` 时仍会尝试降回 base、继续核对 base，
   且 engine 始终被 dispose。

真实 PostgreSQL 上的升级/降级语义由同一文件的集成用例覆盖。
"""

import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

# 集成 fixture 与单元测试同属 tests 但不同目录且都不是包，显式加入 sys.path 后再导入。
sys.path.insert(0, str(Path(__file__).parents[1] / "integration"))

import test_ingest_job_profile_migration as migration  # noqa: E402

TEST_DATABASE_NAME = "citemind_test"
ALEMBIC_CONFIG_SENTINEL = object()


class FakeEngine:
    """只实现清理路径需要的最小 engine 接口。"""

    def __init__(self, *, database_name: str) -> None:
        self.database_name = database_name
        self.disposed = False

    def connect(self) -> "FakeConnection":
        return FakeConnection(self)

    def begin(self) -> Any:
        raise AssertionError("单元测试不应通过 engine 写库")

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
    """替代 alembic ``command``：只记录 upgrade/downgrade 调用顺序。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def upgrade(self, config: Any, revision: str) -> None:
        self.calls.append(f"upgrade:{revision}")

    def downgrade(self, config: Any, revision: str) -> None:
        self.calls.append(f"downgrade:{revision}")


def fake_target() -> Any:
    """生成器只读取 ``url`` 与 ``database_name``，其余字段不参与。"""

    return SimpleNamespace(
        url=f"postgresql+psycopg://citemind_migrator@127.0.0.1:55432/{TEST_DATABASE_NAME}",
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
    monkeypatch.setattr(migration, "create_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(migration, "alembic_config", lambda _url: ALEMBIC_CONFIG_SENTINEL)
    monkeypatch.setattr(migration, "command", recorder)
    monkeypatch.setattr(migration, "alembic_revision", revision)
    monkeypatch.setattr(migration, "business_tables", tables)
    monkeypatch.setattr(
        migration, "ingest_job_columns", lambda _connection: {"generation_id"}
    )


def test_dirty_precondition_never_downgrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """库中已有业务表时前置断言失败，且不发出任何 downgrade。"""

    engine = FakeEngine(database_name=TEST_DATABASE_NAME)
    recorder = CommandRecorder()
    patch_schema_environment(
        monkeypatch,
        engine=engine,
        recorder=recorder,
        revision=lambda connection: None,
        tables=lambda connection: {"leftover_business_table"},
    )

    generator = migration.open_ingest_job_profile_schema(fake_target())
    with pytest.raises(AssertionError):
        next(generator)

    assert recorder.calls == []
    assert engine.disposed is True


def test_failed_previous_verification_still_downgrades_to_base_and_disposes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """降回 0005 后的核对失败不阻止降回 base，dispose 仍然执行。"""

    engine = FakeEngine(database_name=TEST_DATABASE_NAME)
    recorder = CommandRecorder()
    revisions = iter([None, migration.IDENTITY_REVISION])
    tables = iter([set(), set(migration.ALL_TABLES)])
    legacy = migration.LegacyJob(
        organization_id=uuid.uuid4(),
        kb_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
    )
    verify_calls: list[str] = []

    def fail_identity_verification(_engine: Any, _legacy: Any) -> None:
        verify_calls.append("identity")
        raise AssertionError("降回 0005 后的结构核对失败")

    patch_schema_environment(
        monkeypatch,
        engine=engine,
        recorder=recorder,
        revision=lambda connection: next(revisions),
        tables=lambda connection: next(tables),
    )
    monkeypatch.setattr(migration, "seed_legacy_job", lambda _engine: legacy)
    monkeypatch.setattr(migration, "verify_identity_stage", fail_identity_verification)
    monkeypatch.setattr(
        migration, "verify_base_stage", lambda _engine: verify_calls.append("base")
    )

    generator = migration.open_ingest_job_profile_schema(fake_target())
    # 生成器签名声明真实 Engine；这里用假 engine 驱动，仅比较 identity。
    assert next(generator) == (cast(Any, engine), legacy)

    with pytest.raises(AssertionError, match="核对失败"):
        next(generator)

    assert verify_calls == ["identity", "base"]
    assert recorder.calls == [
        f"upgrade:{migration.IDENTITY_REVISION}",
        f"upgrade:{migration.INGEST_JOB_PROFILE_REVISION}",
        f"downgrade:{migration.IDENTITY_REVISION}",
        "downgrade:base",
    ]
    assert engine.disposed is True