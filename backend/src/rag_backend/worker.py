"""Celery worker 入口：与 API 进程分离，使用带认证的 Redis 作为 broker。

这里只装配 broker 与一个无业务副作用的诊断 probe 任务。任务事实仍保存在 PostgreSQL，
因此不配置 result backend。probe 默认只回显；仅当受信配置 ``PROBE_MARKER_DIRECTORY``
显式设置时，才在由该目录与 Celery task id 推导出的路径原子写入诊断 marker JSON，
作为 probe 确实在 worker 进程执行的确定性证据。payload 不能控制路径，也不写 business 表、
任务状态或 Redis result key。
"""

import json
import logging
import os
import re
import socket
import tempfile
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Final

from celery import Celery, current_app, current_task
from sqlalchemy import text

from rag_backend.config import Settings, get_settings
from rag_backend.database import (
    SyncSessionFactory,
    create_sync_database_engine,
    create_sync_session_factory,
)
from rag_backend.dispatch import protocol

WORKER_APP_NAME: Final = "rag_backend"
PROBE_TASK_NAME: Final = "rag_backend.probe"
# 入库接收任务名与队列统一从 dispatch 协议导入，避免 worker 与 dispatcher 各写一份。
INGEST_TASK_NAME: Final = protocol.INGEST_TASK_NAME
INGEST_QUEUE_NAME: Final = protocol.INGEST_QUEUE
# probe 仍用 Celery 默认队列，与 ingest 专用队列隔离投递。
DEFAULT_QUEUE_NAME: Final = "celery"

WORKER_JOB_STATUS_QUEUED: Final = "QUEUED"

logger = logging.getLogger(__name__)

# broker 可见性超时必须大于将来入库任务的硬时限，否则未确认消息会提前回到队列被重复消费。
BROKER_VISIBILITY_TIMEOUT_SECONDS: Final = 3600

# Celery task id 只用于拼接受信目录下的单个安全文件名；不允许路径分隔符或可逃逸片段。
PROBE_TASK_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def require_redis_url(settings: Settings) -> str:
    """返回 broker URL；缺失时直接失败，不回退到 localhost 或占位连接。"""

    if not settings.redis_url:
        raise ValueError(
            "worker 必须设置 REDIS_URL 才能启动，"
            "例如 redis://:password@127.0.0.1:56379/0"
        )
    return settings.redis_url


def probe_marker_path(marker_directory: str, task_id: str) -> Path:
    """由受信 marker 目录与已校验的 task id 推导 marker 路径。"""

    if not PROBE_TASK_ID_PATTERN.fullmatch(task_id):
        raise ValueError(f"probe task id 不能安全用作 marker 文件名: {task_id!r}")
    return Path(marker_directory) / f"{task_id}.json"


