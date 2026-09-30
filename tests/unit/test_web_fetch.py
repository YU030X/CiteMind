"""受限静态网页抓取的纯本地测试：MockTransport + 假解析器，不联网、不起服务。

覆盖：URL/主机规范化、允许列表 fail closed、逐跳解析与重定向、ssrf 类地址拒绝、
Content-Type/Encoding/大小/超时静态失败，以及请求头不含 Cookie/Authorization。
"""

from __future__ import annotations

import socket
import ssl
from collections.abc import Callable, Iterator
from typing import Any

import httpcore
import httpx
import pytest
from rag_backend.ingestion import web_fetch as wf
from rag_backend.ingestion import web_fetch_transport as wft

ALLOWED = frozenset({"example.com"})
_PUBLIC_IP = "93.184.216.34"


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )


def _ok_resolver(seen: list[tuple[str, int]] | None = None) -> wf.Resolver:
    def resolver(host: str, port: int) -> tuple[str, ...]:
        if seen is not None:
            seen.append((host, port))
        return (_PUBLIC_IP,)

    return resolver


def _reject_resolver(_host: str, _port: int) -> tuple[str, ...]:
    raise wf.WebFetchError(wf.CODE_NOT_ALLOWED)


def test_normalize_host_strips_trailing_dot_and_lowercases_idna() -> None:
    assert wf.normalize_web_host("Example.COM.") == "example.com"
    assert wf.normalize_web_host("b\u00fccher.example") == "xn--bcher-kva.example"


def test_parse_allowed_web_hosts_rejects_wildcard_and_port() -> None:
    assert wf.parse_allowed_web_hosts("Example.com., api.example.com") == frozenset(
        {"example.com", "api.example.com"}
    )
    assert wf.parse_allowed_web_hosts("") == frozenset()
    for bad in ("*.example.com", "example.com:8080", "http://example.com"):
        with pytest.raises(ValueError):
            wf.normalize_web_host(bad)


def test_normalize_url_rejects_userinfo_fragment_and_custom_port() -> None:
    for bad in (
        "ftp://example.com/a",
        "https://user:pass@example.com/a",
        "https://example.com/a#frag",
        "https://example.com:8443/a",
        "https://",
    ):
        with pytest.raises(wf.WebFetchError) as error:
            wf.normalize_web_url(bad)
        assert error.value.code == wf.CODE_URL_INVALID
    assert wf.normalize_web_url("https://Example.com/a?q=1").url == (
        "https://example.com/a?q=1"
    )


def test_empty_allowlist_fails_closed_without_network() -> None:
    def explode(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("空允许列表不得发起连接")

    with pytest.raises(wf.WebFetchError) as error:
        wf.fetch_web_html(
            "https://example.com/",
            allowed_hosts=frozenset(),
            client=_client(explode),
            resolver=_ok_resolver(),
        )
    assert error.value.code == wf.CODE_NOT_ALLOWED


def test_non_public_dns_address_is_rejected() -> None:
    with pytest.raises(wf.WebFetchError) as error:
        wf.fetch_web_html(
            "https://example.com/",
            allowed_hosts=ALLOWED,
            client=_client(lambda _r: httpx.Response(200)),
            resolver=_reject_resolver,
        )
    assert error.value.code == wf.CODE_NOT_ALLOWED


def test_resolver_rejects_private_and_ipv4_mapped_addresses() -> None:
    import ipaddress

    assert not wf._is_public_address(ipaddress.ip_address("127.0.0.1"))
    assert not wf._is_public_address(ipaddress.ip_address("10.0.0.5"))
    assert not wf._is_public_address(ipaddress.ip_address("100.64.0.1"))
    assert not wf._is_public_address(ipaddress.ip_address("169.254.169.254"))
    assert not wf._is_public_address(ipaddress.ip_address("::ffff:8.8.8.8"))
    assert wf._is_public_address(ipaddress.ip_address("8.8.8.8"))


def test_happy_path_returns_raw_html_and_headers_are_minimal() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=[b"<html><body><p>hello</p></body></html>"],
        )

    seen: list[tuple[str, int]] = []
    result = wf.fetch_web_html(
        "https://example.com/page",
        allowed_hosts=ALLOWED,
        client=_client(handler),
        resolver=_ok_resolver(seen),
    )

    assert result.content == b"<html><body><p>hello</p></body></html>"
    assert result.final_url == "https://example.com/page"
    assert seen == [("example.com", 443)]
    assert "cookie" not in captured["headers"]
    assert "authorization" not in captured["headers"]
    assert captured["headers"]["accept-encoding"] == "identity"


