"""文档 ACL 首片在真实 PostgreSQL 上的聚焦验收：读取收紧、revision、检索/证据拒绝与下载。

覆盖：

- ``INHERIT`` 回归：KB 成员（含 OWNER）仍可读；
- ``RESTRICTED`` 只允许名单内且仍是有效 KB 成员的用户，空名单连 OWNER 也拒绝读；
- OWNER 管理可恢复（切回 ``INHERIT``）；非 OWNER、重复、跨组织、非成员被拒；
- ``acl_revision`` 只在 ``acl_mode``/名单实际变化时原子递增，重复替换幂等；
- 两条候选路、证据正文、来源状态复核都按 ACL 收紧；
- 下载目标只对可读用户可见，跨文档版本与已删除文档返回 ``None``；
- blob 通过真实 :class:`DocumentBlobStore` 读取与损坏检测；
- 锁序为 ``document → knowledge_base``：持有 KB 行锁时替换事务先拿到文档行锁再等 KB。

只使用被守卫的 ``_test`` 数据库与 pytest 临时目录；缺 DSN 时按既有契约跳过。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command
from database_guard import DestructiveTestDatabase
from database_roles_guard import RoleTestDatabases, assert_destructive_matches_roles
from httpx import ASGITransport, AsyncClient
from rag_backend.app import create_app
from rag_backend.auth.context import AuthContext
from rag_backend.auth.dependencies import get_auth_context
from rag_backend.auth.tokens import CSRF_HEADER_NAME
from rag_backend.config import Settings
from rag_backend.ingestion.errors import BlobCorrupt
from rag_backend.ingestion.storage import DocumentBlobStore
from rag_backend.knowledge.document_acl import (
    DocumentAclDocumentNotFound,
    DocumentAclInvalid,
    DocumentAclMember,
    DocumentAclMemberInvalid,
    DocumentAclMode,
    DocumentAclView,
    replace_document_acl,
    resolve_document_read_access,
)
from rag_backend.knowledge.document_content import (
    DocumentContentTarget,
    SqlDocumentContentRepository,
)
from rag_backend.retrieval.repository import SqlRetrievalRepository
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from test_core_migration import alembic_config, alembic_revision, business_tables
from test_retrieval_flow import (
    FakeAnalyzer,
    FakeEmbedder,
    _vector,
    api_session,
    insert_chunk,
    insert_document,
    insert_embedding,
    insert_generation,
    insert_kb,
    insert_member,
    insert_profile,
    insert_user,
    insert_version,
    run_search,
)

pytestmark = pytest.mark.integration

SCHEMA_REVISION = "20260928_0012"
ORGANIZATION_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
ORIGIN = "http://127.0.0.1"
CSRF_TOKEN = "integration-acl-csrf"

TRUNCATE_SQL = (
    "TRUNCATE document_acl, chunk_embedding, chunk, index_generation, outbox_event, "
    "ingest_job, document_version, document, knowledge_base, index_profile, kb_member, "
    "auth_session, user_account CASCADE"
)


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, Any]]:
    return ("asyncio", {"loop_factory": asyncio.SelectorEventLoop})


@pytest.fixture(scope="module")
def acl_schema(
    destructive_test_database: DestructiveTestDatabase,
    role_test_databases: RoleTestDatabases,
) -> Iterator[Engine]:
    assert_destructive_matches_roles(destructive_test_database.url, role_test_databases)
    config = alembic_config(destructive_test_database.url)
    engine = create_engine(destructive_test_database.url, pool_pre_ping=True)
    owns_schema = False
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT current_database()"))
                == destructive_test_database.database_name
            )
            assert alembic_revision(connection) is None
            assert business_tables(connection) == set()
        owns_schema = True
        command.upgrade(config, SCHEMA_REVISION)
        yield engine
    finally:
        try:
            if owns_schema:
                command.downgrade(config, "base")
                with engine.connect() as connection:
                    assert alembic_revision(connection) is None
                    assert business_tables(connection) == set()
        finally:
            engine.dispose()


@pytest.fixture(autouse=True)
def clean_rows(acl_schema: Engine) -> Iterator[None]:
    with acl_schema.begin() as connection:
        connection.execute(text(TRUNCATE_SQL))
    yield


# --- 造数据 -------------------------------------------------------------------


def seed_org_users(engine: Engine) -> dict[str, uuid.UUID]:
    owner_id = insert_user(engine, organization_id=ORGANIZATION_ID)
    reader_id = insert_user(engine, organization_id=ORGANIZATION_ID)
    outsider_id = insert_user(engine, organization_id=ORGANIZATION_ID)
    other_org_id = insert_user(engine, organization_id=uuid.uuid4())
    return {
        "owner": owner_id,
        "reader": reader_id,
        "outsider": outsider_id,
        "other_org": other_org_id,
    }


def seed_acl_document(
    engine: Engine, *, owner_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """建 KB、文档、版本并激活，OWNER 成员为 ``owner_id``。"""

    kb_id = insert_kb(engine, organization_id=ORGANIZATION_ID, active_profile_id=None)
    insert_member(engine, kb_id=kb_id, user_id=owner_id, role="OWNER")
    document_id = insert_document(engine, kb_id=kb_id)
    version_id = insert_version(engine, document_id=document_id)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE document SET active_version_id = :version_id "
                "WHERE id = :document_id"
            ),
            {"version_id": version_id, "document_id": document_id},
        )
    return kb_id, document_id, version_id


def seed_searchable(
    engine: Engine, *, kb_id: uuid.UUID, profile_id: uuid.UUID
) -> uuid.UUID:
    document_id = insert_document(engine, kb_id=kb_id)
    version_id = insert_version(engine, document_id=document_id)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE document SET active_version_id = :version_id "
                "WHERE id = :document_id"
            ),
            {"version_id": version_id, "document_id": document_id},
        )
    generation_id = insert_generation(engine, version_id=version_id, profile_id=profile_id)
    chunk_id = insert_chunk(
        engine,
        generation_id=generation_id,
        document_id=document_id,
        version_id=version_id,
        organization_id=ORGANIZATION_ID,
        kb_id=kb_id,
        fts_terms="hello world",
    )
    insert_embedding(
        engine, chunk_id=chunk_id, profile_id=profile_id, embedding=_vector(1.0, 0.0)
    )
    return document_id


def first_chunk_id(engine: Engine, document_id: uuid.UUID) -> uuid.UUID:
    with engine.connect() as connection:
        return uuid.UUID(
            str(
                connection.scalar(
                    text(
                        "SELECT c.id FROM chunk c JOIN document_version dv "
                        "ON dv.id = c.version_id WHERE dv.document_id = :id LIMIT 1"
                    ),
                    {"id": document_id},
                )
            )
        )


def acl_mode(engine: Engine, document_id: uuid.UUID) -> str:
    with engine.connect() as connection:
        return str(
            connection.scalar(
                text("SELECT acl_mode FROM document WHERE id = :id"),
                {"id": document_id},
            )
        )


def acl_revision(engine: Engine, kb_id: uuid.UUID) -> int:
    with engine.connect() as connection:
        return int(
            connection.scalar(
                text("SELECT acl_revision FROM knowledge_base WHERE id = :id"),
                {"id": kb_id},
            )
        )


def acl_rows(engine: Engine, document_id: uuid.UUID) -> set[uuid.UUID]:
    with engine.connect() as connection:
        return {
            row[0]
            for row in connection.execute(
                text("SELECT principal_id FROM document_acl WHERE document_id = :id"),
                {"id": document_id},
            )
        }


async def replace(
    url: str,
    *,
    document_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    mode: DocumentAclMode,
    member_ids: list[uuid.UUID],
) -> DocumentAclView:
    async with api_session(url) as session:
        return await replace_document_acl(
            session,
            document_id=document_id,
            organization_id=ORGANIZATION_ID,
            actor_user_id=actor_user_id,
            mode=mode,
            members=[DocumentAclMember(user_id=member) for member in member_ids],
        )


async def read_allowed(
    url: str, *, document_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    async with api_session(url) as session:
        access = await resolve_document_read_access(
            session,
            document_id=document_id,
            user_id=user_id,
            organization_id=ORGANIZATION_ID,
        )
        return access is not None


# --- 权限、revision 与幂等 ------------------------------------------------------


@pytest.mark.anyio
async def test_inherit_regression_and_restricted_rules(
    acl_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    users = seed_org_users(acl_schema)
    kb_id, document_id, _ = seed_acl_document(acl_schema, owner_id=users["owner"])
    owner_id = users["owner"]
    reader_id = users["reader"]
    insert_member(acl_schema, kb_id=kb_id, user_id=reader_id, role="READER")
    api_url = role_test_databases.api_url

    # 默认 INHERIT：有效成员（含 OWNER）可读，非成员不可读。
    assert acl_mode(acl_schema, document_id) == "INHERIT"
    assert await read_allowed(api_url, document_id=document_id, user_id=owner_id)
    assert await read_allowed(api_url, document_id=document_id, user_id=reader_id)
    assert not await read_allowed(
        api_url, document_id=document_id, user_id=users["outsider"]
    )

    # RESTRICTED 只放行名单内用户；未登记的 OWNER 也被拒绝读。
    view = await replace(
        api_url,
        document_id=document_id,
        actor_user_id=owner_id,
        mode=DocumentAclMode.RESTRICTED,
        member_ids=[reader_id],
    )
    assert view.mode is DocumentAclMode.RESTRICTED
    assert view.member_ids == (reader_id,)
    assert view.acl_revision == 1
    assert acl_revision(acl_schema, kb_id) == 1
    assert acl_rows(acl_schema, document_id) == {reader_id}
    assert await read_allowed(api_url, document_id=document_id, user_id=reader_id)
    assert not await read_allowed(api_url, document_id=document_id, user_id=owner_id)

    # 重复同一请求幂等：revision 不变、行不变。
    repeat = await replace(
        api_url,
        document_id=document_id,
        actor_user_id=owner_id,
        mode=DocumentAclMode.RESTRICTED,
        member_ids=[reader_id],
    )
    assert repeat.acl_revision == 1
    assert acl_revision(acl_schema, kb_id) == 1

    # 空名单：任何人都不可读，连 OWNER 也拒绝。
    empty = await replace(
        api_url,
        document_id=document_id,
        actor_user_id=owner_id,
        mode=DocumentAclMode.RESTRICTED,
        member_ids=[],
    )
    assert empty.acl_revision == 2
    assert acl_rows(acl_schema, document_id) == set()
    assert not await read_allowed(api_url, document_id=document_id, user_id=owner_id)
    assert not await read_allowed(api_url, document_id=document_id, user_id=reader_id)

    # OWNER 管理可恢复：切回 INHERIT，成员读权限恢复。
    restored = await replace(
        api_url,
        document_id=document_id,
        actor_user_id=owner_id,
        mode=DocumentAclMode.INHERIT,
        member_ids=[],
    )
    assert restored.acl_revision == 3
    assert await read_allowed(api_url, document_id=document_id, user_id=owner_id)
    assert await read_allowed(api_url, document_id=document_id, user_id=reader_id)


@pytest.mark.anyio
async def test_replace_rejects_invalid_actor_and_members(
    acl_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    users = seed_org_users(acl_schema)
    kb_id, document_id, _ = seed_acl_document(acl_schema, owner_id=users["owner"])
    owner_id = users["owner"]
    reader_id = users["reader"]
    outsider_id = users["outsider"]
    other_org_id = users["other_org"]
    insert_member(acl_schema, kb_id=kb_id, user_id=reader_id, role="READER")
    api_url = role_test_databases.api_url

    # 非 OWNER 不能管理 ACL。
    with pytest.raises(DocumentAclDocumentNotFound):
        await replace(
            api_url,
            document_id=document_id,
            actor_user_id=reader_id,
            mode=DocumentAclMode.RESTRICTED,
            member_ids=[reader_id],
        )

    with pytest.raises(DocumentAclInvalid):
        await replace(
            api_url,
            document_id=document_id,
            actor_user_id=owner_id,
            mode=DocumentAclMode.INHERIT,
            member_ids=[reader_id],
        )

    with pytest.raises(DocumentAclInvalid):
        await replace(
            api_url,
            document_id=document_id,
            actor_user_id=owner_id,
            mode=DocumentAclMode.RESTRICTED,
            member_ids=[reader_id, reader_id],
        )

    # 不存在用户、跨组织用户、同组织但不是该 KB 成员的用户都拒绝。
    with pytest.raises(DocumentAclMemberInvalid):
        await replace(
            api_url,
            document_id=document_id,
            actor_user_id=owner_id,
            mode=DocumentAclMode.RESTRICTED,
            member_ids=[uuid.uuid4()],
        )
    with pytest.raises(DocumentAclMemberInvalid):
        await replace(
            api_url,
            document_id=document_id,
            actor_user_id=owner_id,
            mode=DocumentAclMode.RESTRICTED,
            member_ids=[other_org_id],
        )
    with pytest.raises(DocumentAclMemberInvalid):
        await replace(
            api_url,
            document_id=document_id,
            actor_user_id=owner_id,
            mode=DocumentAclMode.RESTRICTED,
            member_ids=[outsider_id],
        )

    # 被拒绝的请求没有部分写入：仍是 INHERIT、无名单行、revision 未动。
    assert acl_mode(acl_schema, document_id) == "INHERIT"
    assert acl_rows(acl_schema, document_id) == set()
    assert acl_revision(acl_schema, kb_id) == 0


@pytest.mark.anyio
async def test_replace_acl_on_deleted_document_is_not_found_and_unchanged(
    acl_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    """已删除文档的 ACL 替换与不存在/越权一样返回 404，不改 ACL 也不递增 revision。"""

    users = seed_org_users(acl_schema)
    owner_id = users["owner"]
    kb_id, document_id, _ = seed_acl_document(acl_schema, owner_id=owner_id)
    api_url = role_test_databases.api_url

    first = await replace(
        api_url,
        document_id=document_id,
        actor_user_id=owner_id,
        mode=DocumentAclMode.RESTRICTED,
        member_ids=[owner_id],
    )
    assert first.acl_revision == 1

    with acl_schema.begin() as connection:
        connection.execute(
            text(
                "UPDATE document SET deleted_at = now(), lifecycle_status = 'DELETED' "
                "WHERE id = :id"
            ),
            {"id": document_id},
        )

    with pytest.raises(DocumentAclDocumentNotFound):
        await replace(
            api_url,
            document_id=document_id,
            actor_user_id=owner_id,
            mode=DocumentAclMode.INHERIT,
            member_ids=[],
        )

    assert acl_mode(acl_schema, document_id) == "RESTRICTED"
    assert acl_rows(acl_schema, document_id) == {owner_id}
    assert acl_revision(acl_schema, kb_id) == 1


@pytest.mark.anyio
async def test_replace_acl_route_on_deleted_document_returns_404(
    acl_schema: Engine, role_test_databases: RoleTestDatabases, tmp_path: Any
) -> None:
    """真实路由 + 真实服务：已删除文档的 PUT ACL 返回 404，且库内 ACL/revision 不变。"""

    users = seed_org_users(acl_schema)
    owner_id = users["owner"]
    kb_id, document_id, _ = seed_acl_document(acl_schema, owner_id=owner_id)
    api_url = role_test_databases.api_url

    with acl_schema.begin() as connection:
        connection.execute(
            text(
                "UPDATE document SET deleted_at = now(), lifecycle_status = 'DELETED' "
                "WHERE id = :id"
            ),
            {"id": document_id},
        )

    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "session_cookie_secure": False,
        "allowed_origins": ORIGIN,
        "csrf_secret": "integration-acl-secret",
        "document_storage_directory": str(tmp_path),
    }
    settings = Settings(database_url=api_url, **values)
    app = create_app(settings)
    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        user_id=owner_id,
        organization_id=ORGANIZATION_ID,
        username="owner",
        is_admin=False,
        session_id=uuid.uuid4(),
        csrf_token=CSRF_TOKEN,
    )
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, client=("10.20.0.1", 12345))
        async with AsyncClient(transport=transport, base_url=ORIGIN) as client:
            response = await client.put(
                f"/api/v1/documents/{document_id}/acl",
                json={"mode": "INHERIT", "members": []},
                headers={"Origin": ORIGIN, CSRF_HEADER_NAME: CSRF_TOKEN},
            )

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "DOCUMENT_NOT_FOUND"
    assert acl_mode(acl_schema, document_id) == "INHERIT"
    assert acl_rows(acl_schema, document_id) == set()
    assert acl_revision(acl_schema, kb_id) == 0


# --- 检索、证据与来源状态 --------------------------------------------------------


@pytest.mark.anyio
async def test_retrieval_and_evidence_respect_acl(
    acl_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    allowed_id = insert_user(acl_schema, organization_id=ORGANIZATION_ID)
    denied_id = insert_user(acl_schema, organization_id=ORGANIZATION_ID)
    profile_id = insert_profile(acl_schema, revision="rev-acl")
    kb_id = insert_kb(
        acl_schema, organization_id=ORGANIZATION_ID, active_profile_id=profile_id
    )
    insert_member(acl_schema, kb_id=kb_id, user_id=allowed_id, role="OWNER")
    insert_member(acl_schema, kb_id=kb_id, user_id=denied_id, role="READER")
    document_id = seed_searchable(acl_schema, kb_id=kb_id, profile_id=profile_id)
    chunk_id = first_chunk_id(acl_schema, document_id)
    api_url = role_test_databases.api_url

    # INHERIT：KB 成员都能召回。
    async with api_session(api_url) as session:
        inherit_hits = await run_search(
            session,
            user_id=allowed_id,
            organization_id=ORGANIZATION_ID,
            kb_ids=[kb_id],
            embedder=FakeEmbedder(),
            analyzer=FakeAnalyzer(terms="hello world"),
        )
    assert {candidate.chunk_id for candidate in inherit_hits} == {chunk_id}

    await replace(
        api_url,
        document_id=document_id,
        actor_user_id=allowed_id,
        mode=DocumentAclMode.RESTRICTED,
        member_ids=[allowed_id],
    )

    # RESTRICTED：只有名单内成员能召回；其他 KB 成员一条都拿不到。
    async with api_session(api_url) as session:
        allowed_hits = await run_search(
            session,
            user_id=allowed_id,
            organization_id=ORGANIZATION_ID,
            kb_ids=[kb_id],
            embedder=FakeEmbedder(),
            analyzer=FakeAnalyzer(terms="hello world"),
        )
    async with api_session(api_url) as session:
        denied_hits = await run_search(
            session,
            user_id=denied_id,
            organization_id=ORGANIZATION_ID,
            kb_ids=[kb_id],
            embedder=FakeEmbedder(),
            analyzer=FakeAnalyzer(terms="hello world"),
        )
    assert {candidate.chunk_id for candidate in allowed_hits} == {chunk_id}
    assert denied_hits == []

    # 证据正文读取同样收紧；来源状态行仍会返回（不能因 ACL 过滤掉行而误判为已授权）。
    async with api_session(api_url) as session:
        repository = SqlRetrievalRepository(session)
        allowed_evidence = await repository.load_evidence_chunks(
            user_id=allowed_id, organization_id=ORGANIZATION_ID, chunk_ids=[chunk_id]
        )
        await repository.release()
    async with api_session(api_url) as session:
        repository = SqlRetrievalRepository(session)
        denied_evidence = await repository.load_evidence_chunks(
            user_id=denied_id, organization_id=ORGANIZATION_ID, chunk_ids=[chunk_id]
        )
        denied_states = await repository.load_chunk_source_states(
            user_id=denied_id, organization_id=ORGANIZATION_ID, chunk_ids=[chunk_id]
        )
        await repository.release()
    assert [row.chunk_id for row in allowed_evidence] == [chunk_id]
    assert denied_evidence == []
    assert len(denied_states) == 1
    assert denied_states[0].is_authorized() is False

    # 名单恢复后 denied 又能读。
    await replace(
        api_url,
        document_id=document_id,
        actor_user_id=allowed_id,
        mode=DocumentAclMode.INHERIT,
        member_ids=[],
    )
    async with api_session(api_url) as session:
        repository = SqlRetrievalRepository(session)
        restored = await repository.load_chunk_source_states(
            user_id=denied_id, organization_id=ORGANIZATION_ID, chunk_ids=[chunk_id]
        )
        await repository.release()
    assert restored[0].is_authorized() is True


@pytest.mark.anyio
async def test_adjacent_evidence_respects_document_acl(
    acl_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    """相邻证据与直接证据一样受文档 ACL 收紧：RESTRICTED 名单外成员拿不到邻居。

    真实 SQL 负例：定义后由具备守卫 DSN 的环境运行；未运行不声称已实测。
    """

    allowed_id = insert_user(acl_schema, organization_id=ORGANIZATION_ID)
    denied_id = insert_user(acl_schema, organization_id=ORGANIZATION_ID)
    profile_id = insert_profile(acl_schema, revision="rev-acl-adjacent")
    kb_id = insert_kb(
        acl_schema, organization_id=ORGANIZATION_ID, active_profile_id=profile_id
    )
    insert_member(acl_schema, kb_id=kb_id, user_id=allowed_id, role="OWNER")
    insert_member(acl_schema, kb_id=kb_id, user_id=denied_id, role="READER")
    document_id = seed_searchable(acl_schema, kb_id=kb_id, profile_id=profile_id)
    seed_chunk = first_chunk_id(acl_schema, document_id)
    with acl_schema.connect() as connection:
        row = connection.execute(
            text("SELECT generation_id, version_id, kb_id FROM chunk WHERE id = :id"),
            {"id": seed_chunk},
        ).mappings().one()
    neighbor = insert_chunk(
        acl_schema,
        generation_id=row["generation_id"],
        document_id=document_id,
        version_id=row["version_id"],
        organization_id=ORGANIZATION_ID,
        kb_id=row["kb_id"],
        chunk_index=1,
        fts_terms="hello neighbor",
    )
    insert_embedding(
        acl_schema, chunk_id=neighbor, profile_id=profile_id, embedding=_vector(1.0, 0.0)
    )
    await replace(
        role_test_databases.api_url,
        document_id=document_id,
        actor_user_id=allowed_id,
        mode=DocumentAclMode.RESTRICTED,
        member_ids=[allowed_id],
    )

    async with api_session(role_test_databases.api_url) as session:
        repository = SqlRetrievalRepository(session)
        allowed = await repository.load_adjacent_evidence_chunks(
            user_id=allowed_id, organization_id=ORGANIZATION_ID, chunk_ids=[seed_chunk]
        )
        await repository.release()
    assert [item.chunk.chunk_id for item in allowed] == [neighbor]

    async with api_session(role_test_databases.api_url) as session:
        repository = SqlRetrievalRepository(session)
        denied = await repository.load_adjacent_evidence_chunks(
            user_id=denied_id, organization_id=ORGANIZATION_ID, chunk_ids=[seed_chunk]
        )
        await repository.release()
    assert denied == []


# --- 下载目标与 blob ------------------------------------------------------------


@pytest.mark.anyio
async def test_content_target_selects_active_and_explicit_versions(
    acl_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    users = seed_org_users(acl_schema)
    owner_id = users["owner"]
    reader_id = users["reader"]
    kb_id, document_id, version_id = seed_acl_document(acl_schema, owner_id=owner_id)
    insert_member(acl_schema, kb_id=kb_id, user_id=reader_id, role="READER")
    api_url = role_test_databases.api_url

    old_version_id = uuid.uuid4()
    with acl_schema.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO document_version (id, document_id, version_no, file_ref, "
                "file_hash, mime, parser_version, status) VALUES (:id, :document_id, 2, "
                "'ref/old', :hash, 'text/markdown', 'p', 'READY')"
            ),
            {"id": old_version_id, "document_id": document_id, "hash": "b" * 64},
        )

    async def target(
        user_id: uuid.UUID, version: uuid.UUID | None
    ) -> DocumentContentTarget | None:
        async with api_session(api_url) as session:
            repository = SqlDocumentContentRepository(session)
            return await repository.load_content_target(
                document_id=document_id,
                user_id=user_id,
                organization_id=ORGANIZATION_ID,
                version_id=version,
            )

    # INHERIT：成员可读 active 与显式旧版本；非成员不可读。
    active_target = await target(owner_id, None)
    assert active_target is not None and active_target.version_id == version_id
    old_target = await target(owner_id, old_version_id)
    assert old_target is not None and old_target.version_id == old_version_id
    assert await target(users["outsider"], None) is None

    # RESTRICTED 名单外成员不可读；名单内可读。
    await replace(
        api_url,
        document_id=document_id,
        actor_user_id=owner_id,
        mode=DocumentAclMode.RESTRICTED,
        member_ids=[reader_id],
    )
    assert await target(owner_id, None) is None
    restricted_old = await target(reader_id, old_version_id)
    assert restricted_old is not None and restricted_old.version_id == old_version_id

    # 跨文档版本一律不可见。
    _other_kb, _other_document, other_version = seed_acl_document(
        acl_schema, owner_id=owner_id
    )
    assert await target(reader_id, other_version) is None

    # 已删除文档不可读。
    with acl_schema.begin() as connection:
        connection.execute(
            text(
                "UPDATE document SET deleted_at = now(), lifecycle_status = 'DELETED' "
                "WHERE id = :id"
            ),
            {"id": document_id},
        )
    assert await target(reader_id, version_id) is None


def test_blob_read_and_corruption_detection(tmp_path: Any, acl_schema: Engine) -> None:
    users = seed_org_users(acl_schema)
    kb_id, _document_id, version_id = seed_acl_document(
        acl_schema, owner_id=users["owner"]
    )
    store = DocumentBlobStore(tmp_path)
    content = b"# real bytes\n"
    file_hash = hashlib.sha256(content).hexdigest()
    file_ref = store.publish(kb_id, file_hash, content)

    with acl_schema.begin() as connection:
        connection.execute(
            text(
                "UPDATE document_version SET file_ref = :file_ref, file_hash = :file_hash "
                "WHERE id = :id"
            ),
            {"file_ref": file_ref, "file_hash": file_hash, "id": version_id},
        )
    assert store.read_verified_blob(kb_id, file_ref, file_hash) == content

    # 篡改字节后摘要不符：读失败为静态 BlobCorrupt，不含路径。
    path = store.path_for(file_ref)
    path.write_bytes(b"tampered")
    with pytest.raises(BlobCorrupt) as failure:
        store.read_verified_blob(kb_id, file_ref, file_hash)
    assert str(path) not in str(failure.value)


# --- 锁序：document → knowledge_base -------------------------------------------


@pytest.mark.anyio
async def test_acl_replace_locks_document_before_knowledge_base(
    acl_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    """持 KB 行锁时，替换事务必须先拿到文档行锁再阻塞在 KB 更新上。

    这验证 ``document → knowledge_base`` 与删除同序；probe 会话用短 ``lock_timeout``
    观察文档行是否已被替换事务持有。
    """

    users = seed_org_users(acl_schema)
    owner_id = users["owner"]
    kb_id, document_id, _ = seed_acl_document(acl_schema, owner_id=owner_id)
    api_url = role_test_databases.api_url
    migrator_url = role_test_databases.migrator_url

    async with api_session(migrator_url) as kb_holder:
        await kb_holder.execute(
            text("SELECT id FROM knowledge_base WHERE id = :id FOR UPDATE"),
            {"id": kb_id},
        )

        task = asyncio.create_task(
            replace(
                api_url,
                document_id=document_id,
                actor_user_id=owner_id,
                mode=DocumentAclMode.RESTRICTED,
                member_ids=[owner_id],
            )
        )

        deadline = time.monotonic() + 15.0
        document_locked = False
        while time.monotonic() < deadline:
            async with api_session(migrator_url) as probe:
                try:
                    await probe.execute(text("SET LOCAL lock_timeout = '300ms'"))
                    await probe.execute(
                        text("SELECT id FROM document WHERE id = :id FOR UPDATE"),
                        {"id": document_id},
                    )
                    await probe.rollback()
                except OperationalError:
                    await probe.rollback()
                    document_locked = True
                    break
            await asyncio.sleep(0.05)

        assert document_locked, "替换事务未按 document → knowledge_base 顺序先持有文档行锁"
        assert not task.done(), "替换事务不应在 KB 行锁释放前完成"

        await kb_holder.rollback()
        view = await task

    assert view.acl_revision == 1
    assert acl_mode(acl_schema, document_id) == "RESTRICTED"
