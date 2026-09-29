"""Markdown 上传事务片的真实 PostgreSQL + Redis 验收。

覆盖：EDITOR+KB 成员授权先于正文读取、CSRF/Origin、单事务写入四张表、202 响应、
Idempotency-Key 复用/冲突/跨 KB 隔离、不同 key 同内容复用 KB 私有 blob、并发唯一冲突
回滚重读、体积与内容校验且不落库不落文件。缺少守卫 DSN 或 Redis 时按既有契约跳过。

绝不触碰开发库或开发卷：所有写入只发生在被守卫的 ``_test`` 库与 pytest 临时目录。
"""

import asyncio
import hashlib
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
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
from rag_backend.ingestion.parsing import MARKDOWN_PARSER_VERSION
from rag_backend.ingestion.validation import MAX_MARKDOWN_BYTES
from rag_backend.models.profile_contract import default_index_profile
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session
from test_core_migration import (
    alembic_config,
    alembic_revision,
    assert_statement_denied,
    business_tables,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from docx_samples import positive_simple_table  # noqa: E402

pytestmark = pytest.mark.integration

# 上传事务的 ORM 写入 ``ingest_job.profile_id``（0006）、受理时刻的 ``request_title``
# （0008），并依赖 ``document.acl_mode`` 的 server default（0012）；DOCX 上传还需 ``source_type``
# 允许 ``docx``（0013）与 ``web``（0015），因此上传片建在当前 head。
SCHEMA_REVISION = "20260929_0016"

# 默认 index profile 契约与其规范 JSON 的 SHA-256；与 profile 契约/登记聚焦测试一致。
GOLDEN_CONFIG_HASH = "4af4c33d4e8d5571cc513dc8c623b1fe66a5565683f95fc75a7b3f8a28dc57fa"

ORIGIN = "http://127.0.0.1"
ORGANIZATION_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
CSRF_SECRET = "integration-upload-csrf-secret"
PASSWORD = "integration-upload-password-123"

MARKDOWN_TITLE = "示例文档"
MARKDOWN_BYTES = "# 标题\n\n正文内容。\n".encode()


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, Any]]:
    # Windows 默认 ProactorEventLoop 不能用于 psycopg 异步连接；与生产入口一致。
    return ("asyncio", {"loop_factory": asyncio.SelectorEventLoop})


def assert_empty_schema(engine: Engine) -> None:
    """核对库处于空 schema：无 ``alembic_version`` 且 public schema 无任何业务表。"""

    with engine.connect() as connection:
        assert alembic_revision(connection) is None
        # business_tables 只排除 alembic_version，空集即 public schema 无表。
        assert business_tables(connection) == set()


def open_upload_schema(
    destructive_test_database: DestructiveTestDatabase,
) -> Iterator[Engine]:
    """真实上传 schema 生命周期；fixture 只是它的薄包装，单元测试直接驱动本函数。

    ``owns_schema`` 初始为 False，只有升级前的库名与空 schema 核对全部通过后才置
    True。前置条件不干净（库不是本 fixture 独占的空库）时不做任何 downgrade，避免
    误删不属于本 fixture 的数据。
    """

    config = alembic_config(destructive_test_database.url)
    engine = create_engine(destructive_test_database.url, pool_pre_ping=True)
    owns_schema = False
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT current_database()"))
                == destructive_test_database.database_name
            )
        assert_empty_schema(engine)
        owns_schema = True

        command.upgrade(config, SCHEMA_REVISION)
        yield engine
    finally:
        try:
            if owns_schema:
                # migration 0013 的 downgrade 在存在 docx 行时会拒绝（不删数据）；测试库先清空
                # document 及其依赖，再降级到 base。这里只作用于被独占的空库守卫。
                with engine.begin() as connection:
                    connection.execute(text("TRUNCATE document CASCADE"))
                # best-effort：即使升级/前置之后的步骤失败，也必须尝试降回 base。
                command.downgrade(config, "base")
                assert_empty_schema(engine)
        finally:
            # dispose 在任何分支都必须执行。
            engine.dispose()


@pytest.fixture(scope="module")
def upload_schema(destructive_test_database: DestructiveTestDatabase) -> Iterator[Engine]:
    """模块级 fixture；实现见 ``open_upload_schema``（单测直接驱动它）。"""

    yield from open_upload_schema(destructive_test_database)


@pytest.fixture(scope="module")
def blob_directory(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """模块级临时存储根；绝不写入仓库目录或开发卷。"""

    return tmp_path_factory.mktemp("upload-documents")


def make_settings(
    *,
    database_url: str,
    redis_url: str,
    storage_directory: Path,
    **overrides: Any,
) -> Settings:
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
        "document_storage_directory": str(storage_directory),
    }
    values.update(overrides)
    return Settings(
        database_url=database_url, redis_url=redis_url, **values
    )


