"""默认 index profile 幂等登记的真实 PostgreSQL 验收。

覆盖：登记后按 golden ``config_hash`` 落一行、重复调用复用同一行、同哈希但字段被篡改时
显式冲突、并发登记在唯一索引上真实阻塞（由迁移角色在 ``pg_stat_activity`` 观测等待）后复用
已提交行、并发先回滚时后来者插入自己的行、调用方回滚不落行且无脏读、``autoflush=True``
下函数不替调用方 flush 待写行、worker 角色无 INSERT 权限，以及登记完全不改
``knowledge_base.active_index_profile_id``。

只使用 ``_test`` 结尾且显式 opt-in 的破坏性测试库与三个角色 DSN 守卫；缺少守卫 DSN 时按
既有契约跳过（绝不把跳过当通过）。setup 在干净空库上迁移到当前 head，teardown 降回 base
并断言无残留，不触碰任何开发库。
"""

import asyncio
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command
from database_guard import DestructiveTestDatabase
from database_roles_guard import API_ROLE, WORKER_ROLE, RoleTestDatabases
from rag_backend.database import create_session_factory
from rag_backend.ingestion.profile_repository import (
    IndexProfileConflictError,
    ensure_default_index_profile,
)
from rag_backend.models.indexing import IndexProfile
from rag_backend.models.profile_contract import default_index_profile
from sqlalchemy import Engine, create_engine, func, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from test_core_migration import (
    alembic_config,
    alembic_revision,
    assert_statement_denied,
    business_tables,
    role_grants,
)

pytestmark = pytest.mark.integration

HEAD_REVISION = "20260923_0005"
GOLDEN_CONFIG_HASH = "4af4c33d4e8d5571cc513dc8c623b1fe66a5565683f95fc75a7b3f8a28dc57fa"

INSERT_PROFILE_SQL = (
    "INSERT INTO index_profile (id, embedding_model, model_revision, dimension, normalize, "
    "tokenizer_revision, chunker_version, keyword_analyzer_version, config_hash) "
    "VALUES (:id, :embedding_model, :model_revision, :dimension, :normalize, "
    ":tokenizer_revision, :chunker_version, :keyword_analyzer_version, :config_hash)"
)

# 观测是否有后端正阻塞在 ``ON CONFLICT`` 的唯一索引等待上。只匹配以 INSERT 开头的语句，
# 避免把观测查询自身（以 SELECT 开头）算进去。
LOCK_WAITER_QUERY = text(
    "SELECT count(*) FROM pg_stat_activity "
    "WHERE datname = current_database() "
    "AND wait_event_type = 'Lock' "
    "AND state = 'active' "
    "AND query LIKE 'INSERT INTO index_profile%'"
)


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, Any]]:
    # Windows 默认 ProactorEventLoop 不能用于 psycopg 异步连接；与生产入口一致。
    return ("asyncio", {"loop_factory": asyncio.SelectorEventLoop})


@pytest.fixture(scope="module")
def profile_schema(
    destructive_test_database: DestructiveTestDatabase,
    role_test_databases: RoleTestDatabases,
) -> Iterator[Engine]:
    """在空测试库上迁移到 head，测试结束后降回 base 并断言无残留。"""

    assert role_test_databases.database_name == destructive_test_database.database_name
    config = alembic_config(destructive_test_database.url)
    engine = create_engine(destructive_test_database.url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT current_database()"))
                == destructive_test_database.database_name
            )
            assert alembic_revision(connection) is None
            assert business_tables(connection) == set()

        command.upgrade(config, HEAD_REVISION)
        yield engine
    finally:
        command.downgrade(config, "base")
        with engine.connect() as connection:
            assert alembic_revision(connection) is None
            assert business_tables(connection) == set()
        engine.dispose()


def _delete_registered_state(engine: Engine) -> None:
    """用迁移角色清理本测试写入的行；只作用于隔离测试库。"""

    with engine.begin() as connection:
        connection.execute(text("DELETE FROM knowledge_base"))
        connection.execute(text("DELETE FROM index_profile"))


@pytest.fixture
def clean_profile_state(profile_schema: Engine) -> Iterator[None]:
    _delete_registered_state(profile_schema)
    yield
    _delete_registered_state(profile_schema)


def _api_engine(databases: RoleTestDatabases) -> AsyncEngine:
    return create_async_engine(databases.api_url, pool_pre_ping=True)


