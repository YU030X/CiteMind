"""dispatcher → 真实 Redis/Celery → 独立 worker → DB 接收 marker 的端到端验收。

覆盖仅有真 PG 假 publisher 的用例无法证明的链路：api 角色用真实 ``CeleryPublisher`` 把
事件投到专用 ``ingest`` 队列，独立 ``--pool=solo`` worker 消费后以 ``citemind_worker``
角色在 ``ingest_job`` 写下 ``lease_owner='event:<outbox.id>'`` + heartbeat +
``error_code='HANDLER_NOT_READY'`` 且 ``status`` 仍 ``QUEUED``；重复投递不改 marker；
probe 默认队列路由仍可用。

另含两项**应用层故障注入**（真实 PG + 真实 Redis；不是物理停库、停 broker 或杀 worker）：

- 不可达 broker 使事件 PENDING + 指数退避；注入退避到期后用真实 broker 投递才 SENT，
  且断言真 ``ingest`` 队列长度相对基线精确 +1。
- broker 已接受消息，但 ``SqlOutboxRepository.mark_sent`` 的真实 PG 事务在提交前回滚；
  事件仍 PENDING，强制租约过期重领后 SENT，job 仍 QUEUED，业务事实不删除。

安全边界：worker 子进程 cwd 设为临时目录并剔除继承环境里所有旧 ``CITEMIND_*`` 键，避免
加载仓库真实 ``.env``；只显式传入被守卫校验的 ``_test`` 库 worker 角色 DSN 与测试 broker；
不连接任何用户 dev 库，不 FLUSHDB，只删除本次运行涉及的两个已知队列 key。本模块要求
``TEST_REDIS_URL`` 指向**独占**测试逻辑库（守卫强制回环+密码+非 0 库）；若与其他写入者
共享同一逻辑库，清理会误删其队列 key。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from broker_guard import RedisBrokerTarget
from database_guard import DestructiveTestDatabase
from database_roles_guard import RoleTestDatabases
from rag_backend.config import Settings
from rag_backend.database import SessionFactory, create_session_factory
from rag_backend.dispatch import protocol
from rag_backend.dispatch.publisher import CeleryPublisher
from rag_backend.dispatch.repository import (
    MARK_SENT_SQL,
    ClaimedOutboxEvent,
    SqlOutboxRepository,
)
from rag_backend.dispatch.service import DispatchOutcome, OutboxDispatcher, dispatch_one
from rag_backend.ingestion.parsing import MARKDOWN_PARSER_VERSION
from rag_backend.ingestion.profile_repository import ensure_default_index_profile
from rag_backend.worker import INGEST_TASK_NAME, PROBE_TASK_NAME, create_celery_app
from redis import Redis
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from test_core_migration import alembic_config, alembic_revision, business_tables
from test_worker_broker import delete_queue, terminate_process, wait_until

pytestmark = [pytest.mark.integration, pytest.mark.broker]

# 上传受理与 worker 接收所需的 ``ingest_job.profile_id`` 由 ``20260925_0006`` 新增；
# 本模块独立运行，不与 0005 混用。
SCHEMA_REVISION = "20260925_0006"
DEFAULT_QUEUE = "celery"
WORKER_READY_TIMEOUT_SECONDS = 60.0
TASK_TIMEOUT_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 0.2
UNREACHABLE_BROKER_URL = "redis://:secret@127.0.0.1:1/0"

BUSINESS_TABLES_CLEANUP = (
    "TRUNCATE outbox_event, ingest_job, document_version, document, knowledge_base CASCADE"
)


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, Any]]:
    # Windows 默认 ProactorEventLoop 不能用于 psycopg 异步连接；与生产入口一致。
    return ("asyncio", {"loop_factory": asyncio.SelectorEventLoop})


def make_settings(**overrides: Any) -> Settings:
    """构造不读取本地 .env 的配置；调用方必须传入受守卫校验的 DSN。"""

    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "session_cookie_secure": False,
    }
    values.update(overrides)
    return Settings(**values)


def _database_location(url_text: str) -> tuple[str | None, int, str | None]:
    """从 DSN 提取 ``(host, port, database)``；不保留也不输出密码。"""

    parsed = make_url(url_text)
    return (parsed.host, parsed.port or 5432, parsed.database)


@pytest.fixture(autouse=True)
def assert_shared_test_database(
    destructive_test_database: DestructiveTestDatabase,
    role_test_databases: RoleTestDatabases,
) -> None:
    """迁移 DSN 与三个角色 DSN 必须指向同一 host/port/database。

    角色守卫已在三角色间比较，这里再与 ``TEST_DATABASE_URL`` 比较，防止同名 ``_test``
    库位于不同实例而错位；断言只比较 host/port/database，不涉及密码。
    """

    expected = _database_location(destructive_test_database.url)
    assert _database_location(role_test_databases.migrator_url) == expected
    assert _database_location(role_test_databases.api_url) == expected
    assert _database_location(role_test_databases.worker_url) == expected


def queue_length(redis_url: str, queue: str) -> int:
    """读取指定队列 key 的当前长度，用于断言投递的精确增量。"""

    with Redis.from_url(redis_url) as client:
        return int(client.llen(queue))


@pytest.fixture(scope="module")
def dispatcher_broker_schema(
    destructive_test_database: DestructiveTestDatabase,
) -> Iterator[Engine]:
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
            assert alembic_revision(connection) is None
            assert business_tables(connection) == set()
        engine.dispose()


@pytest.fixture(autouse=True)
def clean_business_rows(dispatcher_broker_schema: Engine) -> Iterator[None]:
    with dispatcher_broker_schema.begin() as connection:
        connection.execute(text(BUSINESS_TABLES_CLEANUP))
    yield


@asynccontextmanager
async def api_sessions(database_url: str) -> AsyncIterator[SessionFactory]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


@dataclass(frozen=True)
class DispatcherWorker:
    process: subprocess.Popen[bytes]
    log_path: Path
    marker_directory: Path
    node_name: str

    def read_log(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace")


@pytest.fixture
def dispatcher_worker(
    dispatcher_broker_schema: Engine,
    role_test_databases: RoleTestDatabases,
    test_redis: RedisBrokerTarget,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[DispatcherWorker]:
    # 与 schema 同一 _test 库、worker 角色由 assert_shared_test_database 与守卫共同保证。
    work_dir = tmp_path_factory.mktemp("dispatcher-broker")
    log_path = work_dir / "worker.log"
    marker_directory = work_dir / "markers"
    marker_directory.mkdir(parents=True, exist_ok=True)
    node_name = f"dispatcher-test@{uuid.uuid4().hex}"

    # 剔除继承环境里的旧 CITEMIND_* 键（大小写不敏感），避免用户 shell 遗留键让子进程启动失败；
    # 只显式注入 _test 库 worker 角色 DSN 与测试 broker，cwd 为临时目录，不回退 dev 库/broker。
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("CITEMIND_")
    }
    environment["DATABASE_URL"] = role_test_databases.worker_url
    environment["REDIS_URL"] = test_redis.url
    environment["ENVIRONMENT"] = "test"
    environment["PROBE_MARKER_DIRECTORY"] = str(marker_directory)
    environment["PYTHONUNBUFFERED"] = "1"

    command = [
        sys.executable,
        "-m",
        "celery",
        "-A",
        "rag_backend.worker:celery_app",
        "worker",
        "--loglevel=INFO",
        "--pool=solo",
        "--queues",
        f"{protocol.INGEST_QUEUE},{DEFAULT_QUEUE}",
        "--hostname",
        node_name,
        "--without-gossip",
        "--without-mingle",
    ]

    with log_path.open("wb") as log_file:
        process = subprocess.Popen(
            command,
            cwd=work_dir,
            env=environment,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        try:
            control_app = create_celery_app(
                make_settings(
                    database_url=role_test_databases.api_url, redis_url=test_redis.url
                )
            )
            try:
                inspector = control_app.control.inspect(timeout=2.0, destination=[node_name])
                wait_until(
                    lambda: bool(inspector.ping()),
                    process,
                    log_path,
                    WORKER_READY_TIMEOUT_SECONDS,
                    f"worker {node_name} 未在 {WORKER_READY_TIMEOUT_SECONDS:.0f} 秒内就绪",
                )
            finally:
                control_app.close()
            yield DispatcherWorker(process, log_path, marker_directory, node_name)
        finally:
            # 逐项尝试清理；即使终止子进程失败也继续删已知队列 key，不 FLUSHDB。
            cleanup_errors: list[BaseException] = []
            try:
                terminate_process(process)
            except BaseException as error:
                cleanup_errors.append(error)
            for queue in (protocol.INGEST_QUEUE, DEFAULT_QUEUE):
                try:
                    delete_queue(test_redis.url, queue)
                except BaseException as error:
                    cleanup_errors.append(error)
            if cleanup_errors and sys.exc_info()[0] is None:
                raise cleanup_errors[0]


@dataclass(frozen=True)
class JobMarker:
    status: str
    lease_owner: str | None
    lease_token: str | None
    heartbeat_at: datetime | None
    error_code: str | None


async def insert_job(factory: SessionFactory) -> uuid.UUID:
    """按外键顺序写入 KB/document/version/job；只使用 api 角色被授予的 DML。

    同一事务内登记/复用默认全局 profile 并绑定到 ``ingest_job.profile_id``，parser 用真实
    实现版本，使新接收壳写下 ``HANDLER_NOT_READY`` marker；本模块不构造旧任务。
    """

    kb_id = uuid.uuid4()
    document_id = uuid.uuid4()
    version_id = uuid.uuid4()
    job_id = uuid.uuid4()
    async with factory() as session:
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
                "INSERT INTO document (id, kb_id, title, source_type, lifecycle_status) "
                "VALUES (:id, :kb_id, :title, 'markdown', 'CREATED')"
            ),
            {"id": document_id, "kb_id": kb_id, "title": "doc"},
        )
        await session.execute(
            text(
                "INSERT INTO document_version "
                "(id, document_id, version_no, file_ref, file_hash, mime, parser_version, "
                " status) "
                "VALUES (:id, :document_id, 1, 'ref', 'hash', 'text/markdown', "
                ":parser_version, 'PENDING')"
            ),
            {
                "id": version_id,
                "document_id": document_id,
                "parser_version": MARKDOWN_PARSER_VERSION,
            },
        )
        await session.execute(
            text(
                "INSERT INTO ingest_job "
                "(id, document_id, version_id, profile_id, status, attempt, next_run_at, "
                " dedupe_key) "
                "VALUES (:id, :document_id, :version_id, :profile_id, 'QUEUED', 0, now(), "
                " :dedupe_key)"
            ),
            {
                "id": job_id,
                "document_id": document_id,
                "version_id": version_id,
                "profile_id": profile_id,
                "dedupe_key": uuid.uuid4().hex,
            },
        )
        await session.commit()
    return job_id


async def insert_event(factory: SessionFactory, job_id: uuid.UUID) -> uuid.UUID:
    event_id = uuid.uuid4()
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO outbox_event "
                "(id, job_id, event_type, status, dispatch_attempt, next_send_at) "
                "VALUES (:id, :job_id, :event_type, 'PENDING', 0, now())"
            ),
            {
                "id": event_id,
                "job_id": job_id,
                "event_type": protocol.INGEST_REQUESTED_EVENT_TYPE,
            },
        )
        await session.commit()
    return event_id


async def read_job(factory: SessionFactory, job_id: uuid.UUID) -> JobMarker | None:
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, lease_owner, lease_token, heartbeat_at, error_code "
                    "FROM ingest_job WHERE id = :id"
                ),
                {"id": job_id},
            )
        ).first()
    if row is None:
        return None
    return JobMarker(
        status=row[0],
        lease_owner=row[1],
        lease_token=row[2],
        heartbeat_at=row[3],
        error_code=row[4],
    )


@dataclass(frozen=True)
class EventState:
    status: str
    attempt: int
    pushed: bool
    sent_at: datetime | None


async def read_event_state(factory: SessionFactory, event_id: uuid.UUID) -> EventState:
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, dispatch_attempt, (next_send_at > now()) AS pushed, sent_at "
                    "FROM outbox_event WHERE id = :id"
                ),
                {"id": event_id},
            )
        ).first()
    assert row is not None
    return EventState(status=row[0], attempt=row[1], pushed=row[2], sent_at=row[3])


async def count_events(factory: SessionFactory, job_id: uuid.UUID) -> int:
    async with factory() as session:
        return int(
            (
                await session.execute(
                    text("SELECT count(*) FROM outbox_event WHERE job_id = :id"),
                    {"id": job_id},
                )
            ).scalar_one()
        )


class _RollbackOnMarkSentRepository(SqlOutboxRepository):
    """真实执行 ``MARK_SENT_SQL`` 后在提交前回滚并抛错。

    模拟 broker 已接受消息，但回写 SENT 的真实 PG 事务在提交前失败；这是应用层故障注入，
    不是物理停库。
    """

    async def mark_sent(self, claim: ClaimedOutboxEvent) -> bool:
        await self._session.execute(
            MARK_SENT_SQL,
            {
                "event_id": claim.event_id,
                "owner": claim.lease_owner,
                "token": claim.lease_token,
            },
        )
        await self._session.rollback()
        raise RuntimeError("injected mark_sent failure before commit")


async def wait_for_job_marker(
    factory: SessionFactory,
    worker: DispatcherWorker,
    job_id: uuid.UUID,
    event_id: uuid.UUID,
    timeout: float,
) -> JobMarker:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if worker.process.poll() is not None:
            pytest.fail(f"worker 提前退出：\n{worker.read_log()}")
        marker = await read_job(factory, job_id)
        if (
            marker is not None
            and marker.lease_owner == protocol.receive_marker_owner(event_id)
            and marker.heartbeat_at is not None
        ):
            return marker
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    pytest.fail(
        f"job {job_id} 未在 {timeout:.0f} 秒内出现接收 marker\nworker 日志：\n{worker.read_log()}"
    )


async def wait_for_probe_marker(
    worker: DispatcherWorker, task_id: str, payload: dict[str, Any], timeout: float
) -> None:
    path = worker.marker_directory / f"{task_id}.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if worker.process.poll() is not None:
            pytest.fail(f"worker 提前退出：\n{worker.read_log()}")
        if path.exists():
            record = json.loads(path.read_text(encoding="utf-8"))
            assert record["taskId"] == task_id
            assert record["payload"] == payload
            return
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    pytest.fail(
        f"probe marker {path} 未在 {timeout:.0f} 秒内出现\nworker 日志：\n{worker.read_log()}"
    )


@pytest.mark.anyio
async def test_ingest_event_reaches_worker_marker_over_real_broker(
    dispatcher_worker: DispatcherWorker,
    role_test_databases: RoleTestDatabases,
    test_redis: RedisBrokerTarget,
) -> None:
    api_url = role_test_databases.api_url
    publisher_app = create_celery_app(
        make_settings(database_url=api_url, redis_url=test_redis.url)
    )
    try:
        async with api_sessions(api_url) as factory:
            job_id = await insert_job(factory)
            event_id = await insert_event(factory, job_id)
            dispatcher = OutboxDispatcher(
                session_factory=factory,
                publisher=CeleryPublisher(publisher_app),
                owner="dispatcher:integration",
            )

            assert await dispatcher.dispatch_once() is DispatchOutcome.SENT
            first = await wait_for_job_marker(
                factory, dispatcher_worker, job_id, event_id, TASK_TIMEOUT_SECONDS
            )
            assert first.status == "QUEUED"
            assert first.error_code == protocol.HANDLER_NOT_READY
            assert first.lease_owner == protocol.receive_marker_owner(event_id)
            assert first.lease_token
            first_token = first.lease_token

            # 重复投递同一 eventId：worker 命中已有 marker，不覆盖、不制造进度。
            publisher_app.send_task(
                INGEST_TASK_NAME,
                args=[protocol.build_dispatch_payload(job_id)],
                task_id=str(event_id),
                queue=protocol.INGEST_QUEUE,
            )
            # 随后事件的 marker 出现，说明排在其前面的重复消息已被消费。
            second_job = await insert_job(factory)
            second_event = await insert_event(factory, second_job)
            assert await dispatcher.dispatch_once() is DispatchOutcome.SENT
            await wait_for_job_marker(
                factory, dispatcher_worker, second_job, second_event, TASK_TIMEOUT_SECONDS
            )

            unchanged = await read_job(factory, job_id)
            assert unchanged is not None
            assert unchanged.lease_owner == first.lease_owner
            assert unchanged.lease_token == first_token
            assert unchanged.heartbeat_at == first.heartbeat_at
            assert unchanged.status == "QUEUED"

            # probe 仍走默认队列并在同一 worker 上执行。
            probe_payload = {"probeId": uuid.uuid4().hex, "source": "dispatcher-broker-test"}
            async_result = publisher_app.send_task(PROBE_TASK_NAME, args=[probe_payload])
            await wait_for_probe_marker(
                dispatcher_worker, async_result.id, probe_payload, TASK_TIMEOUT_SECONDS
            )
    finally:
        publisher_app.close()


@pytest.mark.anyio
async def test_unreachable_broker_keeps_event_pending(
    dispatcher_broker_schema: Engine,
    role_test_databases: RoleTestDatabases,
) -> None:
    api_url = role_test_databases.api_url
    publisher_app = create_celery_app(
        make_settings(database_url=api_url, redis_url=UNREACHABLE_BROKER_URL)
    )
    try:
        async with api_sessions(api_url) as factory:
            job_id = await insert_job(factory)
            event_id = await insert_event(factory, job_id)
            dispatcher = OutboxDispatcher(
                session_factory=factory,
                publisher=CeleryPublisher(publisher_app),
                owner="dispatcher:outage",
            )

            assert await dispatcher.dispatch_once() is DispatchOutcome.DEFERRED

            async with factory() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT status, dispatch_attempt, lease_owner, "
                            "(next_send_at > now()) AS pushed "
                            "FROM outbox_event WHERE id = :id"
                        ),
                        {"id": event_id},
                    )
                ).first()
            assert row is not None
            assert row[0] == "PENDING"  # 不假成功、不落 FAILED
            assert row[1] == 1
            assert row[2] is None  # 退避后清除租约
            assert row[3] is True  # next_send_at 被指数退避推后
    finally:
        publisher_app.close()


@pytest.mark.anyio
async def test_unreachable_then_recovered_delivery_marks_sent(
    dispatcher_broker_schema: Engine,
    role_test_databases: RoleTestDatabases,
    test_redis: RedisBrokerTarget,
) -> None:
    """不可达 broker 使事件保持 PENDING + 退避；注入退避到期后用真实 broker 投递才 SENT。

    初始 DEFERRED 只证明发送失败，不能当作恢复成功；真实 broker 投递同样要求真 PG + 真 Redis。
    """

    api_url = role_test_databases.api_url
    unreachable_app = create_celery_app(
        make_settings(database_url=api_url, redis_url=UNREACHABLE_BROKER_URL)
    )
    real_app = create_celery_app(make_settings(database_url=api_url, redis_url=test_redis.url))
    try:
        async with api_sessions(api_url) as factory:
            job_id = await insert_job(factory)
            event_id = await insert_event(factory, job_id)

            failing = OutboxDispatcher(
                session_factory=factory,
                publisher=CeleryPublisher(unreachable_app),
                owner="dispatcher:outage",
            )
            assert await failing.dispatch_once() is DispatchOutcome.DEFERRED
            deferred = await read_event_state(factory, event_id)
            assert deferred.status == "PENDING"
            assert deferred.attempt == 1
            assert deferred.pushed is True  # 已按指数退避推后 next_send_at

            # 注入 DB 退避窗口结束（不是等待真实 5 秒）。
            async with factory() as session:
                await session.execute(
                    text(
                        "UPDATE outbox_event SET next_send_at = now() - interval '1 second' "
                        "WHERE id = :id"
                    ),
                    {"id": event_id},
                )
                await session.commit()

            recovered = OutboxDispatcher(
                session_factory=factory,
                publisher=CeleryPublisher(real_app),
                owner="dispatcher:recovered",
            )
            baseline = queue_length(test_redis.url, protocol.INGEST_QUEUE)
            assert await recovered.dispatch_once() is DispatchOutcome.SENT
            # 真实 broker 确实收到本次投递：ingest 队列相对基线精确 +1。
            assert queue_length(test_redis.url, protocol.INGEST_QUEUE) == baseline + 1
            sent = await read_event_state(factory, event_id)
            assert sent.status == "SENT"
            assert sent.sent_at is not None
            assert sent.attempt == 2
    finally:
        cleanup_errors: list[BaseException] = []
        for app in (unreachable_app, real_app):
            try:
                app.close()
            except BaseException as error:
                cleanup_errors.append(error)
        # 本次投递的消息没有 worker 消费；只删除本次的队列 key。
        try:
            delete_queue(test_redis.url, protocol.INGEST_QUEUE)
        except BaseException as error:
            cleanup_errors.append(error)
        if cleanup_errors and sys.exc_info()[0] is None:
            raise cleanup_errors[0]


@pytest.mark.anyio
async def test_broker_accepted_but_mark_sent_rollback_keeps_pending_then_recovers(
    dispatcher_broker_schema: Engine,
    role_test_databases: RoleTestDatabases,
    test_redis: RedisBrokerTarget,
) -> None:
    """故障注入：broker 已接收，但 mark_sent 的真实 PG 事务在提交前回滚。

    这是应用层注入（真实 SQL + rollback + 抛错），不是物理停库或磁盘故障。验证事件仍
    PENDING、旧业务事实保留；强制租约过期重领后最终 SENT，job 仍 QUEUED。
    """

    api_url = role_test_databases.api_url
    real_app = create_celery_app(make_settings(database_url=api_url, redis_url=test_redis.url))
    try:
        async with api_sessions(api_url) as factory:
            job_id = await insert_job(factory)
            event_id = await insert_event(factory, job_id)
            baseline = queue_length(test_redis.url, protocol.INGEST_QUEUE)

            # 阶段 1：真实 CeleryPublisher 投递成功后，mark_sent 执行真实 SQL 但回滚。
            async with factory() as session:
                outcome = await dispatch_one(
                    _RollbackOnMarkSentRepository(session),
                    CeleryPublisher(real_app),
                    owner="dispatcher:inject",
                )
            assert outcome is DispatchOutcome.DEFERRED

            # broker 确实已接受消息：ingest 队列相对基线精确 +1。
            assert queue_length(test_redis.url, protocol.INGEST_QUEUE) == baseline + 1

            # 回写事务未提交：outbox 仍 PENDING、未标 SENT、未删除业务事实。
            rolled_back = await read_event_state(factory, event_id)
            assert rolled_back.status == "PENDING"
            assert rolled_back.attempt == 1
            assert rolled_back.sent_at is None
            assert await count_events(factory, job_id) == 1

            # 阶段 2：强制租约到期后重领，真实回写提交。
            async with factory() as session:
                await session.execute(
                    text(
                        "UPDATE outbox_event SET lease_until = now() - interval '1 second' "
                        "WHERE id = :id"
                    ),
                    {"id": event_id},
                )
                await session.commit()

            async with factory() as session:
                outcome = await dispatch_one(
                    SqlOutboxRepository(session),
                    CeleryPublisher(real_app),
                    owner="dispatcher:recovered",
                )
            assert outcome is DispatchOutcome.SENT
            # 阶段 2 的真实投递同样进入队列：相对基线精确 +2（阶段 1 + 阶段 2）。
            assert queue_length(test_redis.url, protocol.INGEST_QUEUE) == baseline + 2

            recovered = await read_event_state(factory, event_id)
            assert recovered.status == "SENT"
            assert recovered.sent_at is not None
            assert recovered.attempt == 2

            job = await read_job(factory, job_id)
            assert job is not None
            assert job.status == "QUEUED"  # 未被误置 READY/PARSING/FAILED
            assert await count_events(factory, job_id) == 1  # 原事件保留，未新增
    finally:
        cleanup_errors: list[BaseException] = []
        try:
            real_app.close()
        except BaseException as error:
            cleanup_errors.append(error)
        # 阶段 1/2 在 ingest 队列留下至多 2 份消息；删除该 queue key 一并清理。
        try:
            delete_queue(test_redis.url, protocol.INGEST_QUEUE)
        except BaseException as error:
            cleanup_errors.append(error)
        if cleanup_errors and sys.exc_info()[0] is None:
            raise cleanup_errors[0]