@pytest.fixture
def upload_settings(
    upload_schema: Engine,
    destructive_test_database: DestructiveTestDatabase,
    role_test_databases: RoleTestDatabases,
    test_redis: Any,
    blob_directory: Path,
) -> Settings:
    assert role_test_databases.database_name == destructive_test_database.database_name
    return make_settings(
        database_url=role_test_databases.api_url,
        redis_url=test_redis.url,
        storage_directory=blob_directory,
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


@asynccontextmanager
async def api_client_no_reraise(settings: Settings) -> AsyncIterator[AsyncClient]:
    """同 ``api_client``，但关闭 ASGI 异常重抛，便于断言应用内 500 的静态错误体。"""

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(
            app=app, client=("10.20.0.1", 12345), raise_app_exceptions=False
        )
        async with AsyncClient(
            transport=transport, base_url=ORIGIN, headers={"Origin": ORIGIN}
        ) as client:
            yield client


def unique_username() -> str:
    return f"user-{uuid.uuid4().hex[:12]}"


def unique_name() -> str:
    return f"kb-{uuid.uuid4().hex[:12]}"


def unique_key() -> str:
    return f"idem-{uuid.uuid4().hex}"


def seed_user(engine: Engine, *, username: str, is_admin: bool = False) -> uuid.UUID:
    with Session(engine) as session:
        account = create_account(
            session,
            organization_id=ORGANIZATION_ID,
            username=username,
            password=PASSWORD,
            is_admin=is_admin,
        )
        return account.id


def seed_kb(engine: Engine, *, name: str) -> uuid.UUID:
    kb_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO knowledge_base (id, organization_id, name) "
                "VALUES (:id, :organization_id, :name)"
            ),
            {"id": kb_id, "organization_id": ORGANIZATION_ID, "name": name},
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


async def login_csrf(client: AsyncClient, username: str) -> str:
    response = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return str(response.json()["csrfToken"])


async def upload(
    client: AsyncClient,
    kb_id: uuid.UUID,
    *,
    idempotency_key: str | None,
    title: str = MARKDOWN_TITLE,
    content: bytes = MARKDOWN_BYTES,
    filename: str = "notes.md",
    content_type: str = "text/markdown",
    csrf: str | None = None,
    origin: str = ORIGIN,
) -> Any:
    headers: dict[str, str] = {"Origin": origin}
    if csrf is not None:
        headers[CSRF_HEADER_NAME] = csrf
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents",
        files={"file": (filename, content, content_type)},
        data={"title": title},
        headers=headers,
    )


def count_rows(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return int(connection.scalar(text(f"SELECT count(*) FROM {table}")))


def profile_count(engine: Engine) -> int:
    return count_rows(engine, "index_profile")


def blob_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*") if path.is_file())


def kb_blob_files(directory: Path, *kb_ids: uuid.UUID) -> list[Path]:
    """只统计指定 KB 名下的 blob，避免模块内其他用例的文件干扰。"""

    files: list[Path] = []
    for kb_id in kb_ids:
        root = directory / str(kb_id)
        if root.exists():
            files.extend(path for path in root.rglob("*") if path.is_file())
    return sorted(files)


def document_count_for_kb(engine: Engine, kb_id: uuid.UUID) -> int:
    with engine.connect() as connection:
        return int(
            connection.scalar(
                text("SELECT count(*) FROM document WHERE kb_id = :kb_id"),
                {"kb_id": kb_id},
            )
        )


def job_count_for_kb(engine: Engine, kb_id: uuid.UUID) -> int:
    with engine.connect() as connection:
        return int(
            connection.scalar(
                text(
                    "SELECT count(*) FROM ingest_job j "
                    "JOIN document d ON d.id = j.document_id WHERE d.kb_id = :kb_id"
                ),
                {"kb_id": kb_id},
            )
        )


def outbox_count_for_kb(engine: Engine, kb_id: uuid.UUID) -> int:
    with engine.connect() as connection:
        return int(
            connection.scalar(
                text(
                    "SELECT count(*) FROM outbox_event o "
                    "JOIN ingest_job j ON j.id = o.job_id "
                    "JOIN document d ON d.id = j.document_id WHERE d.kb_id = :kb_id"
                ),
                {"kb_id": kb_id},
            )
        )


def document_row(engine: Engine, document_id: uuid.UUID) -> dict[str, Any]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT title, source_type, active_version_id, lifecycle_status, deleted_at "
                "FROM document WHERE id = :id"
            ),
            {"id": document_id},
        ).one()
    return {
        "title": row[0],
        "source_type": row[1],
        "active_version_id": row[2],
        "lifecycle_status": row[3],
        "deleted_at": row[4],
    }


def version_row(engine: Engine, version_id: uuid.UUID) -> dict[str, Any]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT version_no, file_ref, file_hash, mime, parser_version, status "
                "FROM document_version WHERE id = :id"
            ),
            {"id": version_id},
        ).one()
    return {
        "version_no": row[0],
        "file_ref": row[1],
        "file_hash": row[2],
        "mime": row[3],
        "parser_version": row[4],
        "status": row[5],
    }


def job_row(engine: Engine, job_id: uuid.UUID) -> dict[str, Any]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT status, attempt, generation_id, dedupe_key, error_code, profile_id "
                "FROM ingest_job WHERE id = :id"
            ),
            {"id": job_id},
        ).one()
    return {
        "status": row[0],
        "attempt": row[1],
        "generation_id": row[2],
        "dedupe_key": row[3],
        "error_code": row[4],
        "profile_id": row[5],
    }


