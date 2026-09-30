"""把已校验的解析 IP 固定到实际 TCP 连接，关闭 DNS-rebinding 竞态窗口。

httpx 默认在连接阶段用 hostname 再做一次 DNS 解析，因此“先解析校验目标为公网地址、再用
hostname 连接”之间可被 DNS rebinding 利用。本模块用 httpcore 公开的 ``network_backend``
注入点，在 TCP 建连时用调用方已校验的 IP 替换 hostname；请求 URL 仍是原 hostname，因此
``Host`` 头、TLS SNI 与证书校验都保留原 hostname，且不关闭 ``verify``。没有固定 IP 的
host 一律拒绝，绝不回退到未固定的 DNS 解析。

边界：本模块只负责把“已校验 IP”固定到连接层；地址是否为公网、每跳允许主机与重定向策略
仍由 :mod:`rag_backend.ingestion.web_fetch` 负责，本模块不重复判定，也不声称覆盖其它
SSRF 或网络策略。
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from types import TracebackType
from typing import Final, cast

import httpcore
import httpx

__all__ = [
    "PinnedHTTPTransport",
    "PinnedNetworkBackend",
    "build_pinned_client",
]

# httpcore 异常到 httpx 异常的最小映射，顺序与 httpx HTTPTransport 保持一致，保证
# 连接/读取失败与超时仍以调用方认识的 httpx 异常类型冒泡。
_HTTPCORE_EXCEPTIONS: Final[dict[type[Exception], type[httpx.HTTPError]]] = {
    httpcore.TimeoutException: httpx.TimeoutException,
    httpcore.ConnectTimeout: httpx.ConnectTimeout,
    httpcore.ReadTimeout: httpx.ReadTimeout,
    httpcore.WriteTimeout: httpx.WriteTimeout,
    httpcore.PoolTimeout: httpx.PoolTimeout,
    httpcore.NetworkError: httpx.NetworkError,
    httpcore.ConnectError: httpx.ConnectError,
    httpcore.ReadError: httpx.ReadError,
    httpcore.WriteError: httpx.WriteError,
    httpcore.ProxyError: httpx.ProxyError,
    httpcore.UnsupportedProtocol: httpx.UnsupportedProtocol,
    httpcore.ProtocolError: httpx.ProtocolError,
    httpcore.LocalProtocolError: httpx.LocalProtocolError,
    httpcore.RemoteProtocolError: httpx.RemoteProtocolError,
}


@contextlib.contextmanager
def _map_httpcore_exceptions() -> Iterator[None]:
    """把 httpcore 异常转成对应的 httpx 异常，保持既有错误处理契约。"""

    try:
        yield
    except Exception as exc:
        mapped: type[httpx.HTTPError] | None = None
        for from_exc, to_exc in _HTTPCORE_EXCEPTIONS.items():
            if not isinstance(exc, from_exc):
                continue
            if mapped is None or issubclass(to_exc, mapped):
                mapped = to_exc
        if mapped is None:
            raise
        raise mapped(str(exc)) from exc


class PinnedNetworkBackend(httpcore.NetworkBackend):
    """只用调用方已校验的 IP 建立 TCP；按顺序尝试，不回落 hostname DNS。

    每个 host 对应一个已校验 IP 列表（保序）。一个 IP 连接失败就试下一个，但只在列表内；列表
    耗尽或没有该 host 时静态拒绝。多次尝试共享调用方传入的 connect 超时预算，超时不会因
    fallback 无限延长。
    """

    def __init__(
        self,
        pins: Mapping[str, Sequence[str]],
        *,
        delegate: httpcore.NetworkBackend | None = None,
    ) -> None:
        self._pins = {host: tuple(ips) for host, ips in pins.items()}
        self._delegate: httpcore.NetworkBackend = (
            httpcore.SyncBackend() if delegate is None else delegate
        )

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        pinned_ips = self._pins.get(host)
        if not pinned_ips:
            # 没有经过校验的固定 IP 就不连接，避免回退到未固定的 DNS 解析。
            raise httpcore.ConnectError("目标主机没有固定的已校验 IP")
        deadline = None if timeout is None else time.monotonic() + timeout
        last_error: httpcore.ConnectError | httpcore.ConnectTimeout | None = None
        for pinned_ip in pinned_ips:
            remaining: float | None = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
            try:
                return self._delegate.connect_tcp(
                    pinned_ip,
                    port,
                    timeout=remaining,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as error:
                last_error = error
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError("目标主机没有可用的已校验 IP")

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        return self._delegate.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options
        )

    def sleep(self, seconds: float) -> None:
        self._delegate.sleep(seconds)


class _HttpcoreResponseStream(httpx.SyncByteStream):
    """把 httpcore 响应流适配为 httpx ``SyncByteStream``，并保留 close 语义。"""

    def __init__(self, stream: Iterable[bytes]) -> None:
        self._stream = stream

    def __iter__(self) -> Iterator[bytes]:
        with _map_httpcore_exceptions():
            yield from self._stream

    def close(self) -> None:
        close = getattr(self._stream, "close", None)
        if close is not None:
            with _map_httpcore_exceptions():
                close()


class PinnedHTTPTransport(httpx.BaseTransport):
    """基于 httpcore :class:`~httpcore.ConnectionPool` 的 httpx transport。

    ``pins`` 是 hostname → 已校验 IP 列表的固定映射；请求 URL 保持 hostname，因此 ``Host`` 头、
    TLS SNI 与证书校验都用原 hostname，只有 TCP 建连改用固定 IP。
    """

    def __init__(
        self,
        pins: Mapping[str, Sequence[str]],
        *,
        delegate: httpcore.NetworkBackend | None = None,
    ) -> None:
        self._pool = httpcore.ConnectionPool(
            # 显式 verify=True 且不读环境变量，证书校验不因固定 IP 而关闭。
            ssl_context=httpx.create_ssl_context(verify=True, trust_env=False),
            network_backend=PinnedNetworkBackend(pins, delegate=delegate),
            retries=0,
            http1=True,
            http2=False,
        )

    def __enter__(self) -> PinnedHTTPTransport:
        self._pool.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        self._pool.__exit__(exc_type, exc_value, traceback)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with _map_httpcore_exceptions():
            core_response = self._pool.handle_request(core_request)
        return httpx.Response(
            status_code=core_response.status,
            headers=core_response.headers,
            stream=_HttpcoreResponseStream(cast("Iterable[bytes]", core_response.stream)),
            extensions=core_response.extensions,
        )

    def close(self) -> None:
        self._pool.close()


def build_pinned_client(host: str, pinned_ips: Sequence[str]) -> httpx.Client:
    """构造只把 ``host`` 固定到已校验 IP 列表的 httpx 客户端。

    客户端不跟随重定向、不读环境代理与自定义 CA、不发 Cookie、显式请求
    ``Accept-Encoding: identity``；每次抓取跳只应固定一个 host。
    """

    return httpx.Client(
        transport=PinnedHTTPTransport({host: pinned_ips}),
        trust_env=False,
        follow_redirects=False,
        cookies=None,
        headers={"Accept-Encoding": "identity"},
    )