async def _wait_for_lock_waiter(engine: Engine, *, timeout_seconds: float = 5.0) -> int:
    """在有界时间内等迁移角色观测到阻塞的 INSERT 后端；不无限轮询。"""

    deadline = time.monotonic() + timeout_seconds
    while True:
        with engine.connect() as connection:
            waiters = int(connection.scalar(LOCK_WAITER_QUERY) or 0)
        if waiters > 0:
            return waiters
        if time.monotonic() >= deadline:
            return 0
        await asyncio.sleep(0.05)


@pytest.mark.anyio
async def test_registers_default_profile_and_reuses_existing_row(
    clean_profile_state: None, profile_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    contract = default_index_profile()
    engine = _api_engine(role_test_databases)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            first_id = await ensure_default_index_profile(session)
            await session.commit()
        async with factory() as session:
            second_id = await ensure_default_index_profile(session)
            await session.commit()
    finally:
        await engine.dispose()

    assert first_id == second_id
    with profile_schema.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, embedding_model, model_revision, dimension, normalize, "
                "tokenizer_revision, chunker_version, keyword_analyzer_version, config_hash "
                "FROM index_profile"
            )
        ).all()

    assert len(rows) == 1
    row = rows[0]
    assert row[0] == first_id
    assert row[1] == contract.embedding_model
    assert row[2] == contract.model_revision
    assert row[3] == contract.dimension
    assert row[4] == contract.normalize
    assert row[5] == contract.tokenizer_revision
    assert row[6] == contract.chunker_version
    assert row[7] == contract.keyword_analyzer_version
    assert row[8] == GOLDEN_CONFIG_HASH == contract.config_hash()


