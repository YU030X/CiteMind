"""客户端 IP 解析：只信任显式配置的可信代理。

真实部署里 API 只从网关接收流量，直连对端是网关容器地址。限流必须按真实客户端 IP
计数，但客户端可以伪造请求头，因此规则是：

1. 直连对端不在 ``trusted_proxy_cidrs`` 内时，一律使用对端地址；不读任何转发头。
2. 对端可信时，只接受 ``client_ip_header``（默认 ``X-Real-IP``）中的一个合法 IP；
   缺失、逗号列表或非法值都退回对端地址。
3. 网关必须用 ``$remote_addr`` 覆盖该头（见 ``deploy/compose/gateway/nginx.conf``），
   因此客户端自带的同名头不会生效。不使用 ``X-Forwarded-For``，因为常见配置会追加
   客户端值，最左值可被伪造。
"""

from __future__ import annotations

import ipaddress

from fastapi import Request

from rag_backend.config import Settings

UNKNOWN_CLIENT_IP = "unknown"


def _parse_peer(peer: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(peer)
    except ValueError:
        return None


def _is_trusted_proxy(
    peer: ipaddress.IPv4Address | ipaddress.IPv6Address,
    settings: Settings,
) -> bool:
    return any(peer in network for network in settings.trusted_proxy_networks)


def resolve_client_ip(request: Request, settings: Settings) -> str:
    """返回用于限流的客户端 IP；不可信时退回直连对端地址。"""

    peer = request.client.host if request.client else UNKNOWN_CLIENT_IP
    peer_address = _parse_peer(peer)
    if peer_address is None or not _is_trusted_proxy(peer_address, settings):
        return peer

    header_value = request.headers.get(settings.client_ip_header)
    if not header_value:
        return peer
    candidate = header_value.strip()
    # 单值契约：出现逗号说明不是网关单方覆盖的头，直接忽略。
    if not candidate or "," in candidate:
        return peer
    candidate_address = _parse_peer(candidate)
    if candidate_address is None:
        return peer
    return str(candidate_address)
