"""配置校验测试：会话 Cookie、CSRF 与 Origin 白名单。"""

import uuid
from typing import Any

import pytest
from pydantic import ValidationError
from rag_backend.config import (
    DEFAULT_CSRF_SECRET,
    DEFAULT_ORGANIZATION_ID,
    Settings,
    normalise_origin,
    parse_allowed_origins,
)

PRODUCTION_DATABASE_URL = "postgresql+psycopg://citemind_app:strong-password@postgres:5432/citemind"
PRODUCTION_REDIS_URL = "redis://:strong-password@redis:6379/0"
STRONG_CSRF_SECRET = "c" * 48
PRODUCTION_PROXY_CIDRS = "172.28.10.0/24"
PRODUCTION_ALLOWED_ORIGINS = "https://kb.example.com"


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"_env_file": None, "environment": "test"}
    values.update(overrides)
    return Settings(**values)


def test_normalise_origin_strips_trailing_slash_and_rejects_paths() -> None:
    assert normalise_origin("http://127.0.0.1:58080/") == "http://127.0.0.1:58080"

    with pytest.raises(ValueError, match="path"):
        normalise_origin("http://127.0.0.1:58080/api")
    with pytest.raises(ValueError, match="http"):
        normalise_origin("ftp://127.0.0.1")


def test_parse_allowed_origins_dedupes_preserving_order() -> None:
    parsed = parse_allowed_origins("http://a.example, http://b.example,http://a.example")

    assert parsed == ["http://a.example", "http://b.example"]


def test_parse_allowed_origins_rejects_empty() -> None:
    with pytest.raises(ValidationError, match="allowed_origins 不能为空"):
        settings(allowed_origins="  ,  ")


def test_defaults_are_single_organization_and_secure() -> None:
    resolved = settings()

    assert resolved.organization_id == DEFAULT_ORGANIZATION_ID
    assert isinstance(resolved.organization_id, uuid.UUID)
    assert resolved.session_cookie_secure is True
    assert resolved.session_cookie_name == "citemind_session"
    assert resolved.allowed_origin_set == frozenset(
        {"http://127.0.0.1:58080", "http://localhost:58080"}
    )


def test_custom_organization_id_is_accepted() -> None:
    organization_id = uuid.uuid4()

    assert settings(organization_id=organization_id).organization_id == organization_id


def test_production_requires_secure_cookie() -> None:
    with pytest.raises(ValidationError, match="Secure 会话 Cookie"):
        settings(
            environment="production",
            database_url=PRODUCTION_DATABASE_URL,
            redis_url=PRODUCTION_REDIS_URL,
            csrf_secret=STRONG_CSRF_SECRET,
            trusted_proxy_cidrs=PRODUCTION_PROXY_CIDRS,
            session_cookie_secure=False,
        )


def test_production_requires_redis_for_rate_limiting() -> None:
    with pytest.raises(ValidationError, match="Redis URL"):
        settings(
            environment="production",
            database_url=PRODUCTION_DATABASE_URL,
            csrf_secret=STRONG_CSRF_SECRET,
            trusted_proxy_cidrs=PRODUCTION_PROXY_CIDRS,
        )


def test_production_requires_explicit_trusted_proxy_boundary() -> None:
    with pytest.raises(ValidationError, match="TRUSTED_PROXY_CIDRS"):
        settings(
            environment="production",
            database_url=PRODUCTION_DATABASE_URL,
            redis_url=PRODUCTION_REDIS_URL,
            csrf_secret=STRONG_CSRF_SECRET,
        )


@pytest.mark.parametrize(
    "secret",
    [DEFAULT_CSRF_SECRET, "short"],
    ids=["placeholder", "too-short"],
)
def test_production_rejects_weak_csrf_secret(secret: str) -> None:
    with pytest.raises(ValidationError, match="csrf_secret"):
        settings(
            environment="production",
            database_url=PRODUCTION_DATABASE_URL,
            redis_url=PRODUCTION_REDIS_URL,
            csrf_secret=secret,
            trusted_proxy_cidrs=PRODUCTION_PROXY_CIDRS,
        )


