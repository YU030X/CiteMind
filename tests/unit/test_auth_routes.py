"""认证路由的进程内 HTTP 契约测试：真实 FastAPI 应用，假数据库会话与假限流器。"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from rag_backend.api.errors import (
    CODE_AUTH_DEPENDENCY_UNAVAILABLE,
    CODE_AUTH_INVALID_CREDENTIALS,
    CODE_AUTH_RATE_LIMITED,
    CODE_AUTH_REQUIRED,
    CODE_CSRF_INVALID,
    CODE_ORIGIN_NOT_ALLOWED,
)
from rag_backend.app import create_app
from rag_backend.auth.passwords import hash_password
from rag_backend.auth.ratelimit import RateLimitExceeded
from rag_backend.auth.tokens import CSRF_HEADER_NAME, derive_csrf_token, hash_token
from rag_backend.config import DEFAULT_ORGANIZATION_ID, Settings
from rag_backend.database import get_database_session
from rag_backend.models.identity import AuthSession, UserAccount

CSRF_SECRET = "unit-route-csrf-secret"
ORIGIN = "http://127.0.0.1"
PASSWORD = "correct horse battery staple"
PASSWORD_HASH = hash_password(PASSWORD)


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "session_cookie_secure": False,
        "allowed_origins": ORIGIN,
        "csrf_secret": CSRF_SECRET,
    }
    values.update(overrides)
    return Settings(**values)


def make_user(
    *, enabled: bool = True, organization_id: uuid.UUID | None = None
) -> UserAccount:
    return UserAccount(
        id=uuid.uuid4(),
        organization_id=organization_id or DEFAULT_ORGANIZATION_ID,
        username="alice",
        password_hash=PASSWORD_HASH,
        enabled=enabled,
        is_admin=False,
    )


def make_session_row(
    user: UserAccount,
    token: str,
    *,
    revoked: bool = False,
    expires_in: int = 3600,
) -> AuthSession:
    csrf_token = derive_csrf_token(CSRF_SECRET, token)
    return AuthSession(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash=hash_token(token),
        csrf_token_hash=hash_token(csrf_token),
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        revoked_at=datetime.now(UTC) if revoked else None,
    )


class FakeResult:
    def __init__(self, scalar: Any, row: Any, rows: Any = None) -> None:
        self._scalar = scalar
        self._row = row
        self._rows = rows if rows is not None else []

    def scalar_one_or_none(self) -> Any:
        return self._scalar

    def first(self) -> Any:
        return self._row

    def all(self) -> Any:
        return self._rows


class FakeAsyncSession:
    def __init__(
        self,
        *,
        scalar: Any = None,
        row: Any = None,
        rows: Any = None,
        get_result: Any = None,
    ) -> None:
        self.scalar = scalar
        self.row = row
        self.rows = rows if rows is not None else []
        self.get_result = get_result
        self.added: list[Any] = []
        self.commits = 0

    async def execute(self, statement: Any) -> FakeResult:
        return FakeResult(self.scalar, self.row, self.rows)

    def add(self, instance: Any) -> None:
        self.added.append(instance)

    async def commit(self) -> None:
        self.commits += 1

    async def get(self, model: Any, identifier: Any) -> Any:
        return self.get_result


class FakeLoginRateLimiter:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def check(self, *, client_ip: str, username: str) -> None:
        self.calls.append((client_ip, username))
        if self.error is not None:
            raise self.error


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def build_app(
    session: FakeAsyncSession | None,
    limiter: FakeLoginRateLimiter | None,
    *,
    app_settings: Settings | None = None,
) -> Any:
    app = create_app(app_settings if app_settings is not None else settings())
    if session is not None:
        app.dependency_overrides[get_database_session] = lambda: session
    app.state.login_rate_limiter = limiter
    return app


async def post_login(
    app: Any,
    *,
    origin: str | None = ORIGIN,
    username: str = "alice",
    password: str = PASSWORD,
) -> Any:
    headers = {"Origin": origin} if origin is not None else {}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=ORIGIN, headers=headers
    ) as client:
        return await client.post(
            "/api/v1/auth/login", json={"username": username, "password": password}
        )


def body(response: Any) -> Any:
    return json.loads(response.content)


# --- login ------------------------------------------------------------------


@pytest.mark.anyio
async def test_login_rejects_untrusted_origin_before_side_effects() -> None:
    limiter = FakeLoginRateLimiter()
    app = build_app(FakeAsyncSession(scalar=make_user()), limiter)

    response = await post_login(app, origin="https://evil.example")

    assert response.status_code == 403
    assert body(response)["code"] == CODE_ORIGIN_NOT_ALLOWED
    assert limiter.calls == []


@pytest.mark.anyio
async def test_login_rejects_missing_origin() -> None:
    app = build_app(FakeAsyncSession(scalar=make_user()), FakeLoginRateLimiter())

    response = await post_login(app, origin=None)

    assert response.status_code == 403
    assert body(response)["code"] == CODE_ORIGIN_NOT_ALLOWED


@pytest.mark.anyio
async def test_login_rejects_malformed_referer_without_server_error() -> None:
    limiter = FakeLoginRateLimiter()
    app = build_app(FakeAsyncSession(scalar=make_user()), limiter)

    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": PASSWORD},
            headers={"Referer": "http://[::1"},
        )

    assert response.status_code == 403
    assert body(response)["code"] == CODE_ORIGIN_NOT_ALLOWED
    assert limiter.calls == []


@pytest.mark.anyio
async def test_login_success_sets_hardened_cookie_and_returns_csrf() -> None:
    user = make_user()
    session = FakeAsyncSession(scalar=user)
    limiter = FakeLoginRateLimiter()
    app = build_app(session, limiter)

    response = await post_login(app)

    assert response.status_code == 200
    payload = body(response)
    assert payload["user"]["username"] == "alice"
    assert payload["user"]["organizationId"] == str(user.organization_id)
    assert payload["csrfToken"]
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Secure" not in cookie
    # 会话令牌只落 hash。
    (auth_session,) = session.added
    assert auth_session.token_hash != response.cookies["citemind_session"]
    assert auth_session.user_id == user.id
    assert limiter.calls and limiter.calls[0][1] == "alice"


@pytest.mark.anyio
async def test_login_unknown_user_and_wrong_password_share_error() -> None:
    unknown = await post_login(build_app(FakeAsyncSession(scalar=None), FakeLoginRateLimiter()))
    wrong_user = make_user()
    wrong = await post_login(
        build_app(FakeAsyncSession(scalar=wrong_user), FakeLoginRateLimiter()),
        password="definitely-wrong",
    )

    for response in (unknown, wrong):
        assert response.status_code == 401
        assert body(response)["code"] == CODE_AUTH_INVALID_CREDENTIALS
        assert "set-cookie" not in response.headers


@pytest.mark.anyio
async def test_login_disabled_user_is_rejected_uniformly() -> None:
    app = build_app(FakeAsyncSession(scalar=make_user(enabled=False)), FakeLoginRateLimiter())

    response = await post_login(app)

    assert response.status_code == 401
    assert body(response)["code"] == CODE_AUTH_INVALID_CREDENTIALS


@pytest.mark.anyio
async def test_login_rate_limited_returns_429_with_retry_after() -> None:
    app = build_app(
        FakeAsyncSession(scalar=make_user()),
        FakeLoginRateLimiter(error=RateLimitExceeded(retry_after=42)),
    )

    response = await post_login(app)

    assert response.status_code == 429
    assert response.headers["retry-after"] == "42"
    assert body(response)["code"] == CODE_AUTH_RATE_LIMITED
    assert body(response)["details"]["retryAfter"] == 42


@pytest.mark.anyio
async def test_login_without_redis_limiter_fails_closed() -> None:
    app = build_app(FakeAsyncSession(scalar=make_user()), None)

    response = await post_login(app)

    assert response.status_code == 503
    assert body(response)["code"] == CODE_AUTH_DEPENDENCY_UNAVAILABLE


@pytest.mark.anyio
async def test_login_validation_error_uses_uniform_body() -> None:
    app = build_app(FakeAsyncSession(scalar=make_user()), FakeLoginRateLimiter())

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=ORIGIN, headers={"Origin": ORIGIN}
    ) as client:
        response = await client.post("/api/v1/auth/login", json={"username": "alice"})

    assert response.status_code == 422
    payload = body(response)
    assert payload["code"] == "VALIDATION_ERROR"
    assert payload["requestId"]
    assert "details" in payload


# --- me ---------------------------------------------------------------------


@pytest.mark.anyio
async def test_me_requires_session() -> None:
    app = build_app(FakeAsyncSession(), FakeLoginRateLimiter())

    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await client.get("/api/v1/me")

    assert response.status_code == 401
    assert body(response)["code"] == CODE_AUTH_REQUIRED


@pytest.mark.anyio
async def test_me_returns_recoverable_csrf_token() -> None:
    user = make_user()
    token = "session-token-value"
    auth_session = make_session_row(user, token)
    session = FakeAsyncSession(row=(auth_session, user))
    app = build_app(session, FakeLoginRateLimiter())
    cookie = f"citemind_session={token}"

    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await client.get("/api/v1/me", headers={"Cookie": cookie})

    assert response.status_code == 200
    payload = body(response)
    assert payload["csrfToken"] == derive_csrf_token(CSRF_SECRET, token)
    assert payload["user"]["id"] == str(user.id)


@pytest.mark.anyio
async def test_me_rejects_revoked_session() -> None:
    user = make_user()
    token = "session-token-value"
    row = make_session_row(user, token, revoked=True)
    app = build_app(FakeAsyncSession(row=(row, user)), None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await client.get(
            "/api/v1/me", headers={"Cookie": f"citemind_session={token}"}
        )

    assert response.status_code == 401


@pytest.mark.anyio
async def test_me_rejects_expired_session() -> None:
    user = make_user()
    token = "session-token-value"
    row = make_session_row(user, token, expires_in=-1)
    app = build_app(FakeAsyncSession(row=(row, user)), None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await client.get(
            "/api/v1/me", headers={"Cookie": f"citemind_session={token}"}
        )

    assert response.status_code == 401


@pytest.mark.anyio
async def test_me_rejects_disabled_user() -> None:
    user = make_user(enabled=False)
    token = "session-token-value"
    row = make_session_row(user, token)
    app = build_app(FakeAsyncSession(row=(row, user)), None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await client.get(
            "/api/v1/me", headers={"Cookie": f"citemind_session={token}"}
        )

    assert response.status_code == 401


@pytest.mark.anyio
async def test_me_rejects_session_from_other_organization() -> None:
    """同库改单组织配置后，旧组织会话必须 fail closed。"""

    user = make_user(organization_id=uuid.uuid4())
    token = "session-token-value"
    row = make_session_row(user, token)
    app = build_app(FakeAsyncSession(row=(row, user)), None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await client.get(
            "/api/v1/me", headers={"Cookie": f"citemind_session={token}"}
        )

    assert response.status_code == 401


# --- logout -----------------------------------------------------------------


@pytest.mark.anyio
async def test_logout_requires_csrf_then_revokes_session() -> None:
    user = make_user()
    token = "session-token-value"
    auth_session = make_session_row(user, token)
    session = FakeAsyncSession(row=(auth_session, user), get_result=auth_session)
    app = build_app(session, FakeLoginRateLimiter())
    cookie_header = {"Cookie": f"citemind_session={token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        without_csrf = await client.post(
            "/api/v1/auth/logout", headers={**cookie_header, "Origin": ORIGIN}
        )

    assert without_csrf.status_code == 403
    assert body(without_csrf)["code"] == CODE_CSRF_INVALID
    assert auth_session.revoked_at is None

    csrf_token = derive_csrf_token(CSRF_SECRET, token)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        with_csrf = await client.post(
            "/api/v1/auth/logout",
            headers={
                **cookie_header,
                "Origin": ORIGIN,
                CSRF_HEADER_NAME: csrf_token,
            },
        )

    assert with_csrf.status_code == 204
    assert auth_session.revoked_at is not None
    assert "Max-Age=0" in with_csrf.headers["set-cookie"]


@pytest.mark.anyio
async def test_login_uses_trusted_proxy_header_for_rate_limit_ip() -> None:
    limiter = FakeLoginRateLimiter()
    app = build_app(
        FakeAsyncSession(scalar=make_user()),
        limiter,
        app_settings=settings(trusted_proxy_cidrs="127.0.0.0/8"),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=ORIGIN,
        headers={"Origin": ORIGIN, "X-Real-IP": "203.0.113.7"},
    ) as client:
        response = await client.post(
            "/api/v1/auth/login", json={"username": "alice", "password": PASSWORD}
        )

    assert response.status_code == 200
    assert limiter.calls == [("203.0.113.7", "alice")]


@pytest.mark.anyio
async def test_login_ignores_forwarding_header_from_untrusted_peer() -> None:
    limiter = FakeLoginRateLimiter()
    app = build_app(FakeAsyncSession(scalar=make_user()), limiter)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=ORIGIN,
        headers={"Origin": ORIGIN, "X-Real-IP": "203.0.113.7"},
    ) as client:
        response = await client.post(
            "/api/v1/auth/login", json={"username": "alice", "password": PASSWORD}
        )

    assert response.status_code == 200
    assert limiter.calls and limiter.calls[0][0] == "127.0.0.1"


@pytest.mark.anyio
async def test_login_validation_error_never_reflects_password() -> None:
    app = build_app(FakeAsyncSession(scalar=make_user()), FakeLoginRateLimiter())
    long_password = "p" * 1100

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=ORIGIN, headers={"Origin": ORIGIN}
    ) as client:
        response = await client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": long_password},
        )

    assert response.status_code == 422
    assert long_password not in response.text
    payload = body(response)
    assert payload["code"] == "VALIDATION_ERROR"
    assert all("input" not in entry and "ctx" not in entry for entry in payload["details"])


@pytest.mark.anyio
async def test_logout_without_session_is_idempotent() -> None:
    app = build_app(FakeAsyncSession(), FakeLoginRateLimiter())

    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await client.post("/api/v1/auth/logout", headers={"Origin": ORIGIN})

    assert response.status_code == 204
    # 无有效会话时不得下发清除 Cookie，避免无会话的跨站 POST 强制登出。
    assert "set-cookie" not in response.headers