def profile_row(engine: Engine, profile_id: uuid.UUID) -> dict[str, Any]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT embedding_model, model_revision, dimension, normalize, "
                "tokenizer_revision, chunker_version, keyword_analyzer_version, config_hash "
                "FROM index_profile WHERE id = :id"
            ),
            {"id": profile_id},
        ).one()
    return {
        "embedding_model": row[0],
        "model_revision": row[1],
        "dimension": row[2],
        "normalize": row[3],
        "tokenizer_revision": row[4],
        "chunker_version": row[5],
        "keyword_analyzer_version": row[6],
        "config_hash": row[7],
    }


def kb_active_profile(engine: Engine, kb_id: uuid.UUID) -> object:
    with engine.connect() as connection:
        return connection.scalar(
            text("SELECT active_index_profile_id FROM knowledge_base WHERE id = :id"),
            {"id": kb_id},
        )


def outbox_for_job(engine: Engine, job_id: uuid.UUID) -> dict[str, Any]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT event_type, status, dispatch_attempt, sent_at "
                "FROM outbox_event WHERE job_id = :job_id"
            ),
            {"job_id": job_id},
        ).one()
    return {
        "event_type": row[0],
        "status": row[1],
        "dispatch_attempt": row[2],
        "sent_at": row[3],
    }


@pytest.mark.anyio
async def test_editor_upload_writes_single_transaction_facts(
    upload_schema: Engine, upload_settings: Settings, blob_directory: Path
) -> None:
    username = unique_username()
    user_id = seed_user(upload_schema, username=username)
    kb_id = seed_kb(upload_schema, name=unique_name())
    seed_member(upload_schema, kb_id=kb_id, user_id=user_id, role="EDITOR")
    key = unique_key()

    async with api_client(upload_settings) as client:
        csrf = await login_csrf(client, username)
        response = await upload(client, kb_id, idempotency_key=key, csrf=csrf)

    assert response.status_code == 202, response.text
    payload = response.json()
    assert set(payload) == {"documentId", "versionId", "jobId"}
    document_id = uuid.UUID(payload["documentId"])
    version_id = uuid.UUID(payload["versionId"])
    job_id = uuid.UUID(payload["jobId"])

    document = document_row(upload_schema, document_id)
    assert document == {
        "title": MARKDOWN_TITLE,
        "source_type": "markdown",
        "active_version_id": None,
        "lifecycle_status": "CREATED",
        "deleted_at": None,
    }
    # 新上传默认 INHERIT（读取沿用 KB 成员权限），不因本片改成 RESTRICTED。
    with upload_schema.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT acl_mode FROM document WHERE id = :id"),
                {"id": document_id},
            )
            == "INHERIT"
        )

    digest = hashlib.sha256(MARKDOWN_BYTES).hexdigest()
    version = version_row(upload_schema, version_id)
    assert version == {
        "version_no": 1,
        "file_ref": f"{kb_id}/{digest}",
        "file_hash": digest,
        "mime": "text/markdown",
        "parser_version": MARKDOWN_PARSER_VERSION,
        "status": "PENDING",
    }

    job = job_row(upload_schema, job_id)
    assert job["status"] == "QUEUED"
    assert job["attempt"] == 0
    assert job["generation_id"] is None
    assert job["error_code"] is None
    assert job["dedupe_key"] != key
    assert len(job["dedupe_key"]) == 64
    outbox = outbox_for_job(upload_schema, job_id)
    assert outbox == {
        "event_type": "ingest.requested",
        "status": "PENDING",
        "dispatch_attempt": 0,
        "sent_at": None,
    }

    # 原文件精确落在 KB 与 SHA-256 派生的路径上，且不残留临时文件。
    stored = blob_directory / f"{kb_id}/{digest}"
    assert stored.read_bytes() == MARKDOWN_BYTES
    assert kb_blob_files(blob_directory, kb_id) == [stored]
    assert document_count_for_kb(upload_schema, kb_id) == 1
    assert job_count_for_kb(upload_schema, kb_id) == 1
    assert outbox_count_for_kb(upload_schema, kb_id) == 1

    # 本切片不改动 KB 的 ACL/知识库 revision。
    with upload_schema.connect() as connection:
        row = connection.execute(
            text("SELECT acl_revision, kb_revision FROM knowledge_base WHERE id = :id"),
            {"id": kb_id},
        ).one()
    assert (row[0], row[1]) == (0, 0)


@pytest.mark.anyio
async def test_editor_upload_binds_default_profile_and_leaves_kb_pointer_null(
    upload_schema: Engine, upload_settings: Settings, blob_directory: Path
) -> None:
    """新上传在同一事务登记/复用唯一 profile 并把它绑定到 job；KB 指针仍为 NULL。"""

    username = unique_username()
    user_id = seed_user(upload_schema, username=username)
    kb_id = seed_kb(upload_schema, name=unique_name())
    seed_member(upload_schema, kb_id=kb_id, user_id=user_id, role="EDITOR")

    async with api_client(upload_settings) as client:
        csrf = await login_csrf(client, username)
        response = await upload(client, kb_id, idempotency_key=unique_key(), csrf=csrf)

    assert response.status_code == 202, response.text
    version_id = uuid.UUID(response.json()["versionId"])
    job = job_row(upload_schema, uuid.UUID(response.json()["jobId"]))
    assert job["profile_id"] is not None

    # 全局默认 profile 只登记一行，job 绑定该行；登记不代表任何 KB 可检索。
    assert profile_count(upload_schema) == 1
    contract = default_index_profile()
    assert contract.config_hash() == GOLDEN_CONFIG_HASH
    assert profile_row(upload_schema, job["profile_id"]) == {
        "embedding_model": contract.embedding_model,
        "model_revision": contract.model_revision,
        "dimension": contract.dimension,
        "normalize": contract.normalize,
        "tokenizer_revision": contract.tokenizer_revision,
        "chunker_version": contract.chunker_version,
        "keyword_analyzer_version": contract.keyword_analyzer_version,
        "config_hash": contract.config_hash(),
    }
    assert kb_active_profile(upload_schema, kb_id) is None
    assert version_row(upload_schema, version_id)["parser_version"] == MARKDOWN_PARSER_VERSION
    assert kb_blob_files(blob_directory, kb_id)
    assert not list(blob_directory.rglob("*.tmp"))