def test_development_allows_insecure_cookie_only_for_loopback_origins() -> None:
    resolved = settings(
        session_cookie_secure=False,
        allowed_origins="http://127.0.0.1:58080,http://localhost:58080",
    )

    assert resolved.session_cookie_secure is False

    with pytest.raises(ValidationError, match="回环"):
        settings(
            session_cookie_secure=False,
            allowed_origins="https://kb.example.com",
        )


def test_empty_csrf_secret_is_rejected() -> None:
    with pytest.raises(ValidationError, match="csrf_secret"):
        settings(csrf_secret="   ")


@pytest.mark.parametrize(
    "field",
    [
        "session_ttl_seconds",
        "login_rate_limit_per_ip",
        "login_rate_limit_per_username",
        "login_rate_limit_window_seconds",
    ],
)
def test_non_positive_limits_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        settings(**{field: 0})


def test_empty_session_cookie_name_is_rejected() -> None:
    with pytest.raises(ValidationError, match="session_cookie_name"):
        settings(session_cookie_name="  ")


def test_trusted_proxy_networks_parse_comma_separated_cidrs() -> None:
    resolved = settings(trusted_proxy_cidrs="172.28.10.0/24, 10.0.0.0/8")

    assert [str(network) for network in resolved.trusted_proxy_networks] == [
        "172.28.10.0/24",
        "10.0.0.0/8",
    ]
    assert settings().trusted_proxy_networks == ()


def test_invalid_trusted_proxy_cidr_is_rejected() -> None:
    with pytest.raises(ValidationError, match="CIDR"):
        settings(trusted_proxy_cidrs="not-a-network")


@pytest.mark.parametrize(
    "cidrs",
    ["0.0.0.0/0", "::/0", "10.0.0.0/8,0.0.0.0/0"],
    ids=["ipv4-wildcard", "ipv6-wildcard", "mixed"],
)
def test_production_rejects_wildcard_trusted_proxy_cidrs(cidrs: str) -> None:
    # 信任全网段等于接受任意客户端伪造 X-Real-IP；只有真正的 /0 被拒绝。
    with pytest.raises(ValidationError, match="全网段"):
        settings(
            environment="production",
            database_url=PRODUCTION_DATABASE_URL,
            redis_url=PRODUCTION_REDIS_URL,
            csrf_secret=STRONG_CSRF_SECRET,
            trusted_proxy_cidrs=cidrs,
        )


def test_production_accepts_broad_but_bounded_trusted_proxy_cidr() -> None:
    resolved = settings(
        environment="production",
        database_url=PRODUCTION_DATABASE_URL,
        redis_url=PRODUCTION_REDIS_URL,
        csrf_secret=STRONG_CSRF_SECRET,
        trusted_proxy_cidrs="10.0.0.0/8",
        allowed_origins=PRODUCTION_ALLOWED_ORIGINS,
    )

    assert [str(network) for network in resolved.trusted_proxy_networks] == ["10.0.0.0/8"]


@pytest.mark.parametrize(
    "origins",
    [
        "http://kb.example.com",
        "https://kb.example.com,http://127.0.0.1:58080",
    ],
    ids=["http-only", "mixed"],
)
def test_production_requires_https_allowed_origins(origins: str) -> None:
    with pytest.raises(ValidationError, match="https"):
        settings(
            environment="production",
            database_url=PRODUCTION_DATABASE_URL,
            redis_url=PRODUCTION_REDIS_URL,
            csrf_secret=STRONG_CSRF_SECRET,
            trusted_proxy_cidrs=PRODUCTION_PROXY_CIDRS,
            allowed_origins=origins,
        )


def test_invalid_client_ip_header_is_rejected() -> None:
    with pytest.raises(ValidationError, match="client_ip_header"):
        settings(client_ip_header="bad header")
