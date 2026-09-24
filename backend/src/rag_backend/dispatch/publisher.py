"""Celery broker publisher：把 outbox 事件发送到专用 ``ingest`` 队列。

``Celery.send_task`` 是阻塞网络调用，必须放线程池，不能在持有数据库事务或事件循环里执行。
单次发布禁用 Celery 的发布重试，并显式设置有限的 Redis 连接/读写超时，使 broker 故障在
可观察的短时间内失败。每次发布使用 ``connection_for_write()`` 受控连接，退出上下文即
``release()`` → ``_close()`` 实际关闭底层 socket；``Celery.close()`` 只解除应用注册，
不能当作 socket 关闭来用。
"""

from __future__ import annotations

import functools
from typing import Any, Final

from celery import Celery
from starlette.concurrency import run_in_threadpool

# 单次发布的 Redis 连接/IO 上限；不修改调用方已有的 visibility_timeout。
BROKER_PUBLISH_TIMEOUT_SECONDS: Final = 2.0


class CeleryPublisher:
    """按事件发送任务的 publisher；task id 使用 ``outbox.id``。"""

    def __init__(self, app: Celery) -> None:
        self._app = app
        # 在保留调用方 broker_transport_options（含 visibility_timeout）的前提下，追加
        # 连接与读写超时，确保 broker 不可达时显式快速失败。
        transport_options = dict(app.conf.broker_transport_options or {})
        transport_options["socket_connect_timeout"] = BROKER_PUBLISH_TIMEOUT_SECONDS
        transport_options["socket_timeout"] = BROKER_PUBLISH_TIMEOUT_SECONDS
        app.conf.update(
            task_publish_retry=False,
            broker_connection_timeout=BROKER_PUBLISH_TIMEOUT_SECONDS,
            broker_transport_options=transport_options,
        )

    async def publish(
        self,
        *,
        task_name: str,
        payload: dict[str, object],
        task_id: str,
        queue: str,
    ) -> None:
        send: Any = functools.partial(
            self._send,
            task_name=task_name,
            payload=payload,
            task_id=task_id,
            queue=queue,
        )
        await run_in_threadpool(send)

    def _send(
        self, *, task_name: str, payload: dict[str, object], task_id: str, queue: str
    ) -> None:
        # connection_for_write() 上下文退出时会 release 连接（kombu __exit__ → release
        # → _close），真正关闭本次发布使用的 socket；异常也照常传播，不在此吞掉。
        with self._app.connection_for_write() as connection:
            self._app.send_task(
                task_name,
                args=[payload],
                task_id=task_id,
                queue=queue,
                connection=connection,
                retry=False,
            )

    async def aclose(self) -> None:
        """解除 Celery 应用注册；单次发布连接由 ``_send`` 的上下文释放。

        ``Celery.close()`` 只做 ``self._pool = None`` 与注销，不关闭 kombu 全局连接池，
        因此不能替代单次连接释放。
        """

        await run_in_threadpool(self._app.close)