@pytest.mark.anyio
async def test_authorization_precedes_body_and_creates_nothing(
    upload_schema: Engine, upload_settings: Settings, blob_directory: Path
) -> None:
    reader_name = unique_username()
    reader_id = seed_user(upload_schema, username=reader_name)
    outsider_name = unique_username()
    seed_user(upload_schema, username=outsider_name)
    editor_name = unique_username()
    editor_id = seed_user(upload_schema, username=editor_name)
    kb_id = seed_kb(upload_schema, name=unique_name())
    seed_member(upload_schema, kb_id=kb_id, user_id=reader_id, role="READER")
    seed_member(upload_schema, kb_id=kb_id, user_id=editor_id, role="EDITOR")

    before = count_rows(upload_schema, "document")
    before_files = blob_files(blob_directory)

    async with api_client(upload_settings) as anonymous:
        response = await upload(anonymous, kb_id, idempotency_key=unique_key())
        assert response.status_code == 401, response.text

    async with api_client(upload_settings) as reader:
        csrf = await login_csrf(reader, reader_name)
        response = await upload(reader, kb_id, idempotency_key=unique_key(), csrf=csrf)
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "KNOWLEDGE_BASE_NOT_FOUND"

    async with api_client(upload_settings) as outsider:
        csrf = await login_csrf(outsider, outsider_name)
        response = await upload(outsider, kb_id, idempotency_key=unique_key(), csrf=csrf)
        assert response.status_code == 404, response.text

    async with api_client(upload_settings) as editor:
        csrf = await login_csrf(editor, editor_name)
        no_csrf = await upload(editor, kb_id, idempotency_key=unique_key())
        assert no_csrf.status_code == 403, no_csrf.text
        assert no_csrf.json()["code"] == "CSRF_INVALID"
        bad_origin = await upload(
            editor,
            kb_id,
            idempotency_key=unique_key(),
            csrf=csrf,
            origin="https://evil.example",
        )
        assert bad_origin.status_code == 403, bad_origin.text
        assert bad_origin.json()["code"] == "ORIGIN_NOT_ALLOWED"

    assert count_rows(upload_schema, "document") == before
    assert blob_files(blob_directory) == before_files


@pytest.mark.anyio
async def test_missing_idempotency_key_is_rejected(
    upload_schema: Engine, upload_settings: Settings
) -> None:
    username = unique_username()
    user_id = seed_user(upload_schema, username=username)
    kb_id = seed_kb(upload_schema, name=unique_name())
    seed_member(upload_schema, kb_id=kb_id, user_id=user_id, role="EDITOR")

    before = count_rows(upload_schema, "document")
    async with api_client(upload_settings) as client:
        csrf = await login_csrf(client, username)
        response = await upload(client, kb_id, idempotency_key=None, csrf=csrf)

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert count_rows(upload_schema, "document") == before


@pytest.mark.anyio
async def test_idempotent_replay_returns_same_ids(
    upload_schema: Engine, upload_settings: Settings, blob_directory: Path
) -> None:
    username = unique_username()
    user_id = seed_user(upload_schema, username=username)
    kb_id = seed_kb(upload_schema, name=unique_name())
    seed_member(upload_schema, kb_id=kb_id, user_id=user_id, role="EDITOR")
    key = unique_key()

    before_documents = count_rows(upload_schema, "document")
    async with api_client(upload_settings) as client:
        csrf = await login_csrf(client, username)
        first = await upload(client, kb_id, idempotency_key=key, csrf=csrf)
        second = await upload(client, kb_id, idempotency_key=key, csrf=csrf)

    assert first.status_code == 202 and second.status_code == 202, second.text
    assert first.json() == second.json()
    assert count_rows(upload_schema, "document") == before_documents + 1
    assert document_count_for_kb(upload_schema, kb_id) == 1
    assert job_count_for_kb(upload_schema, kb_id) == 1
    assert outbox_count_for_kb(upload_schema, kb_id) == 1
    assert len(kb_blob_files(blob_directory, kb_id)) == 1