def test_redirects_are_revalidated_and_final_url_follows() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/end"})
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=[b"<p>end</p>"]
        )

    seen: list[tuple[str, int]] = []
    result = wf.fetch_web_html(
        "https://example.com/start",
        allowed_hosts=ALLOWED,
        client=_client(handler),
        resolver=_ok_resolver(seen),
    )

    assert result.content == b"<p>end</p>"
    assert result.final_url == "https://example.com/end"
    assert len(seen) == 2


def test_too_many_redirects_fails_statically() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": str(request.url) + "x"})

    with pytest.raises(wf.WebFetchError) as error:
        wf.fetch_web_html(
            "https://example.com/start",
            allowed_hosts=ALLOWED,
            client=_client(handler),
            resolver=_ok_resolver(),
        )
    assert error.value.code == wf.CODE_TOO_MANY_REDIRECTS


def test_https_to_http_redirect_is_rejected() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"location": "http://example.com/plain"})

    with pytest.raises(wf.WebFetchError) as error:
        wf.fetch_web_html(
            "https://example.com/start",
            allowed_hosts=ALLOWED,
            client=_client(handler),
            resolver=_ok_resolver(),
        )
    assert error.value.code == wf.CODE_NOT_ALLOWED


def test_non_html_content_type_is_rejected() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/json"}, content=b"{}"
        )

    with pytest.raises(wf.WebFetchError) as error:
        wf.fetch_web_html(
            "https://example.com/x",
            allowed_hosts=ALLOWED,
            client=_client(handler),
            resolver=_ok_resolver(),
        )
    assert error.value.code == wf.CODE_NOT_HTML


def test_content_encoding_is_rejected() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html", "content-encoding": "gzip"},
            content=b"compressed-ish",
        )

    with pytest.raises(wf.WebFetchError) as error:
        wf.fetch_web_html(
            "https://example.com/x",
            allowed_hosts=ALLOWED,
            client=_client(handler),
            resolver=_ok_resolver(),
        )
    assert error.value.code == wf.CODE_FETCH_FAILED


def test_content_length_over_limit_is_rejected_early() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "text/html",
                "content-length": str(wf.MAX_WEB_CONTENT_BYTES + 1),
            },
            content=b"x",
        )

    with pytest.raises(wf.WebFetchError) as error:
        wf.fetch_web_html(
            "https://example.com/x",
            allowed_hosts=ALLOWED,
            client=_client(handler),
            resolver=_ok_resolver(),
        )
    assert error.value.code == wf.CODE_TOO_LARGE


def test_streaming_body_over_limit_is_rejected() -> None:
    oversized = b"a" * (wf.MAX_WEB_CONTENT_BYTES + 1)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=oversized
        )

    with pytest.raises(wf.WebFetchError) as error:
        wf.fetch_web_html(
            "https://example.com/x",
            allowed_hosts=ALLOWED,
            client=_client(handler),
            resolver=_ok_resolver(),
        )
    assert error.value.code == wf.CODE_TOO_LARGE


def test_non_200_status_is_static_failure() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"nope")

    with pytest.raises(wf.WebFetchError) as error:
        wf.fetch_web_html(
            "https://example.com/x",
            allowed_hosts=ALLOWED,
            client=_client(handler),
            resolver=_ok_resolver(),
        )
    assert error.value.code == wf.CODE_FETCH_FAILED


def test_timeout_is_static_failure() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom")

    with pytest.raises(wf.WebFetchError) as error:
        wf.fetch_web_html(
            "https://example.com/x",
            allowed_hosts=ALLOWED,
            client=_client(handler),
            resolver=_ok_resolver(),
        )
    assert error.value.code == wf.CODE_FETCH_TIMEOUT


def test_resolve_public_addresses_returns_validated_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_getaddrinfo(host: str, port: int, **kwargs: Any) -> list[tuple[Any, ...]]:
        assert host == "example.com"
        assert port == 443
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_IP, port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    assert wf.resolve_public_addresses("example.com", 443) == (_PUBLIC_IP,)


def test_resolve_public_addresses_keeps_order_and_dedupes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_getaddrinfo(host: str, port: int, **kwargs: Any) -> list[tuple[Any, ...]]:
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:4860:4860::8888", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_IP, port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_IP, port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    assert wf.resolve_public_addresses("example.com", 443) == (
        "2001:4860:4860::8888",
        _PUBLIC_IP,
    )


