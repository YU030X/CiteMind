"""dispatcher 核心的真实 PostgreSQL 验收（publisher 为 fake，不连接 Redis）。

覆盖只有在真实数据库里才成立的语义：``FOR UPDATE SKIP LOCKED`` 领取竞争、租约 token 的
CAS 回写（迟到旧发送者不能覆盖新领取者）、补偿的有界补投与重复避免、未知事件类型落 FAILED
并写 job.error_code、worker 接收 marker 的行锁写入，以及投递使用专用队列与 ``outbox.id``
作为 task id。这里不验证 Redis 发送成功/失败：真正的 broker 链路与「broker 已接受但 mark_sent
事务回滚」故障注入见 ``test_dispatcher_broker.py``。所有写入只发生在守卫放行的 ``_test`` 库，
并在每个用例前清空。
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from alembic import command
from database_guard import DestructiveTestDatabase
from database_roles_guard import RoleTestDatabases
from rag_backend.database import (
    SessionFactory,
    create_session_factory,
    create_sync_session_factory,
)
from rag_backend.dispatch import protocol
from rag_backend.dispatch.repository import (
    CLAIM_SQL,
    ClaimedOutboxEvent,
    SqlOutboxRepository,
)
from rag_backend.dispatch.service import (
    DispatchOutcome,
    OutboxDispatcher,
    compensate_unconfirmed_jobs,
    recover_expired_pipeline_jobs,
)
from rag_backend.ingestion import indexing_worker as iw
from rag_backend.ingestion.parsing import MARKDOWN_PARSER_VERSION
from rag_backend.ingestion.profile_repository import ensure_default_index_profile
from rag_backend.worker import (
    RECEIVE_STATUS_ALREADY_RECEIVED,
    RECEIVE_STATUS_DELETED,
    RECEIVE_STATUS_NOT_QUEUED,
    RECEIVE_STATUS_RECEIVED,
    receive_ingest_event,
)
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from test_core_migration import alembic_config, alembic_revision, business_tables

pytestmark = pytest.mark.integration

# 上传受理与 worker 接收所需的 ``ingest_job.profile_id`` 由 ``20260925_0006`` 新增，
# 因此本模块钉在含该列的线性 schema 上；每个模块独立运行，不与 0005 混用。
SCHEMA_REVISION = "20260925_0006"
# 旧占位解析器版本；仅显式构造旧任务属性时使用，新接收壳会静态拒绝。
LEGACY_PARSER_VERSION = "markdown-v1"

BUSINESS_TABLES_CLEANUP = (
    "TRUNCATE outbox_event, ingest_job, document_version, document, knowledge_base CASCADE"
)


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, Any]]:
    # Windows 默认 ProactorEventLoop 不能用于 psycopg 异步连接；与生产入口一致。
    return ("asyncio", {"loop_factory": asyncio.SelectorEventLoop})


@pytest.fixture(scope="module")
def dispatcher_schema(destructive_test_database: DestructiveTestDatabase) -> Iterator[Engine]:
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
        command.upgrade(config, SCHEMA_REVISION)
        yield engine
    finally:
        command.downgrade(config, "base")
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT current_database()"))
                == destructive_test_database.database_name
            )
            assert alembic_revision(connection) is None
            assert business_tables(connection) == set()
        engine.dispose()


@pytest.fixture(autouse=True)
def clean_business_rows(dispatcher_schema: Engine) -> Iterator[None]:
    with dispatcher_schema.begin() as connection:
        connection.execute(text(BUSINESS_TABLES_CLEANUP))
    yield


@asynccontextmanager
async def api_sessions(database_url: str) -> AsyncIterator[SessionFactory]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


async def insert_job(
    session: AsyncSession,
    *,
    status: str = "QUEUED",
    error_code: str | None = None,
    receive_marker: bool = False,
    document_deleted: bool = False,
    legacy: bool = False,
) -> uuid.UUID:
    """按外键顺序写入 KB/document/version/job；只使用 API 角色被授予的 DML。

    默认构造新接收壳可处理的 job：同一事务内登记/复用默认全局 profile 并绑定到
    ``ingest_job.profile_id``，parser 用真实实现版本。``legacy=True`` 显式构造 0005 时代
    属性（``profile_id`` NULL 且 parser 为旧占位），供只关心任务属性的 claim/补偿用例使用。
    """

    kb_id = uuid.uuid4()
    document_id = uuid.uuid4()
    version_id = uuid.uuid4()
    job_id = uuid.uuid4()
    profile_id: uuid.UUID | None = None
    parser_version = MARKDOWN_PARSER_VERSION
    if legacy:
        parser_version = LEGACY_PARSER_VERSION
    else:
        profile_id = await ensure_default_index_profile(session)
    await session.execute(
        text(
            "INSERT INTO knowledge_base (id, organization_id, name) "
            "VALUES (:id, :organization_id, :name)"
        ),
        {"id": kb_id, "organization_id": uuid.uuid4(), "name": "kb"},
    )
    deleted_sql = "now()" if document_deleted else "NULL"
    await session.execute(
        text(
            "INSERT INTO document (id, kb_id, title, source_type, lifecycle_status, deleted_at) "
            f"VALUES (:id, :kb_id, :title, 'markdown', 'CREATED', {deleted_sql})"
        ),
        {"id": document_id, "kb_id": kb_id, "title": "doc"},
    )
    await session.execute(
        text(
            "INSERT INTO document_version "
            "(id, document_id, version_no, file_ref, file_hash, mime, parser_version, status) "
            "VALUES (:id, :document_id, 1, 'ref', 'hash', 'text/markdown', :parser_version, "
            "'PENDING')"
        ),
        {
            "id": version_id,
            "document_id": document_id,
            "parser_version": parser_version,
        },
    )
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_until_sql = "NULL"
    heartbeat_sql = "NULL"
    if receive_marker:
        lease_owner = f"event:{uuid.uuid4()}"
        lease_token = "1"
        lease_until_sql = "now() + interval '1 hour'"
        heartbeat_sql = "now()"
    await session.execute(
        text(
            "INSERT INTO ingest_job "
            "(id, document_id, version_id, profile_id, status, attempt, next_run_at, "
            " dedupe_key, error_code, lease_owner, lease_token, lease_until, heartbeat_at) "
            "VALUES (:id, :document_id, :version_id, :profile_id, :status, 0, now(), "
            f" :dedupe_key, :error_code, :lease_owner, :lease_token, {lease_until_sql}, "
            f"{heartbeat_sql})"
        ),
        {
            "id": job_id,
            "document_id": document_id,
            "version_id": version_id,
            "profile_id": profile_id,
            "status": status,
            "dedupe_key": uuid.uuid4().hex,
            "error_code": error_code,
            "lease_owner": lease_owner,
            "lease_token": lease_token,
        },
    )
    await session.commit()
    return job_id


async def insert_event(
    session: AsyncSession,
    job_id: uuid.UUID,
    *,
    status: str = "PENDING",
    event_type: str = protocol.INGEST_REQUESTED_EVENT_TYPE,
    dispatch_attempt: int = 0,
    send_offset_seconds: int = 0,
) -> uuid.UUID:
    event_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO outbox_event "
            "(id, job_id, event_type, status, dispatch_attempt, next_send_at) "
            "VALUES (:id, :job_id, :event_type, :status, :dispatch_attempt, "
            "now() + (:send_offset_seconds * interval '1 second'))"
        ),
        {
            "id": event_id,
            "job_id": job_id,
            "event_type": event_type,
            "status": status,
            "dispatch_attempt": dispatch_attempt,
            "send_offset_seconds": send_offset_seconds,
        },
    )
    await session.commit()
    return event_id


@pytest.mark.anyio
async def test_claim_uses_skip_locked_under_contention(
    role_test_databases: RoleTestDatabases, dispatcher_schema: Engine
) -> None:
    async with api_sessions(role_test_databases.api_url) as factory:
        async with factory() as setup:
            job_id = await insert_job(setup)
            first = await insert_event(setup, job_id)
            second = await insert_event(setup, job_id)

        async with factory() as session_a, factory() as session_b:
            claim_a = (
                await session_a.execute(
                    CLAIM_SQL, {"owner": "dispatcher:a", "lease_seconds": 60}
                )
            ).first()
            assert claim_a is not None
            # session_a 持有该行锁不提交；session_b 的 SKIP LOCKED 应跳过它领取另一行。
            claim_b = (
                await session_b.execute(
                    CLAIM_SQL, {"owner": "dispatcher:b", "lease_seconds": 60}
                )
            ).first()
            assert claim_b is not None
            assert claim_a[0] != claim_b[0]
            assert {claim_a[0], claim_b[0]} == {first, second}
            await session_a.rollback()
            await session_b.rollback()


@pytest.mark.anyio
async def test_stale_lease_writeback_is_rejected_by_token_cas(
    role_test_databases: RoleTestDatabases, dispatcher_schema: Engine
) -> None:
    async with api_sessions(role_test_databases.api_url) as factory:
        async with factory() as setup:
            job_id = await insert_job(setup)
            event_id = await insert_event(setup, job_id)

        async with factory() as session:
            repo = SqlOutboxRepository(session)
            stale = await repo.claim_due_event(owner="dispatcher:old")
            assert stale is not None
            assert stale.event_id == event_id
            assert stale.lease_token == protocol.lease_token_for(1)

            # 让旧租约过期，新 dispatcher 重新领取同一事件。
            await session.execute(
                text(
                    "UPDATE outbox_event SET lease_until = now() - interval '1 second' "
                    "WHERE id = :id"
                ),
                {"id": event_id},
            )
            await session.commit()
            fresh = await repo.claim_due_event(owner="dispatcher:new")
            assert fresh is not None
            assert fresh.dispatch_attempt == stale.dispatch_attempt + 1
            assert fresh.lease_token != stale.lease_token

            # 迟到旧发送者与错误 token 的回写都被 CAS 拒绝。
            assert await repo.mark_sent(stale) is False
            wrong_token = ClaimedOutboxEvent(
                event_id=fresh.event_id,
                job_id=fresh.job_id,
                event_type=fresh.event_type,
                dispatch_attempt=fresh.dispatch_attempt,
                lease_owner=fresh.lease_owner,
                lease_token="not-the-token",
                lease_until=fresh.lease_until,
            )
            assert await repo.mark_sent(wrong_token) is False
            row = (
                await session.execute(
                    text(
                        "SELECT status, lease_owner, lease_token FROM outbox_event "
                        "WHERE id = :id"
                    ),
                    {"id": event_id},
                )
            ).first()
            assert row is not None
            assert row[0] == "PENDING"
            assert row[1] == "dispatcher:new"
            assert row[2] == fresh.lease_token

            # 当前持有者回写成功。
            assert await repo.mark_sent(fresh) is True
            row = (
                await session.execute(
                    text(
                        "SELECT status, sent_at, lease_owner, lease_token, lease_until "
                        "FROM outbox_event WHERE id = :id"
                    ),
                    {"id": event_id},
                )
            ).first()
            assert row is not None
            assert row[0] == "SENT"
            assert row[1] is not None
            assert row[2] is None and row[3] is None and row[4] is None


@pytest.mark.anyio
async def test_compensation_creates_one_followup_and_does_not_duplicate(
    role_test_databases: RoleTestDatabases, dispatcher_schema: Engine
) -> None:
    async with api_sessions(role_test_databases.api_url) as factory:
        async with factory() as setup:
            job_id = await insert_job(setup)
            await insert_event(setup, job_id, status="SENT")

        async with factory() as session:
            repo = SqlOutboxRepository(session)
            first = await compensate_unconfirmed_jobs(repo)
            assert len(first.created_event_ids) == 1
            assert first.unconfirmed_job_ids == ()

            second = await compensate_unconfirmed_jobs(repo)
            assert second.created_event_ids == ()
            assert second.unconfirmed_job_ids == ()

            count = (
                await session.execute(
                    text("SELECT count(*) FROM outbox_event WHERE job_id = :id"),
                    {"id": job_id},
                )
            ).scalar()
            assert count == 2
            statuses = (
                (
                    await session.execute(
                        text(
                            "SELECT status FROM outbox_event WHERE job_id = :id "
                            "ORDER BY created_at, id"
                        ),
                        {"id": job_id},
                    )
                )
                .scalars()
                .all()
            )
            assert sorted(statuses) == ["PENDING", "SENT"]


@pytest.mark.anyio
async def test_compensation_flags_job_at_cap_without_changing_status(
    role_test_databases: RoleTestDatabases, dispatcher_schema: Engine
) -> None:
    async with api_sessions(role_test_databases.api_url) as factory:
        async with factory() as setup:
            job_id = await insert_job(setup)
            for _ in range(protocol.MAX_DELIVERY_ATTEMPTS):
                await insert_event(setup, job_id, status="SENT")

        async with factory() as session:
            repo = SqlOutboxRepository(session)
            result = await compensate_unconfirmed_jobs(repo)
            assert result.created_event_ids == ()
            assert result.unconfirmed_job_ids == (job_id,)

            row = (
                await session.execute(
                    text("SELECT status, error_code FROM ingest_job WHERE id = :id"),
                    {"id": job_id},
                )
            ).first()
            assert row is not None
            assert row[0] == "QUEUED"
            assert row[1] == protocol.DELIVERY_UNCONFIRMED

            # 已标记的 job 不再被补偿扫描（停止热循环）。
            again = await compensate_unconfirmed_jobs(repo)
            assert again.created_event_ids == ()
            assert again.unconfirmed_job_ids == ()


@pytest.mark.anyio
async def test_compensation_skips_job_with_receive_marker(
    role_test_databases: RoleTestDatabases, dispatcher_schema: Engine
) -> None:
    async with api_sessions(role_test_databases.api_url) as factory:
        async with factory() as setup:
            job_id = await insert_job(setup, receive_marker=True)
            await insert_event(setup, job_id, status="SENT")

        async with factory() as session:
            repo = SqlOutboxRepository(session)
            result = await compensate_unconfirmed_jobs(repo)
            assert result.created_event_ids == ()
            assert result.unconfirmed_job_ids == ()
            count = (
                await session.execute(
                    text("SELECT count(*) FROM outbox_event WHERE job_id = :id"),
                    {"id": job_id},
                )
            ).scalar()
            assert count == 1


class _RecordingPublisher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object], str, str]] = []

    async def publish(
        self,
        *,
        task_name: str,
        payload: dict[str, object],
        task_id: str,
        queue: str,
    ) -> None:
        self.calls.append((task_name, payload, task_id, queue))


@pytest.mark.anyio
async def test_dispatcher_marks_sent_and_uses_ingest_queue(
    role_test_databases: RoleTestDatabases, dispatcher_schema: Engine
) -> None:
    async with api_sessions(role_test_databases.api_url) as factory:
        async with factory() as setup:
            job_id = await insert_job(setup)
            event_id = await insert_event(setup, job_id)

        publisher = _RecordingPublisher()
        dispatcher = OutboxDispatcher(
            session_factory=factory, publisher=publisher, owner="dispatcher:test"
        )
        assert await dispatcher.dispatch_once() is DispatchOutcome.SENT
        assert publisher.calls == [
            (
                protocol.INGEST_TASK_NAME,
                {"protocolVersion": 1, "jobId": str(job_id)},
                str(event_id),
                protocol.INGEST_QUEUE,
            )
        ]

        async with factory() as check:
            row = (
                await check.execute(
                    text(
                        "SELECT status, sent_at, lease_owner, lease_token, lease_until "
                        "FROM outbox_event WHERE id = :id"
                    ),
                    {"id": event_id},
                )
            ).first()
            assert row is not None
            assert row[0] == "SENT"
            assert row[1] is not None
            assert row[2] is None and row[3] is None and row[4] is None


@pytest.mark.anyio
async def test_recent_sent_blocks_compensation_until_grace_elapses(
    role_test_databases: RoleTestDatabases, dispatcher_schema: Engine
) -> None:
    async with api_sessions(role_test_databases.api_url) as factory:
        async with factory() as setup:
            job_id = await insert_job(setup)
            event_id = await insert_event(setup, job_id)

        async with factory() as session:
            repo = SqlOutboxRepository(session)
            claim = await repo.claim_due_event(owner="dispatcher:a")
            assert claim is not None
            assert await repo.mark_sent(claim) is True

        async with factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT j.next_run_at <= now(), e.sent_at "
                        "FROM ingest_job AS j "
                        "JOIN outbox_event AS e ON e.job_id = j.id "
                        "WHERE j.id = :id AND e.id = :event_id"
                    ),
                    {"id": job_id, "event_id": event_id},
                )
            ).first()
            # mark_sent 不再改写 job.next_run_at；接收宽限由 SENT.sent_at 判定。
            assert row is not None and row[0] is True and row[1] is not None

            # 60s 宽限内事件为 SENT 且无 PENDING，补偿不得新建。
            repo = SqlOutboxRepository(session)
            assert (await compensate_unconfirmed_jobs(repo)).created_event_ids == ()

        # 把最近 SENT 的 sent_at 回拨到宽限之前，才允许补投一次。
        async with factory() as session:
            await session.execute(
                text(
                    "UPDATE outbox_event SET sent_at = now() - interval '61 seconds' "
                    "WHERE id = :id"
                ),
                {"id": event_id},
            )
            await session.commit()
            repo = SqlOutboxRepository(session)
            created = (await compensate_unconfirmed_jobs(repo)).created_event_ids
            assert len(created) == 1
            assert created[0] != event_id


# 外部事务持锁时，被阻塞语句必须的有界等待上限；避免用例无限阻塞。
LOCK_WAIT_DEADLINE_SECONDS = 10.0
# 释放行锁后等待被阻塞工作真正结束的上限；避免清理阶段无限阻塞，也避免 engine.dispose
# 与仍在执行的连接竞态。
WORKER_JOIN_TIMEOUT_SECONDS = 10.0


async def _join_thread_bounded(thread: threading.Thread, timeout: float) -> bool:
    """在有界时间内 join 线程；返回线程是否已结束。join 超时不会挂死事件循环。"""

    await asyncio.get_running_loop().run_in_executor(None, thread.join, timeout)
    return not thread.is_alive()


async def _release_lock_and_finish_task(
    locker: AsyncSession, task: asyncio.Task[Any]
) -> str:
    """失败路径清理：先释放持锁事务，再结束被阻塞的 asyncio 任务。

    释放锁失败只能并入返回的 detail，不能抛出去掩盖调用方的 ``pytest.fail`` 诊断。
    """

    detail = f"task_done={task.done()}"
    if task.done() and not task.cancelled():
        detail += f" exc={task.exception()!r}"
    try:
        await locker.rollback()
    except Exception as exc:
        detail += f" rollback_exc={exc!r}"
    if not task.done():
        task.cancel()
    with suppress(BaseException):
        await task
    return detail


async def _release_lock_and_finish_thread(
    locker: AsyncSession, thread: threading.Thread
) -> str:
    """失败路径清理：先释放持锁事务，再真正 join 后台线程。

    ``asyncio`` 侧取消等待不会 join 底层工作线程；这里显式有界 join，保证随后的
    ``engine.dispose()`` 不与仍在执行的连接竞态。释放锁失败并入 detail，不掩盖原诊断。
    """

    detail = f"thread_alive={thread.is_alive()}"
    try:
        await locker.rollback()
    except Exception as exc:
        detail += f" rollback_exc={exc!r}"
    joined = await _join_thread_bounded(thread, WORKER_JOIN_TIMEOUT_SECONDS)
    return f"{detail} joined={joined}"


@pytest.mark.anyio
@pytest.mark.anyio
async def test_compensation_reaches_cap_only_after_grace_cycles(
    role_test_databases: RoleTestDatabases, dispatcher_schema: Engine
) -> None:
    async with api_sessions(role_test_databases.api_url) as factory:
        async with factory() as setup:
            job_id = await insert_job(setup)
            await insert_event(setup, job_id, status="SENT")

        async with factory() as session:
            repo = SqlOutboxRepository(session)
            assert len((await compensate_unconfirmed_jobs(repo)).created_event_ids) == 1
            # 宽限期内不重复补投。
            assert (await compensate_unconfirmed_jobs(repo)).created_event_ids == ()

            for _ in range(protocol.MAX_DELIVERY_ATTEMPTS - 2):
                await session.execute(
                    text(
                        "UPDATE outbox_event SET status = 'SENT' "
                        "WHERE job_id = :id AND status = 'PENDING'"
                    ),
                    {"id": job_id},
                )
                await session.execute(
                    text(
                        "UPDATE ingest_job SET next_run_at = now() - interval '1 second' "
                        "WHERE id = :id"
                    ),
                    {"id": job_id},
                )
                await session.commit()
                assert len((await compensate_unconfirmed_jobs(repo)).created_event_ids) == 1

            await session.execute(
                text(
                    "UPDATE outbox_event SET status = 'SENT' "
                    "WHERE job_id = :id AND status = 'PENDING'"
                ),
                {"id": job_id},
            )
            await session.execute(
                text(
                    "UPDATE ingest_job SET next_run_at = now() - interval '1 second' "
                    "WHERE id = :id"
                ),
                {"id": job_id},
            )
            await session.commit()
            result = await compensate_unconfirmed_jobs(repo)
            assert result.created_event_ids == ()
            assert result.unconfirmed_job_ids == (job_id,)

        async with factory() as check:
            row = (
                await check.execute(
                    text("SELECT status, error_code FROM ingest_job WHERE id = :id"),
                    {"id": job_id},
                )
            ).first()
            assert row is not None
            assert row[0] == "QUEUED"
            assert row[1] == protocol.DELIVERY_UNCONFIRMED


@pytest.mark.anyio
async def test_unknown_event_type_fails_and_marks_job_error_code(
    role_test_databases: RoleTestDatabases, dispatcher_schema: Engine
) -> None:
    async with api_sessions(role_test_databases.api_url) as factory:
        async with factory() as setup:
            job_id = await insert_job(setup)
            event_id = await insert_event(setup, job_id, event_type="mystery.event")

        publisher = _RecordingPublisher()
        dispatcher = OutboxDispatcher(
            session_factory=factory, publisher=publisher, owner="dispatcher:test"
        )
        assert await dispatcher.dispatch_once() is DispatchOutcome.FAILED
        assert publisher.calls == []

        async with factory() as check:
            event_row = (
                await check.execute(
                    text("SELECT status FROM outbox_event WHERE id = :id"),
                    {"id": event_id},
                )
            ).first()
            job_row = (
                await check.execute(
                    text("SELECT status, error_code FROM ingest_job WHERE id = :id"),
                    {"id": job_id},
                )
            ).first()
            assert event_row is not None and event_row[0] == "FAILED"
            assert job_row is not None
            assert job_row[0] == "QUEUED"
            assert job_row[1] == protocol.UNSUPPORTED_EVENT_TYPE


@pytest.mark.anyio
async def test_expired_lease_with_receive_marker_reconciles_to_sent(
    role_test_databases: RoleTestDatabases, dispatcher_schema: Engine
) -> None:
    async with api_sessions(role_test_databases.api_url) as factory:
        async with factory() as setup:
            job_id = await insert_job(setup)
            event_id = await insert_event(setup, job_id)
            # worker 已写下与本次 eventId 对应的接收 marker。
            await setup.execute(
                text(
                    "UPDATE ingest_job SET lease_owner = :owner, lease_token = 'recv', "
                    "lease_until = now() + interval '1 hour', heartbeat_at = now() "
                    "WHERE id = :id"
                ),
                {"owner": f"event:{event_id}", "id": job_id},
            )
            await setup.commit()

        publisher = _RecordingPublisher()
        dispatcher = OutboxDispatcher(
            session_factory=factory, publisher=publisher, owner="dispatcher:test"
        )
        assert await dispatcher.dispatch_once() is DispatchOutcome.SENT
        assert publisher.calls == []

        async with factory() as check:
            row = (
                await check.execute(
                    text("SELECT status FROM outbox_event WHERE id = :id"),
                    {"id": event_id},
                )
            ).first()
            assert row is not None and row[0] == "SENT"


@pytest.mark.anyio
async def test_worker_receiver_writes_marker_once_and_skips_other_states(
    role_test_databases: RoleTestDatabases, dispatcher_schema: Engine
) -> None:
    async with api_sessions(role_test_databases.api_url) as factory:
        async with factory() as setup:
            queued_job = await insert_job(setup)
            ready_job = await insert_job(setup, status="READY")
            deleted_job = await insert_job(setup, document_deleted=True)
            await insert_event(setup, queued_job)

    worker_engine = create_engine(role_test_databases.worker_url, pool_pre_ping=True)
    try:
        worker_factory = create_sync_session_factory(worker_engine)
        event_id = str(uuid.uuid4())
        assert (
            receive_ingest_event(worker_factory, job_id=queued_job, event_id=event_id)
            == RECEIVE_STATUS_RECEIVED
        )

        with worker_engine.connect() as connection:
            first = connection.execute(
                text(
                    "SELECT status, lease_owner, lease_token, lease_until, heartbeat_at, "
                    "error_code FROM ingest_job WHERE id = :id"
                ),
                {"id": queued_job},
            ).first()
        assert first is not None
        assert first[0] == "QUEUED"
        assert first[1] == f"event:{event_id}"
        assert first[2]
        assert first[3] is not None and first[4] is not None
        assert first[5] == protocol.HANDLER_NOT_READY
        heartbeat = first[4]

        # 重复消息（不同 eventId）命中已有 marker，不覆盖、不制造新进度。
        assert (
            receive_ingest_event(worker_factory, job_id=queued_job, event_id=str(uuid.uuid4()))
            == RECEIVE_STATUS_ALREADY_RECEIVED
        )
        with worker_engine.connect() as connection:
            second = connection.execute(
                text("SELECT lease_owner, heartbeat_at FROM ingest_job WHERE id = :id"),
                {"id": queued_job},
            ).first()
        assert second is not None
        assert second[0] == f"event:{event_id}"
        assert second[1] == heartbeat

        assert (
            receive_ingest_event(worker_factory, job_id=ready_job, event_id=str(uuid.uuid4()))
            == RECEIVE_STATUS_NOT_QUEUED
        )
        assert (
            receive_ingest_event(worker_factory, job_id=deleted_job, event_id=str(uuid.uuid4()))
            == RECEIVE_STATUS_DELETED
        )
    finally:
        worker_engine.dispose()


# 回归：worker 接收 marker 在“行锁获取之后”写入，heartbeat/lease 必须用真实时钟。
# 旧实现用事务开始（SELECT ... FOR UPDATE 被阻塞之前）的 now()，等锁越久 marker 越旧。
WORKER_MARKER_LOCK_WAIT_SECONDS = 4.0
WORKER_MARKER_MIN_LEASE = timedelta(seconds=protocol.LEASE_DURATION_SECONDS - 2)


@pytest.mark.anyio
async def test_worker_receive_marker_uses_real_clock_after_job_lock_wait(
    role_test_databases: RoleTestDatabases, dispatcher_schema: Engine
) -> None:
    """worker 等 job 行锁超过 4 秒后，marker 的 heartbeat/lease 仍须接近真实时刻。

    ``SELECT ... FOR UPDATE`` 在外部锁上阻塞时，事务 ``now()`` 停留在阻塞前；用它在
    解锁后写 heartbeat/lease 会写入过期时间。以唯一 ``application_name`` 精确锁定本用例
    的 worker 连接（并读出其真实 pid），避免匹配到别的等待者；heartbeat/lease 相对解锁前
    记录的 ``unlock_time`` 判断，忙时不会因检查延迟而假失败。
    """

    async with api_sessions(role_test_databases.api_url) as factory:
        async with factory() as setup:
            job_id = await insert_job(setup)

        application_name = f"clock-test-worker-{uuid.uuid4().hex[:12]}"
        worker_url = role_test_databases.worker_url
        separator = "&" if "?" in worker_url else "?"
        worker_engine = create_engine(
            f"{worker_url}{separator}application_name={application_name}",
            pool_pre_ping=True,
        )
        observer_engine = create_async_engine(
            role_test_databases.migrator_url, pool_pre_ping=True
        )
        worker_results: list[str] = []
        worker_errors: list[BaseException] = []
        worker_thread: threading.Thread | None = None
        try:
            worker_factory = create_sync_session_factory(worker_engine)
            observer_factory = create_session_factory(observer_engine)
            async with observer_factory() as locker, observer_factory() as observer:
                await locker.execute(
                    text("SELECT id FROM ingest_job WHERE id = :id FOR UPDATE"),
                    {"id": job_id},
                )
                event_id = str(uuid.uuid4())

                def run_receive() -> None:
                    try:
                        worker_results.append(
                            receive_ingest_event(
                                worker_factory, job_id=job_id, event_id=event_id
                            )
                        )
                    except BaseException as exc:  # 线程异常留作诊断，绝不静默丢失
                        worker_errors.append(exc)

                # 用显式线程而非 asyncio.to_thread：取消 Task 不会 join 底层线程，
                # 会让随后的 engine.dispose 与活动连接竞态。
                worker_thread = threading.Thread(
                    target=run_receive, name="clock-test-worker", daemon=True
                )
                wait_start = time.monotonic()
                worker_thread.start()

                blocked_row = None
                deadline = wait_start + LOCK_WAIT_DEADLINE_SECONDS
                while time.monotonic() < deadline:
                    if not worker_thread.is_alive():
                        break
                    blocked_row = (
                        await observer.execute(
                            text(
                                "SELECT wait_event_type, xact_start, pid "
                                "FROM pg_stat_activity "
                                "WHERE application_name = :app_name "
                                "  AND state = 'active' "
                                "  AND wait_event_type = 'Lock'"
                            ),
                            {"app_name": application_name},
                        )
                    ).first()
                    await observer.rollback()
                    if blocked_row is not None:
                        break
                    await asyncio.sleep(0.1)
                if blocked_row is None:
                    detail = await _release_lock_and_finish_thread(locker, worker_thread)
                    pytest.fail(
                        f"worker SELECT ... FOR UPDATE 未在 "
                        f"{LOCK_WAIT_DEADLINE_SECONDS:.0f} 秒内阻塞在 job 行锁上"
                        f"（{detail}，application_name={application_name}）"
                    )
                assert worker_thread.is_alive()
                txn_start = blocked_row[1]
                assert txn_start is not None and blocked_row[2] is not None

                # 从被阻塞事务的 xact_start 起等满 4 秒，确保旧代码的 now() 确实过期。
                progressed = False
                progress_deadline = time.monotonic() + LOCK_WAIT_DEADLINE_SECONDS
                while time.monotonic() < progress_deadline:
                    if not worker_thread.is_alive():
                        break
                    progressed = (
                        await observer.execute(
                            text(
                                "SELECT clock_timestamp() >= :txn_start + "
                                "make_interval(secs => :wait)"
                            ),
                            {
                                "txn_start": txn_start,
                                "wait": WORKER_MARKER_LOCK_WAIT_SECONDS,
                            },
                        )
                    ).scalar_one()
                    await observer.rollback()
                    if progressed:
                        break
                    await asyncio.sleep(0.1)
                if not progressed:
                    detail = await _release_lock_and_finish_thread(locker, worker_thread)
                    pytest.fail(
                        f"worker marker 未在 {LOCK_WAIT_DEADLINE_SECONDS:.0f} 秒内等满"
                        f"租约过期窗口（{detail}）"
                    )
                # 解锁前记录真实时刻；marker 的 lease/heartbeat 必须晚于它。
                unlock_time = (
                    await observer.execute(text("SELECT clock_timestamp()"))
                ).scalar_one()
                await observer.rollback()
                try:
                    await locker.rollback()
                except Exception as exc:
                    detail = await _release_lock_and_finish_thread(locker, worker_thread)
                    pytest.fail(f"释放 job 行锁失败（rollback_exc={exc!r}，{detail}）")
                # 释放锁后必须真正 join 线程，才能安全 dispose。
                if not await _join_thread_bounded(
                    worker_thread, WORKER_JOIN_TIMEOUT_SECONDS
                ):
                    pytest.fail(
                        f"worker 线程释放锁后 {WORKER_JOIN_TIMEOUT_SECONDS:.0f} 秒内未结束"
                        f"（worker_errors={worker_errors!r}）"
                    )
            assert not worker_errors, f"worker 线程异常：{worker_errors[0]!r}"
            assert worker_results == [RECEIVE_STATUS_RECEIVED]

            async with factory() as check:
                row = (
                    await check.execute(
                        text(
                            "SELECT heartbeat_at, lease_until "
                            "FROM ingest_job WHERE id = :id"
                        ),
                        {"id": job_id},
                    )
                ).first()
                await check.rollback()
            assert row is not None
            # 旧代码等锁 4 秒后租约只剩约 56 秒、heartbeat 停在解锁前；新代码都在解锁后。
            assert row[1] is not None and row[1] >= unlock_time + WORKER_MARKER_MIN_LEASE
            assert row[0] is not None and row[0] >= unlock_time
        finally:
            if worker_thread is not None and worker_thread.is_alive():
                await _join_thread_bounded(worker_thread, WORKER_JOIN_TIMEOUT_SECONDS)
            await observer_engine.dispose()
            worker_engine.dispose()


# 回归：外部事务更新同一 outbox 行（owner/token/租约不变）并提交，跨越短租约到期。
# READ COMMITTED 下 mark_sent 的 CAS 谓词会用 EvalPlanQual 针对新版本重新求值：
# clock_timestamp() 看到解锁后的真实时刻而拒绝迟到回写；旧 now() 停在事务开始时刻，
# 会错误地回写 SENT。本用例不涉及 job 行锁。
OUTBOX_LOCK_LEASE_SECONDS = 2.0
OUTBOX_LOCK_WAIT_SECONDS = 4.0


@pytest.mark.anyio
async def test_mark_sent_rejects_late_writeback_after_outbox_lock_outlives_lease(
    role_test_databases: RoleTestDatabases, dispatcher_schema: Engine
) -> None:
    """外部 outbox 行锁跨过短租约到期时，mark_sent 必须拒绝迟到回写。

    外部事务对同一 outbox 行做一次 UPDATE（owner/token/lease_until 均不变）并保持到短租约
    过期后提交；mark_sent 的 MARK_SENT_SQL 先读到旧版本、在行锁上等待，随后由 EvalPlanQual
    针对新版本重新求值。``clock_timestamp()`` 此时已超过租约，UPDATE 影响 0 行，mark_sent
    返回 False 且 outbox 仍为 PENDING；旧 ``now()`` 停留在事务开始时刻，会错误回写 SENT。
    """

    async with api_sessions(role_test_databases.api_url) as factory:
        async with factory() as setup:
            job_id = await insert_job(setup)
            event_id = await insert_event(setup, job_id)

        observer_engine = create_async_engine(
            role_test_databases.migrator_url, pool_pre_ping=True
        )
        try:
            observer_factory = create_session_factory(observer_engine)
            async with (
                factory() as repo_session,
                observer_factory() as locker,
                observer_factory() as observer,
            ):
                repo = SqlOutboxRepository(repo_session)
                claim = await repo.claim_due_event(owner="dispatcher:outbox-lock")
                assert claim is not None and claim.event_id == event_id
                repo_pid = (
                    await repo_session.execute(text("SELECT pg_backend_pid()"))
                ).scalar_one()
                # 缩短租约但保留同一 owner/token，使拒绝只能来自租约谓词。
                await repo_session.execute(
                    text(
                        "UPDATE outbox_event SET lease_until = clock_timestamp() + "
                        "(:lease_seconds * interval '1 second') WHERE id = :id"
                    ),
                    {"id": event_id, "lease_seconds": OUTBOX_LOCK_LEASE_SECONDS},
                )
                await repo_session.commit()

                # 外部事务更新同一行（制造新版本）并保持到租约过期后才提交。
                await locker.execute(
                    text(
                        "UPDATE outbox_event SET updated_at = clock_timestamp() "
                        "WHERE id = :id"
                    ),
                    {"id": event_id},
                )
                wait_start = time.monotonic()
                mark_task = asyncio.create_task(repo.mark_sent(claim))

                blocked_row = None
                deadline = wait_start + LOCK_WAIT_DEADLINE_SECONDS
                while time.monotonic() < deadline:
                    if mark_task.done():
                        break
                    blocked_row = (
                        await observer.execute(
                            text(
                                "SELECT wait_event_type, query, xact_start "
                                "FROM pg_stat_activity WHERE pid = :pid"
                            ),
                            {"pid": repo_pid},
                        )
                    ).first()
                    await observer.rollback()
                    if (
                        blocked_row is not None
                        and blocked_row[0] == "Lock"
                        and "outbox_event" in (blocked_row[1] or "")
                    ):
                        break
                    blocked_row = None
                    await asyncio.sleep(0.1)
                if blocked_row is None:
                    detail = await _release_lock_and_finish_task(locker, mark_task)
                    pytest.fail(
                        f"mark_sent 未在 {LOCK_WAIT_DEADLINE_SECONDS:.0f} 秒内阻塞在 "
                        f"outbox 行锁上（{detail}）"
                    )
                assert not mark_task.done()
                txn_start = blocked_row[2]
                assert txn_start is not None

                progressed = False
                progress_deadline = time.monotonic() + LOCK_WAIT_DEADLINE_SECONDS
                while time.monotonic() < progress_deadline:
                    if mark_task.done():
                        break
                    progressed = (
                        await observer.execute(
                            text(
                                "SELECT clock_timestamp() >= :txn_start + "
                                "         make_interval(secs => :wait) "
                                "   AND (SELECT lease_until FROM outbox_event "
                                "        WHERE id = :id) < clock_timestamp()"
                            ),
                            {
                                "txn_start": txn_start,
                                "wait": OUTBOX_LOCK_WAIT_SECONDS,
                                "id": event_id,
                            },
                        )
                    ).scalar_one()
                    await observer.rollback()
                    if progressed:
                        break
                    await asyncio.sleep(0.1)
                if not progressed:
                    detail = await _release_lock_and_finish_task(locker, mark_task)
                    pytest.fail(
                        f"outbox 行锁未在 {LOCK_WAIT_DEADLINE_SECONDS:.0f} 秒内跨过租约"
                        f"到期（{detail}）"
                    )
                # 提交外部更新，制造新版本，让 CAS 谓词走 EvalPlanQual 重估。
                await locker.commit()
                assert await mark_task is False

            async with factory() as check:
                row = (
                    await check.execute(
                        text(
                            "SELECT status, sent_at, lease_owner, lease_token "
                            "FROM outbox_event WHERE id = :id"
                        ),
                        {"id": event_id},
                    )
                ).first()
                await check.rollback()
            assert row is not None
            assert row[0] == "PENDING"
            assert row[1] is None
            assert row[2] == claim.lease_owner and row[3] == claim.lease_token
        finally:
            await observer_engine.dispose()


# --- 处理中任务过期租约恢复（真实 PostgreSQL） ------------------------------------


@dataclass(frozen=True)
class RecoverableJob:
    job_id: uuid.UUID
    kb_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID


async def insert_recoverable_job(
    session: AsyncSession,
    *,
    status: str = "PARSING",
    attempt: int = 1,
    expired: bool = True,
    document_deleted: bool = False,
    active_version: bool = False,
    error_code: str | None = None,
) -> RecoverableJob:
    """按外键顺序写入 KB/document/version/job（可选已有 active 版本）。

    使用 API 角色被授予的 DML 与默认全局 profile；租约默认已过期，供恢复扫描锁定。
    """

    kb_id = uuid.uuid4()
    document_id = uuid.uuid4()
    version_id = uuid.uuid4()
    job_id = uuid.uuid4()
    profile_id = await ensure_default_index_profile(session)
    await session.execute(
        text(
            "INSERT INTO knowledge_base (id, organization_id, name) "
            "VALUES (:id, :organization_id, :name)"
        ),
        {"id": kb_id, "organization_id": uuid.uuid4(), "name": "kb"},
    )
    await session.execute(
        text(
            "INSERT INTO document (id, kb_id, title, source_type, lifecycle_status, "
            "deleted_at) VALUES (:id, :kb_id, :title, 'markdown', 'CREATED', "
            "CASE WHEN :deleted THEN now() ELSE NULL END)"
        ),
        {
            "id": document_id,
            "kb_id": kb_id,
            "title": "doc",
            "deleted": document_deleted,
        },
    )
    await session.execute(
        text(
            "INSERT INTO document_version "
            "(id, document_id, version_no, file_ref, file_hash, mime, parser_version, status) "
            "VALUES (:id, :document_id, 1, 'ref', 'hash', 'text/markdown', :parser_version, "
            "'PENDING')"
        ),
        {
            "id": version_id,
            "document_id": document_id,
            "parser_version": MARKDOWN_PARSER_VERSION,
        },
    )
    if active_version:
        active_version_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO document_version "
                "(id, document_id, version_no, file_ref, file_hash, mime, parser_version, "
                " status) "
                "VALUES (:id, :document_id, 2, 'ref2', 'hash2', 'text/markdown', "
                " :parser_version, 'READY')"
            ),
            {
                "id": active_version_id,
                "document_id": document_id,
                "parser_version": MARKDOWN_PARSER_VERSION,
            },
        )
        await session.execute(
            text(
                "UPDATE document SET active_version_id = :active_version_id, "
                "lifecycle_status = 'READY' WHERE id = :id"
            ),
            {"active_version_id": active_version_id, "id": document_id},
        )
    lease_sql = (
        "clock_timestamp() - interval '1 second'"
        if expired
        else "clock_timestamp() + interval '1 hour'"
    )
    await session.execute(
        text(
            "INSERT INTO ingest_job "
            "(id, document_id, version_id, profile_id, status, attempt, "
            " next_run_at, dedupe_key, error_code, lease_owner, lease_token, lease_until, "
            " heartbeat_at) "
            "VALUES (:id, :document_id, :version_id, :profile_id, :status, "
            f" :attempt, now(), :dedupe_key, :error_code, 'pipeline:old', 'old-token', "
            f" {lease_sql}, clock_timestamp() - interval '5 seconds')"
        ),
        {
            "id": job_id,
            "document_id": document_id,
            "version_id": version_id,
            "profile_id": profile_id,
            "status": status,
            "attempt": attempt,
            "dedupe_key": uuid.uuid4().hex,
            "error_code": error_code,
        },
    )
    await session.commit()
    return RecoverableJob(job_id, kb_id, document_id, version_id)


async def read_job_row(session: AsyncSession, job_id: uuid.UUID) -> dict[str, Any]:
    row = (
        await session.execute(
            text(
                "SELECT status, error_code, attempt, next_run_at, lease_owner, lease_token, "
                "lease_until, heartbeat_at, generation_id FROM ingest_job WHERE id = :id"
            ),
            {"id": job_id},
        )
    ).mappings().one()
    return dict(row)


def make_sync_factory(database_url: str) -> Any:
    engine = create_engine(database_url, pool_pre_ping=True)
    return create_sync_session_factory(engine), engine


@pytest.mark.anyio
async def test_recovery_requeues_all_active_statuses_with_backoff_event(
    role_test_databases: RoleTestDatabases, dispatcher_schema: Engine
) -> None:
    statuses = ("PARSING", "CHUNKING", "EMBEDDING", "INDEXING")
    async with api_sessions(role_test_databases.api_url) as factory:
        async with factory() as setup:
            jobs = [
                await insert_recoverable_job(setup, status=status, attempt=1)
                for status in statuses
            ]

        async with factory() as session:
            result = await recover_expired_pipeline_jobs(SqlOutboxRepository(session))

        assert set(result.requeued_job_ids) == {job.job_id for job in jobs}
        assert result.exhausted_job_ids == ()

        async with factory() as check:
            for job in jobs:
                row = await read_job_row(check, job.job_id)
                assert row["status"] == "QUEUED"
                assert row["attempt"] == 1  # attempt 由下一次 claim 递增
                assert row["lease_owner"] is None and row["lease_token"] is None
                assert row["lease_until"] is None and row["heartbeat_at"] is None
                assert row["error_code"] is None
                events = (
                    await check.execute(
                        text(
                            "SELECT status, next_send_at = ("
                            "  SELECT next_run_at FROM ingest_job WHERE id = :id"
                            ") AS same_deadline FROM outbox_event WHERE job_id = :id"
                        ),
                        {"id": job.job_id},
                    )
                ).all()
                assert len(events) == 1
                assert events[0][0] == "PENDING" and events[0][1] is True
            await check.rollback()


@pytest.mark.anyio
async def test_recovery_leaves_valid_lease_terminal_and_queued_unchanged(
    role_test_databases: RoleTestDatabases, dispatcher_schema: Engine
) -> None:
    async with api_sessions(role_test_databases.api_url) as factory:
        async with factory() as setup:
            valid = await insert_recoverable_job(setup, status="PARSING", expired=False)
            ready = await insert_recoverable_job(setup, status="READY", attempt=2)
            queued = await insert_recoverable_job(setup, status="QUEUED", attempt=0)

        async with factory() as session:
            result = await recover_expired_pipeline_jobs(SqlOutboxRepository(session))

        assert result.requeued_job_ids == ()
        assert result.exhausted_job_ids == ()

        async with factory() as check:
            valid_row = await read_job_row(check, valid.job_id)
            assert valid_row["status"] == "PARSING" and valid_row["lease_token"] == "old-token"
            ready_row = await read_job_row(check, ready.job_id)
            assert ready_row["status"] == "READY"
            queued_row = await read_job_row(check, queued.job_id)
            assert queued_row["status"] == "QUEUED"
            events = (
                await check.execute(text("SELECT count(*) FROM outbox_event"))
            ).scalar_one()
            assert events == 0
            await check.rollback()


@pytest.mark.anyio
async def test_recovery_exhausts_at_cap_and_syncs_dependents(
    role_test_databases: RoleTestDatabases, dispatcher_schema: Engine
) -> None:
    async with api_sessions(role_test_databases.api_url) as factory:
        async with factory() as setup:
            first = await insert_recoverable_job(
                setup,
                status="INDEXING",
                attempt=protocol.MAX_PIPELINE_ATTEMPTS,
            )
            updated = await insert_recoverable_job(
                setup,
                status="PARSING",
                attempt=protocol.MAX_PIPELINE_ATTEMPTS,
                active_version=True,
            )

        async with factory() as session:
            result = await recover_expired_pipeline_jobs(SqlOutboxRepository(session))

        assert set(result.exhausted_job_ids) == {first.job_id, updated.job_id}
        assert result.requeued_job_ids == ()

        async with factory() as check:
            for job in (first, updated):
                row = await read_job_row(check, job.job_id)
                assert row["status"] == "FAILED"
                assert row["error_code"] == protocol.PIPELINE_RETRY_EXHAUSTED
                assert row["lease_owner"] is None and row["lease_token"] is None
                version_status = (
                    await check.execute(
                        text("SELECT status FROM document_version WHERE id = :id"),
                        {"id": job.version_id},
                    )
                ).scalar_one()
                assert version_status == "FAILED"
                pending_events = (
                    await check.execute(
                        text(
                            "SELECT count(*) FROM outbox_event WHERE job_id = :id "
                            "AND status = 'PENDING'"
                        ),
                        {"id": job.job_id},
                    )
                ).scalar_one()
                assert pending_events == 0
            first_document = (
                await check.execute(
                    text(
                        "SELECT lifecycle_status, active_version_id FROM document "
                        "WHERE id = :id"
                    ),
                    {"id": first.document_id},
                )
            ).one()
            assert first_document[0] == "FAILED" and first_document[1] is None
            # 已有 active 版本的文档保持可用，不被耗尽失败下线。
            updated_document = (
                await check.execute(
                    text(
                        "SELECT lifecycle_status, active_version_id FROM document "
                        "WHERE id = :id"
                    ),
                    {"id": updated.document_id},
                )
            ).one()
            assert updated_document[0] == "READY" and updated_document[1] is not None
            await check.rollback()


@pytest.mark.anyio
async def test_recovery_does_not_revive_deleted_document(
    role_test_databases: RoleTestDatabases, dispatcher_schema: Engine
) -> None:
    async with api_sessions(role_test_databases.api_url) as factory:
        async with factory() as setup:
            job = await insert_recoverable_job(
                setup, status="PARSING", attempt=1, document_deleted=True
            )

        async with factory() as session:
            result = await recover_expired_pipeline_jobs(SqlOutboxRepository(session))

        assert result.requeued_job_ids == ()
        assert result.exhausted_job_ids == ()
        async with factory() as check:
            row = await read_job_row(check, job.job_id)
            assert row["status"] == "PARSING" and row["lease_token"] == "old-token"
            events = (
                await check.execute(text("SELECT count(*) FROM outbox_event"))
            ).scalar_one()
            assert events == 0
            await check.rollback()


@pytest.mark.anyio
async def test_recovery_is_idempotent_under_concurrent_scan(
    role_test_databases: RoleTestDatabases, dispatcher_schema: Engine
) -> None:
    async with api_sessions(role_test_databases.api_url) as factory:
        async with factory() as setup:
            job = await insert_recoverable_job(setup, status="PARSING", attempt=1)

        async with factory() as first_session, factory() as second_session:
            first, second = await asyncio.gather(
                recover_expired_pipeline_jobs(SqlOutboxRepository(first_session)),
                recover_expired_pipeline_jobs(SqlOutboxRepository(second_session)),
            )

        requeued = set(first.requeued_job_ids) | set(second.requeued_job_ids)
        assert requeued == {job.job_id}
        async with factory() as check:
            pending = (
                await check.execute(
                    text(
                        "SELECT count(*) FROM outbox_event WHERE job_id = :id "
                        "AND status = 'PENDING'"
                    ),
                    {"id": job.job_id},
                )
            ).scalar_one()
            assert pending == 1
            await check.rollback()


@pytest.mark.anyio
async def test_recovered_job_backoff_blocks_old_message_claim(
    role_test_databases: RoleTestDatabases, dispatcher_schema: Engine
) -> None:
    async with api_sessions(role_test_databases.api_url) as factory:
        async with factory() as setup:
            job = await insert_recoverable_job(setup, status="PARSING", attempt=1)
            old_event_id = await insert_event(setup, job.job_id, status="SENT")

        async with factory() as session:
            result = await recover_expired_pipeline_jobs(SqlOutboxRepository(session))
        assert result.requeued_job_ids == (job.job_id,)

        sync_factory, sync_engine = make_sync_factory(role_test_databases.worker_url)
        try:
            # 退避未到期：旧 Redis 重投的同一消息也不能领取。
            blocked = iw.claim_ingest_job(
                sync_factory,
                job_id=job.job_id,
                event_id=str(old_event_id),
                expected_parser_versions={"markdown": MARKDOWN_PARSER_VERSION},
            )
            assert blocked.status == iw.PROCESS_STATUS_NOT_DUE
            assert blocked.claimed is None

            async with factory() as check:
                row = await read_job_row(check, job.job_id)
                assert row["status"] == "QUEUED" and row["lease_token"] is None
                await check.execute(
                    text(
                        "UPDATE ingest_job SET next_run_at = now() - interval '1 second' "
                        "WHERE id = :id"
                    ),
                    {"id": job.job_id},
                )
                await check.commit()

            # 退避到期后可以正常领取，attempt 由本次 claim 递增。
            claimed = iw.claim_ingest_job(
                sync_factory,
                job_id=job.job_id,
                event_id=str(old_event_id),
                expected_parser_versions={"markdown": MARKDOWN_PARSER_VERSION},
            )
            assert claimed.status == iw.PROCESS_STATUS_CLAIMED
            assert claimed.claimed is not None

            async with factory() as check:
                row = await read_job_row(check, job.job_id)
                assert row["status"] == "PARSING" and row["attempt"] == 2
                await check.rollback()
        finally:
            sync_engine.dispose()


@pytest.mark.anyio
async def test_recovered_job_rejects_old_token_writes(
    role_test_databases: RoleTestDatabases, dispatcher_schema: Engine
) -> None:
    async with api_sessions(role_test_databases.api_url) as factory:
        async with factory() as setup:
            job = await insert_recoverable_job(setup, status="PARSING", attempt=1)

        async with factory() as session:
            await recover_expired_pipeline_jobs(SqlOutboxRepository(session))

        sync_factory, sync_engine = make_sync_factory(role_test_databases.worker_url)
        try:
            # 旧租约 token 的迟到写必须被拒绝。
            assert (
                iw.advance_ingest_stage(
                    sync_factory,
                    job_id=job.job_id,
                    lease_token="old-token",
                    from_status="PARSING",
                    to_status="CHUNKING",
                )
                is False
            )
            assert (
                iw.fail_ingest_job(
                    sync_factory,
                    job_id=job.job_id,
                    lease_token="old-token",
                    error_code="PIPELINE_DB_ERROR",
                )
                is False
            )

            # 退避到期后才允许下一次 claim 拿到新 token。
            async with factory() as check:
                await check.execute(
                    text(
                        "UPDATE ingest_job SET next_run_at = now() - interval '1 second' "
                        "WHERE id = :id"
                    ),
                    {"id": job.job_id},
                )
                await check.commit()

            claimed = iw.claim_ingest_job(
                sync_factory,
                job_id=job.job_id,
                event_id=str(uuid.uuid4()),
                expected_parser_versions={"markdown": MARKDOWN_PARSER_VERSION},
            )
            assert claimed.status == iw.PROCESS_STATUS_CLAIMED and claimed.claimed is not None
            assert (
                iw.advance_ingest_stage(
                    sync_factory,
                    job_id=job.job_id,
                    lease_token="old-token",
                    from_status="PARSING",
                    to_status="CHUNKING",
                )
                is False
            )
            assert iw.advance_ingest_stage(
                sync_factory,
                job_id=job.job_id,
                lease_token=claimed.claimed.lease_token,
                from_status="PARSING",
                to_status="CHUNKING",
            )
        finally:
            sync_engine.dispose()
