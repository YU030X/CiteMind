"""静态网页抓取允许主机的纯标准库规范化规则。"""

from __future__ import annotations


def normalize_web_host(raw: str) -> str:
    """把配置或 URL 中的 host 规范化为 IDNA 小写、去掉尾点的精确 host。"""

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
    """解析逗号分隔的精确允许 host；空值返回空集合。"""

    hosts: set[str] = set()
    for item in value.split(","):
        candidate = item.strip()
        if candidate:
            hosts.add(normalize_web_host(candidate))
    return frozenset(hosts)


__all__ = ["normalize_web_host", "parse_allowed_web_hosts"]
