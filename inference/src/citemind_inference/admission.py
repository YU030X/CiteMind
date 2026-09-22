"""有界准入：请求体字节上限、CPU 许可与后台任务登记。

三件事在这里收口：

* :class:`BodySizeLimitMiddleware` 在 JSON 解析前按实际收到的字节数限制请求体，额外
  字段、空白、``\\u`` 转义与分块传输都无法绕过；
* :class:`CpuGate` 限制同时执行的编码批次数与排队深度，整个 tokenize + 预算检查 +
  encode 都在同一个许可内；
* :class:`EmbeddingTaskRegistry` 持有编码任务的强引用，使许可只在真实 CPU 工作结束后
  才归还，并在关闭时等待与观察未处理的异常。
"""

import asyncio
import json
from collections import deque
from collections.abc import Coroutine
from typing import Any, TypeVar

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_T = TypeVar("_T")

# 413 的错误码与路由层保持一致：超限一律返回机器可读的标准错误体。
REQUEST_TOO_LARGE_CODE = "EMBEDDING_PAYLOAD_TOO_LARGE"


class EmbeddingBusyError(Exception):
    """并发许可与等待队列都已占满；调用方应立即返回可重试的 503。"""


class EmbeddingQueueTimeoutError(Exception):
    """在配置的等待时间内没有拿到并发许可。"""


class CpuGate:
    """限制同时执行的 CPU 批次数与排队深度。

    许可只由真正完成 CPU 工作的执行体归还（见 app 中由工作线程调度 ``release``），
    所以取消 HTTP 等待不会放宽实际并发。
    """

    def __init__(self, *, concurrency: int, queue_depth: int) -> None:
        self._concurrency = concurrency
        self._queue_depth = queue_depth
        self._active = 0
        self._waiters: deque[asyncio.Future[None]] = deque()

    @property
    def concurrency(self) -> int:
        return self._concurrency

    @property
    def queue_depth(self) -> int:
        return self._queue_depth

    @property
    def active(self) -> int:
        return self._active

    @property
    def waiting(self) -> int:
        return len(self._waiters)

    async def acquire(self, timeout: float) -> None:
        """占用一个许可、排队等待，或明确失败。

        - 有空闲许可：立即返回；
        - 队列已满：立即抛 :class:`EmbeddingBusyError`（调用方立即回 503）；
        - 排队超时：抛 :class:`EmbeddingQueueTimeoutError`。
        """

        if self._active < self._concurrency:
            self._active += 1
            return
        if len(self._waiters) >= self._queue_depth:
            raise EmbeddingBusyError(
                f"embedding 并发与等待队列已满（并发 {self._concurrency}，"
                f"队列 {self._queue_depth}）"
            )

        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            # shield：超时或取消只结束本次等待，不取消许可交接本身。
            await asyncio.wait_for(asyncio.shield(waiter), timeout)
        except TimeoutError:
            self._abandon(waiter)
            raise EmbeddingQueueTimeoutError("embedding 队列等待超时") from None
        except asyncio.CancelledError:
            self._abandon(waiter)
            raise

    def release(self) -> None:
        """归还许可；有空队者时直接转交（``active`` 保持不变）。"""

        while self._waiters:
            waiter = self._waiters.popleft()
            if waiter.done():
                continue
            waiter.set_result(None)
            return
        if self._active <= 0:
            raise RuntimeError("CpuGate.release 被调用的次数多于 acquire")
        self._active -= 1

    def _abandon(self, waiter: asyncio.Future[None]) -> None:
        """放弃等待：把队列位置或已转交的许可还回去，避免许可泄漏。"""

        if waiter in self._waiters:
            self._waiters.remove(waiter)
        elif waiter.done() and not waiter.cancelled():
            # 恰好被 release 选中：许可已经转交，必须归还。
            self.release()


class EmbeddingTaskRegistry:
    """持有后台编码任务的强引用，并在关闭时等待它们结束。

    编码任务不会被 HTTP 请求的取消连带取消，因此必须有人保留引用、取走异常，否则会出现
    “Task exception was never retrieved”。运行中的 torch 线程无法被中断，所以 ``drain``
    只做有界等待，不假装能杀掉线程。
    """

    def __init__(self, *, max_failures: int = 16) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._failures: deque[BaseException] = deque(maxlen=max_failures)

    @property
    def pending(self) -> int:
        return len(self._tasks)

    @property
    def failures(self) -> tuple[BaseException, ...]:
        return tuple(self._failures)

    @property
    def failure_count(self) -> int:
        return len(self._failures)

    def start(self, coro: Coroutine[Any, Any, _T]) -> asyncio.Task[_T]:
        task: asyncio.Task[_T] = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        # 取一次异常：既记录失败，也避免未观察异常告警。
        error = task.exception()
        if error is not None:
            self._failures.append(error)

    async def drain(self, timeout: float) -> None:
        if not self._tasks:
            return
        await asyncio.wait(set(self._tasks), timeout=timeout)


def _content_length(scope: Scope) -> int | None:
    """读取 Content-Length；只作为快速拒绝的提示，不作为放行依据。"""

    for name, value in scope.get("headers", []):
        if name.lower() == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _send_too_large(send: Send, max_bytes: int) -> None:
    payload = json.dumps(
        {
            "code": REQUEST_TOO_LARGE_CODE,
            "message": f"请求体超过上限 {max_bytes} 字节",
        },
        ensure_ascii=False,
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


class BodySizeLimitMiddleware:
    """在 JSON 解析前按实际收到的字节数限制受保护路径的请求体。

    请求体在上限内会被缓冲一次并重放给下游应用，因此下游看到的仍是完整正文。超出上限时
    直接返回 413，不调用下游、不触发 tokenizer，也不回显正文。检查发生在鉴权之前：拒绝
    超大请求体比先解析凭证更重要，且错误体不含任何请求内容。
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        paths: tuple[str, ...],
        max_bytes: int,
    ) -> None:
        self._app = app
        self._paths = paths
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or scope.get("path") not in self._paths:
            await self._app(scope, receive, send)
            return

        declared = _content_length(scope)
        if declared is not None and declared > self._max_bytes:
            await _send_too_large(send, self._max_bytes)
            return

        received = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                # 客户端已断开：不再调用下游，也没有响应可发。
                return
            if message["type"] != "http.request":
                continue
            received.extend(message.get("body", b""))
            if len(received) > self._max_bytes:
                await _send_too_large(send, self._max_bytes)
                return
            if not message.get("more_body", False):
                break

        buffered = bytes(received)
        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if delivered:
                # 正文已交付；后续读取返回空正文而不是断开，避免误导下游。
                return {"type": "http.request", "body": b"", "more_body": False}
            delivered = True
            return {"type": "http.request", "body": buffered, "more_body": False}

        await self._app(scope, replay, send)