@pytest.mark.anyio
async def test_idempotent_replay_does_not_upgrade_legacy_parser_version(
    upload_schema: Engine, upload_settings: Settings, blob_directory: Path
) -> None:
    """回放只复用旧 job：旧行的占位 parser 与 NULL profile 都不被就地补写。"""

    username = unique_username()
    user_id = seed_user(upload_schema, username=username)
    kb_id = seed_kb(upload_schema, name=unique_name())
    seed_member(upload_schema, kb_id=kb_id, user_id=user_id, role="EDITOR")
    key = unique_key()

    async with api_client(upload_settings) as client:
        csrf = await login_csrf(client, username)
        first = await upload(client, kb_id, idempotency_key=key, csrf=csrf)
        assert first.status_code == 202, first.text
        version_id = uuid.UUID(first.json()["versionId"])
        job_id = uuid.UUID(first.json()["jobId"])
        profile_before = profile_count(upload_schema)

        # 模拟旧切片留下的占位版本行与未绑定 profile 的旧 job，其余字段不动。
        with upload_schema.begin() as connection:
            connection.execute(
                text(
                    "UPDATE document_version SET parser_version = 'markdown-v1' "
                    "WHERE id = :id"
                ),
                {"id": version_id},
            )
            connection.execute(
                text("UPDATE ingest_job SET profile_id = NULL WHERE id = :id"),
                {"id": job_id},
            )

        second = await upload(client, kb_id, idempotency_key=key, csrf=csrf)

    assert second.status_code == 202, second.text
    assert first.json() == second.json()
    assert document_count_for_kb(upload_schema, kb_id) == 1
    assert job_count_for_kb(upload_schema, kb_id) == 1
    # 旧行保持原样：回放只复用 job，不补绑 profile、不假称旧数据已按新解析器处理。
    assert version_row(upload_schema, version_id)["parser_version"] == "markdown-v1"
    assert job_row(upload_schema, job_id)["profile_id"] is None
    assert profile_count(upload_schema) == profile_before


@pytest.mark.anyio
async def test_same_key_different_content_or_title_conflicts(
    upload_schema: Engine, upload_settings: Settings, blob_directory: Path
) -> None:
    username = unique_username()
    user_id = seed_user(upload_schema, username=username)
    kb_id = seed_kb(upload_schema, name=unique_name())
    seed_member(upload_schema, kb_id=kb_id, user_id=user_id, role="EDITOR")
    key = unique_key()

    async with api_client(upload_settings) as client:
        csrf = await login_csrf(client, username)
        first = await upload(client, kb_id, idempotency_key=key, csrf=csrf)
        assert first.status_code == 202, first.text
        documents_after_first = document_count_for_kb(upload_schema, kb_id)

        different_content = await upload(
            client,
            kb_id,
            idempotency_key=key,
            content=b"# different",
            csrf=csrf,
        )
        different_title = await upload(
            client, kb_id, idempotency_key=key, title="另一个标题", csrf=csrf
        )

    for response in (different_content, different_title):
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
        # 错误体不回显 Idempotency-Key 原值。
        assert key not in response.text

    assert document_count_for_kb(upload_schema, kb_id) == documents_after_first
    assert job_count_for_kb(upload_schema, kb_id) == 1
    # 冲突发生在写 blob 之前，因此只留下第一次上传的那份文件。
    assert len(kb_blob_files(blob_directory, kb_id)) == 1


@pytest.mark.anyio
async def test_same_key_across_kbs_is_independent(
    upload_schema: Engine, upload_settings: Settings, blob_directory: Path
) -> None:
    username = unique_username()
    user_id = seed_user(upload_schema, username=username)
    kb_a = seed_kb(upload_schema, name=unique_name())
    kb_b = seed_kb(upload_schema, name=unique_name())
    seed_member(upload_schema, kb_id=kb_a, user_id=user_id, role="EDITOR")
    seed_member(upload_schema, kb_id=kb_b, user_id=user_id, role="EDITOR")
    key = unique_key()

    async with api_client(upload_settings) as client:
        csrf = await login_csrf(client, username)
        first = await upload(client, kb_a, idempotency_key=key, csrf=csrf)
        second = await upload(client, kb_b, idempotency_key=key, csrf=csrf)

    assert first.status_code == 202 and second.status_code == 202, second.text
    first_ids = first.json()
    second_ids = second.json()
    # 跨 KB 不得复用同一 job，也不得通过响应互相泄露。
    assert first_ids["jobId"] != second_ids["jobId"]
    assert first_ids["documentId"] != second_ids["documentId"]

    assert document_count_for_kb(upload_schema, kb_a) == 1
    assert document_count_for_kb(upload_schema, kb_b) == 1
    assert job_count_for_kb(upload_schema, kb_a) == 1
    assert job_count_for_kb(upload_schema, kb_b) == 1
    with upload_schema.connect() as connection:
        keys = [
            str(row[0])
            for row in connection.execute(
                text(
                    "SELECT j.dedupe_key FROM ingest_job j "
                    "JOIN document d ON d.id = j.document_id "
                    "WHERE d.kb_id IN (:kb_a, :kb_b)"
                ),
                {"kb_a": kb_a, "kb_b": kb_b},
            )
        ]
    assert len(set(keys)) == 2
    # 相同内容在同一 KB 作用域下共享一份 blob（两个 KB 各一份）。
    assert len(kb_blob_files(blob_directory, kb_a, kb_b)) == 2


