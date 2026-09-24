"""客户端 IP 解析的纯逻辑测试：可信代理白名单、单值头与退回对端。"""

from typing import Any

from rag_backend.auth.client_ip import resolve_client_ip
from rag_backend.config import Settings
from starlette.requests import Request

TRUSTED = "172.28.10.0/24"


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"_env_file": None, "environment": "test"}
    values.update(overrides)
    return Settings(**values)


def make_request(
    *,
    client: tuple[str, int] | None = ("172.28.10.5", 12345),
    headers: dict[str, str] | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/auth/login",
        "headers": [
            (key.lower().encode(), value.encode())
            for key, value in (headers or {}).items()
        ],
        "state": {},
    }
    if client is not None:
        scope["client"] = client
    return Request(scope)


def test_untrusted_peer_ignores_forwarding_header() -> None:
    request = make_request(client=("203.0.113.9", 12345), headers={"X-Real-IP": "198.51.100.1"})

    assert resolve_client_ip(request, make_settings()) == "203.0.113.9"


def test_untrusted_peer_in_trusted_network_but_different_subnet() -> None:
    request = make_request(client=("10.1.2.3", 12345), headers={"X-Real-IP": "198.51.100.1"})

    assert resolve_client_ip(request, make_settings(trusted_proxy_cidrs=TRUSTED)) == "10.1.2.3"


def test_trusted_peer_accepts_single_valid_forwarded_ip() -> None:
    request = make_request(headers={"X-Real-IP": "198.51.100.7"})

    assert (
        resolve_client_ip(request, make_settings(trusted_proxy_cidrs=TRUSTED))
        == "198.51.100.7"
    )


def test_trusted_peer_normalises_ipv6_forwarded_ip() -> None:
    request = make_request(headers={"X-Real-IP": "2001:0db8::1"})

    assert (
        resolve_client_ip(request, make_settings(trusted_proxy_cidrs=TRUSTED))
        == "2001:db8::1"
    )


def test_trusted_peer_missing_header_falls_back_to_peer() -> None:
    request = make_request()

    assert resolve_client_ip(request, make_settings(trusted_proxy_cidrs=TRUSTED)) == "172.28.10.5"


def test_trusted_peer_rejects_comma_separated_header() -> None:
    request = make_request(headers={"X-Real-IP": "198.51.100.7, 10.0.0.1"})

    assert resolve_client_ip(request, make_settings(trusted_proxy_cidrs=TRUSTED)) == "172.28.10.5"


def test_trusted_peer_rejects_invalid_header_value() -> None:
    request = make_request(headers={"X-Real-IP": "not-an-ip"})

    assert resolve_client_ip(request, make_settings(trusted_proxy_cidrs=TRUSTED)) == "172.28.10.5"


def test_trusted_peer_rejects_blank_header_value() -> None:
    request = make_request(headers={"X-Real-IP": "   "})

    assert resolve_client_ip(request, make_settings(trusted_proxy_cidrs=TRUSTED)) == "172.28.10.5"


def test_custom_header_name_is_honored() -> None:
    request = make_request(headers={"X-Client-IP": "198.51.100.7"})
    settings = make_settings(trusted_proxy_cidrs=TRUSTED, client_ip_header="X-Client-IP")

    assert resolve_client_ip(request, settings) == "198.51.100.7"


def test_missing_client_falls_back_to_unknown() -> None:
    request = make_request(client=None)

    assert resolve_client_ip(request, make_settings(trusted_proxy_cidrs=TRUSTED)) == "unknown"