def test_resolve_public_addresses_rejects_if_any_record_is_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_getaddrinfo(host: str, port: int, **kwargs: Any) -> list[tuple[Any, ...]]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_IP, port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(wf.WebFetchError) as error:
        wf.resolve_public_addresses("example.com", 443)
    assert error.value.code == wf.CODE_NOT_ALLOWED


class _CloseSpyClient(httpx.Client):
    """记录 close 次数的 httpx 客户端，用来断言每跳客户端都被回收。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        super().close()


def test_fetch_pins_each_hop_to_its_validated_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pinned: list[tuple[str, tuple[str, ...]]] = []
    ips = {"example.com": ("93.184.216.34",), "cdn.example.com": ("18.65.0.1",)}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "https://cdn.example.com/end"})
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=[b"<p>end</p>"]
        )

    def fake_build(host: str, pinned_ips: Any) -> httpx.Client:
        pinned.append((host, tuple(pinned_ips)))
        return _CloseSpyClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        )

    monkeypatch.setattr(wf, "build_pinned_client", fake_build)

    result = wf.fetch_web_html(
        "https://example.com/start",
        allowed_hosts=frozenset({"example.com", "cdn.example.com"}),
        resolver=lambda host, _port: ips[host],
    )

    assert result.content == b"<p>end</p>"
    assert pinned == [
        ("example.com", ("93.184.216.34",)),
        ("cdn.example.com", ("18.65.0.1",)),
    ]


def test_fetch_closes_pinned_client_for_each_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: list[_CloseSpyClient] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "https://cdn.example.com/end"})
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=[b"<p>end</p>"]
        )

    def fake_build(_host: str, _pinned_ips: Any) -> httpx.Client:
        client = _CloseSpyClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        )
        clients.append(client)
        return client

    monkeypatch.setattr(wf, "build_pinned_client", fake_build)

    wf.fetch_web_html(
        "https://example.com/start",
        allowed_hosts=frozenset({"example.com", "cdn.example.com"}),
        resolver=_ok_resolver(),
    )

    assert len(clients) == 2
    assert [client.close_calls for client in clients] == [1, 1]


def test_fetch_closes_pinned_client_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: list[_CloseSpyClient] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    def fake_build(_host: str, _pinned_ips: Any) -> httpx.Client:
        client = _CloseSpyClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        )
        clients.append(client)
        return client

    monkeypatch.setattr(wf, "build_pinned_client", fake_build)

    with pytest.raises(wf.WebFetchError) as error:
        wf.fetch_web_html(
            "https://example.com/x",
            allowed_hosts=ALLOWED,
            resolver=_ok_resolver(),
        )

    assert error.value.code == wf.CODE_FETCH_FAILED
    assert len(clients) == 1
    assert clients[0].close_calls == 1


def test_settings_default_disables_web_and_rejects_wildcard() -> None:
    from rag_backend.config import Settings

    # ``_env_file`` 用 kwargs 字典传，显式不读仓库根 .env 且避免 mypy 误报。
    values: dict[str, Any] = {"_env_file": None}
    assert Settings(**values).web_fetch_allowed_host_set == frozenset()
    values = {
        "_env_file": None,
        "web_fetch_allowed_hosts": "Example.com., api.example.com",
    }
    settings = Settings(**values)
    assert settings.web_fetch_allowed_host_set == frozenset(
        {"example.com", "api.example.com"}
    )
    values = {"_env_file": None, "web_fetch_allowed_hosts": "*.example.com"}
    with pytest.raises(ValueError):
        Settings(**values)


class _RecordingTlsStream(httpcore.NetworkStream):
    """替身流：记录 httpcore 在 TLS 阶段传入的 SNI 与证书上下文后失败。"""

    def __init__(self) -> None:
        self.tls_hostname: str | None = None
        self.ssl_context: ssl.SSLContext | None = None

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        raise NotImplementedError

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        self.ssl_context = ssl_context
        self.tls_hostname = server_hostname
        raise httpcore.ConnectError("stop before handshake")

    def get_extra_info(self, info: str) -> Any:
        return None


class _RecordingBackend(httpcore.NetworkBackend):
    """替身 network backend：记录实际传给 TCP 建连的 host/port，可指定哪些 host 连接失败。"""

    def __init__(self, *, failing: frozenset[str] = frozenset()) -> None:
        self.attempts: list[tuple[str, int]] = []
        self.failing = failing
        self.stream = _RecordingTlsStream()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        self.attempts.append((host, port))
        if host in self.failing:
            raise httpcore.ConnectError("unreachable")
        return self.stream

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        raise NotImplementedError

    def sleep(self, seconds: float) -> None:
        pass


class _CloseFailingStream:
    """替身响应流：close 抛 httpcore 错误，用于验证关闭时的异常映射。"""

    def __iter__(self) -> Iterator[bytes]:
        return iter(())

    def close(self) -> None:
        raise httpcore.ReadError("close failed")


def test_transport_connects_to_pinned_ip_and_keeps_hostname_for_tls() -> None:
    backend = _RecordingBackend()
    transport = wft.PinnedHTTPTransport({"example.com": (_PUBLIC_IP,)}, delegate=backend)

    with httpx.Client(transport=transport, trust_env=False) as client:
        with pytest.raises(httpx.ConnectError):
            client.get("https://example.com/")

    # TCP 建连接收已校验 IP，而不是 hostname；TLS SNI 与证书校验仍用原 hostname。
    assert backend.attempts == [(_PUBLIC_IP, 443)]
    assert backend.stream.tls_hostname == "example.com"
    assert backend.stream.ssl_context is not None
    assert backend.stream.ssl_context.check_hostname is True
    assert backend.stream.ssl_context.verify_mode == ssl.CERT_REQUIRED


def test_transport_falls_back_within_validated_ips() -> None:
    """首个（AAAA v6）连接失败后只在已校验列表内按序回退到 A v4。"""

    backend = _RecordingBackend(failing=frozenset({"2001:4860:4860::8888"}))
    transport = wft.PinnedHTTPTransport(
        {"example.com": ("2001:4860:4860::8888", _PUBLIC_IP)}, delegate=backend
    )

    with httpx.Client(transport=transport, trust_env=False) as client:
        with pytest.raises(httpx.ConnectError):
            client.get("https://example.com/")

    assert backend.attempts == [("2001:4860:4860::8888", 443), (_PUBLIC_IP, 443)]
    assert backend.stream.tls_hostname == "example.com"


def test_transport_does_not_re_resolve_hostname_after_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """即使校验后 DNS 变指向私网，连接仍使用已校验 IP。"""

    def rebind(host: str, port: int, **kwargs: Any) -> list[tuple[Any, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", port))]

    monkeypatch.setattr(socket, "getaddrinfo", rebind)
    backend = _RecordingBackend()
    transport = wft.PinnedHTTPTransport({"example.com": (_PUBLIC_IP,)}, delegate=backend)

    with httpx.Client(transport=transport, trust_env=False) as client:
        with pytest.raises(httpx.ConnectError):
            client.get("https://example.com/")

    assert backend.attempts == [(_PUBLIC_IP, 443)]


def test_transport_uses_punycode_host_as_pin_key_and_sni() -> None:
    backend = _RecordingBackend()
    normalized = wf.normalize_web_url("https://b\u00fccher.example/page")
    assert normalized.host == "xn--bcher-kva.example"
    transport = wft.PinnedHTTPTransport({normalized.host: (_PUBLIC_IP,)}, delegate=backend)

    with httpx.Client(transport=transport, trust_env=False) as client:
        with pytest.raises(httpx.ConnectError):
            client.get(normalized.url)

    assert backend.attempts == [(_PUBLIC_IP, 443)]
    assert backend.stream.tls_hostname == "xn--bcher-kva.example"


def test_network_backend_rejects_host_without_pinned_ip() -> None:
    backend = wft.PinnedNetworkBackend({"example.com": (_PUBLIC_IP,)})

    with pytest.raises(httpcore.ConnectError):
        backend.connect_tcp("evil.example", 443)


def test_network_backend_only_attempts_validated_ips() -> None:
    delegate = _RecordingBackend(failing=frozenset({"1.2.3.4", "5.6.7.8"}))
    backend = wft.PinnedNetworkBackend(
        {"example.com": ("1.2.3.4", "5.6.7.8")}, delegate=delegate
    )

    with pytest.raises(httpcore.ConnectError):
        backend.connect_tcp("example.com", 443)

    assert delegate.attempts == [("1.2.3.4", 443), ("5.6.7.8", 443)]


def test_response_stream_close_maps_httpcore_errors() -> None:
    stream = wft._HttpcoreResponseStream(_CloseFailingStream())

    with pytest.raises(httpx.ReadError):
        stream.close()