@pytest.mark.anyio
async def test_different_key_same_content_reuses_blob_with_new_document(
    upload_schema: Engine, upload_settings: Settings, blob_directory: Path
) -> None:
    username = unique_username()
    user_id = seed_user(upload_schema, username=username)
    kb_id = seed_kb(upload_schema, name=unique_name())
    seed_member(upload_schema, kb_id=kb_id, user_id=user_id, role="EDITOR")

    async with api_client(upload_settings) as client:
        csrf = await login_csrf(client, username)
        first = await upload(client, kb_id, idempotency_key=unique_key(), csrf=csrf)
        second = await upload(client, kb_id, idempotency_key=unique_key(), csrf=csrf)

    assert first.status_code == 202 and second.status_code == 202, second.text
    assert first.json()["documentId"] != second.json()["documentId"]
    assert document_count_for_kb(upload_schema, kb_id) == 2
    assert job_count_for_kb(upload_schema, kb_id) == 2
    # 不同 document 共享同一份内容寻址 blob，磁盘上只有一份文件。
    assert len(kb_blob_files(blob_directory, kb_id)) == 1
    first_version = version_row(upload_schema, uuid.UUID(first.json()["versionId"]))
    second_version = version_row(upload_schema, uuid.UUID(second.json()["versionId"]))
    assert first_version["file_ref"] == second_version["file_ref"]
    # 两次登记都按 config_hash 幂等复用全局唯一 profile。
    first_job = job_row(upload_schema, uuid.UUID(first.json()["jobId"]))
    second_job = job_row(upload_schema, uuid.UUID(second.json()["jobId"]))
    assert first_job["profile_id"] == second_job["profile_id"]
    assert profile_count(upload_schema) == 1


@pytest.mark.anyio
async def test_invalid_content_and_type_create_nothing(
    upload_schema: Engine, upload_settings: Settings, blob_directory: Path
) -> None:
    username = unique_username()
    user_id = seed_user(upload_schema, username=username)
    kb_id = seed_kb(upload_schema, name=unique_name())
    seed_member(upload_schema, kb_id=kb_id, user_id=user_id, role="EDITOR")

    before = count_rows(upload_schema, "document")
    before_files = blob_files(blob_directory)
    async with api_client(upload_settings) as client:
        csrf = await login_csrf(client, username)
        binary = await upload(
            client,
            kb_id,
            idempotency_key=unique_key(),
            content=b"\x89PNG\r\n\x1a\n\x00binary",
            csrf=csrf,
        )
        empty = await upload(
            client, kb_id, idempotency_key=unique_key(), content=b"", csrf=csrf
        )
        wrong_type = await upload(
            client,
            kb_id,
            idempotency_key=unique_key(),
            filename="notes.rtf",
            content_type="application/rtf",
            csrf=csrf,
        )
        wrong_pdf = await upload(
            client,
            kb_id,
            idempotency_key=unique_key(),
            filename="notes.pdf",
            content_type="application/pdf",
            csrf=csrf,
        )

    assert binary.status_code == 422 and binary.json()["code"] == "DOCUMENT_NOT_TEXT"
    assert empty.status_code == 422 and empty.json()["code"] == "DOCUMENT_EMPTY"
    assert (
        wrong_type.status_code == 422
        and wrong_type.json()["code"] == "UNSUPPORTED_DOCUMENT_TYPE"
    )
    assert wrong_pdf.status_code == 422 and wrong_pdf.json()["code"] == "DOCUMENT_NOT_PDF"
    assert count_rows(upload_schema, "document") == before
    assert blob_files(blob_directory) == before_files


@pytest.mark.anyio
async def test_upload_size_boundary_and_oversize(
    upload_schema: Engine, upload_settings: Settings
) -> None:
    username = unique_username()
    user_id = seed_user(upload_schema, username=username)
    kb_id = seed_kb(upload_schema, name=unique_name())
    seed_member(upload_schema, kb_id=kb_id, user_id=user_id, role="EDITOR")

    at_limit = b"a" * MAX_MARKDOWN_BYTES
    over_limit = b"a" * (MAX_MARKDOWN_BYTES + 1)
    before = count_rows(upload_schema, "document")
    async with api_client(upload_settings) as client:
        csrf = await login_csrf(client, username)
        accepted = await upload(
            client, kb_id, idempotency_key=unique_key(), content=at_limit, csrf=csrf
        )
        rejected = await upload(
            client, kb_id, idempotency_key=unique_key(), content=over_limit, csrf=csrf
        )

    assert accepted.status_code == 202, accepted.text
    assert rejected.status_code == 413, rejected.text
    assert rejected.json()["code"] == "DOCUMENT_TOO_LARGE"
    assert count_rows(upload_schema, "document") == before + 1


@pytest.mark.anyio
async def test_concurrent_same_key_creates_one_document(
    upload_schema: Engine, upload_settings: Settings
) -> None:
    username = unique_username()
    user_id = seed_user(upload_schema, username=username)
    kb_id = seed_kb(upload_schema, name=unique_name())
    seed_member(upload_schema, kb_id=kb_id, user_id=user_id, role="EDITOR")
    key = unique_key()

    before = count_rows(upload_schema, "document")

    async def one_upload() -> Any:
        async with api_client(upload_settings) as client:
            csrf = await login_csrf(client, username)
            return await upload(client, kb_id, idempotency_key=key, csrf=csrf)

    first, second = await asyncio.gather(one_upload(), one_upload())

    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    # 唯一冲突后回滚重读，应复用同一个 job，而不是创建第二份文档。
    assert first.json() == second.json()
    assert count_rows(upload_schema, "document") == before + 1
    assert document_count_for_kb(upload_schema, kb_id) == 1
    assert job_count_for_kb(upload_schema, kb_id) == 1
    # 并发登记同一个默认契约只会留下一行 profile，两边都绑定它。
    assert profile_count(upload_schema) == 1
    job = job_row(upload_schema, uuid.UUID(first.json()["jobId"]))
    assert job["profile_id"] is not None


