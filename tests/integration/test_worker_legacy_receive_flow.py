"""worker 接收壳对无接收标记旧 job 的真实 PostgreSQL 静态拒绝验收。

覆盖只有在真实数据库上才成立的语义：``profile_id`` 未绑定或 ``parser_version`` 为旧占位
``markdown-v1`` 的 ``QUEUED`` 且无接收标记 job 被置 ``FAILED`` + 静态
``LEGACY_JOB_UNSUPPORTED``；行锁与 CAS 保证重复/并发投递只拒绝一次，后续落到
``not_queued``；已有 ``HANDLER_NOT_READY`` 接收标记的旧 job 保留原样为
``already_received``；绑定 profile 且 parser 为真实实现版本的 job 仍写
``HANDLER_NOT_READY`` marker 且 ``status`` 保持 ``QUEUED``。无接收 marker 但已有非 NULL 诊断
错误码（``DELIVERY_UNCONFIRMED``/``UNSUPPORTED_EVENT_TYPE``）的 job 保持 ``QUEUED`` 与
原错误码，返回只读 ``existing_diagnostic``，交由既有手工恢复守卫处理，不写任何字段。
拒绝路径不改 attempt/租约/heartbeat/next_run_at、文档/版本状态或 outbox，也不新增
GRANT/迁移。

本模块使用独立的 ``20260925_0006`` schema（含 ``ingest_job.profile_id``），不混用 0005。
所有写入只发生在被守卫放行的独立 ``_test`` 库。
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command
from database_guard import DestructiveTestDatabase
from database_roles_guard import RoleTestDatabases
from rag_backend.database import SyncSessionFactory, create_sync_session_factory
from rag_backend.dispatch import protocol
from rag_backend.ingestion.parsing import MARKDOWN_PARSER_VERSION
from rag_backend.worker import (
    RECEIVE_STATUS_ALREADY_RECEIVED,
    RECEIVE_STATUS_DELETED,
    RECEIVE_STATUS_EXISTING_DIAGNOSTIC,
    RECEIVE_STATUS_LEGACY_UNSUPPORTED,
    RECEIVE_STATUS_NOT_QUEUED,
    RECEIVE_STATUS_RECEIVED,
    RECEIVE_STATUS_VERSION_MISMATCH,
    receive_ingest_event,
)
from sqlalchemy import Engine, create_engine, text
from test_core_migration import alembic_config, alembic_revision, business_tables

pytestmark = pytest.mark.integration

# 旧占位解析器版本；与 worker.LEGACY_PARSER_VERSIONS 对应的真实数据值。
LEGACY_PARSER_VERSION = "markdown-v1"
# 上传受理与 0006 profile 列所在的线性 schema head。
SCHEMA_REVISION = "20260925_0006"
BUSINESS_TABLES_CLEANUP = (
    "TRUNCATE outbox_event, ingest_job, document_version, document, knowledge_base, "
    "index_profile CASCADE"
)

JOB_UNCHANGED_FIELDS = (
    "attempt",
    "lease_owner",
    "lease_token",
    "lease_until",
    "heartbeat_at",
    "next_run_at",
    "profile_id",
)


@pytest.fixture(scope="module")
def legacy_receive_schema(
    destructive_test_database: DestructiveTestDatabase,
) -> Iterator[Engine]:
    """独立 0006 schema 生命周期；前置不干净时不做任何 downgrade。"""

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
def clean_business_rows(legacy_receive_schema: Engine) -> Iterator[None]:
    with legacy_receive_schema.begin() as connection:
        connection.execute(text(BUSINESS_TABLES_CLEANUP))
    yield


@pytest.fixture(scope="module")
def worker_engine(
    legacy_receive_schema: Engine, role_test_databases: RoleTestDatabases
) -> Iterator[Engine]:
    """worker 角色的同步引擎；只用于真实执行 ``receive_ingest_event``。"""

    engine = create_engine(role_test_databases.worker_url, pool_pre_ping=True)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def worker_sessions(worker_engine: Engine) -> SyncSessionFactory:
    return create_sync_session_factory(worker_engine)


def seed_job(
    engine: Engine,
    *,
    profile_bound: bool,
    parser_version: str,
    receive_marker: bool = False,
    document_deleted: bool = False,
    version_mismatch: bool = False,
    status: str = "QUEUED",
    error_code: str | None = None,
) -> uuid.UUID:
    """按外键顺序写入一整套事实，并返回 job id；不复用 0005 夹具。"""

    kb_id = uuid.uuid4()
    document_id = uuid.uuid4()
    version_id = uuid.uuid4()
    job_id = uuid.uuid4()
    profile_id = uuid.uuid4() if profile_bound else None
    version_document_id = document_id
    if version_mismatch:
        version_document_id = uuid.uuid4()
    with engine.begin() as connection:
        if profile_id is not None:
            connection.execute(
                text(
                    "INSERT INTO index_profile "
                    "(id, embedding_model, model_revision, dimension, normalize, "
                    " tokenizer_revision, chunker_version, keyword_analyzer_version, "
                    " config_hash) "
                    "VALUES (:id, 'test/model', 'test-revision', 512, true, 'test-tokenizer', "
                    "'test-chunker', 'test-analyzer', :config_hash)"
                ),
                {"id": profile_id, "config_hash": uuid.uuid4().hex},
            )
        connection.execute(
            text(
                "INSERT INTO knowledge_base (id, organization_id, name) "
                "VALUES (:id, :organization_id, :name)"
            ),
            {"id": kb_id, "organization_id": uuid.uuid4(), "name": "kb"},
        )
        connection.execute(
            text(
                "INSERT INTO document (id, kb_id, title, source_type, lifecycle_status, "
                "deleted_at) "
                "VALUES (:id, :kb_id, 'doc', 'markdown', 'CREATED', "
                f"{'now()' if document_deleted else 'NULL'})"
            ),
            {"id": document_id, "kb_id": kb_id},
        )
        if version_mismatch:
            connection.execute(
                text(
                    "INSERT INTO document (id, kb_id, title, source_type, lifecycle_status) "
                    "VALUES (:id, :kb_id, 'other', 'markdown', 'CREATED')"
                ),
                {"id": version_document_id, "kb_id": kb_id},
            )
        connection.execute(
            text(
                "INSERT INTO document_version "
                "(id, document_id, version_no, file_ref, file_hash, mime, parser_version, "
                " status) "
                "VALUES (:id, :document_id, 1, 'ref', 'hash', 'text/markdown', "
                ":parser_version, 'PENDING')"
            ),
            {
                "id": version_id,
                "document_id": version_document_id,
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
            if error_code is None:
                error_code = protocol.HANDLER_NOT_READY
        connection.execute(
            text(
                "INSERT INTO ingest_job "
                "(id, document_id, version_id, profile_id, status, attempt, next_run_at, "
                " dedupe_key, error_code, lease_owner, lease_token, lease_until, heartbeat_at) "
                "VALUES (:id, :document_id, :version_id, :profile_id, :status, 0, now(), "
                " :dedupe_key, :error_code, :lease_owner, :lease_token, "
                f" {lease_until_sql}, {heartbeat_sql})"
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
        connection.execute(
            text(
                "INSERT INTO outbox_event "
                "(id, job_id, event_type, status, dispatch_attempt, next_send_at) "
                "VALUES (:id, :job_id, :event_type, 'PENDING', 0, now())"
            ),
            {
                "id": uuid.uuid4(),
                "job_id": job_id,
                "event_type": protocol.INGEST_REQUESTED_EVENT_TYPE,
            },
        )
    return job_id


def read_job(engine: Engine, job_id: uuid.UUID) -> dict[str, Any]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT status, attempt, lease_owner, lease_token, lease_until, heartbeat_at, "
                "next_run_at, error_code, profile_id, updated_at "
                "FROM ingest_job WHERE id = :id"
            ),
            {"id": job_id},
        ).one()
    return {
        "status": row[0],
        "attempt": row[1],
        "lease_owner": row[2],
        "lease_token": row[3],
        "lease_until": row[4],
        "heartbeat_at": row[5],
        "next_run_at": row[6],
        "error_code": row[7],
        "profile_id": row[8],
        "updated_at": row[9],
    }


def read_document_version_status(engine: Engine, job_id: uuid.UUID) -> tuple[str, str]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT d.lifecycle_status, dv.status "
                "FROM ingest_job AS j "
                "JOIN document AS d ON d.id = j.document_id "
                "JOIN document_version AS dv ON dv.id = j.version_id "
                "WHERE j.id = :id"
            ),
            {"id": job_id},
        ).one()
    return (str(row[0]), str(row[1]))


def read_outbox(engine: Engine, job_id: uuid.UUID) -> list[tuple[Any, ...]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT event_type, status, dispatch_attempt, sent_at "
                "FROM outbox_event WHERE job_id = :id ORDER BY id"
            ),
            {"id": job_id},
        ).all()
    return [tuple(row) for row in rows]


def test_unbound_legacy_job_is_rejected_without_touching_other_facts(
    legacy_receive_schema: Engine, worker_sessions: SyncSessionFactory
) -> None:
    job_id = seed_job(
        legacy_receive_schema, profile_bound=False, parser_version=LEGACY_PARSER_VERSION
    )
    before = read_job(legacy_receive_schema, job_id)
    before_outbox = read_outbox(legacy_receive_schema, job_id)

    status = receive_ingest_event(
        worker_sessions, job_id=job_id, event_id=str(uuid.uuid4())
    )

    assert status == RECEIVE_STATUS_LEGACY_UNSUPPORTED
    after = read_job(legacy_receive_schema, job_id)
    assert after["status"] == "FAILED"
    assert after["error_code"] == protocol.LEGACY_JOB_UNSUPPORTED
    # 只改 status/error_code/updated_at；attempt、租约/heartbeat、next_run_at、profile
    # 绑定以及文档/版本/outbox 都保持不变。
    for field in JOB_UNCHANGED_FIELDS:
        assert after[field] == before[field], field
    assert after["updated_at"] >= before["updated_at"]
    # worker 只读 document/document_version，不把版本或文档状态改成 READY/INDEXING。
    assert read_document_version_status(legacy_receive_schema, job_id) == (
        "CREATED",
        "PENDING",
    )
    assert read_outbox(legacy_receive_schema, job_id) == before_outbox


def test_placeholder_parser_with_bound_profile_is_rejected_as_legacy(
    legacy_receive_schema: Engine, worker_sessions: SyncSessionFactory
) -> None:
    job_id = seed_job(
        legacy_receive_schema, profile_bound=True, parser_version=LEGACY_PARSER_VERSION
    )
    before = read_job(legacy_receive_schema, job_id)
    assert before["profile_id"] is not None

    status = receive_ingest_event(
        worker_sessions, job_id=job_id, event_id=str(uuid.uuid4())
    )

    assert status == RECEIVE_STATUS_LEGACY_UNSUPPORTED
    after = read_job(legacy_receive_schema, job_id)
    assert after["status"] == "FAILED"
    assert after["error_code"] == protocol.LEGACY_JOB_UNSUPPORTED
    # 旧占位 parser 即使 profile 已绑定也拒绝；既不补绑也不解绑 profile。
    assert after["profile_id"] == before["profile_id"]


def test_bound_profile_with_real_parser_writes_handler_not_ready_marker(
    legacy_receive_schema: Engine, worker_sessions: SyncSessionFactory
) -> None:
    job_id = seed_job(
        legacy_receive_schema, profile_bound=True, parser_version=MARKDOWN_PARSER_VERSION
    )
    before = read_job(legacy_receive_schema, job_id)
    event_id = str(uuid.uuid4())

    status = receive_ingest_event(worker_sessions, job_id=job_id, event_id=event_id)

    assert status == RECEIVE_STATUS_RECEIVED
    after = read_job(legacy_receive_schema, job_id)
    assert after["status"] == "QUEUED"
    assert after["error_code"] == protocol.HANDLER_NOT_READY
    assert after["lease_owner"] == f"event:{event_id}"
    assert after["lease_token"]
    assert after["lease_until"] is not None
    assert after["heartbeat_at"] is not None
    assert after["attempt"] == before["attempt"] == 0
    assert after["next_run_at"] == before["next_run_at"]
    assert after["profile_id"] == before["profile_id"]
    assert read_document_version_status(legacy_receive_schema, job_id) == (
        "CREATED",
        "PENDING",
    )


def test_repeat_delivery_of_rejected_legacy_job_is_not_queued(
    legacy_receive_schema: Engine, worker_sessions: SyncSessionFactory
) -> None:
    job_id = seed_job(
        legacy_receive_schema, profile_bound=False, parser_version=LEGACY_PARSER_VERSION
    )
    first = receive_ingest_event(
        worker_sessions, job_id=job_id, event_id=str(uuid.uuid4())
    )
    assert first == RECEIVE_STATUS_LEGACY_UNSUPPORTED
    rejected = read_job(legacy_receive_schema, job_id)

    # 重复投递读到 FAILED 状态，不写 marker、不改任何字段。
    second = receive_ingest_event(
        worker_sessions, job_id=job_id, event_id=str(uuid.uuid4())
    )
    assert second == RECEIVE_STATUS_NOT_QUEUED
    after = read_job(legacy_receive_schema, job_id)
    assert after["status"] == "FAILED"
    assert after["error_code"] == protocol.LEGACY_JOB_UNSUPPORTED
    assert after["lease_owner"] is None and after["heartbeat_at"] is None
    assert after["updated_at"] == rejected["updated_at"]


def test_existing_handler_not_ready_marker_on_legacy_job_is_kept(
    legacy_receive_schema: Engine, worker_sessions: SyncSessionFactory
) -> None:
    job_id = seed_job(
        legacy_receive_schema,
        profile_bound=False,
        parser_version=LEGACY_PARSER_VERSION,
        receive_marker=True,
    )
    before = read_job(legacy_receive_schema, job_id)
    assert before["status"] == "QUEUED"
    assert before["error_code"] == protocol.HANDLER_NOT_READY

    # 已有 HANDLER_NOT_READY 标记的旧 job 保留原样，交由授权运维手工恢复。
    status = receive_ingest_event(
        worker_sessions, job_id=job_id, event_id=str(uuid.uuid4())
    )
    assert status == RECEIVE_STATUS_ALREADY_RECEIVED
    after = read_job(legacy_receive_schema, job_id)
    assert after["status"] == "QUEUED"
    assert after["error_code"] == protocol.HANDLER_NOT_READY
    assert after["lease_owner"] == before["lease_owner"]
    assert after["lease_token"] == before["lease_token"]
    assert after["heartbeat_at"] == before["heartbeat_at"]
    assert after["updated_at"] == before["updated_at"]


def test_deleted_legacy_job_is_reported_deleted_before_legacy(
    legacy_receive_schema: Engine, worker_sessions: SyncSessionFactory
) -> None:
    job_id = seed_job(
        legacy_receive_schema,
        profile_bound=False,
        parser_version=LEGACY_PARSER_VERSION,
        document_deleted=True,
    )
    status = receive_ingest_event(
        worker_sessions, job_id=job_id, event_id=str(uuid.uuid4())
    )
    assert status == RECEIVE_STATUS_DELETED
    after = read_job(legacy_receive_schema, job_id)
    assert after["status"] == "QUEUED"
    assert after["error_code"] is None


def test_version_mismatch_legacy_job_is_reported_before_legacy(
    legacy_receive_schema: Engine, worker_sessions: SyncSessionFactory
) -> None:
    job_id = seed_job(
        legacy_receive_schema,
        profile_bound=False,
        parser_version=LEGACY_PARSER_VERSION,
        version_mismatch=True,
    )
    status = receive_ingest_event(
        worker_sessions, job_id=job_id, event_id=str(uuid.uuid4())
    )
    assert status == RECEIVE_STATUS_VERSION_MISMATCH
    after = read_job(legacy_receive_schema, job_id)
    assert after["status"] == "QUEUED"
    assert after["error_code"] is None


def test_concurrent_delivery_rejects_legacy_job_exactly_once(
    legacy_receive_schema: Engine, worker_sessions: SyncSessionFactory
) -> None:
    job_id = seed_job(
        legacy_receive_schema, profile_bound=False, parser_version=LEGACY_PARSER_VERSION
    )
    results: list[str] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def deliver() -> None:
        try:
            barrier.wait(timeout=10)
            results.append(
                receive_ingest_event(
                    worker_sessions, job_id=job_id, event_id=str(uuid.uuid4())
                )
            )
        except BaseException as error:  # 线程内异常留作诊断，不静默丢失
            errors.append(error)

    threads = [
        threading.Thread(target=deliver, name=f"legacy-recv-{index}") for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    # 行锁串行化：只有一个投递把 job 置 FAILED，另一个读到 FAILED 后落 not_queued。
    assert sorted(results) == [
        RECEIVE_STATUS_LEGACY_UNSUPPORTED,
        RECEIVE_STATUS_NOT_QUEUED,
    ]
    after = read_job(legacy_receive_schema, job_id)
    assert after["status"] == "FAILED"
    assert after["error_code"] == protocol.LEGACY_JOB_UNSUPPORTED


@pytest.mark.parametrize(
    "diagnostic",
    [protocol.DELIVERY_UNCONFIRMED, protocol.UNSUPPORTED_EVENT_TYPE],
    ids=["delivery-unconfirmed", "unsupported-event-type"],
)
def test_legacy_job_with_existing_diagnostic_stays_queued_and_unwritten(
    legacy_receive_schema: Engine,
    worker_sessions: SyncSessionFactory,
    diagnostic: str,
) -> None:
    job_id = seed_job(
        legacy_receive_schema,
        profile_bound=False,
        parser_version=LEGACY_PARSER_VERSION,
        error_code=diagnostic,
    )
    before = read_job(legacy_receive_schema, job_id)
    before_outbox = read_outbox(legacy_receive_schema, job_id)
    assert before["status"] == "QUEUED"
    assert before["error_code"] == diagnostic

    status = receive_ingest_event(
        worker_sessions, job_id=job_id, event_id=str(uuid.uuid4())
    )

    # 无 marker 的既有诊断保持 QUEUED 原样：不覆写为 FAILED，也不写接收 marker。
    assert status == RECEIVE_STATUS_EXISTING_DIAGNOSTIC
    after = read_job(legacy_receive_schema, job_id)
    assert after == before
    assert read_document_version_status(legacy_receive_schema, job_id) == (
        "CREATED",
        "PENDING",
    )
    assert read_outbox(legacy_receive_schema, job_id) == before_outbox


def test_bound_new_job_with_existing_diagnostic_is_preserved_without_marker(
    legacy_receive_schema: Engine, worker_sessions: SyncSessionFactory
) -> None:
    job_id = seed_job(
        legacy_receive_schema,
        profile_bound=True,
        parser_version=MARKDOWN_PARSER_VERSION,
        error_code=protocol.DELIVERY_UNCONFIRMED,
    )
    before = read_job(legacy_receive_schema, job_id)
    assert before["profile_id"] is not None

    status = receive_ingest_event(
        worker_sessions, job_id=job_id, event_id=str(uuid.uuid4())
    )

    # 新 profile 的先有诊断同样不被覆盖，也不写接收 marker。
    assert status == RECEIVE_STATUS_EXISTING_DIAGNOSTIC
    after = read_job(legacy_receive_schema, job_id)
    assert after == before
    assert after["lease_owner"] is None and after["heartbeat_at"] is None


@pytest.mark.parametrize("bad_event_id", ["not-a-uuid", "", "123"])
def test_invalid_event_id_writes_nothing(
    legacy_receive_schema: Engine,
    worker_sessions: SyncSessionFactory,
    bad_event_id: str,
) -> None:
    job_id = seed_job(
        legacy_receive_schema, profile_bound=True, parser_version=MARKDOWN_PARSER_VERSION
    )
    before = read_job(legacy_receive_schema, job_id)

    with pytest.raises(ValueError, match="event id"):
        receive_ingest_event(worker_sessions, job_id=job_id, event_id=bad_event_id)

    assert read_job(legacy_receive_schema, job_id) == before
