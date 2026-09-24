"""Celery worker 入口：与 API 进程分离，使用带认证的 Redis 作为 broker。

这里只装配 broker 与一个无业务副作用的诊断 probe 任务。任务事实仍保存在 PostgreSQL，
因此不配置 result backend。probe 默认只回显；仅当受信配置 ``PROBE_MARKER_DIRECTORY``
显式设置时，才在由该目录与 Celery task id 推导出的路径原子写入诊断 marker JSON，
作为 probe 确实在 worker 进程执行的确定性证据。payload 不能控制路径，也不写 business 表、
任务状态或 Redis result key。
"""

import json
import os
import re
import socket
import tempfile
from pathlib import Path
from typing import Any, Final

from celery import Celery, current_app, current_task

from rag_backend.config import Settings, get_settings

WORKER_APP_NAME: Final = "rag_backend"
PROBE_TASK_NAME: Final = "rag_backend.probe"

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
        # broker 不可达时不无限重试，让 worker 启动或任务派发显式失败。
        broker_connection_retry_on_startup=False,
        broker_transport_options={"visibility_timeout": BROKER_VISIBILITY_TIMEOUT_SECONDS},
        # 事件不再作为执行证据（执行证据由受信目录下的 marker 提供），关闭以省去额外发布。
        task_send_sent_event=False,
        worker_send_task_events=False,
        # 仅当显式配置受信目录时，probe 才写诊断 marker。
        probe_marker_directory=settings.probe_marker_directory,
        timezone="UTC",
        enable_utc=True,
    )
    app.task(name=PROBE_TASK_NAME)(probe)
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
