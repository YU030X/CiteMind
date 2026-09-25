"""默认 index profile 幂等登记模块的聚焦单测：不连接数据库。

这些测试只验证语句形状、七个契约字段与 golden ``config_hash``，以及用假 session 覆盖
插入成功 / 复用既有行 / 字段冲突 / 冲突后缺行的控制流，并断言函数绝不提交或回滚事务。
真实唯一约束、并发与角色授权由 ``tests/integration/test_profile_repository.py`` 在隔离
测试库上验收。

文件名与集成测试不同，避免 mypy 在无 ``__init__.py`` 的测试目录下把同名模块判为重复。
"""

from __future__ import annotations

import dataclasses
import uuid
from contextlib import AbstractContextManager, nullcontext
from typing import cast

import pytest
from rag_backend.ingestion import profile_repository as repository
from rag_backend.ingestion.profile_repository import (
    CONTRACT_FIELDS,
    IndexProfileConflictError,
    IndexProfileNotFoundError,
    IndexProfileRegistrationError,
    ensure_default_index_profile,
)
from rag_backend.models.indexing import IndexProfile
from rag_backend.models.profile_contract import IndexProfileContract, default_index_profile
from sqlalchemy import ClauseElement
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

# 一次算出后冻结的 golden；与 tests/unit/test_profile_contract.py 一致。
GOLDEN_CONFIG_HASH = "4af4c33d4e8d5571cc513dc8c623b1fe66a5565683f95fc75a7b3f8a28dc57fa"


def _compiled_sql(statement: ClauseElement) -> str:
    """用 PostgreSQL dialect 编译语句，只用于断言 SQL 形状（不连库）。"""

    # postgresql.dialect() 在 SQLAlchemy 类型标注里是无签名的构造器。
    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    return str(statement.compile(dialect=dialect))


class _FakeResult:
    """只支持标量读取的结果替身。"""

    def __init__(self, value: object | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object | None:
        return self._value


class _FakeSession:
    """按顺序返回预设结果的 AsyncSession 替身；记录语句与 commit/rollback 调用。"""

    def __init__(self, results: list[object | None]) -> None:
        self._results = list(results)
        self.statements: list[object] = []
        self.commit_calls = 0
        self.rollback_calls = 0
        self.no_autoflush_entered = False

    async def execute(self, statement: object, *args: object, **kwargs: object) -> _FakeResult:
        self.statements.append(statement)
        return _FakeResult(self._results.pop(0))

    @property
    def no_autoflush(self) -> AbstractContextManager[None]:
        self.no_autoflush_entered = True
        return nullcontext()

    async def commit(self) -> None:
        self.commit_calls += 1

    async def rollback(self) -> None:
        self.rollback_calls += 1


def _existing_profile(*, embedding_model: str | None = None) -> IndexProfile:
    """构造一行与默认契约一致的（或指定字段被篡改的）``IndexProfile``。"""

    contract = default_index_profile()
    return IndexProfile(
        id=uuid.uuid4(),
        embedding_model=contract.embedding_model if embedding_model is None else embedding_model,
        model_revision=contract.model_revision,
        dimension=contract.dimension,
        normalize=contract.normalize,
        tokenizer_revision=contract.tokenizer_revision,
        chunker_version=contract.chunker_version,
        keyword_analyzer_version=contract.keyword_analyzer_version,
        config_hash=contract.config_hash(),
    )


def test_contract_fields_derive_from_contract_dataclass_and_cover_model() -> None:
    # 生产从 frozen dataclass 派生，而非手写七字面量；这里同时锁定两者一致。
    assert CONTRACT_FIELDS == tuple(
        field.name for field in dataclasses.fields(IndexProfileContract)
    )
    assert len(CONTRACT_FIELDS) == 7
    assert "config_hash" not in CONTRACT_FIELDS
    assert "schema_version" not in CONTRACT_FIELDS

    # 七个契约字段必须都落在 ``index_profile`` 表的列上。
    columns = set(IndexProfile.__table__.columns.keys())
    assert set(CONTRACT_FIELDS) <= columns
    assert {"id", "config_hash", "created_at"} <= columns


def test_insert_values_cover_id_config_hash_and_seven_fields() -> None:
    profile_id = uuid.uuid4()
    values = repository._insert_values(default_index_profile(), profile_id)

    assert set(values) == {"id", "config_hash", *CONTRACT_FIELDS}
    assert values["id"] == profile_id
    assert values["config_hash"] == GOLDEN_CONFIG_HASH


def test_insert_statement_conflicts_on_config_hash_and_returns_id() -> None:
    values = repository._insert_values(default_index_profile(), uuid.uuid4())
    sql = _compiled_sql(repository._insert_statement(values))

    assert "INSERT INTO index_profile" in sql
    assert "ON CONFLICT (config_hash) DO NOTHING" in sql
    assert "RETURNING index_profile.id" in sql
    assert "DO UPDATE" not in sql


def test_reload_statement_filters_by_config_hash() -> None:
    sql = _compiled_sql(repository._select_by_config_hash(GOLDEN_CONFIG_HASH))

    assert "FROM index_profile" in sql
    assert "index_profile.config_hash = " in sql


@pytest.mark.anyio
async def test_insert_returns_new_id_without_commit_or_rollback() -> None:
    new_id = uuid.uuid4()
    session = _FakeSession([new_id])

    returned = await ensure_default_index_profile(cast(AsyncSession, session))

    assert returned == new_id
    assert session.commit_calls == 0
    assert session.rollback_calls == 0
    assert session.no_autoflush_entered
    # 只执行了一条插入语句，未发生冲突后重读。
    assert len(session.statements) == 1


@pytest.mark.anyio
async def test_existing_matching_row_is_reused_without_commit() -> None:
    existing = _existing_profile()
    session = _FakeSession([None, existing])

    returned = await ensure_default_index_profile(cast(AsyncSession, session))

    assert returned == existing.id
    assert session.commit_calls == 0
    assert session.rollback_calls == 0
    # 一条插入 + 一条按 config_hash 的重新 SELECT。
    assert len(session.statements) == 2


@pytest.mark.anyio
async def test_tampered_row_with_same_hash_raises_conflict() -> None:
    session = _FakeSession([None, _existing_profile(embedding_model="other/model")])

    with pytest.raises(IndexProfileConflictError):
        await ensure_default_index_profile(cast(AsyncSession, session))

    assert session.commit_calls == 0


@pytest.mark.anyio
async def test_conflict_without_readable_row_raises_explicit_failure() -> None:
    session = _FakeSession([None, None])

    with pytest.raises(IndexProfileNotFoundError):
        await ensure_default_index_profile(cast(AsyncSession, session))


def test_registration_errors_share_a_common_base() -> None:
    assert issubclass(IndexProfileConflictError, IndexProfileRegistrationError)
    assert issubclass(IndexProfileNotFoundError, IndexProfileRegistrationError)
    assert issubclass(IndexProfileRegistrationError, RuntimeError)