@pytest.mark.anyio
async def test_tampered_row_with_same_hash_is_rejected(
    clean_profile_state: None, profile_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    contract = default_index_profile()
    tampered_id = uuid.uuid4()
    with profile_schema.begin() as connection:
        connection.execute(
            text(INSERT_PROFILE_SQL),
            {
                "id": tampered_id,
                "embedding_model": "tampered/model",
                "model_revision": contract.model_revision,
                "dimension": contract.dimension,
                "normalize": contract.normalize,
                "tokenizer_revision": contract.tokenizer_revision,
                "chunker_version": contract.chunker_version,
                "keyword_analyzer_version": contract.keyword_analyzer_version,
                "config_hash": contract.config_hash(),
            },
        )

    engine = _api_engine(role_test_databases)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            with pytest.raises(IndexProfileConflictError):
                await ensure_default_index_profile(session)
            await session.rollback()
    finally:
        await engine.dispose()

    # 原来那行不被改写，也没有新增第二行。
    with profile_schema.connect() as connection:
        rows = connection.execute(
            text("SELECT id, embedding_model, config_hash FROM index_profile")
        ).all()
    assert len(rows) == 1
    assert rows[0][0] == tampered_id
    assert rows[0][1] == "tampered/model"
    assert rows[0][2] == GOLDEN_CONFIG_HASH


@pytest.mark.anyio
async def test_rollback_leaves_no_row_and_no_dirty_read(
    clean_profile_state: None, profile_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    engine = _api_engine(role_test_databases)
    factory = create_session_factory(engine)
    writer = factory()
    try:
        await ensure_default_index_profile(writer)

        # writer 未提交：另一 session 在 READ COMMITTED 下不得看到未提交行。
        async with factory() as reader:
            assert await reader.scalar(text("SELECT count(*) FROM index_profile")) == 0

        await writer.rollback()

        async with factory() as reader:
            assert await reader.scalar(text("SELECT count(*) FROM index_profile")) == 0
    finally:
        await writer.close()
        await engine.dispose()


@pytest.mark.anyio
async def test_ensure_does_not_flush_caller_pending_profile(
    clean_profile_state: None, profile_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    """``autoflush=True`` 下函数仍须抑制 flush，待写行保持 pending 且对本事务不可见。"""

    engine = _api_engine(role_test_databases)
    # 生产工厂是 autoflush=False；这里用 autoflush=True 才能证明函数内部真的抑制了 flush。
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=True)
    pending_hash = uuid.uuid4().hex
    session = factory()
    try:
        pending = IndexProfile(
            id=uuid.uuid4(),
            embedding_model="pending/model",
            model_revision="pending-revision",
            dimension=512,
            normalize=True,
            tokenizer_revision="pending-tokenizer",
            chunker_version="pending-chunker",
            keyword_analyzer_version="pending-keyword",
            config_hash=pending_hash,
        )
        session.add(pending)
        assert inspect(pending).pending

        await ensure_default_index_profile(session)

        # 函数没有替调用方 flush 待写行。
        assert inspect(pending).pending
        assert pending in session.new

        # 同一 session 内、抑制 autoflush 时待写行不可见；显式 flush 后才可见。
        with session.no_autoflush:
            hidden = await session.scalar(
                select(func.count())
                .select_from(IndexProfile)
                .where(IndexProfile.config_hash == pending_hash)
            )
        assert hidden == 0

        await session.flush()
        visible = await session.scalar(
            select(func.count())
            .select_from(IndexProfile)
            .where(IndexProfile.config_hash == pending_hash)
        )
        assert visible == 1
        await session.rollback()
    finally:
        await session.close()
        await engine.dispose()


@pytest.mark.anyio
async def test_concurrent_registration_blocks_then_reuses_committed_row(
    clean_profile_state: None, profile_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    engine = _api_engine(role_test_databases)
    factory = create_session_factory(engine)
    session_a = factory()
    session_b = factory()
    try:
        first_id = await ensure_default_index_profile(session_a)
        # B 的 ON CONFLICT DO NOTHING 会在 A 未提交的唯一索引上等待；A 提交后才继续。
        task_b = asyncio.create_task(ensure_default_index_profile(session_b))
        assert await _wait_for_lock_waiter(profile_schema) >= 1, "未观测到阻塞的唯一索引等待"
        assert not task_b.done()

        await session_a.commit()
        second_id = await asyncio.wait_for(task_b, timeout=10)
        await session_b.commit()
    finally:
        await session_a.close()
        await session_b.close()
        await engine.dispose()

    assert first_id == second_id
    with profile_schema.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM index_profile")) == 1


@pytest.mark.anyio
async def test_concurrent_registration_after_rollback_inserts_own_row(
    clean_profile_state: None, profile_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    engine = _api_engine(role_test_databases)
    factory = create_session_factory(engine)
    session_a = factory()
    session_b = factory()
    try:
        rolled_back_id = await ensure_default_index_profile(session_a)
        task_b = asyncio.create_task(ensure_default_index_profile(session_b))
        assert await _wait_for_lock_waiter(profile_schema) >= 1, "未观测到阻塞的唯一索引等待"
        assert not task_b.done()

        # A 回滚释放唯一索引上的等待；B 不再冲突，插入并使用自己生成的主键。
        await session_a.rollback()

        second_id = await asyncio.wait_for(task_b, timeout=10)
        await session_b.commit()
    finally:
        await session_a.close()
        await session_b.close()
        await engine.dispose()

    assert second_id != rolled_back_id
    with profile_schema.connect() as connection:
        rows = connection.execute(text("SELECT id FROM index_profile")).all()
    assert [row[0] for row in rows] == [second_id]


def test_worker_role_cannot_insert_into_index_profile(
    profile_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    assert_statement_denied(
        role_test_databases.worker_url,
        "INSERT INTO index_profile (id, embedding_model, model_revision, dimension, normalize, "
        "tokenizer_revision, chunker_version, keyword_analyzer_version, config_hash) "
        "VALUES ('00000000-0000-0000-0000-000000000099', 'm', 'r', 512, true, 't', 'c', 'k', 'h')",
    )


def test_index_profile_grants_admit_only_select_and_insert_for_api(
    profile_schema: Engine,
) -> None:
    with profile_schema.connect() as connection:
        assert role_grants(connection, "index_profile", API_ROLE) == {"SELECT", "INSERT"}
        assert role_grants(connection, "index_profile", WORKER_ROLE) == {"SELECT"}


@pytest.mark.anyio
async def test_registration_does_not_touch_kb_active_pointer(
    clean_profile_state: None, profile_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    kb_id = uuid.uuid4()
    with profile_schema.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO knowledge_base (id, organization_id, name) "
                "VALUES (:id, :organization_id, 'kb')"
            ),
            {"id": kb_id, "organization_id": uuid.uuid4()},
        )

    engine = _api_engine(role_test_databases)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await ensure_default_index_profile(session)
            await session.commit()
    finally:
        await engine.dispose()

    with profile_schema.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT active_index_profile_id FROM knowledge_base WHERE id = :id"),
                {"id": kb_id},
            )
            is None
        )
        assert connection.scalar(text("SELECT count(*) FROM index_profile")) == 1
