"""文档新版本与逻辑删除在真实 PostgreSQL + Redis 上的验收。

覆盖：EDITOR 上传新版本、文档行锁分配 ``version_no``、同键幂等复用与并发、expected
active 过期 409、发布前后旧版持续服务/切换、发布失败旧版仍 READY、删除 tombstone 后
双路零候选、删除终止排队与在途 job、在途发布不复活、二次删除幂等、已删除文档的旧幂等
回放不得当有效资源、OWNER 授权/CSRF/Origin。

授权与发布使用真实 api/worker 角色；向量编码器与 token 计数器是显式假实现，**不**代表真实
模型。缺少守卫 DSN 或 Redis 时按既有契约跳过，绝不触碰开发库或真实 ``.env``。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from database_guard import DestructiveTestDatabase
from database_roles_guard import (
    RoleTestDatabases,
    assert_destructive_matches_roles,
)
from rag_backend.api.documents import _delete_document_with_retry
from rag_backend.api.errors import CODE_IDEMPOTENCY_KEY_REUSED, ApiError
from rag_backend.auth.context import AuthContext
from rag_backend.auth.tokens import CSRF_HEADER_NAME
from rag_backend.config import Settings
from rag_backend.database import create_sync_session_factory
from rag_backend.dispatch import protocol
from rag_backend.dispatch.repository import SqlOutboxRepository
from rag_backend.ingestion import indexing_worker as iw
from rag_backend.ingestion import service as ingestion_service
from rag_backend.ingestion.parsing import MARKDOWN_PARSER_VERSION
from rag_backend.ingestion.storage import DocumentBlobStore
from rag_backend.ingestion.validation import (
    build_version_dedupe_key,
    build_version_dedupe_key_prefix,
)
from rag_backend.models.profile_contract import IndexProfileContract
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from test_core_migration import alembic_config, alembic_revision, business_tables
from test_document_upload_flow import (
    ORGANIZATION_ID,
    ORIGIN,
    api_client,
    count_rows,
    document_row,
    job_row,
    kb_active_profile,
    login_csrf,
    make_settings,
    seed_kb,
    seed_member,
    seed_user,
    unique_key,
    unique_name,
    unique_username,
    upload,
    version_row,
)
from test_indexing_pipeline_flow import (
    FakeAnalyzer as PipelineAnalyzer,
)
from test_indexing_pipeline_flow import (
    FakeCounter,
    FakeEmbedder,
    FakeIdentity,
    make_dependencies,
)
from test_retrieval_flow import (
    FakeAnalyzer as RetrievalAnalyzer,
)
from test_retrieval_flow import (
    FakeEmbedder as RetrievalEmbedder,
)
from test_retrieval_flow import (
    api_session,
    run_search,
)

pytestmark = pytest.mark.integration

# 发布事务需要 worker 对 ``knowledge_base(active_index_profile_id, kb_revision)`` 的列级
# UPDATE（0007），而上传/新版本 ORM 还写入受理时刻的 ``ingest_job.request_title``
# （0008），因此本片 schema 必须升到 0008。
SCHEMA_REVISION = "20260926_0008"
V1_BODY = b"# alpha\n\nbeta gamma\n"
V2_BODY = b"# delta\n\nepsilon zeta\n"
V1_TITLE = "版本一"
V2_TITLE = "版本二"

TRUNCATE_SQL = (
    "TRUNCATE chunk_embedding, chunk, index_generation, outbox_event, ingest_job, "
    "document_version, document, knowledge_base, index_profile, kb_member, auth_session, "
    "user_account CASCADE"
)


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, Any]]:
    return ("asyncio", {"loop_factory": asyncio.SelectorEventLoop})


@pytest.fixture(scope="module")
def update_delete_schema(
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
def clean_business_rows(update_delete_schema: Engine) -> Iterator[None]:
    with update_delete_schema.begin() as connection:
        connection.execute(text(TRUNCATE_SQL))
    yield


@pytest.fixture(scope="module")
def blob_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("update-delete-documents")


@pytest.fixture
def storage(blob_root: Path) -> DocumentBlobStore:
    return DocumentBlobStore(blob_root)


@pytest.fixture(scope="module")
def worker_sessions(
    update_delete_schema: Engine, role_test_databases: RoleTestDatabases
) -> Iterator[Any]:
    engine = create_engine(role_test_databases.worker_url, pool_pre_ping=True)
    try:
        yield create_sync_session_factory(engine)
    finally:
        engine.dispose()


@pytest.fixture
def update_delete_settings(
    update_delete_schema: Engine,
    destructive_test_database: DestructiveTestDatabase,
    role_test_databases: RoleTestDatabases,
    test_redis: Any,
    blob_root: Path,
) -> Settings:
    assert role_test_databases.database_name == destructive_test_database.database_name
    return make_settings(
        database_url=role_test_databases.api_url,
        redis_url=test_redis.url,
        storage_directory=blob_root,
    )


# --- 辅助 ---------------------------------------------------------------------


def seed_editor(engine: Engine, kb_id: uuid.UUID) -> tuple[str, uuid.UUID]:
    username = unique_username()
    user_id = seed_user(engine, username=username)
    seed_member(engine, kb_id=kb_id, user_id=user_id, role="EDITOR")
    return username, user_id


async def upload_version(
    client: Any,
    document_id: uuid.UUID,
    *,
    idempotency_key: str | None,
    expected_version_id: uuid.UUID,
    title: str = V2_TITLE,
    content: bytes = V2_BODY,
    filename: str = "notes.md",
    csrf: str | None = None,
    origin: str = ORIGIN,
) -> Any:
    headers: dict[str, str] = {"Origin": origin}
    if csrf is not None:
        headers[CSRF_HEADER_NAME] = csrf
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return await client.post(
        f"/api/v1/documents/{document_id}/versions",
        files={"file": (filename, content, "text/markdown")},
        data={"title": title, "expectedVersionId": str(expected_version_id)},
        headers=headers,
    )


async def delete_document_request(
    client: Any, document_id: uuid.UUID, *, csrf: str | None, origin: str = ORIGIN
) -> Any:
    headers: dict[str, str] = {"Origin": origin}
    if csrf is not None:
        headers[CSRF_HEADER_NAME] = csrf
    return await client.delete(f"/api/v1/documents/{document_id}", headers=headers)


async def first_upload(
    client: Any, kb_id: uuid.UUID, *, csrf: str, title: str = V1_TITLE, content: bytes = V1_BODY
) -> Any:
    return await upload(
        client, kb_id, idempotency_key=unique_key(), title=title, content=content, csrf=csrf
    )


def job_profile_id(engine: Engine, job_id: uuid.UUID) -> uuid.UUID:
    with engine.connect() as connection:
        return cast(
            uuid.UUID,
            connection.execute(
                text("SELECT profile_id FROM ingest_job WHERE id = :id"), {"id": job_id}
            ).one()[0],
        )


def read_profile_contract(engine: Engine, profile_id: uuid.UUID) -> IndexProfileContract:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT embedding_model, model_revision, dimension, normalize, "
                "tokenizer_revision, chunker_version, keyword_analyzer_version "
                "FROM index_profile WHERE id = :id"
            ),
            {"id": profile_id},
        ).mappings().one()
    return IndexProfileContract(
        embedding_model=row["embedding_model"],
        model_revision=row["model_revision"],
        dimension=row["dimension"],
        normalize=row["normalize"],
        tokenizer_revision=row["tokenizer_revision"],
        chunker_version=row["chunker_version"],
        keyword_analyzer_version=row["keyword_analyzer_version"],
    )


def identity_for_job(engine: Engine, job_id: uuid.UUID) -> FakeIdentity:
    contract = read_profile_contract(engine, job_profile_id(engine, job_id))
    return FakeIdentity(
        profile=contract,
        parser_version=MARKDOWN_PARSER_VERSION,
        pdf_parser_version="pypdf-6.19.0-v1",
        token_counter=FakeCounter(),
        keyword_analyzer=PipelineAnalyzer(),
    )


def publish(
    engine: Engine,
    worker_sessions: Any,
    storage: DocumentBlobStore,
    job_id: uuid.UUID,
    *,
    mode: str = "ok",
) -> str:
    identity = identity_for_job(engine, job_id)
    dependencies = make_dependencies(
        worker_sessions, storage, embedder=FakeEmbedder(mode=mode), identity=identity
    )
    return iw.process_ingest_event(
        dependencies, job_id=job_id, event_id=str(uuid.uuid4())
    )


def kb_keyword_analyzer_version(engine: Engine, kb_id: uuid.UUID) -> str:
    with engine.connect() as connection:
        return str(
            connection.scalar(
                text(
                    "SELECT p.keyword_analyzer_version FROM knowledge_base kb "
                    "JOIN index_profile p ON p.id = kb.active_index_profile_id "
                    "WHERE kb.id = :id"
                ),
                {"id": kb_id},
            )
        )


def kb_profile_id(engine: Engine, kb_id: uuid.UUID) -> uuid.UUID:
    with engine.connect() as connection:
        return cast(
            uuid.UUID,
            connection.execute(
                text("SELECT active_index_profile_id FROM knowledge_base WHERE id = :id"),
                {"id": kb_id},
            ).one()[0],
        )


async def search_with_real_analyzer_identity(
    api_url: str, engine: Engine, *, user_id: uuid.UUID, kb_id: uuid.UUID, query: str, terms: str
) -> list[Any]:
    analyzer = RetrievalAnalyzer(
        terms=terms, analyzer_id=kb_keyword_analyzer_version(engine, kb_id)
    )
    async with api_session(api_url) as session:
        return await run_search(
            session,
            user_id=user_id,
            organization_id=ORGANIZATION_ID,
            kb_ids=[kb_id],
            query=query,
            embedder=RetrievalEmbedder(vector=(1.0,)),
            analyzer=analyzer,
        )


# --- 更新：发布前后与失败 -----------------------------------------------------


@pytest.mark.anyio
async def test_update_publish_switches_pointer_and_keeps_old_before_after(
    update_delete_schema: Engine,
    update_delete_settings: Settings,
    worker_sessions: Any,
    storage: DocumentBlobStore,
    role_test_databases: RoleTestDatabases,
) -> None:
    kb_id = seed_kb(update_delete_schema, name=unique_name())
    username, user_id = seed_editor(update_delete_schema, kb_id)

    async with api_client(update_delete_settings) as client:
        csrf = await login_csrf(client, username)
        first = await first_upload(client, kb_id, csrf=csrf)
        assert first.status_code == 202, first.text
        document_id = uuid.UUID(first.json()["documentId"])
        v1_id = uuid.UUID(first.json()["versionId"])
        job1 = uuid.UUID(first.json()["jobId"])

    assert publish(update_delete_schema, worker_sessions, storage, job1) == iw.PROCESS_STATUS_READY
    assert document_row(update_delete_schema, document_id)["active_version_id"] == v1_id
    assert kb_active_profile(update_delete_schema, kb_id) is not None
    revision_after_v1 = _kb_revision(update_delete_schema, kb_id)

    # 发布 v1 后：两条路都只召回 v1。
    found = await search_with_real_analyzer_identity(
        role_test_databases.api_url,
        update_delete_schema,
        user_id=user_id,
        kb_id=kb_id,
        query="beta",
        terms="beta",
    )
    assert found and {candidate.version_id for candidate in found} == {v1_id}

    key = unique_key()
    async with api_client(update_delete_settings) as client:
        csrf = await login_csrf(client, username)
        second = await upload_version(
            client,
            document_id,
            idempotency_key=key,
            expected_version_id=v1_id,
            csrf=csrf,
        )
        assert second.status_code == 202, second.text
        v2_id = uuid.UUID(second.json()["versionId"])
        job2 = uuid.UUID(second.json()["jobId"])
        assert second.json()["documentId"] == str(document_id)

        # 同键同 expected 的有效回放返回同一 v2，不新建版本。
        replay = await upload_version(
            client,
            document_id,
            idempotency_key=key,
            expected_version_id=v1_id,
            csrf=csrf,
        )
        assert replay.status_code == 202, replay.text
        assert replay.json() == second.json()
        assert count_rows(update_delete_schema, "document_version") == 2

    v2 = version_row(update_delete_schema, v2_id)
    assert v2["version_no"] == 2
    assert v2["status"] == "PENDING"
    assert job_row(update_delete_schema, job2)["status"] == "QUEUED"
    # v2 未发布：active 仍指 v1，旧版继续服务。
    assert document_row(update_delete_schema, document_id)["active_version_id"] == v1_id
    still_v1 = await search_with_real_analyzer_identity(
        role_test_databases.api_url,
        update_delete_schema,
        user_id=user_id,
        kb_id=kb_id,
        query="beta",
        terms="beta",
    )
    assert still_v1 and {candidate.version_id for candidate in still_v1} == {v1_id}

    # 发布 v2：active 原子切到 v2，v1 仍 READY 但不可入检索。
    assert publish(update_delete_schema, worker_sessions, storage, job2) == iw.PROCESS_STATUS_READY
    document = document_row(update_delete_schema, document_id)
    assert document["active_version_id"] == v2_id
    assert document["lifecycle_status"] == "READY"
    assert version_row(update_delete_schema, v2_id)["status"] == "READY"
    assert version_row(update_delete_schema, v1_id)["status"] == "READY"

    only_v2 = await search_with_real_analyzer_identity(
        role_test_databases.api_url,
        update_delete_schema,
        user_id=user_id,
        kb_id=kb_id,
        query="epsilon",
        terms="epsilon",
    )
    assert only_v2 and {candidate.version_id for candidate in only_v2} == {v2_id}
    # 旧版本的词项只可能经向量路命中，但候选版本必为 v2，绝不能是 v1。
    old_terms = await search_with_real_analyzer_identity(
        role_test_databases.api_url,
        update_delete_schema,
        user_id=user_id,
        kb_id=kb_id,
        query="beta",
        terms="beta",
    )
    assert old_terms and {candidate.version_id for candidate in old_terms} == {v2_id}
    assert all(candidate.keyword_rank is None for candidate in old_terms)
    # profile 契约不变，每次发布递增一次 kb_revision。
    assert kb_profile_id(update_delete_schema, kb_id) is not None
    assert _kb_revision(update_delete_schema, kb_id) == revision_after_v1 + 1


@pytest.mark.anyio
async def test_update_publish_failure_keeps_old_version_ready(
    update_delete_schema: Engine,
    update_delete_settings: Settings,
    worker_sessions: Any,
    storage: DocumentBlobStore,
    role_test_databases: RoleTestDatabases,
) -> None:
    kb_id = seed_kb(update_delete_schema, name=unique_name())
    username, user_id = seed_editor(update_delete_schema, kb_id)

    async with api_client(update_delete_settings) as client:
        csrf = await login_csrf(client, username)
        first = await first_upload(client, kb_id, csrf=csrf)
        document_id = uuid.UUID(first.json()["documentId"])
        v1_id = uuid.UUID(first.json()["versionId"])
        job1 = uuid.UUID(first.json()["jobId"])

    assert publish(update_delete_schema, worker_sessions, storage, job1) == iw.PROCESS_STATUS_READY

    async with api_client(update_delete_settings) as client:
        csrf = await login_csrf(client, username)
        second = await upload_version(
            client, document_id, idempotency_key=unique_key(), expected_version_id=v1_id, csrf=csrf
        )
        v2_id = uuid.UUID(second.json()["versionId"])
        job2 = uuid.UUID(second.json()["jobId"])

    assert publish(update_delete_schema, worker_sessions, storage, job2, mode="permanent") == (
        iw.PROCESS_STATUS_FAILED
    )
    assert job_row(update_delete_schema, job2)["status"] == "FAILED"
    assert version_row(update_delete_schema, v2_id)["status"] == "FAILED"
    document = document_row(update_delete_schema, document_id)
    assert document["active_version_id"] == v1_id
    assert document["lifecycle_status"] == "READY"

    found = await search_with_real_analyzer_identity(
        role_test_databases.api_url,
        update_delete_schema,
        user_id=user_id,
        kb_id=kb_id,
        query="beta",
        terms="beta",
    )
    assert found and {candidate.version_id for candidate in found} == {v1_id}


# --- 更新：授权、过期与并发 ---------------------------------------------------


@pytest.mark.anyio
async def test_update_api_authorization_conflicts_and_concurrency(
    update_delete_schema: Engine,
    update_delete_settings: Settings,
    worker_sessions: Any,
    storage: DocumentBlobStore,
) -> None:
    kb_id = seed_kb(update_delete_schema, name=unique_name())
    editor_name, _ = seed_editor(update_delete_schema, kb_id)
    reader_name = unique_username()
    reader_id = seed_user(update_delete_schema, username=reader_name)
    seed_member(update_delete_schema, kb_id=kb_id, user_id=reader_id, role="READER")
    outsider_name = unique_username()
    seed_user(update_delete_schema, username=outsider_name)

    async with api_client(update_delete_settings) as client:
        csrf = await login_csrf(client, editor_name)
        first = await first_upload(client, kb_id, csrf=csrf)
        document_id = uuid.UUID(first.json()["documentId"])
        v1_id = uuid.UUID(first.json()["versionId"])
        job1 = uuid.UUID(first.json()["jobId"])

    assert publish(update_delete_schema, worker_sessions, storage, job1) == iw.PROCESS_STATUS_READY

    async with api_client(update_delete_settings) as anonymous:
        response = await upload_version(
            anonymous,
            document_id,
            idempotency_key=unique_key(),
            expected_version_id=v1_id,
        )
        assert response.status_code == 401, response.text

    async with api_client(update_delete_settings) as reader:
        csrf = await login_csrf(reader, reader_name)
        response = await upload_version(
            reader, document_id, idempotency_key=unique_key(), expected_version_id=v1_id, csrf=csrf
        )
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "DOCUMENT_NOT_FOUND"

    async with api_client(update_delete_settings) as outsider:
        csrf = await login_csrf(outsider, outsider_name)
        response = await upload_version(
            outsider,
            document_id,
            idempotency_key=unique_key(),
            expected_version_id=v1_id,
            csrf=csrf,
        )
        assert response.status_code == 404, response.text

    async with api_client(update_delete_settings) as editor:
        csrf = await login_csrf(editor, editor_name)
        no_csrf = await upload_version(
            editor, document_id, idempotency_key=unique_key(), expected_version_id=v1_id
        )
        assert no_csrf.status_code == 403, no_csrf.text
        stale = await upload_version(
            editor,
            document_id,
            idempotency_key=unique_key(),
            expected_version_id=uuid.uuid4(),
            csrf=csrf,
        )
        assert stale.status_code == 409, stale.text
        assert stale.json()["code"] == "DOCUMENT_VERSION_CONFLICT"

        # 同键不同 expected → 409；不同内容同键 → 409。
        key = unique_key()
        ok = await upload_version(
            editor, document_id, idempotency_key=key, expected_version_id=v1_id, csrf=csrf
        )
        assert ok.status_code == 202, ok.text
        version_id = ok.json()["versionId"]

        different_expected = await upload_version(
            editor,
            document_id,
            idempotency_key=key,
            expected_version_id=uuid.uuid4(),
            content=V2_BODY,
            csrf=csrf,
        )
        different_title = await upload_version(
            editor, document_id, idempotency_key=key, expected_version_id=v1_id,
            title="另一个标题", csrf=csrf,
        )
        for response in (different_expected, different_title):
            assert response.status_code == 409, response.text
            assert response.json()["code"] == "IDEMPOTENCY_KEY_REUSED"

        # 并发同键同 expected 只创建一个版本，两边返回同一 id。
        concurrent_key = unique_key()

        async def concurrent_upload() -> Any:
            return await upload_version(
                editor,
                document_id,
                idempotency_key=concurrent_key,
                expected_version_id=v1_id,
                csrf=csrf,
            )

        first_race, second_race = await asyncio.gather(
            concurrent_upload(), concurrent_upload()
        )
        assert first_race.status_code == 202, first_race.text
        assert second_race.status_code == 202, second_race.text
        assert first_race.json() == second_race.json()
        assert first_race.json()["versionId"] != version_id

    with update_delete_schema.connect() as connection:
        version_count = connection.scalar(
            text("SELECT count(*) FROM document_version WHERE document_id = :id"),
            {"id": document_id},
        )
    # 首版 + 一个有效新版本 + 一个并发新版本 = 3。
    assert version_count == 3


# --- 删除 ---------------------------------------------------------------------


@pytest.mark.anyio
async def test_delete_tombstones_cancels_queued_job_and_yields_zero_candidates(
    update_delete_schema: Engine,
    update_delete_settings: Settings,
    worker_sessions: Any,
    storage: DocumentBlobStore,
    role_test_databases: RoleTestDatabases,
) -> None:
    kb_id = seed_kb(update_delete_schema, name=unique_name())
    owner_name = unique_username()
    owner_id = seed_user(update_delete_schema, username=owner_name)
    seed_member(update_delete_schema, kb_id=kb_id, user_id=owner_id, role="OWNER")
    editor_name, editor_id = seed_editor(update_delete_schema, kb_id)

    async with api_client(update_delete_settings) as client:
        csrf = await login_csrf(client, editor_name)
        first = await first_upload(client, kb_id, csrf=csrf)
        document_id = uuid.UUID(first.json()["documentId"])
        v1_id = uuid.UUID(first.json()["versionId"])
        job1 = uuid.UUID(first.json()["jobId"])

    assert publish(update_delete_schema, worker_sessions, storage, job1) == iw.PROCESS_STATUS_READY

    async with api_client(update_delete_settings) as client:
        csrf = await login_csrf(client, editor_name)
        queued = await upload_version(
            client, document_id, idempotency_key=unique_key(), expected_version_id=v1_id, csrf=csrf
        )
        assert queued.status_code == 202, queued.text
        v2_id = uuid.UUID(queued.json()["versionId"])
        job2 = uuid.UUID(queued.json()["jobId"])
        assert job_row(update_delete_schema, job2)["status"] == "QUEUED"

        # EDITOR（非 OWNER）不能删除。
        forbidden = await delete_document_request(client, document_id, csrf=csrf)
        assert forbidden.status_code == 404, forbidden.text
        assert forbidden.json()["code"] == "DOCUMENT_NOT_FOUND"

        csrf = await login_csrf(client, owner_name)
        deleted = await delete_document_request(client, document_id, csrf=csrf)
        assert deleted.status_code == 204, deleted.text
        again = await delete_document_request(client, document_id, csrf=csrf)
        assert again.status_code == 204, again.text

    document = document_row(update_delete_schema, document_id)
    assert document["deleted_at"] is not None
    assert document["lifecycle_status"] == "DELETED"
    # active 指针保留，但文档已被 tombstone 挡住。
    assert document["active_version_id"] == v1_id
    # 排队中的 v2 job 被取消并清租约。
    cancelled = job_row(update_delete_schema, job2)
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["error_code"] == "DOCUMENT_DELETED"
    assert version_row(update_delete_schema, v2_id)["status"] == "PENDING"
    assert kb_active_profile(update_delete_schema, kb_id) is not None

    # 双路零候选（vector + keyword）。
    for query, terms in (("beta", "beta"), ("epsilon", "epsilon")):
        candidates = await search_with_real_analyzer_identity(
            role_test_databases.api_url,
            update_delete_schema,
            user_id=editor_id,
            kb_id=kb_id,
            query=query,
            terms=terms,
        )
        assert candidates == []

    # dispatcher 补偿不会把已取消 job 当作候选再次补投。
    async with api_session(role_test_databases.api_url) as session:
        repository = SqlOutboxRepository(session)
        compensation = await repository.lock_compensation_candidates(
            limit=50, grace_seconds=protocol.RECEIVE_GRACE_SECONDS
        )
        await repository.commit()
    assert all(candidate.job_id != job2 for candidate in compensation)


@pytest.mark.anyio
async def test_delete_blocks_replay_and_in_flight_publish(
    update_delete_schema: Engine,
    update_delete_settings: Settings,
    worker_sessions: Any,
    storage: DocumentBlobStore,
) -> None:
    kb_id = seed_kb(update_delete_schema, name=unique_name())
    owner_name = unique_username()
    owner_id = seed_user(update_delete_schema, username=owner_name)
    seed_member(update_delete_schema, kb_id=kb_id, user_id=owner_id, role="OWNER")
    editor_name, _ = seed_editor(update_delete_schema, kb_id)

    upload_key = unique_key()
    version_key = unique_key()
    async with api_client(update_delete_settings) as client:
        csrf = await login_csrf(client, editor_name)
        first = await upload(
            client,
            kb_id,
            idempotency_key=upload_key,
            title=V1_TITLE,
            content=V1_BODY,
            csrf=csrf,
        )
        document_id = uuid.UUID(first.json()["documentId"])
        v1_id = uuid.UUID(first.json()["versionId"])
        job1 = uuid.UUID(first.json()["jobId"])

    assert publish(update_delete_schema, worker_sessions, storage, job1) == iw.PROCESS_STATUS_READY

    # 造一个正在处理中（持租约、已暂存 BUILDING generation）的 v2 job。
    generation_id = uuid.uuid4()
    lease_token = uuid.uuid4().hex
    with update_delete_schema.begin() as connection:
        job2 = uuid.uuid4()
        version2 = uuid.uuid4()
        profile_id = job_profile_id(update_delete_schema, job1)
        dedupe = f"ver1:{document_id}:{'0' * 64}:{v1_id}"
        connection.execute(
            text(
                "INSERT INTO document_version (id, document_id, version_no, file_ref, file_hash, "
                "mime, parser_version, status) VALUES (:id, :document_id, 2, "
                "(SELECT file_ref FROM document_version WHERE id = :v1), :hash, 'text/markdown', "
                ":parser, 'PENDING')"
            ),
            {
                "id": version2,
                "document_id": document_id,
                "v1": v1_id,
                "hash": "f" * 64,
                "parser": MARKDOWN_PARSER_VERSION,
            },
        )
        connection.execute(
            text(
                "INSERT INTO ingest_job (id, document_id, version_id, profile_id, status, attempt, "
                "next_run_at, dedupe_key, lease_owner, lease_token, lease_until, heartbeat_at) "
                "VALUES (:id, :document_id, :version_id, :profile_id, 'PARSING', 1, now(), "
                ":dedupe, 'pipeline:inflight', :token, now() + interval '1 hour', now())"
            ),
            {
                "id": job2,
                "document_id": document_id,
                "version_id": version2,
                "profile_id": profile_id,
                "dedupe": dedupe,
                "token": lease_token,
            },
        )
        connection.execute(
            text(
                "INSERT INTO index_generation (id, version_id, profile_id, status, "
                "expected_chunks, actual_chunks) "
                "VALUES (:id, :version_id, :profile_id, 'BUILDING', 1, 0)"
            ),
            {"id": generation_id, "version_id": version2, "profile_id": profile_id},
        )
        connection.execute(
            text("UPDATE ingest_job SET generation_id = :g WHERE id = :id"),
            {"g": generation_id, "id": job2},
        )

    async with api_client(update_delete_settings) as client:
        csrf = await login_csrf(client, owner_name)
        deleted = await delete_document_request(client, document_id, csrf=csrf)
        assert deleted.status_code == 204, deleted.text

    # 删除后：在途 job 被取消并清租约，发布事务无法复活文档。
    inflight = job_row(update_delete_schema, job2)
    assert inflight["status"] == "CANCELLED"
    outcome = iw.publish_ingest_generation(
        worker_sessions,
        job_id=job2,
        lease_token=lease_token,
        generation_id=generation_id,
        expected_chunks=1,
    )
    assert outcome is iw.PublishOutcome.LEASE_LOST
    assert document_row(update_delete_schema, document_id)["deleted_at"] is not None
    assert document_row(update_delete_schema, document_id)["active_version_id"] == v1_id

    # 已删除文档：旧首次上传幂等回放与新版本回放都不得当有效资源。
    async with api_client(update_delete_settings) as client:
        csrf = await login_csrf(client, editor_name)
        replay_upload = await upload(
            client, kb_id, idempotency_key=upload_key, title=V1_TITLE, content=V1_BODY, csrf=csrf
        )
        assert replay_upload.status_code == 409, replay_upload.text
        assert replay_upload.json()["code"] == "DOCUMENT_DELETED"
        replay_version = await upload_version(
            client,
            document_id,
            idempotency_key=version_key,
            expected_version_id=v1_id,
            csrf=csrf,
        )
        # 该 key 从未成功登记，因此这是对已删除文档的新写入，同样被拒绝。
        assert replay_version.status_code == 409, replay_version.text
        assert replay_version.json()["code"] == "DOCUMENT_DELETED"


def _kb_revision(engine: Engine, kb_id: uuid.UUID) -> int:
    with engine.connect() as connection:
        return int(
            connection.scalar(
                text("SELECT kb_revision FROM knowledge_base WHERE id = :id"),
                {"id": kb_id},
            )
        )


# --- review 修复：幂等身份、早拒、发布 CAS 与删除死锁 -------------------------------


VERSION_VECTOR_512 = "[" + ",".join("0.0" for _ in range(512)) + "]"


def request_title_of(engine: Engine, job_id: uuid.UUID) -> str | None:
    with engine.connect() as connection:
        return cast(
            "str | None",
            connection.scalar(
                text("SELECT request_title FROM ingest_job WHERE id = :id"),
                {"id": job_id},
            ),
        )


def generation_status(engine: Engine, generation_id: uuid.UUID) -> str:
    with engine.connect() as connection:
        return str(
            connection.scalar(
                text("SELECT status FROM index_generation WHERE id = :id"),
                {"id": generation_id},
            )
        )


def seed_staged_update(
    engine: Engine,
    *,
    kb_id: uuid.UUID,
    document_id: uuid.UUID,
    expected_active_version_id: uuid.UUID,
    profile_id: uuid.UUID,
    idempotency_key: str,
    version_no: int,
    lease_token: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """直接造一个已暂存 BUILDING generation、仍持租约的文档新版本 job。

    用于确定性验证发布事务的 CAS（不经过解析/编码），以及删除保留租约时发布不得复活。
    """

    version_id = uuid.uuid4()
    generation_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    job_id = uuid.uuid4()
    file_hash = uuid.uuid4().hex + uuid.uuid4().hex
    key_prefix = build_version_dedupe_key_prefix(
        ORGANIZATION_ID, kb_id, document_id, idempotency_key
    )
    dedupe = build_version_dedupe_key(key_prefix, expected_active_version_id)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO document_version (id, document_id, version_no, file_ref, "
                "file_hash, mime, parser_version, status) VALUES (:id, :document_id, "
                ":version_no, :file_ref, :file_hash, 'text/markdown', :parser, 'PENDING')"
            ),
            {
                "id": version_id,
                "document_id": document_id,
                "version_no": version_no,
                "file_ref": f"{kb_id}/{file_hash}",
                "file_hash": file_hash,
                "parser": MARKDOWN_PARSER_VERSION,
            },
        )
        connection.execute(
            text(
                "INSERT INTO index_generation (id, version_id, profile_id, status, "
                "expected_chunks, actual_chunks) "
                "VALUES (:id, :version_id, :profile_id, 'BUILDING', 1, 0)"
            ),
            {"id": generation_id, "version_id": version_id, "profile_id": profile_id},
        )
        connection.execute(
            text(
                "INSERT INTO chunk (id, generation_id, organization_id, kb_id, document_id, "
                "version_id, chunk_index, text, text_hash, model_input_hash, parser_version, "
                "chunker_version, token_count, heading_path, source_locator, fts) "
                "SELECT :id, :generation_id, kb.organization_id, :kb_id, :document_id, "
                ":version_id, 0, '正文', 'h', 'h', :parser, 'heading-pack-v1', 1, "
                "'[]'::jsonb, '{}'::jsonb, to_tsvector('simple', 'body') "
                "FROM knowledge_base AS kb WHERE kb.id = :kb_id"
            ),
            {
                "id": chunk_id,
                "generation_id": generation_id,
                "kb_id": kb_id,
                "document_id": document_id,
                "version_id": version_id,
                "parser": MARKDOWN_PARSER_VERSION,
            },
        )
        connection.execute(
            text(
                "INSERT INTO chunk_embedding (chunk_id, profile_id, embedding) "
                "VALUES (:chunk_id, :profile_id, CAST(:embedding AS vector))"
            ),
            {
                "chunk_id": chunk_id,
                "profile_id": profile_id,
                "embedding": VERSION_VECTOR_512,
            },
        )
        connection.execute(
            text(
                "INSERT INTO ingest_job (id, document_id, version_id, profile_id, status, "
                "attempt, next_run_at, dedupe_key, lease_owner, lease_token, lease_until, "
                "heartbeat_at, generation_id) "
                "VALUES (:id, :document_id, :version_id, :profile_id, 'INDEXING', 1, now(), "
                ":dedupe, 'pipeline:test', :lease_token, now() + interval '1 hour', now(), "
                ":generation_id)"
            ),
            {
                "id": job_id,
                "document_id": document_id,
                "version_id": version_id,
                "profile_id": profile_id,
                "dedupe": dedupe,
                "lease_token": lease_token,
                "generation_id": generation_id,
            },
        )
    return job_id, version_id, generation_id


@pytest.mark.anyio
async def test_replay_uses_immutable_request_title_after_title_change(
    update_delete_schema: Engine,
    update_delete_settings: Settings,
    worker_sessions: Any,
    storage: DocumentBlobStore,
) -> None:
    """新版本改标题后，原上传 key 仍能以原始标题原样重放；旧数据边界单独说明。"""

    kb_id = seed_kb(update_delete_schema, name=unique_name())
    username, _ = seed_editor(update_delete_schema, kb_id)
    upload_key = unique_key()
    version_key = unique_key()

    async with api_client(update_delete_settings) as client:
        csrf = await login_csrf(client, username)
        first = await upload(
            client,
            kb_id,
            idempotency_key=upload_key,
            title="原始标题",
            content=V1_BODY,
            csrf=csrf,
        )
        assert first.status_code == 202, first.text
        document_id = uuid.UUID(first.json()["documentId"])
        v1_id = uuid.UUID(first.json()["versionId"])
        job1 = uuid.UUID(first.json()["jobId"])

    assert publish(update_delete_schema, worker_sessions, storage, job1) == iw.PROCESS_STATUS_READY

    async with api_client(update_delete_settings) as client:
        csrf = await login_csrf(client, username)
        second = await upload_version(
            client,
            document_id,
            idempotency_key=version_key,
            expected_version_id=v1_id,
            title="新标题",
            csrf=csrf,
        )
        assert second.status_code == 202, second.text
        job2 = uuid.UUID(second.json()["jobId"])
        # 展示标题已切换。
        assert document_row(update_delete_schema, document_id)["title"] == "新标题"

        # 原上传 key + 原标题重放：必须复用原资源，而不是因可变标题变成 409。
        replay_first = await upload(
            client,
            kb_id,
            idempotency_key=upload_key,
            title="原始标题",
            content=V1_BODY,
            csrf=csrf,
        )
        assert replay_first.status_code == 202, replay_first.text
        assert replay_first.json() == first.json()

        # 原上传 key + 不同标题仍必须是 409，身份契约没有退化。
        conflicting = await upload(
            client,
            kb_id,
            idempotency_key=upload_key,
            title="新标题",
            content=V1_BODY,
            csrf=csrf,
        )
        assert conflicting.status_code == 409, conflicting.text
        assert conflicting.json()["code"] == CODE_IDEMPOTENCY_KEY_REUSED

        # 版本 key + 提交时标题重放：复用同一新版本。
        replay_version = await upload_version(
            client,
            document_id,
            idempotency_key=version_key,
            expected_version_id=v1_id,
            title="新标题",
            csrf=csrf,
        )
        assert replay_version.status_code == 202, replay_version.text
        assert replay_version.json() == second.json()

        # 版本 key + 不同标题仍必须是 409。
        version_conflict = await upload_version(
            client,
            document_id,
            idempotency_key=version_key,
            expected_version_id=v1_id,
            title="另一个标题",
            csrf=csrf,
        )
        assert version_conflict.status_code == 409, version_conflict.text
        assert version_conflict.json()["code"] == CODE_IDEMPOTENCY_KEY_REUSED

    # request_title 分别固化受理时的原标题与新标题。
    assert request_title_of(update_delete_schema, job1) == "原始标题"
    assert request_title_of(update_delete_schema, job2) == "新标题"


@pytest.mark.anyio
async def test_different_keys_same_expected_loser_is_statically_rejected(
    update_delete_schema: Engine,
    update_delete_settings: Settings,
    worker_sessions: Any,
    storage: DocumentBlobStore,
) -> None:
    """不同 key、同一 expected 的两个更新：先发布者生效，后一个在领取期静态冲突拒绝。"""

    kb_id = seed_kb(update_delete_schema, name=unique_name())
    username, _ = seed_editor(update_delete_schema, kb_id)

    async with api_client(update_delete_settings) as client:
        csrf = await login_csrf(client, username)
        first = await first_upload(client, kb_id, csrf=csrf)
        document_id = uuid.UUID(first.json()["documentId"])
        v1_id = uuid.UUID(first.json()["versionId"])
        job1 = uuid.UUID(first.json()["jobId"])

    assert publish(update_delete_schema, worker_sessions, storage, job1) == iw.PROCESS_STATUS_READY

    async with api_client(update_delete_settings) as client:
        csrf = await login_csrf(client, username)
        winner = await upload_version(
            client,
            document_id,
            idempotency_key=unique_key(),
            expected_version_id=v1_id,
            content=b"# delta\n\nwinner body\n",
            csrf=csrf,
        )
        loser = await upload_version(
            client,
            document_id,
            idempotency_key=unique_key(),
            expected_version_id=v1_id,
            content=b"# delta\n\nloser body\n",
            csrf=csrf,
        )
        assert winner.status_code == 202, winner.text
        assert loser.status_code == 202, loser.text
        winner_version = uuid.UUID(winner.json()["versionId"])
        winner_job = uuid.UUID(winner.json()["jobId"])
        loser_version = uuid.UUID(loser.json()["versionId"])
        loser_job = uuid.UUID(loser.json()["jobId"])

    assert publish(update_delete_schema, worker_sessions, storage, winner_job) == (
        iw.PROCESS_STATUS_READY
    )
    loser_status = publish(update_delete_schema, worker_sessions, storage, loser_job)

    assert loser_status == iw.PROCESS_STATUS_STALE_EXPECTED
    assert job_row(update_delete_schema, loser_job)["error_code"] == (
        iw.ERROR_PIPELINE_STALE_EXPECTED
    )
    assert job_row(update_delete_schema, loser_job)["status"] == "FAILED"
    assert version_row(update_delete_schema, loser_version)["status"] == "PENDING"
    assert version_row(update_delete_schema, winner_version)["status"] == "READY"
    assert document_row(update_delete_schema, document_id)["active_version_id"] == (
        winner_version
    )


@pytest.mark.anyio
async def test_update_publish_cas_admits_only_one_active_version(
    update_delete_schema: Engine,
    update_delete_settings: Settings,
    worker_sessions: Any,
    storage: DocumentBlobStore,
) -> None:
    """两个已领取的更新都满足领取条件：发布事务 CAS 只让一个切换指针，另一个冲突回滚。"""

    kb_id = seed_kb(update_delete_schema, name=unique_name())
    username, _ = seed_editor(update_delete_schema, kb_id)

    async with api_client(update_delete_settings) as client:
        csrf = await login_csrf(client, username)
        first = await first_upload(client, kb_id, csrf=csrf)
        document_id = uuid.UUID(first.json()["documentId"])
        v1_id = uuid.UUID(first.json()["versionId"])
        job1 = uuid.UUID(first.json()["jobId"])

    assert publish(update_delete_schema, worker_sessions, storage, job1) == iw.PROCESS_STATUS_READY
    profile_id = job_profile_id(update_delete_schema, job1)
    revision_after_v1 = _kb_revision(update_delete_schema, kb_id)

    job_a, version_a, generation_a = seed_staged_update(
        update_delete_schema,
        kb_id=kb_id,
        document_id=document_id,
        expected_active_version_id=v1_id,
        profile_id=profile_id,
        idempotency_key=unique_key(),
        version_no=2,
        lease_token="lease-a",
    )
    job_b, version_b, generation_b = seed_staged_update(
        update_delete_schema,
        kb_id=kb_id,
        document_id=document_id,
        expected_active_version_id=v1_id,
        profile_id=profile_id,
        idempotency_key=unique_key(),
        version_no=3,
        lease_token="lease-b",
    )

    first_publish = iw.publish_ingest_generation(
        worker_sessions,
        job_id=job_a,
        lease_token="lease-a",
        generation_id=generation_a,
        expected_chunks=1,
    )
    second_publish = iw.publish_ingest_generation(
        worker_sessions,
        job_id=job_b,
        lease_token="lease-b",
        generation_id=generation_b,
        expected_chunks=1,
    )

    assert first_publish is iw.PublishOutcome.PUBLISHED
    assert second_publish is iw.PublishOutcome.CONFLICT
    assert generation_status(update_delete_schema, generation_a) == "READY"
    assert generation_status(update_delete_schema, generation_b) == "BUILDING"
    assert version_row(update_delete_schema, version_a)["status"] == "READY"
    assert version_row(update_delete_schema, version_b)["status"] == "PENDING"
    document = document_row(update_delete_schema, document_id)
    assert document["active_version_id"] == version_a
    assert _kb_revision(update_delete_schema, kb_id) == revision_after_v1 + 1


@pytest.mark.anyio
async def test_tombstone_with_retained_lease_rejects_publish(
    update_delete_schema: Engine,
    update_delete_settings: Settings,
    worker_sessions: Any,
    storage: DocumentBlobStore,
) -> None:
    """删除只是 tombstone；即使 job 仍持有效租约，发布事务也必须按 deleted_at 拒绝且不复活。"""

    kb_id = seed_kb(update_delete_schema, name=unique_name())
    username, _ = seed_editor(update_delete_schema, kb_id)

    async with api_client(update_delete_settings) as client:
        csrf = await login_csrf(client, username)
        first = await first_upload(client, kb_id, csrf=csrf)
        document_id = uuid.UUID(first.json()["documentId"])
        v1_id = uuid.UUID(first.json()["versionId"])
        job1 = uuid.UUID(first.json()["jobId"])

    assert publish(update_delete_schema, worker_sessions, storage, job1) == iw.PROCESS_STATUS_READY
    profile_id = job_profile_id(update_delete_schema, job1)
    job2, version2, generation2 = seed_staged_update(
        update_delete_schema,
        kb_id=kb_id,
        document_id=document_id,
        expected_active_version_id=v1_id,
        profile_id=profile_id,
        idempotency_key=unique_key(),
        version_no=2,
        lease_token="retained",
    )

    # 直接 tombstone，保留 job 的未过期租约（不同于会清租约的 API 删除路径）。
    with update_delete_schema.begin() as connection:
        connection.execute(
            text(
                "UPDATE document SET deleted_at = now(), lifecycle_status = 'DELETED' "
                "WHERE id = :id"
            ),
            {"id": document_id},
        )

    outcome = iw.publish_ingest_generation(
        worker_sessions,
        job_id=job2,
        lease_token="retained",
        generation_id=generation2,
        expected_chunks=1,
    )

    assert outcome is iw.PublishOutcome.OUT_OF_SCOPE
    assert generation_status(update_delete_schema, generation2) == "BUILDING"
    assert version_row(update_delete_schema, version2)["status"] == "PENDING"
    assert job_row(update_delete_schema, job2)["status"] == "INDEXING"
    document = document_row(update_delete_schema, document_id)
    assert document["deleted_at"] is not None
    assert document["active_version_id"] == v1_id


# --- 删除与 worker 锁序相反导致的死锁：真实 PG 确定性交错 --------------------------


def seed_document_with_queued_job(
    engine: Engine, *, kb_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    document_id = uuid.uuid4()
    version_id = uuid.uuid4()
    job_id = uuid.uuid4()
    file_hash = uuid.uuid4().hex + uuid.uuid4().hex
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO document (id, kb_id, title, source_type, lifecycle_status) "
                "VALUES (:id, :kb_id, 'doc', 'markdown', 'CREATED')"
            ),
            {"id": document_id, "kb_id": kb_id},
        )
        connection.execute(
            text(
                "INSERT INTO document_version (id, document_id, version_no, file_ref, file_hash, "
                "mime, parser_version, status) VALUES (:id, :document_id, 1, :file_ref, "
                ":file_hash, 'text/markdown', :parser, 'PENDING')"
            ),
            {
                "id": version_id,
                "document_id": document_id,
                "file_ref": f"{kb_id}/{file_hash}",
                "file_hash": file_hash,
                "parser": MARKDOWN_PARSER_VERSION,
            },
        )
        connection.execute(
            text(
                "INSERT INTO ingest_job (id, document_id, version_id, status, attempt, "
                "next_run_at, dedupe_key) VALUES (:id, :document_id, :version_id, 'QUEUED', 0, "
                "now(), :dedupe)"
            ),
            {
                "id": job_id,
                "document_id": document_id,
                "version_id": version_id,
                "dedupe": uuid.uuid4().hex,
            },
        )
    return document_id, job_id


def owner_auth_context(user_id: uuid.UUID, username: str) -> AuthContext:
    return AuthContext(
        user_id=user_id,
        organization_id=ORGANIZATION_ID,
        username=username,
        is_admin=False,
        session_id=uuid.uuid4(),
        csrf_token="test-csrf",
    )


async def _wait_until_lock_wait(
    observer: AsyncSession, pid: int, *, timeout: float = 15.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = (
            await observer.execute(
                text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": pid},
            )
        ).first()
        if row is not None and row[0] == "Lock":
            return
        await asyncio.sleep(0.02)
    raise AssertionError("删除事务未按预期阻塞在 job 行锁上")


async def _orchestrate_delete_deadlock(
    *,
    deleter_url: str,
    worker_url: str,
    observer_url: str,
    document_id: uuid.UUID,
    job_id: uuid.UUID,
    delete_call: Callable[[AsyncSession], Coroutine[Any, Any, None]],
    before_cycle_closes: Callable[[], None] | None = None,
) -> None:
    """确定性制造 ``delete(document→kb→job)`` 与 ``worker(job→document)`` 的死锁。

    blocker 用 worker 角色先持 job 行锁；删除事务达到 job 行锁等待后，blocker 再请求 document
    锁补齐环。删除事务用高权限连接压低 ``deadlock_timeout``，因此它稳定地成为被 PG 中止的
    一方；blocker 的 ``SELECT document FOR UPDATE`` 仅在删除事务中止释放锁后才返回。
    """

    async with (
        api_session(worker_url) as blocker,
        api_session(deleter_url) as deleter,
        api_session(observer_url) as observer,
    ):
        await blocker.execute(
            text("SELECT id FROM ingest_job WHERE id = :id FOR UPDATE"), {"id": job_id}
        )
        # 同一事务内压低死锁检测阈值，确保删除侧是被中止的一方。
        await deleter.execute(text("SET deadlock_timeout = '500ms'"))
        pid = int((await deleter.execute(text("SELECT pg_backend_pid()"))).scalar_one())
        delete_task = asyncio.create_task(delete_call(deleter))
        try:
            await _wait_until_lock_wait(observer, pid)
            if before_cycle_closes is not None:
                before_cycle_closes()
            await blocker.execute(
                text("SELECT id FROM document WHERE id = :id FOR UPDATE"),
                {"id": document_id},
            )
        except BaseException:
            delete_task.cancel()
            raise
        finally:
            await blocker.rollback()
        await delete_task


@pytest.mark.anyio
async def test_delete_deadlock_direct_service_raises_and_retry_recovers(
    update_delete_schema: Engine,
    role_test_databases: RoleTestDatabases,
) -> None:
    """先复现死锁（直接 service 删除报 40P01），再证明完整事务重试收敛且无重复副作用。"""

    kb_id = seed_kb(update_delete_schema, name=unique_name())
    owner_name = unique_username()
    owner_id = seed_user(update_delete_schema, username=owner_name)
    seed_member(update_delete_schema, kb_id=kb_id, user_id=owner_id, role="OWNER")
    document_id, job_id = seed_document_with_queued_job(update_delete_schema, kb_id=kb_id)
    context = owner_auth_context(owner_id, owner_name)

    async def direct_delete(session: AsyncSession) -> None:
        await ingestion_service.delete_document(
            session, kb_id=kb_id, document_id=document_id
        )

    with pytest.raises(OperationalError) as failure:
        await _orchestrate_delete_deadlock(
            deleter_url=role_test_databases.migrator_url,
            worker_url=role_test_databases.worker_url,
            observer_url=role_test_databases.migrator_url,
            document_id=document_id,
            job_id=job_id,
            delete_call=direct_delete,
        )
    assert getattr(failure.value.orig, "sqlstate", None) == "40P01"
    # 死锁整体回滚：没有部分副作用。
    assert document_row(update_delete_schema, document_id)["deleted_at"] is None
    assert _kb_revision(update_delete_schema, kb_id) == 0
    assert job_row(update_delete_schema, job_id)["status"] == "QUEUED"

    async def retry_delete(session: AsyncSession) -> None:
        await _delete_document_with_retry(
            session, context=context, document_id=document_id
        )

    await _orchestrate_delete_deadlock(
        deleter_url=role_test_databases.migrator_url,
        worker_url=role_test_databases.worker_url,
        observer_url=role_test_databases.migrator_url,
        document_id=document_id,
        job_id=job_id,
        delete_call=retry_delete,
    )

    document = document_row(update_delete_schema, document_id)
    assert document["deleted_at"] is not None
    assert document["lifecycle_status"] == "DELETED"
    # 重试成功只提交一次：revision 恰好递增一次，job 恰好取消一次。
    assert _kb_revision(update_delete_schema, kb_id) == 1
    cancelled = job_row(update_delete_schema, job_id)
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["error_code"] == "DOCUMENT_DELETED"


@pytest.mark.anyio
async def test_delete_deadlock_retry_reauthorizes_after_revocation(
    update_delete_schema: Engine,
    role_test_databases: RoleTestDatabases,
) -> None:
    """死锁后的重试必须重新授权：等待期间撤权则重试返回 404 且不删除文档。"""

    kb_id = seed_kb(update_delete_schema, name=unique_name())
    owner_name = unique_username()
    owner_id = seed_user(update_delete_schema, username=owner_name)
    seed_member(update_delete_schema, kb_id=kb_id, user_id=owner_id, role="OWNER")
    document_id, job_id = seed_document_with_queued_job(update_delete_schema, kb_id=kb_id)
    context = owner_auth_context(owner_id, owner_name)

    def revoke_owner() -> None:
        with update_delete_schema.begin() as connection:
            connection.execute(
                text(
                    "UPDATE kb_member SET revoked_at = now() "
                    "WHERE kb_id = :kb_id AND user_id = :user_id"
                ),
                {"kb_id": kb_id, "user_id": owner_id},
            )

    async def retry_delete(session: AsyncSession) -> None:
        await _delete_document_with_retry(
            session, context=context, document_id=document_id
        )

    with pytest.raises(ApiError) as failure:
        await _orchestrate_delete_deadlock(
            deleter_url=role_test_databases.migrator_url,
            worker_url=role_test_databases.worker_url,
            observer_url=role_test_databases.migrator_url,
            document_id=document_id,
            job_id=job_id,
            delete_call=retry_delete,
            before_cycle_closes=revoke_owner,
        )

    assert failure.value.status_code == 404
    assert document_row(update_delete_schema, document_id)["deleted_at"] is None
    assert _kb_revision(update_delete_schema, kb_id) == 0
    assert job_row(update_delete_schema, job_id)["status"] == "QUEUED"
