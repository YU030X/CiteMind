"""KB 成员授权切片的真实 PostgreSQL + Redis 验收。

覆盖：管理员创建 KB 并同事务写入创建者 OWNER、仅管理员可创建、Origin/CSRF、
只列同组织有效成员、成员读取与写入的越权统一 404、全量替换的软撤销/重加入/
``acl_revision`` 递增、空/重复/最后 OWNER/跨组织用户的拒绝且无部分事务，以及
知识库行锁下的两会话并发替换。缺少守卫 DSN 或 Redis 时按既有契约跳过。
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from alembic import command
from database_guard import DestructiveTestDatabase
from database_roles_guard import RoleTestDatabases
from httpx import ASGITransport, AsyncClient
from rag_backend.app import create_app
from rag_backend.auth.accounts import create_account
from rag_backend.auth.tokens import CSRF_HEADER_NAME
from rag_backend.config import Settings
from rag_backend.database import create_database_engine, create_session_factory
from rag_backend.knowledge.roles import KbRole, KnowledgeBaseNotFound, MemberReplacement
from rag_backend.knowledge.service import replace_knowledge_base_members
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session
from test_core_migration import alembic_config, alembic_revision, business_tables

pytestmark = pytest.mark.integration

IDENTITY_REVISION = "20260923_0005"

ORIGIN = "http://127.0.0.1"
ORGANIZATION_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
OTHER_ORGANIZATION_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
CSRF_SECRET = "integration-kb-csrf-secret"
PASSWORD = "integration-kb-password-123"


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, Any]]:
    # Windows 默认 ProactorEventLoop 不能用于 psycopg 异步连接；与生产入口一致。
    return ("asyncio", {"loop_factory": asyncio.SelectorEventLoop})


@pytest.fixture(scope="module")
def kb_schema(destructive_test_database: DestructiveTestDatabase) -> Iterator[Engine]:
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
def kb_settings(
    kb_schema: Engine,
    destructive_test_database: DestructiveTestDatabase,
    role_test_databases: RoleTestDatabases,
    test_redis: Any,
) -> Settings:
    assert role_test_databases.database_name == destructive_test_database.database_name
    return make_settings(
        database_url=role_test_databases.api_url,
        redis_url=test_redis.url,
    )


@asynccontextmanager
async def api_client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, client=("10.20.0.1", 12345))
        async with AsyncClient(
            transport=transport, base_url=ORIGIN, headers={"Origin": ORIGIN}
        ) as client:
            yield client


def unique_username() -> str:
    return f"user-{uuid.uuid4().hex[:12]}"


def unique_name() -> str:
    return f"kb-{uuid.uuid4().hex[:12]}"


def seed_user(
    engine: Engine,
    *,
    username: str,
    organization_id: uuid.UUID = ORGANIZATION_ID,
    is_admin: bool = False,
    enabled: bool = True,
) -> uuid.UUID:
    with Session(engine) as session:
        account = create_account(
            session,
            organization_id=organization_id,
            username=username,
            password=PASSWORD,
            is_admin=is_admin,
        )
        account_id = account.id
    if not enabled:
        # create_account 只会创建启用账号；禁用状态由运维直接写库模拟。
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE user_account SET enabled = false WHERE id = :id"),
                {"id": account_id},
            )
    return account_id


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


def seed_member(
    engine: Engine,
    *,
    kb_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str,
    revoked: bool = False,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO kb_member (id, kb_id, user_id, role, revoked_at) "
                "VALUES (:id, :kb_id, :user_id, :role, "
                "CASE WHEN :revoked THEN now() ELSE NULL END)"
            ),
            {
                "id": uuid.uuid4(),
                "kb_id": kb_id,
                "user_id": user_id,
                "role": role,
                "revoked": revoked,
            },
        )


def kb_named_count(engine: Engine, name: str) -> int:
    with engine.connect() as connection:
        return int(
            connection.scalar(
                text("SELECT count(*) FROM knowledge_base WHERE name = :name"), {"name": name}
            )
        )


def valid_member_rows(engine: Engine, kb_id: uuid.UUID) -> dict[uuid.UUID, str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT user_id, role FROM kb_member "
                "WHERE kb_id = :kb_id AND revoked_at IS NULL"
            ),
            {"kb_id": kb_id},
        ).all()
    return {row[0]: row[1] for row in rows}


def response_members(response: Any) -> dict[uuid.UUID, KbRole]:
    """从 PUT/GET 成员响应体解析 userId 到角色。"""

    return {
        uuid.UUID(entry["userId"]): KbRole(entry["role"])
        for entry in response.json()["members"]
    }


def acl_revision(engine: Engine, kb_id: uuid.UUID) -> int:
    with engine.connect() as connection:
        return int(
            connection.scalar(
                text("SELECT acl_revision FROM knowledge_base WHERE id = :kb_id"),
                {"kb_id": kb_id},
            )
        )


async def login_csrf(client: AsyncClient, username: str) -> str:
    response = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return str(response.json()["csrfToken"])


async def create_kb(client: AsyncClient, *, name: str, csrf: str) -> Any:
    return await client.post(
        "/api/v1/knowledge-bases",
        json={"name": name},
        headers={CSRF_HEADER_NAME: csrf},
    )


def member_payload(user_id: uuid.UUID, role: str) -> dict[str, str]:
    return {"userId": str(user_id), "role": role}


async def put_members(
    client: AsyncClient, kb_id: str, members: list[dict[str, str]], csrf: str
) -> Any:
    return await client.put(
        f"/api/v1/knowledge-bases/{kb_id}/members",
        json={"members": members},
        headers={CSRF_HEADER_NAME: csrf},
    )


@pytest.mark.anyio
async def test_admin_creates_kb_with_owner_and_only_admin_may_create(
    kb_schema: Engine, kb_settings: Settings
) -> None:
    admin_username = unique_username()
    admin_id = seed_user(kb_schema, username=admin_username, is_admin=True)
    normal_username = unique_username()
    seed_user(kb_schema, username=normal_username)
    name = unique_name()

    async with api_client(kb_settings) as admin:
        csrf = await login_csrf(admin, admin_username)
        response = await create_kb(admin, name=name, csrf=csrf)
        assert response.status_code == 201, response.text
        payload = response.json()
        kb_id = uuid.UUID(payload["id"])
        assert payload == {
            "id": str(kb_id),
            "name": name,
            "role": "OWNER",
            "aclRevision": 0,
            "kbRevision": 0,
        }

        # 缺少 CSRF 与跨站来源都必须先被拒绝，且不落库。
        missing_csrf_name = unique_name()
        missing = await admin.post(
            "/api/v1/knowledge-bases", json={"name": missing_csrf_name}
        )
        assert missing.status_code == 403
        assert missing.json()["code"] == "CSRF_INVALID"
        foreign_origin_name = unique_name()
        foreign = await admin.post(
            "/api/v1/knowledge-bases",
            json={"name": foreign_origin_name},
            headers={"Origin": "https://evil.example", CSRF_HEADER_NAME: csrf},
        )
        assert foreign.status_code == 403
        assert foreign.json()["code"] == "ORIGIN_NOT_ALLOWED"

    async with api_client(kb_settings) as normal:
        csrf = await login_csrf(normal, normal_username)
        rejected_name = unique_name()
        forbidden = await create_kb(normal, name=rejected_name, csrf=csrf)
        assert forbidden.status_code == 403
        assert forbidden.json()["code"] == "FORBIDDEN"

    assert kb_named_count(kb_schema, missing_csrf_name) == 0
    assert kb_named_count(kb_schema, foreign_origin_name) == 0
    assert kb_named_count(kb_schema, rejected_name) == 0
    assert valid_member_rows(kb_schema, kb_id) == {admin_id: "OWNER"}


@pytest.mark.anyio
async def test_me_and_list_only_expose_same_org_valid_memberships(
    kb_schema: Engine, kb_settings: Settings
) -> None:
    admin_username = unique_username()
    admin_id = seed_user(kb_schema, username=admin_username, is_admin=True)
    member_username = unique_username()
    member_id = seed_user(kb_schema, username=member_username)
    outsider_username = unique_username()
    seed_user(kb_schema, username=outsider_username)

    async with api_client(kb_settings) as admin:
        admin_csrf = await login_csrf(admin, admin_username)
        created = await create_kb(admin, name=unique_name(), csrf=admin_csrf)
        kb_id = uuid.UUID(created.json()["id"])
        added = await put_members(
            admin,
            str(kb_id),
            [
                member_payload(admin_id, "OWNER"),
                member_payload(member_id, "EDITOR"),
            ],
            admin_csrf,
        )
        assert added.status_code == 200, added.text

    # 外组织 KB：即使存在一条把本组织成员指过去的成员行，也不能被列出或读取。
    other_org_kb_id = seed_kb(
        kb_schema, organization_id=OTHER_ORGANIZATION_ID, name=unique_name()
    )
    seed_member(kb_schema, kb_id=other_org_kb_id, user_id=member_id, role="OWNER")

    async with api_client(kb_settings) as member:
        await login_csrf(member, member_username)
        me = await member.get("/api/v1/me")
        listing = await member.get("/api/v1/knowledge-bases")
        cross_org = await member.get(
            f"/api/v1/knowledge-bases/{other_org_kb_id}/members"
        )

    assert me.status_code == 200
    assert [entry["id"] for entry in me.json()["knowledgeBases"]] == [str(kb_id)]
    assert me.json()["knowledgeBases"][0]["role"] == "EDITOR"
    assert [entry["id"] for entry in listing.json()["knowledgeBases"]] == [str(kb_id)]
    assert cross_org.status_code == 404
    assert cross_org.json()["code"] == "KNOWLEDGE_BASE_NOT_FOUND"

    async with api_client(kb_settings) as outsider:
        await login_csrf(outsider, outsider_username)
        outsider_me = await outsider.get("/api/v1/me")
        outsider_listing = await outsider.get("/api/v1/knowledge-bases")

    assert outsider_me.json()["knowledgeBases"] == []
    assert outsider_listing.json()["knowledgeBases"] == []


@pytest.mark.anyio
async def test_member_read_and_unauthorized_access_are_uniform_404(
    kb_schema: Engine, kb_settings: Settings
) -> None:
    admin_username = unique_username()
    admin_id = seed_user(kb_schema, username=admin_username, is_admin=True)
    member_username = unique_username()
    member_id = seed_user(kb_schema, username=member_username)
    outsider_username = unique_username()
    seed_user(kb_schema, username=outsider_username)
    kb_id = seed_kb(kb_schema, organization_id=ORGANIZATION_ID, name=unique_name())
    seed_member(kb_schema, kb_id=kb_id, user_id=admin_id, role="OWNER")
    seed_member(kb_schema, kb_id=kb_id, user_id=member_id, role="READER")

    async with api_client(kb_settings) as member:
        await login_csrf(member, member_username)
        allowed = await member.get(f"/api/v1/knowledge-bases/{kb_id}/members")
        assert allowed.status_code == 200, allowed.text
        assert {entry["role"] for entry in allowed.json()["members"]} == {"OWNER", "READER"}

    async with api_client(kb_settings) as outsider:
        await login_csrf(outsider, outsider_username)
        denied = await outsider.get(f"/api/v1/knowledge-bases/{kb_id}/members")
        unknown = await outsider.get(
            f"/api/v1/knowledge-bases/{uuid.uuid4()}/members"
        )

    for response in (denied, unknown):
        assert response.status_code == 404
        assert response.json()["code"] == "KNOWLEDGE_BASE_NOT_FOUND"

    # 撤销成员关系后，同一请求也变为统一的 404。
    with kb_schema.begin() as connection:
        connection.execute(
            text(
                "UPDATE kb_member SET revoked_at = now() "
                "WHERE kb_id = :kb_id AND user_id = :user_id"
            ),
            {"kb_id": kb_id, "user_id": member_id},
        )
    async with api_client(kb_settings) as revoked:
        await login_csrf(revoked, member_username)
        after_revoke = await revoked.get(f"/api/v1/knowledge-bases/{kb_id}/members")
        revoked_listing = await revoked.get("/api/v1/knowledge-bases")

    assert after_revoke.status_code == 404
    assert after_revoke.json()["code"] == "KNOWLEDGE_BASE_NOT_FOUND"
    assert revoked_listing.json()["knowledgeBases"] == []


@pytest.mark.anyio
async def test_full_member_replacement_semantics_and_acl_revision(
    kb_schema: Engine, kb_settings: Settings
) -> None:
    admin_username = unique_username()
    admin_id = seed_user(kb_schema, username=admin_username, is_admin=True)
    editor_username = unique_username()
    editor_id = seed_user(kb_schema, username=editor_username)
    reader_username = unique_username()
    reader_id = seed_user(kb_schema, username=reader_username)
    other_org_username = unique_username()
    other_org_id = seed_user(
        kb_schema, username=other_org_username, organization_id=OTHER_ORGANIZATION_ID
    )

    async with api_client(kb_settings) as admin:
        csrf = await login_csrf(admin, admin_username)
        created = await create_kb(admin, name=unique_name(), csrf=csrf)
        kb_id = uuid.UUID(created.json()["id"])

        # 新增 EDITOR 与 READER：一次真实变化，acl_revision = 1。
        added = await put_members(
            admin,
            str(kb_id),
            [
                member_payload(admin_id, "OWNER"),
                member_payload(editor_id, "EDITOR"),
                member_payload(reader_id, "READER"),
            ],
            csrf,
        )
        assert added.status_code == 200, added.text
        assert added.json()["aclRevision"] == 1
        assert valid_member_rows(kb_schema, kb_id) == {
            admin_id: "OWNER",
            editor_id: "EDITOR",
            reader_id: "READER",
        }
        # 响应体成员快照必须与同一事务内落库的有效成员一致。
        assert response_members(added) == {
            admin_id: KbRole.OWNER,
            editor_id: KbRole.EDITOR,
            reader_id: KbRole.READER,
        }

        # 同一集合重复提交：无实际变化，acl_revision 不变。
        same = await put_members(
            admin,
            str(kb_id),
            [
                member_payload(admin_id, "OWNER"),
                member_payload(editor_id, "EDITOR"),
                member_payload(reader_id, "READER"),
            ],
            csrf,
        )
        assert same.status_code == 200
        assert same.json()["aclRevision"] == 1

        # 省略 editor：软撤销，acl_revision = 2。
        revoked = await put_members(
            admin,
            str(kb_id),
            [member_payload(admin_id, "OWNER"), member_payload(reader_id, "READER")],
            csrf,
        )
        assert revoked.status_code == 200
        assert revoked.json()["aclRevision"] == 2
        assert valid_member_rows(kb_schema, kb_id) == {
            admin_id: "OWNER",
            reader_id: "READER",
        }

        # 重新加入并改角色：清空 revoked_at，acl_revision = 3。
        rejoined = await put_members(
            admin,
            str(kb_id),
            [
                member_payload(admin_id, "OWNER"),
                member_payload(editor_id, "READER"),
                member_payload(reader_id, "READER"),
            ],
            csrf,
        )
        assert rejoined.status_code == 200
        assert rejoined.json()["aclRevision"] == 3
        assert valid_member_rows(kb_schema, kb_id) == {
            admin_id: "OWNER",
            editor_id: "READER",
            reader_id: "READER",
        }

        # 缩减为仅 OWNER：撤销 editor 与 reader，作为后续拒绝用例的基线。
        reduce_to_owner = await put_members(
            admin,
            str(kb_id),
            [member_payload(admin_id, "OWNER")],
            csrf,
        )
        assert reduce_to_owner.status_code == 200
        revision_before_rejections = reduce_to_owner.json()["aclRevision"]
        members_before = valid_member_rows(kb_schema, kb_id)

        empty = await put_members(admin, str(kb_id), [], csrf)
        assert empty.status_code == 422
        assert empty.json()["code"] == "KB_MEMBERS_INVALID"

        duplicate = await put_members(
            admin,
            str(kb_id),
            [member_payload(admin_id, "OWNER"), member_payload(admin_id, "OWNER")],
            csrf,
        )
        assert duplicate.status_code == 422
        assert duplicate.json()["code"] == "KB_MEMBERS_INVALID"

        no_owner = await put_members(
            admin,
            str(kb_id),
            [member_payload(editor_id, "READER")],
            csrf,
        )
        assert no_owner.status_code == 409
        assert no_owner.json()["code"] == "LAST_OWNER_REQUIRED"

        cross_org = await put_members(
            admin,
            str(kb_id),
            [member_payload(admin_id, "OWNER"), member_payload(other_org_id, "EDITOR")],
            csrf,
        )
        assert cross_org.status_code == 422
        assert cross_org.json()["code"] == "KB_MEMBER_USER_INVALID"

    # 所有被拒绝的写入都没有部分事务，状态与拒绝前一致。
    assert acl_revision(kb_schema, kb_id) == revision_before_rejections
    assert valid_member_rows(kb_schema, kb_id) == members_before
    assert other_org_id not in valid_member_rows(kb_schema, kb_id)


@pytest.mark.anyio
async def test_reader_role_cannot_replace_members(
    kb_schema: Engine, kb_settings: Settings
) -> None:
    admin_username = unique_username()
    admin_id = seed_user(kb_schema, username=admin_username, is_admin=True)
    reader_username = unique_username()
    reader_id = seed_user(kb_schema, username=reader_username)
    kb_id = seed_kb(kb_schema, organization_id=ORGANIZATION_ID, name=unique_name())
    seed_member(kb_schema, kb_id=kb_id, user_id=admin_id, role="OWNER")
    seed_member(kb_schema, kb_id=kb_id, user_id=reader_id, role="READER")

    async with api_client(kb_settings) as reader:
        csrf = await login_csrf(reader, reader_username)
        response = await put_members(
            reader, str(kb_id), [member_payload(reader_id, "OWNER")], csrf
        )

    assert response.status_code == 404
    assert response.json()["code"] == "KNOWLEDGE_BASE_NOT_FOUND"
    assert valid_member_rows(kb_schema, kb_id) == {
        admin_id: "OWNER",
        reader_id: "READER",
    }


@pytest.mark.anyio
async def test_service_rejects_non_owner_actor_inside_transaction(
    kb_schema: Engine, kb_settings: Settings
) -> None:
    owner_id = seed_user(kb_schema, username=unique_username(), is_admin=True)
    reader_id = seed_user(kb_schema, username=unique_username())
    kb_id = seed_kb(kb_schema, organization_id=ORGANIZATION_ID, name=unique_name())
    seed_member(kb_schema, kb_id=kb_id, user_id=owner_id, role="OWNER")
    seed_member(kb_schema, kb_id=kb_id, user_id=reader_id, role="READER")

    engine = create_database_engine(kb_settings)
    factory = create_session_factory(engine)
    session = factory()
    try:
        # 即使绕过路由依赖直接调用服务，锁内的 OWNER 复核也必须拒绝 READER。
        with pytest.raises(KnowledgeBaseNotFound):
            await replace_knowledge_base_members(
                session,
                kb_id=kb_id,
                organization_id=ORGANIZATION_ID,
                actor_user_id=reader_id,
                members=[MemberReplacement(user_id=owner_id, role=KbRole.OWNER)],
            )
    finally:
        await session.close()
        await engine.dispose()

    assert valid_member_rows(kb_schema, kb_id) == {
        owner_id: "OWNER",
        reader_id: "READER",
    }


@pytest.mark.anyio
async def test_replace_rejects_adding_or_promoting_disabled_owner(
    kb_schema: Engine, kb_settings: Settings
) -> None:
    admin_username = unique_username()
    admin_id = seed_user(kb_schema, username=admin_username, is_admin=True)
    disabled_id = seed_user(kb_schema, username=unique_username(), enabled=False)
    kb_id = seed_kb(kb_schema, organization_id=ORGANIZATION_ID, name=unique_name())
    seed_member(kb_schema, kb_id=kb_id, user_id=admin_id, role="OWNER")

    async with api_client(kb_settings) as admin:
        csrf = await login_csrf(admin, admin_username)

        add_disabled_owner = await put_members(
            admin,
            str(kb_id),
            [member_payload(admin_id, "OWNER"), member_payload(disabled_id, "OWNER")],
            csrf,
        )
        assert add_disabled_owner.status_code == 422
        assert add_disabled_owner.json()["code"] == "KB_MEMBER_OWNER_DISABLED"

        # 禁用用户以 READER 加入仍允许；登录能力只在 OWNER 身份上强制。
        add_reader = await put_members(
            admin,
            str(kb_id),
            [member_payload(admin_id, "OWNER"), member_payload(disabled_id, "READER")],
            csrf,
        )
        assert add_reader.status_code == 200
        revision_after_reader = add_reader.json()["aclRevision"]

        promote = await put_members(
            admin,
            str(kb_id),
            [member_payload(admin_id, "OWNER"), member_payload(disabled_id, "OWNER")],
            csrf,
        )
        assert promote.status_code == 422
        assert promote.json()["code"] == "KB_MEMBER_OWNER_DISABLED"

    assert acl_revision(kb_schema, kb_id) == revision_after_reader
    assert valid_member_rows(kb_schema, kb_id) == {
        admin_id: "OWNER",
        disabled_id: "READER",
    }


@pytest.mark.anyio
async def test_replace_requires_at_least_one_enabled_owner(
    kb_schema: Engine, kb_settings: Settings
) -> None:
    actor_username = unique_username()
    actor_id = seed_user(kb_schema, username=actor_username, is_admin=True)
    disabled_owner_id = seed_user(kb_schema, username=unique_username(), enabled=False)
    enabled_owner_id = seed_user(kb_schema, username=unique_username())
    kb_id = seed_kb(kb_schema, organization_id=ORGANIZATION_ID, name=unique_name())
    seed_member(kb_schema, kb_id=kb_id, user_id=actor_id, role="OWNER")
    seed_member(kb_schema, kb_id=kb_id, user_id=disabled_owner_id, role="OWNER")

    async with api_client(kb_settings) as actor:
        csrf = await login_csrf(actor, actor_username)
        # 只留下一名已禁用 OWNER、没有可登录 OWNER，拒绝。
        only_disabled = await put_members(
            actor, str(kb_id), [member_payload(disabled_owner_id, "OWNER")], csrf
        )
        assert only_disabled.status_code == 409
        assert only_disabled.json()["code"] == "LAST_OWNER_REQUIRED"

        # 保留既有已禁用 OWNER 的同时加入一名启用 OWNER 是允许的。
        with_enabled = await put_members(
            actor,
            str(kb_id),
            [
                member_payload(disabled_owner_id, "OWNER"),
                member_payload(enabled_owner_id, "OWNER"),
            ],
            csrf,
        )
        assert with_enabled.status_code == 200, with_enabled.text

    rows = valid_member_rows(kb_schema, kb_id)
    assert rows[disabled_owner_id] == "OWNER"
    assert rows[enabled_owner_id] == "OWNER"


@pytest.mark.anyio
async def test_concurrent_member_replacements_serialise_acl_revision(
    kb_schema: Engine, kb_settings: Settings
) -> None:
    admin_id = seed_user(kb_schema, username=unique_username(), is_admin=True)
    other_id = seed_user(kb_schema, username=unique_username())
    kb_id = seed_kb(kb_schema, organization_id=ORGANIZATION_ID, name=unique_name())
    seed_member(kb_schema, kb_id=kb_id, user_id=admin_id, role="OWNER")

    engine = create_database_engine(kb_settings)
    factory = create_session_factory(engine)
    gate = factory()
    session_a = factory()
    session_b = factory()
    try:
        # 先由 gate 持有知识库行锁，两个替换任务都必须在锁上等待，从而确定性交错。
        await gate.execute(
            text("SELECT id FROM knowledge_base WHERE id = :id FOR UPDATE"),
            {"id": kb_id},
        )

        task_a = asyncio.create_task(
            replace_knowledge_base_members(
                session_a,
                kb_id=kb_id,
                organization_id=ORGANIZATION_ID,
                actor_user_id=admin_id,
                members=[
                    MemberReplacement(user_id=admin_id, role=KbRole.OWNER),
                    MemberReplacement(user_id=other_id, role=KbRole.EDITOR),
                ],
            )
        )
        task_b = asyncio.create_task(
            replace_knowledge_base_members(
                session_b,
                kb_id=kb_id,
                organization_id=ORGANIZATION_ID,
                actor_user_id=admin_id,
                members=[
                    MemberReplacement(user_id=admin_id, role=KbRole.OWNER),
                    MemberReplacement(user_id=other_id, role=KbRole.READER),
                ],
            )
        )

        await asyncio.sleep(0.5)
        assert not task_a.done()
        assert not task_b.done()

        await gate.commit()

        members_a, revision_a = await asyncio.wait_for(task_a, timeout=10)
        members_b, revision_b = await asyncio.wait_for(task_b, timeout=10)
    finally:
        await gate.close()
        await session_a.close()
        await session_b.close()
        await engine.dispose()

    # 两个替换都真实改变状态：第二次看到第一次已提交的成员，故 revision 为 1 与 2，
    # 没有丢失更新；最终 acl_revision = 2。
    # 返回快照必须与各自请求一致（同一事务、KB 行锁内、commit 前读出）；
    # revision 与提交顺序一致且无丢失更新。
    assert {member.user_id: member.role for member in members_a} == {
        admin_id: KbRole.OWNER,
        other_id: KbRole.EDITOR,
    }
    assert {member.user_id: member.role for member in members_b} == {
        admin_id: KbRole.OWNER,
        other_id: KbRole.READER,
    }
    assert sorted([revision_a, revision_b]) == [1, 2]
    assert acl_revision(kb_schema, kb_id) == 2
    final_members = valid_member_rows(kb_schema, kb_id)
    assert set(final_members) == {admin_id, other_id}
    assert final_members[admin_id] == "OWNER"
    assert final_members[other_id] in {"EDITOR", "READER"}