# 篡改用：同 config_hash 但字段不符的既有行不能被静默复用。
BOOTSTRAP_PROFILE_SQL = text(
    "INSERT INTO index_profile (id, embedding_model, model_revision, dimension, normalize, "
    "tokenizer_revision, chunker_version, keyword_analyzer_version, config_hash) "
    "VALUES (:id, :embedding_model, :model_revision, :dimension, :normalize, "
    ":tokenizer_revision, :chunker_version, :keyword_analyzer_version, :config_hash) "
    "ON CONFLICT (config_hash) DO NOTHING"
)


@pytest.mark.anyio
async def test_tampered_profile_fails_closed_without_partial_rows(
    upload_schema: Engine, upload_settings: Settings, blob_directory: Path
) -> None:
    """同 config_hash 但字段被篡改时返回静态 500，四表无新行且响应不泄值。"""

    contract = default_index_profile()
    assert contract.config_hash() == GOLDEN_CONFIG_HASH

    # 保证默认 profile 行存在后用迁移角色篡改其一个契约字段；api 角色只能 SELECT+INSERT。
    with upload_schema.begin() as connection:
        connection.execute(
            BOOTSTRAP_PROFILE_SQL,
            {
                "id": uuid.uuid4(),
                "embedding_model": contract.embedding_model,
                "model_revision": contract.model_revision,
                "dimension": contract.dimension,
                "normalize": contract.normalize,
                "tokenizer_revision": contract.tokenizer_revision,
                "chunker_version": contract.chunker_version,
                "keyword_analyzer_version": contract.keyword_analyzer_version,
                "config_hash": contract.config_hash(),
            },
        )
        connection.execute(
            text(
                "UPDATE index_profile SET embedding_model = 'tampered/model' "
                "WHERE config_hash = :config_hash"
            ),
            {"config_hash": contract.config_hash()},
        )

    username = unique_username()
    user_id = seed_user(upload_schema, username=username)
    kb_id = seed_kb(upload_schema, name=unique_name())
    seed_member(upload_schema, kb_id=kb_id, user_id=user_id, role="EDITOR")

    counts_before = {
        table: count_rows(upload_schema, table)
        for table in ("document", "document_version", "ingest_job", "outbox_event")
    }
    profile_before = profile_count(upload_schema)
    try:
        async with api_client_no_reraise(upload_settings) as client:
            csrf = await login_csrf(client, username)
            response = await upload(
                client, kb_id, idempotency_key=unique_key(), csrf=csrf
            )

        assert response.status_code == 500, response.text
        payload = response.json()
        assert payload["code"] == "INTERNAL_ERROR"
        assert payload["message"] == "服务器内部错误"
        # 响应绝不回显篡改值、契约摘要/词典身份或上传正文。
        for leak in (
            "tampered/model",
            contract.config_hash(),
            contract.keyword_analyzer_version,
            contract.tokenizer_revision,
            MARKDOWN_BYTES.decode(),
        ):
            assert leak not in response.text

        # profile 冲突发生在写 document 之前：四表与 profile 行数都不变。
        assert profile_count(upload_schema) == profile_before
        for table, before_count in counts_before.items():
            assert count_rows(upload_schema, table) == before_count
        assert document_count_for_kb(upload_schema, kb_id) == 0
        assert job_count_for_kb(upload_schema, kb_id) == 0
        # 预检在 publish 之前 fail closed：该 KB 的内容寻址最终 blob 从未出现。
        digest = hashlib.sha256(MARKDOWN_BYTES).hexdigest()
        assert not (blob_directory / str(kb_id) / digest).exists()
        assert kb_blob_files(blob_directory, kb_id) == []
        assert kb_active_profile(upload_schema, kb_id) is None
        # 失败路径不留临时文件（已发布的最终 blob 可能成为孤儿，本切片不 GC）。
        assert not list(blob_directory.rglob("*.tmp"))
    finally:
        # 还原被篡改的字段，避免影响后续用例。
        with upload_schema.begin() as connection:
            connection.execute(
                text(
                    "UPDATE index_profile SET embedding_model = :embedding_model "
                    "WHERE config_hash = :config_hash"
                ),
                {
                    "embedding_model": contract.embedding_model,
                    "config_hash": contract.config_hash(),
                },
            )


def test_worker_role_cannot_register_index_profile(
    upload_schema: Engine, role_test_databases: RoleTestDatabases
) -> None:
    """worker 角色只有 index_profile 的 SELECT，不能登记 profile 契约。"""

    assert_statement_denied(
        role_test_databases.worker_url,
        "INSERT INTO index_profile (id, embedding_model, model_revision, dimension, normalize, "
        "tokenizer_revision, chunker_version, keyword_analyzer_version, config_hash) "
        "VALUES ('00000000-0000-0000-0000-000000000099', 'm', 'r', 512, true, 't', 'c', 'k', 'h')",
    )


# --- 文本 PDF 上传受理 -------------------------------------------------------


