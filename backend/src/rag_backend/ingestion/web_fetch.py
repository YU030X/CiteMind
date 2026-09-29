"""受限静态网页抓取：只做 http/https GET、逐跳校验目标地址并返回原始 HTML 字节。

冻结边界（与 [安全](../../../docs/security.md) 一致）：

- 只接受 ``http``/``https``，拒绝 userinfo、fragment 与非默认端口（只允许 80/443）。
- 允许主机由服务端配置的精确规范化 host 列表给出（IDNA 小写、去掉尾点），**不做**后缀或
  通配符匹配；列表为空即功能禁用，任何请求都静态失败。
- 每次请求（含每一跳重定向）都用标准库解析器解析全部 A/AAAA 记录，任一地址属于非公网、
  回环、私有、link-local、多播、保留、未指定、CGNAT 或 IPv4-mapped 即拒绝；最多 3 跳，
  每跳重新校验，且拒绝 https→http 降级。
- 复用 httpx：``trust_env=False``、零自动重试、不发送 Cookie/Authorization、显式
  ``Accept-Encoding: identity``、只接受 200、Content-Type 必须是 ``text/html`` 或
  ``application/xhtml+xml``、拒绝任意 ``Content-Encoding``、``Content-Length`` 早拒并
  对 ``iter_raw`` 累计硬上限 2 MiB、显式 connect/read/write/pool 超时。
- 用 hostname 正常连接，不做 IP pin 或 ``getpeername`` 校验；DNS 解析校验与实际连接之间
  仍存在竞态窗口，完整 SSRF/网络策略属 Phase 4，本模块**不声称**抗 DNS rebinding。
- 所有失败都收敛为静态脱敏错误码，不携带 URL、主机、地址或底层异常文本。
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Final
from urllib.parse import urljoin, urlsplit

import httpx

# 静态错误码；HTTP 路由按码映射为具名错误体，消息不含 URL 或地址。
CODE_URL_INVALID: Final = "URL_INVALID"
CODE_NOT_ALLOWED: Final = "NOT_ALLOWED"
CODE_TOO_MANY_REDIRECTS: Final = "TOO_MANY_REDIRECTS"
CODE_NOT_HTML: Final = "NOT_HTML"
CODE_TOO_LARGE: Final = "TOO_LARGE"
CODE_FETCH_FAILED: Final = "FETCH_FAILED"
CODE_FETCH_TIMEOUT: Final = "FETCH_TIMEOUT"

# 单次响应的最大原始字节数（2 MiB）；``Content-Length`` 早拒与流式累计硬上限共用。
MAX_WEB_CONTENT_BYTES: Final = 2 * 1024 * 1024
# 最多跟随的重定向跳数；超过即静态失败。
MAX_WEB_REDIRECTS: Final = 3

DEFAULT_PORT_BY_SCHEME: Final = {"http": 80, "https": 443}
HTML_MEDIA_TYPES: Final = frozenset({"text/html", "application/xhtml+xml"})

# 显式的 connect/read/write/pool 超时；不使用 httpx 默认值。
WEB_FETCH_TIMEOUT: Final = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)

_STATIC_MESSAGES: Final = {
    CODE_URL_INVALID: "网页地址无效",
    CODE_NOT_ALLOWED: "该网页地址不被允许",
    CODE_TOO_MANY_REDIRECTS: "网页重定向次数过多",
    CODE_NOT_HTML: "目标不是受支持的 HTML 页面",
    CODE_TOO_LARGE: "网页内容超过 2 MiB 上限",
    CODE_FETCH_FAILED: "网页抓取失败",
    CODE_FETCH_TIMEOUT: "网页抓取超时",
}


class WebFetchError(Exception):
    """静态脱敏的抓取失败；只带稳定错误码，不回显 URL、主机或底层异常。"""

    def __init__(self, code: str) -> None:
        super().__init__(_STATIC_MESSAGES[code])
        self.code = code

    @property
    def static_message(self) -> str:
        return _STATIC_MESSAGES[self.code]


@dataclass(frozen=True, slots=True)
class NormalizedWebUrl:
    """规范化后的目标 URL；``url`` 用于实际连接与幂等比对。"""

    scheme: str
    host: str
    port: int
    url: str


@dataclass(frozen=True, slots=True)
class FetchedWebDocument:
    """一次成功抓取的结果：原始 HTML 字节与最终（跟随重定向后）URL。"""

    content: bytes
    final_url: str


def normalize_web_host(raw: str) -> str:
    """把配置或 URL 中的 host 规范化为 IDNA 小写、去掉尾点的精确 host。

    只接受不带 scheme/端口/path/userinfo/通配符的单一主机名或 IPv4 字面量；不合法抛
    :class:`ValueError`。IPv6 字面量带冒号，这里不支持（抓取目标应是可解析主机名）。
    """

    host = raw.strip().rstrip(".")
    if not host:
        raise ValueError("host 不能为空")
    if any(character in host for character in ("/", ":", "@", "*", "?", "#", " ", "[", "]")):
        raise ValueError("host 只能是不带端口或通配符的精确主机名")
    try:
        return host.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise ValueError("host 不是合法的 IDNA 主机名") from error


def parse_allowed_web_hosts(value: str) -> frozenset[str]:
    """解析逗号分隔的允许 host 列表；空值返回空集合（功能禁用）。

    只做精确规范化，不展开后缀或通配符；任一条目非法即抛出 ``ValueError``，让启动显式失败。
    """

    hosts: set[str] = set()
    for item in value.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        hosts.add(normalize_web_host(candidate))
    return frozenset(hosts)


def normalize_web_url(raw: str) -> NormalizedWebUrl:
    """校验并规范化目标 URL；不合法抛 :class:`WebFetchError(URL_INVALID)`。"""

    try:
        parts = urlsplit(raw)
    except ValueError as error:
        raise WebFetchError(CODE_URL_INVALID) from error
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORT_BY_SCHEME:
        raise WebFetchError(CODE_URL_INVALID)
    if "@" in parts.netloc:
        # userinfo（user:pass@host）是禁止的；凭据绝不随请求外发。
        raise WebFetchError(CODE_URL_INVALID)
    if parts.fragment:
        raise WebFetchError(CODE_URL_INVALID)
    if not parts.hostname:
        raise WebFetchError(CODE_URL_INVALID)
    try:
        port = parts.port
    except ValueError as error:
        raise WebFetchError(CODE_URL_INVALID) from error
    if port is not None and port != DEFAULT_PORT_BY_SCHEME[scheme]:
        # 只允许默认端口；自定义端口会增加绕过网络策略的面。
        raise WebFetchError(CODE_URL_INVALID)
    try:
        host = normalize_web_host(parts.hostname)
    except ValueError as error:
        raise WebFetchError(CODE_URL_INVALID) from error
    path = parts.path or "/"
    query = f"?{parts.query}" if parts.query else ""
    return NormalizedWebUrl(
        scheme=scheme,
        host=host,
        port=DEFAULT_PORT_BY_SCHEME[scheme],
        url=f"{scheme}://{host}{path}{query}",
    )


def _is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """只有真正的公网单播地址才允许；IPv4-mapped IPv6 一律拒绝。"""

    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return False
    return address.is_global


def resolve_public_addresses(host: str, port: int) -> None:
    """用标准库解析器解析 A/AAAA，任一地址非公网即抛 NOT_ALLOWED。

    解析失败或没有记录抛 FETCH_FAILED。这里只校验解析结果，实际连接仍用 hostname，因此
    校验与连接之间存在 DNS 竞态窗口（见模块 docstring）。
    """

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise WebFetchError(CODE_FETCH_FAILED) from error
    if not infos:
        raise WebFetchError(CODE_FETCH_FAILED)
    for info in infos:
        raw_address = info[4][0]
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError as error:
            raise WebFetchError(CODE_FETCH_FAILED) from error
        if not _is_public_address(address):
            raise WebFetchError(CODE_NOT_ALLOWED)


Fetcher = Callable[[str], FetchedWebDocument]
Resolver = Callable[[str, int], None]


def _read_bounded_body(response: httpx.Response) -> bytes:
    """在 2 MiB 硬上限内流式读取原始响应体；超限抛 TOO_LARGE。"""

    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            declared_bytes = int(declared)
        except ValueError as error:
            raise WebFetchError(CODE_FETCH_FAILED) from error
        if declared_bytes < 0:
            raise WebFetchError(CODE_FETCH_FAILED)
        if declared_bytes > MAX_WEB_CONTENT_BYTES:
            raise WebFetchError(CODE_TOO_LARGE)
    body = bytearray()
    try:
        for chunk in response.iter_raw():
            body.extend(chunk)
            if len(body) > MAX_WEB_CONTENT_BYTES:
                raise WebFetchError(CODE_TOO_LARGE)
    except httpx.HTTPError as error:
        raise WebFetchError(CODE_FETCH_FAILED) from error
    return bytes(body)


def _validate_content(response: httpx.Response) -> None:
    """校验 Content-Type 与 Content-Encoding；不合规抛静态错误。"""

    media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type not in HTML_MEDIA_TYPES:
        raise WebFetchError(CODE_NOT_HTML)
    encoding = response.headers.get("content-encoding")
    if encoding is not None and encoding.strip().lower() not in ("", "identity"):
        # 拒绝任意压缩编码，避免解压膨胀与嗅探绕过。
        raise WebFetchError(CODE_FETCH_FAILED)


def fetch_web_html(
    url: str,
    *,
    allowed_hosts: Collection[str],
    client: httpx.Client | None = None,
    resolver: Resolver = resolve_public_addresses,
) -> FetchedWebDocument:
    """按冻结策略抓取单个静态 HTML 页面，返回原始字节与最终 URL。

    允许主机为空集合时任何请求都 NOT_ALLOWED（fail closed）。``client`` 与 ``resolver``
    仅用于测试注入；生产路径自行构造不跟随重定向、``trust_env=False`` 的 httpx 客户端。
    """

    owns_client = client is None
    effective_client = client or httpx.Client(
        timeout=WEB_FETCH_TIMEOUT,
        trust_env=False,
        follow_redirects=False,
        cookies=None,
        headers={"Accept-Encoding": "identity"},
    )
    try:
        current = normalize_web_url(url)
        redirects = 0
        while True:
            if current.host not in allowed_hosts:
                raise WebFetchError(CODE_NOT_ALLOWED)
            resolver(current.host, current.port)
            try:
                with effective_client.stream(
                    "GET",
                    current.url,
                    headers={"Accept-Encoding": "identity"},
                    timeout=WEB_FETCH_TIMEOUT,
                ) as response:
                    if response.status_code == 200:
                        _validate_content(response)
                        return FetchedWebDocument(
                            content=_read_bounded_body(response),
                            final_url=current.url,
                        )
                    if 300 <= response.status_code < 400:
                        location = response.headers.get("location")
                        if not location:
                            raise WebFetchError(CODE_FETCH_FAILED)
                        if redirects >= MAX_WEB_REDIRECTS:
                            raise WebFetchError(CODE_TOO_MANY_REDIRECTS)
                        target = normalize_web_url(urljoin(current.url, location))
                        if current.scheme == "https" and target.scheme == "http":
                            # 拒绝安全降级。
                            raise WebFetchError(CODE_NOT_ALLOWED)
                        current = target
                        redirects += 1
                        continue
                    raise WebFetchError(CODE_FETCH_FAILED)
            except httpx.TimeoutException as error:
                raise WebFetchError(CODE_FETCH_TIMEOUT) from error
            except httpx.HTTPError as error:
                raise WebFetchError(CODE_FETCH_FAILED) from error
    finally:
        if owns_client:
            effective_client.close()


__all__ = [
    "CODE_FETCH_FAILED",
    "CODE_FETCH_TIMEOUT",
    "CODE_NOT_ALLOWED",
    "CODE_NOT_HTML",
    "CODE_TOO_LARGE",
    "CODE_TOO_MANY_REDIRECTS",
    "CODE_URL_INVALID",
    "MAX_WEB_CONTENT_BYTES",
    "MAX_WEB_REDIRECTS",
    "WEB_FETCH_TIMEOUT",
    "FetchedWebDocument",
    "Fetcher",
    "NormalizedWebUrl",
    "Resolver",
    "WebFetchError",
    "fetch_web_html",
    "normalize_web_host",
    "normalize_web_url",
    "parse_allowed_web_hosts",
    "resolve_public_addresses",
]
