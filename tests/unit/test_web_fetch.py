"""受限静态网页抓取的纯本地测试：MockTransport + 假解析器，不联网、不起服务。

覆盖：URL/主机规范化、允许列表 fail closed、逐跳解析与重定向、ssrf 类地址拒绝、
Content-Type/Encoding/大小/超时静态失败，以及请求头不含 Cookie/Authorization。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from rag_backend.ingestion import web_fetch as wf

ALLOWED = frozenset({"example.com"})


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )


def _ok_resolver(seen: list[tuple[str, int]] | None = None) -> wf.Resolver:
    def resolver(host: str, port: int) -> None:
        if seen is not None:
            seen.append((host, port))

    return resolver


def _reject_resolver(_host: str, _port: int) -> None:
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


def test_settings_default_disables_web_and_rejects_wildcard() -> None:
    from rag_backend.config import Settings

    assert Settings(_env_file=None).web_fetch_allowed_host_set == frozenset()
    settings = Settings(_env_file=None, web_fetch_allowed_hosts="Example.com., api.example.com")
    assert settings.web_fetch_allowed_host_set == frozenset(
        {"example.com", "api.example.com"}
    )
    with pytest.raises(ValueError):
        Settings(_env_file=None, web_fetch_allowed_hosts="*.example.com")
