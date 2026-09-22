"""直接驱动 ASGI 的测试助手。

TestClient 无法在请求中途取消、也无法控制 receive 的分块方式，而这两点正是请求体字节
上限与 CPU 许可生命周期回归所必需的。这里直接按 ASGI 协议调用应用。
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from starlette.types import ASGIApp, Message, Scope
from support import TEST_TOKEN

from citemind_inference.app import create_app
from citemind_inference.config import Settings
from citemind_inference.embeddings import Embedder

EMBED_PATH = "/internal/embed"


def embed_scope(*, headers: dict[str, str] | None = None, path: str = EMBED_PATH) -> Scope:
    """构造内部接口的 HTTP scope，默认带上正确的 Bearer token。"""

    raw: dict[bytes, bytes] = {
        b"content-type": b"application/json",
        b"authorization": f"Bearer {TEST_TOKEN}".encode("latin-1"),
    }
    for name, value in (headers or {}).items():
        raw[name.lower().encode("latin-1")] = value.encode("latin-1")
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": list(raw.items()),
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }


def json_body(texts: Sequence[str], *, kind: str = "document", pad: int = 0) -> bytes:
    """构造请求体；``pad`` 额外塞一个很长的无关字段，用来模拟“解析前字节超限”。"""

    payload = json.dumps(
        {"kind": kind, "texts": list(texts), "pad": "P" * pad},
        ensure_ascii=False,
    )
    return payload.encode("utf-8")


@dataclass(frozen=True)
class AsgiResponse:
    status: int
    body: bytes

    def body_json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class AsgiRequest:
    """一次可等待、可取消的 ASGI 调用；receive 按测试给定的分块返回。"""

    def __init__(self, app: ASGIApp, scope: Scope, chunks: Sequence[bytes]) -> None:
        self._app = app
        self._scope = scope
        self._chunks: list[Message] = [
            {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
            for index, chunk in enumerate(chunks)
        ]
        self._messages: list[Message] = []
        self.started = False

    async def _receive(self) -> Message:
        if self._chunks:
            return self._chunks.pop(0)
        return {"type": "http.disconnect"}

    async def _send(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.started = True
        self._messages.append(message)

    async def run(self) -> AsgiResponse:
        await self._app(self._scope, self._receive, self._send)
        return self.response()

    def response(self) -> AsgiResponse:
        status = next(
            message["status"]
            for message in self._messages
            if message["type"] == "http.response.start"
        )
        body = b"".join(
            message.get("body", b"")
            for message in self._messages
            if message["type"] == "http.response.body"
        )
        return AsgiResponse(status=status, body=body)


async def call_embed(
    app: ASGIApp,
    *,
    chunks: Sequence[bytes],
    headers: dict[str, str] | None = None,
) -> AsgiResponse:
    return await AsgiRequest(app, embed_scope(headers=headers), chunks).run()


@asynccontextmanager
async def running_app(settings: Settings, embedder: Embedder) -> AsyncIterator[FastAPI]:
    """在真实 lifespan 下运行应用（gate 与任务登记表都由 lifespan 建立）。"""

    app = create_app(settings, embedder_factory=lambda _resolved: embedder)
    async with app.router.lifespan_context(app):
        yield app


async def wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    """等待条件成立；用于把并发回归钉在确定的状态上，而不是靠固定 sleep。"""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("等待条件在超时前未成立")
