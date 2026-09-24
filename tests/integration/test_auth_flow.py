"""身份首片的真实 PostgreSQL + Redis 验收：登录、会话、CSRF、限流与 CLI 开户。

应用通过 ASGITransport 跑真实 FastAPI lifespan（真实异步 Engine 与 Redis 限流器），
数据库使用专用破坏性测试库的 api 运行角色，Redis 使用回环测试 broker。缺少任一守卫
DSN 时按既有契约跳过，跳过不代表通过。
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alembic import command
from database_guard import DestructiveTestDatabase
from database_roles_guard import RoleTestDatabases
from httpx import ASGITransport, AsyncClient
from rag_backend.app import create_app
from rag_backend.auth.accounts import MAX_USERNAME_LENGTH, create_account
from rag_backend.auth.cli import EXIT_DATABASE_ROLE, EXIT_OK, EXIT_USAGE
from rag_backend.auth.cli import main as cli_main
from rag_backend.auth.tokens import derive_csrf_token, hash_token
from rag_backend.config import Settings
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session
from test_core_migration import alembic_config, alembic_revision, business_tables

pytestmark = pytest.mark.integration

IDENTITY_REVISION = "20260923_0005"

ORIGIN = "http://127.0.0.1"
ORGANIZATION_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
OTHER_ORGANIZATION_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
CSRF_SECRET = "integration-csrf-secret"
PASSWORD = "integration-password-123"
SESSION_COOKIE_NAME = "citemind_session"


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, Any]]:
    # Windows 默认 ProactorEventLoop 不能用于 psycopg 异步连接；测试与 uvicorn 入口
    #（rag_backend.event_loop:create_event_loop）保持一致，显式使用 SelectorEventLoop。
    return ("asyncio", {"loop_factory": asyncio.SelectorEventLoop})


@pytest.fixture(scope="module")
def auth_schema(destructive_test_database: DestructiveTestDatabase) -> Iterator[Engine]:
    config = alembic_config(destructive_test_database.url)
    engine = create_engine(destructive_test_database.url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT current_database()"))
                == destructive_test_database.database_name
            )
            assert alembic_revision(connection) is None
            assert business_tables(connection) == set()

        command.upgrade(config, IDENTITY_REVISION)
        yield engine
    finally:
        command.downgrade(config, "base")
        with engine.connect() as connection:
            assert alembic_revision(connection) is None
            assert business_tables(connection) == set()
        engine.dispose()


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "organization_id": ORGANIZATION_ID,
        "session_cookie_secure": False,
        "allowed_origins": ORIGIN,
        "csrf_secret": CSRF_SECRET,
        "session_ttl_seconds": 3600,
        "login_rate_limit_per_ip": 10_000,
        "login_rate_limit_per_username": 10_000,
        "login_rate_limit_window_seconds": 60,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def auth_settings(
    auth_schema: Engine,
    destructive_test_database: DestructiveTestDatabase,
    role_test_databases: RoleTestDatabases,
    test_redis: Any,
) -> Settings:
    # 应用使用真实 api 运行角色，且必须与迁移库是同一个测试库。
    assert role_test_databases.database_name == destructive_test_database.database_name
    return make_settings(
        database_url=role_test_databases.api_url,
        redis_url=test_redis.url,
    )


@asynccontextmanager
async def api_client(
    settings: Settings, *, client_ip: str = "10.0.0.1"
) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, client=(client_ip, 12345))
        async with AsyncClient(
            transport=transport, base_url=ORIGIN, headers={"Origin": ORIGIN}
        ) as client:
            yield client


def unique_username() -> str:
    return f"user-{uuid.uuid4().hex[:12]}"


def seed_user(
    engine: Engine,
    *,
    username: str,
    password: str = PASSWORD,
    is_admin: bool = False,
) -> uuid.UUID:
    with Session(engine) as session:
        account = create_account(
            session,
            organization_id=ORGANIZATION_ID,
            username=username,
            password=password,
            is_admin=is_admin,
        )
        return account.id


def read_session_row(engine: Engine, token: str) -> dict[str, Any]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT token_hash, csrf_token_hash, revoked_at, expires_at "
                "FROM auth_session WHERE token_hash = :token_hash"
            ),
            {"token_hash": hash_token(token)},
        ).one()
    return {
        "token_hash": row[0],
        "csrf_token_hash": row[1],
        "revoked_at": row[2],
        "expires_at": row[3],
    }


async def login(
    client: AsyncClient, username: str, password: str = PASSWORD
) -> Any:
    return await client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )


def seed_kb(engine: Engine, *, organization_id: uuid.UUID, name: str) -> uuid.UUID:
    kb_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO knowledge_base (id, organization_id, name) "
                "VALUES (:id, :organization_id, :name)"
            ),
            {"id": kb_id, "organization_id": organization_id, "name": name},
        )
    return kb_id


def seed_member(engine: Engine, *, kb_id: uuid.UUID, user_id: uuid.UUID, role: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO kb_member (id, kb_id, user_id, role) "
                "VALUES (:id, :kb_id, :user_id, :role)"
            ),
            {"id": uuid.uuid4(), "kb_id": kb_id, "user_id": user_id, "role": role},
        )


def username_count(engine: Engine, username: str) -> int:
    with engine.connect() as connection:
        return int(
            connection.scalar(
                text("SELECT count(*) FROM user_account WHERE username = :username"),
                {"username": username},
            )
            or 0
        )


@pytest.mark.anyio
async def test_login_me_logout_round_trip_persists_hashes_only(
    auth_schema: Engine, auth_settings: Settings
) -> None:
    username = unique_username()
    seed_user(auth_schema, username=username)

    async with api_client(auth_settings) as client:
        login_response = await login(client, username)
        assert login_response.status_code == 200
        payload = login_response.json()
        csrf_token = payload["csrfToken"]
        assert payload["user"]["username"] == username
        token = client.cookies[SESSION_COOKIE_NAME]

        me_response = await client.get("/api/v1/me")
        assert me_response.status_code == 200
        assert me_response.json()["csrfToken"] == csrf_token

        logout_response = await client.post(
            "/api/v1/auth/logout", headers={"X-CSRF-Token": csrf_token}
        )
        assert logout_response.status_code == 204

        after_logout = await client.get("/api/v1/me")
        assert after_logout.status_code == 401

    stored = read_session_row(auth_schema, token)
    # 数据库只保存 hash，不保存原令牌与明文 CSRF 令牌。
    assert stored["token_hash"] == hash_token(token)
    assert stored["token_hash"] != token
    assert stored["csrf_token_hash"] == hash_token(csrf_token)
    assert stored["csrf_token_hash"] != csrf_token
    assert stored["revoked_at"] is not None


@pytest.mark.anyio
async def test_wrong_password_and_unknown_user_share_response(
    auth_schema: Engine, auth_settings: Settings
) -> None:
    username = unique_username()
    seed_user(auth_schema, username=username)

    async with api_client(auth_settings) as client:
        wrong = await login(client, username, "wrong-password")
        unknown = await login(client, unique_username(), PASSWORD)

    for response in (wrong, unknown):
        assert response.status_code == 401
        assert response.json()["code"] == "AUTH_INVALID_CREDENTIALS"
        assert "set-cookie" not in response.headers
    assert wrong.json()["message"] == unknown.json()["message"]


@pytest.mark.anyio
async def test_login_rejects_untrusted_origin(
    auth_schema: Engine, auth_settings: Settings
) -> None:
    username = unique_username()
    seed_user(auth_schema, username=username)

    async with api_client(auth_settings) as client:
        response = await client.post(
            "/api/v1/auth/login",
            headers={"Origin": "https://evil.example"},
            json={"username": username, "password": PASSWORD},
        )

    assert response.status_code == 403
    assert response.json()["code"] == "ORIGIN_NOT_ALLOWED"


@pytest.mark.anyio
async def test_rate_limit_is_shared_across_app_instances(
    auth_schema: Engine, auth_settings: Settings
) -> None:
    username = unique_username()
    seed_user(auth_schema, username=username)
    limited = make_settings(
        database_url=auth_settings.database_url,
        redis_url=auth_settings.redis_url,
        login_rate_limit_per_username=2,
    )

    async with api_client(limited, client_ip="10.1.0.1") as first_client:
        assert (await login(first_client, username, "wrong")).status_code == 401
        assert (await login(first_client, username, "wrong")).status_code == 401
        blocked = await login(first_client, username, "wrong")
        assert blocked.status_code == 429
        assert blocked.json()["code"] == "AUTH_RATE_LIMITED"
        assert int(blocked.headers["retry-after"]) > 0

    # 第二个应用实例（独立 Engine 与 Redis 客户端）看到同一用户名计数，立即被限流。
    async with api_client(limited, client_ip="10.1.0.2") as second_client:
        shared = await login(second_client, username, "wrong")

    assert shared.status_code == 429
    assert shared.json()["code"] == "AUTH_RATE_LIMITED"


@pytest.mark.anyio
async def test_redis_failure_fails_closed(
    auth_settings: Settings,
) -> None:
    username = unique_username()
    unreachable = make_settings(
        database_url=auth_settings.database_url,
        redis_url="redis://:secret@127.0.0.1:6399/0",
    )

    async with api_client(unreachable) as client:
        response = await login(client, username)

    assert response.status_code == 503
    assert response.json()["code"] == "AUTH_DEPENDENCY_UNAVAILABLE"


@pytest.mark.anyio
async def test_disabled_user_and_expired_session_fail_immediately(
    auth_schema: Engine, auth_settings: Settings
) -> None:
    username = unique_username()
    user_id = seed_user(auth_schema, username=username)

    async with api_client(auth_settings) as client:
        assert (await login(client, username)).status_code == 200
        token = client.cookies[SESSION_COOKIE_NAME]

        with auth_schema.begin() as connection:
            connection.execute(
                text("UPDATE user_account SET enabled = false WHERE id = :id"),
                {"id": user_id},
            )

        disabled = await client.get("/api/v1/me")
        assert disabled.status_code == 401

        with auth_schema.begin() as connection:
            connection.execute(
                text("UPDATE user_account SET enabled = true WHERE id = :id"),
                {"id": user_id},
            )
            connection.execute(
                text("UPDATE auth_session SET expires_at = :expires_at WHERE token_hash = :h"),
                {
                    "expires_at": datetime.now(UTC) - timedelta(seconds=1),
                    "h": hash_token(token),
                },
            )

        expired = await client.get("/api/v1/me")
        assert expired.status_code == 401


@pytest.mark.anyio
async def test_csrf_token_is_recoverable_across_app_restart(
    auth_schema: Engine, auth_settings: Settings
) -> None:
    username = unique_username()
    seed_user(auth_schema, username=username)

    async with api_client(auth_settings, client_ip="10.2.0.1") as first:
        assert (await login(first, username)).status_code == 200
        first_csrf = (await first.get("/api/v1/me")).json()["csrfToken"]
        token = first.cookies[SESSION_COOKIE_NAME]

    # 第二个应用实例（模拟进程重启）只用同一 Cookie 就能恢复同一 CSRF 令牌，
    # 证明 CSRF 由服务端稳定密钥与会话令牌派生，而不依赖进程内状态。
    async with api_client(auth_settings, client_ip="10.2.0.2") as second:
        restored = await second.get(
            "/api/v1/me",
            headers={"Cookie": f"{SESSION_COOKIE_NAME}={token}"},
        )

    assert restored.status_code == 200
    assert restored.json()["csrfToken"] == first_csrf


@pytest.mark.anyio
async def test_logout_requires_csrf(
    auth_schema: Engine, auth_settings: Settings
) -> None:
    username = unique_username()
    seed_user(auth_schema, username=username)

    async with api_client(auth_settings) as client:
        assert (await login(client, username)).status_code == 200

        without_csrf = await client.post("/api/v1/auth/logout")
        assert without_csrf.status_code == 403
        assert without_csrf.json()["code"] == "CSRF_INVALID"

        token = client.cookies[SESSION_COOKIE_NAME]
        csrf_token = derive_csrf_token(CSRF_SECRET, token)
        with_csrf = await client.post(
            "/api/v1/auth/logout", headers={"X-CSRF-Token": csrf_token}
        )
        assert with_csrf.status_code == 204

    assert read_session_row(auth_schema, token)["revoked_at"] is not None


@pytest.mark.anyio
async def test_cli_provisions_loginable_account_without_echoing_password(
    auth_schema: Engine,
    auth_settings: Settings,
    role_test_databases: RoleTestDatabases,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    username = unique_username()
    monkeypatch.setenv("NEW_USER_PASSWORD", PASSWORD)

    exit_code = cli_main(
        [
            "--username",
            username,
            "--password-env",
            "NEW_USER_PASSWORD",
            "--database-url",
            role_test_databases.api_url,
            "--organization-id",
            str(ORGANIZATION_ID),
        ],
        settings_factory=lambda: make_settings(database_url=role_test_databases.api_url),
    )

    captured = capsys.readouterr()
    assert exit_code == EXIT_OK
    assert PASSWORD not in captured.out
    assert PASSWORD not in captured.err

    async with api_client(auth_settings) as client:
        response = await login(client, username)

    assert response.status_code == 200
    assert response.json()["user"]["username"] == username


@pytest.mark.anyio
async def test_session_from_other_organization_is_rejected(
    auth_schema: Engine, auth_settings: Settings
) -> None:
    """同库改单组织配置后，原组织会话在 /me 与 KB 列表都必须 fail closed。"""

    username = unique_username()
    user_id = seed_user(auth_schema, username=username)
    kb_id = seed_kb(
        auth_schema,
        organization_id=ORGANIZATION_ID,
        name=f"kb-{uuid.uuid4().hex[:8]}",
    )
    seed_member(auth_schema, kb_id=kb_id, user_id=user_id, role="OWNER")

    async with api_client(auth_settings, client_ip="10.3.0.1") as client:
        assert (await login(client, username)).status_code == 200
        assert (await client.get("/api/v1/me")).status_code == 200
        baseline = await client.get("/api/v1/knowledge-bases")
        assert baseline.status_code == 200
        assert str(kb_id) in [kb["id"] for kb in baseline.json()["knowledgeBases"]]
        token = client.cookies[SESSION_COOKIE_NAME]

    # 用另一个组织的服务端配置复用同一 Cookie：会话来自旧组织，必须整体失效。
    other_settings = make_settings(
        database_url=auth_settings.database_url,
        redis_url=auth_settings.redis_url,
        organization_id=OTHER_ORGANIZATION_ID,
    )
    cookie = {"Cookie": f"{SESSION_COOKIE_NAME}={token}"}
    async with api_client(other_settings, client_ip="10.3.0.2") as other_client:
        assert (await other_client.get("/api/v1/me", headers=cookie)).status_code == 401
        assert (
            await other_client.get("/api/v1/knowledge-bases", headers=cookie)
        ).status_code == 401


def test_cli_rejects_non_api_database_role_without_writing(
    auth_schema: Engine,
    destructive_test_database: DestructiveTestDatabase,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    username = unique_username()
    monkeypatch.setenv("NEW_USER_PASSWORD", PASSWORD)

    exit_code = cli_main(
        [
            "--username",
            username,
            "--password-env",
            "NEW_USER_PASSWORD",
            "--database-url",
            destructive_test_database.url,
            "--organization-id",
            str(ORGANIZATION_ID),
        ],
        settings_factory=lambda: make_settings(
            database_url=destructive_test_database.url
        ),
    )

    captured = capsys.readouterr()
    assert exit_code == EXIT_DATABASE_ROLE
    assert "citemind_api" in captured.err
    assert PASSWORD not in captured.err
    assert username_count(auth_schema, username) == 0


def test_cli_rejects_mismatched_organization_without_writing(
    auth_schema: Engine,
    role_test_databases: RoleTestDatabases,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    username = unique_username()
    monkeypatch.setenv("NEW_USER_PASSWORD", PASSWORD)

    exit_code = cli_main(
        [
            "--username",
            username,
            "--password-env",
            "NEW_USER_PASSWORD",
            "--database-url",
            role_test_databases.api_url,
            "--organization-id",
            str(OTHER_ORGANIZATION_ID),
        ],
        settings_factory=lambda: make_settings(
            database_url=role_test_databases.api_url
        ),
    )

    captured = capsys.readouterr()
    assert exit_code == EXIT_USAGE
    assert str(OTHER_ORGANIZATION_ID) in captured.err
    assert PASSWORD not in captured.err
    assert username_count(auth_schema, username) == 0


@pytest.mark.anyio
async def test_cli_username_contract_matches_login(
    auth_schema: Engine,
    auth_settings: Settings,
    role_test_databases: RoleTestDatabases,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """255 字符可建且可登录；256 字符（或首尾空白）在读取密码前拒绝且零写入。"""

    monkeypatch.setenv("NEW_USER_PASSWORD", PASSWORD)
    overlong = "u" * (MAX_USERNAME_LENGTH + 1)

    exit_code = cli_main(
        [
            "--username",
            overlong,
            "--password-env",
            "NEW_USER_PASSWORD",
            "--database-url",
            role_test_databases.api_url,
        ],
        settings_factory=lambda: make_settings(
            database_url=role_test_databases.api_url
        ),
    )

    assert exit_code == EXIT_USAGE
    assert username_count(auth_schema, overlong) == 0

    max_length_username = "v" * MAX_USERNAME_LENGTH
    exit_code = cli_main(
        [
            "--username",
            max_length_username,
            "--password-env",
            "NEW_USER_PASSWORD",
            "--database-url",
            role_test_databases.api_url,
        ],
        settings_factory=lambda: make_settings(
            database_url=role_test_databases.api_url
        ),
    )

    assert exit_code == EXIT_OK
    capsys.readouterr()

    async with api_client(auth_settings) as client:
        response = await login(client, max_length_username)

    assert response.status_code == 200
    assert response.json()["user"]["username"] == max_length_username