def write_probe_marker(marker_directory: str, record: dict[str, Any]) -> Path:
    """用同目录临时文件 + fsync + ``os.replace`` 原子写入 probe marker。

    写入失败时清理临时文件并抛出，绝不把半成品留在最终路径上。
    """

    task_id = record.get("taskId")
    if not isinstance(task_id, str):
        raise ValueError("probe marker 需要字符串 taskId")
    target = probe_marker_path(marker_directory, task_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(record, ensure_ascii=False, sort_keys=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=".probe-marker-", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return target


def probe(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """诊断任务：只返回 JSON 可序列化的 worker 身份与入参，不写业务状态。

    只有配置了 ``probe_marker_directory`` 时才写诊断 marker；默认无文件副作用。
    """

    request = current_task.request
    hostname = getattr(request, "hostname", None) or socket.gethostname()
    record = {
        "hostname": hostname,
        "pid": os.getpid(),
        "taskId": getattr(request, "id", None),
        "payload": payload if payload is not None else {},
    }
    marker_directory = current_app.conf.get("probe_marker_directory")
    if marker_directory:
        write_probe_marker(str(marker_directory), record)
    return record


# 接收任务返回的具名状态；不写 job 处理阶段，只记录本次消息是否写下了接收 marker。
RECEIVE_STATUS_RECEIVED: Final = "received"
RECEIVE_STATUS_ALREADY_RECEIVED: Final = "already_received"
RECEIVE_STATUS_NOT_QUEUED: Final = "not_queued"
RECEIVE_STATUS_NOT_FOUND: Final = "not_found"
RECEIVE_STATUS_DELETED: Final = "deleted"
RECEIVE_STATUS_VERSION_MISMATCH: Final = "version_mismatch"
RECEIVE_STATUS_INVALID_PAYLOAD: Final = "invalid_payload"


class ReceiveAction(Enum):
    """根据 job 事实推导出的接收动作。"""

    SET_MARKER = "SET_MARKER"
    ALREADY_RECEIVED = "ALREADY_RECEIVED"
    NOT_QUEUED = "NOT_QUEUED"
    DELETED = "DELETED"
    VERSION_MISMATCH = "VERSION_MISMATCH"


@dataclass(frozen=True)
class IngestJobFacts:
    """行锁下读到的 job 事实，供纯函数判定。"""

    status: str
    document_deleted: bool
    version_matches_document: bool
    receive_marker_present: bool


_RECEIVE_ACTION_STATUS: Final[dict[ReceiveAction, str]] = {
    ReceiveAction.ALREADY_RECEIVED: RECEIVE_STATUS_ALREADY_RECEIVED,
    ReceiveAction.NOT_QUEUED: RECEIVE_STATUS_NOT_QUEUED,
    ReceiveAction.DELETED: RECEIVE_STATUS_DELETED,
    ReceiveAction.VERSION_MISMATCH: RECEIVE_STATUS_VERSION_MISMATCH,
}

# 行锁读取 job、其文档 tombstone 与版本归属；只锁定 ingest_job 行。
SELECT_INGEST_JOB_FOR_UPDATE_SQL: Final = text(
    """
    SELECT j.status,
           d.deleted_at,
           dv.document_id,
           j.document_id,
           j.lease_owner,
           j.heartbeat_at
    FROM ingest_job AS j
    JOIN document AS d ON d.id = j.document_id
    JOIN document_version AS dv ON dv.id = j.version_id
    WHERE j.id = :job_id
    FOR UPDATE OF j
    """
)

# 原子写接收 marker：owner 指向本次 eventId，token 随机，lease/heartbeat 用解锁后的真实
# 时钟 ``clock_timestamp()``，error_code 明确为 HANDLER_NOT_READY；job.status 保持不变。
# 前面的 ``SELECT ... FOR UPDATE`` 可能长时间等在 job 行锁上，若用事务 ``now()`` 会把
# marker 的心跳与租约写成阻塞前的旧时刻，解锁后可能已经过期。
SET_INGEST_RECEIVE_MARKER_SQL: Final = text(
    """
    UPDATE ingest_job
    SET lease_owner = :lease_owner,
        lease_token = :lease_token,
        lease_until = clock_timestamp() + (:lease_seconds * interval '1 second'),
        heartbeat_at = clock_timestamp(),
        error_code = :error_code
    WHERE id = :job_id
      AND status = 'QUEUED'
      AND lease_owner IS NULL
      AND heartbeat_at IS NULL
    RETURNING id
    """
)


def decide_receive_action(facts: IngestJobFacts) -> ReceiveAction:
    """判定接收动作；已接收或无可处理状态时不制造新进度。"""

    if facts.status != WORKER_JOB_STATUS_QUEUED:
        return ReceiveAction.NOT_QUEUED
    if facts.document_deleted:
        return ReceiveAction.DELETED
    if not facts.version_matches_document:
        return ReceiveAction.VERSION_MISMATCH
    if facts.receive_marker_present:
        return ReceiveAction.ALREADY_RECEIVED
    return ReceiveAction.SET_MARKER


def parse_ingest_payload(payload: object) -> uuid.UUID:
    """校验投递消息仅含受支持的 protocolVersion 与 jobId，并返回 jobId。"""

    if not isinstance(payload, dict):
        raise ValueError("ingest 任务 payload 必须是 JSON 对象")
    if payload.get("protocolVersion") != protocol.PROTOCOL_VERSION:
        raise ValueError("ingest 任务 protocolVersion 不受支持")
    job_id = payload.get("jobId")
    if not isinstance(job_id, str):
        raise ValueError("ingest 任务缺少字符串 jobId")
    try:
        return uuid.UUID(job_id)
    except ValueError as error:
        raise ValueError("ingest 任务 jobId 不是合法 UUID") from error


def receive_ingest_event(
    session_factory: SyncSessionFactory, *, job_id: uuid.UUID, event_id: str
) -> str:
    """在同一事务中行锁校验并写入 job 级接收 marker，提交后才返回。

    ``event_id`` 来自 Celery task id（即 ``outbox.id``）；重复消息命中已有 marker 时不写
    任何字段，因此不会制造处理进度。
    """

    with session_factory() as session:
        row = session.execute(
            SELECT_INGEST_JOB_FOR_UPDATE_SQL, {"job_id": job_id}
        ).first()
        if row is None:
            return RECEIVE_STATUS_NOT_FOUND
        facts = IngestJobFacts(
            status=row[0],
            document_deleted=row[1] is not None,
            version_matches_document=row[2] == row[3],
            receive_marker_present=row[4] is not None or row[5] is not None,
        )
        action = decide_receive_action(facts)
        if action is not ReceiveAction.SET_MARKER:
            session.rollback()
            return _RECEIVE_ACTION_STATUS[action]
        updated = session.execute(
            SET_INGEST_RECEIVE_MARKER_SQL,
            {
                "job_id": job_id,
                "lease_owner": protocol.receive_marker_owner(uuid.UUID(event_id)),
                "lease_token": uuid.uuid4().hex,
                "lease_seconds": protocol.LEASE_DURATION_SECONDS,
                "error_code": protocol.HANDLER_NOT_READY,
            },
        ).first()
        if updated is None:
            session.rollback()
            return RECEIVE_STATUS_ALREADY_RECEIVED
        session.commit()
        # marker 的解除只由将来真正的 handler 或授权运维流程负责；此处只给出可观察信号，
        # 不写 job 处理阶段、不自动清除标记。只记 UUID，不记正文或凭据。
        logger.info(
            "ingest marker written job_id=%s event_id=%s error_code=%s",
            job_id,
            event_id,
            protocol.HANDLER_NOT_READY,
        )
        return RECEIVE_STATUS_RECEIVED


def receive_ingest_request(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Celery 接收任务：解析 payload，把 eventId 与 jobId 交给行锁接收逻辑。

    正常返回前 marker 事务已提交；解析失败或数据库异常向上抛出，由 Celery 按失败语义
    （``task_acks_on_failure_or_timeout``）确认而不无限重投，未确认投递交给 dispatcher 补偿。
    """

    request_id = getattr(current_task.request, "id", None)
    event_id = request_id if isinstance(request_id, str) and request_id else None
    try:
        job_id = parse_ingest_payload(payload)
    except ValueError:
        return {"status": RECEIVE_STATUS_INVALID_PAYLOAD, "eventId": event_id}
    if event_id is None:
        raise ValueError("ingest 任务缺少 Celery task id，无法作为 outbox eventId")
    session_factory: Any = current_app.conf.get("ingest_session_factory")
    if session_factory is None:
        raise RuntimeError("worker 未配置 ingest_session_factory，无法写入接收 marker")
    status = receive_ingest_event(session_factory, job_id=job_id, event_id=event_id)
    return {"status": status, "eventId": event_id, "jobId": str(job_id)}


def create_celery_app(settings: Settings) -> Celery:
    """按运行配置创建 worker 应用；不配置 result backend。"""

    app = Celery(WORKER_APP_NAME, broker=require_redis_url(settings))
    app.conf.update(
        # 消息只使用 JSON 协议，不含正文、凭据或可执行函数路径。
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        # 不设置 result_backend；task_ignore_result 明确丢弃返回值，避免 Redis 被当作业务事实源。
        task_ignore_result=True,
        # 一次只预取一个任务，配合 acks_late 让进程崩溃后的任务可被重新投递。
        worker_prefetch_multiplier=1,
        task_acks_late=True,
        # 任务失败也确认，避免畸形或持续失败的消息无限重投；业务事实由数据库 marker
        # 与 dispatcher 补偿决定，不把 broker 重试当作处理进度。
        task_acks_on_failure_or_timeout=True,
        # 入库接收任务只进专用 ingest 队列；probe 保持默认队列。
        task_routes={INGEST_TASK_NAME: {"queue": INGEST_QUEUE_NAME}},
        # broker 不可达时不无限重试，让 worker 启动或任务派发显式失败。
        broker_connection_retry_on_startup=False,
        broker_transport_options={"visibility_timeout": BROKER_VISIBILITY_TIMEOUT_SECONDS},
        # 事件不再作为执行证据（执行证据由受信目录下的 marker 提供），关闭以省去额外发布。
        task_send_sent_event=False,
        worker_send_task_events=False,
        # 仅当显式配置受信目录时，probe 才写诊断 marker。
        probe_marker_directory=settings.probe_marker_directory,
        # 接收任务用的同步 Session 工厂；创建时不建立连接，只在首次执行时连接。
        ingest_session_factory=create_sync_session_factory(
            create_sync_database_engine(settings)
        ),
        timezone="UTC",
        enable_utc=True,
    )
    app.task(name=PROBE_TASK_NAME)(probe)
    app.task(name=INGEST_TASK_NAME)(receive_ingest_request)
    return app


_celery_app: Celery | None = None


def __getattr__(name: str) -> Celery:
    """按需构造 CLI 使用的 worker 应用。

    导入本模块不会读取运行配置，因此单元测试可以只导入 ``create_celery_app``；只有真正
    取得 ``celery_app``（例如 ``celery -A rag_backend.worker:celery_app``）时才校验
    Redis 配置并在缺失时明确失败。
    """

    global _celery_app
    if name == "celery_app":
        if _celery_app is None:
            _celery_app = create_celery_app(get_settings())
        return _celery_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