def _minimal_pdf_bytes(pages: list[str]) -> bytes:
    """用 pypdf 构造带文本层的最小 PDF（仅测试样本，不提交二进制）。"""

    import io

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    for page_text in pages:
        page = writer.add_blank_page(width=200, height=200)
        if page_text:
            stream = DecodedStreamObject()
            stream.set_data(
                ("BT /F1 12 Tf 10 100 Td (" + page_text + ") Tj ET").encode("latin-1")
            )
            page[NameObject("/Contents")] = writer._add_object(stream)
            page[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
            )
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@pytest.mark.anyio
async def test_editor_upload_pdf_writes_pdf_facts(
    upload_schema: Engine, upload_settings: Settings, blob_directory: Path
) -> None:
    """`.pdf` 后缀上传登记 `source_type=pdf`、PDF MIME 与真实 PDF 解析器版本。"""

    from rag_backend.ingestion.pdf_parsing import PDF_PARSER_VERSION

    raw = _minimal_pdf_bytes(["PDF upload page"])
    username = unique_username()
    user_id = seed_user(upload_schema, username=username)
    kb_id = seed_kb(upload_schema, name=unique_name())
    seed_member(upload_schema, kb_id=kb_id, user_id=user_id, role="EDITOR")

    async with api_client(upload_settings) as client:
        csrf = await login_csrf(client, username)
        response = await upload(
            client,
            kb_id,
            idempotency_key=unique_key(),
            title="PDF 报告",
            content=raw,
            filename="report.pdf",
            content_type="application/pdf",
            csrf=csrf,
        )

    assert response.status_code == 202, response.text
    document_id = uuid.UUID(response.json()["documentId"])
    version_id = uuid.UUID(response.json()["versionId"])
    assert document_row(upload_schema, document_id)["source_type"] == "pdf"
    digest = hashlib.sha256(raw).hexdigest()
    version = version_row(upload_schema, version_id)
    assert version["mime"] == "application/pdf"
    assert version["parser_version"] == PDF_PARSER_VERSION
    assert (blob_directory / f"{kb_id}/{digest}").read_bytes() == raw


@pytest.mark.anyio
async def test_editor_upload_docx_writes_docx_facts(
    upload_schema: Engine, upload_settings: Settings, blob_directory: Path
) -> None:
    """``.docx`` 后缀上传登记 ``source_type=docx``、DOCX MIME 与真实 DOCX 解析器版本。"""

    from rag_backend.ingestion.docx_parsing import DOCX_PARSER_VERSION

    raw = positive_simple_table()
    username = unique_username()
    user_id = seed_user(upload_schema, username=username)
    kb_id = seed_kb(upload_schema, name=unique_name())
    seed_member(upload_schema, kb_id=kb_id, user_id=user_id, role="EDITOR")

    async with api_client(upload_settings) as client:
        csrf = await login_csrf(client, username)
        response = await upload(
            client,
            kb_id,
            idempotency_key=unique_key(),
            title="DOCX 指南",
            content=raw,
            filename="guide.docx",
            content_type=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
            csrf=csrf,
        )

    assert response.status_code == 202, response.text
    document_id = uuid.UUID(response.json()["documentId"])
    version_id = uuid.UUID(response.json()["versionId"])
    assert document_row(upload_schema, document_id)["source_type"] == "docx"
    digest = hashlib.sha256(raw).hexdigest()
    version = version_row(upload_schema, version_id)
    assert version["mime"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert version["parser_version"] == DOCX_PARSER_VERSION
    assert (blob_directory / f"{kb_id}/{digest}").read_bytes() == raw


@pytest.mark.anyio
async def test_editor_upload_docx_with_bad_zip_is_rejected(
    upload_schema: Engine, upload_settings: Settings
) -> None:
    """``.docx`` 后缀但不是可识别 ZIP 包：422，不落库。"""

    username = unique_username()
    user_id = seed_user(upload_schema, username=username)
    kb_id = seed_kb(upload_schema, name=unique_name())
    seed_member(upload_schema, kb_id=kb_id, user_id=user_id, role="EDITOR")
    before = count_rows(upload_schema, "document")

    async with api_client(upload_settings) as client:
        csrf = await login_csrf(client, username)
        response = await upload(
            client,
            kb_id,
            idempotency_key=unique_key(),
            content=b"PK\x03\x04broken",
            filename="guide.docx",
            content_type=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
            csrf=csrf,
        )

    assert response.status_code == 422
    assert response.json()["code"] == "DOCUMENT_NOT_DOCX"
    assert count_rows(upload_schema, "document") == before


@pytest.mark.anyio
async def test_editor_upload_pdf_without_magic_is_rejected(
    upload_schema: Engine, upload_settings: Settings
) -> None:
    """`.pdf` 后缀但内容无 `%PDF-` 魔数：422，不落库。"""

    username = unique_username()
    user_id = seed_user(upload_schema, username=username)
    kb_id = seed_kb(upload_schema, name=unique_name())
    seed_member(upload_schema, kb_id=kb_id, user_id=user_id, role="EDITOR")
    before = count_rows(upload_schema, "document")

    async with api_client(upload_settings) as client:
        csrf = await login_csrf(client, username)
        response = await upload(
            client,
            kb_id,
            idempotency_key=unique_key(),
            content=b"plain text pretending pdf",
            filename="report.pdf",
            content_type="application/pdf",
            csrf=csrf,
        )

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "DOCUMENT_NOT_PDF"
    assert count_rows(upload_schema, "document") == before
