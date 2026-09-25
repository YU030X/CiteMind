"""Markdown 上传事务片的真实 PostgreSQL + Redis 验收。

覆盖：EDITOR+KB 成员授权先于正文读取、CSRF/Origin、单事务写入四张表、202 响应、
Idempotency-Key 复用/冲突/跨 KB 隔离、不同 key 同内容复用 KB 私有 blob、并发唯一冲突
回滚重读、体积与内容校验且不落库不落文件。缺少守卫 DSN 或 Redis 时按既有契约跳过。

绝不触碰开发库或开发卷：所有写入只发生在被守卫的 ``_test`` 库与 pytest 临时目录。
"""

import asyncio
import hashlib
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
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session
from test_core_migration import alembic_config, alembic_revision, business_tables

pytestmark = pytest.mark.integration

# 上传事务的 ORM 会显式写入 ``ingest_job.profile_id``（可空），该列由 ``20260925_0006``
# 新增；因此上传片必须建在上含该列的线性 schema 上，不能用 0005。
SCHEMA_REVISION = "20260925_0006"

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
                "SELECT status, attempt, generation_id, dedupe_key, error_code "
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
    }


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
    """回放只复用旧 job；既有旧行的 ``markdown-v1`` 不被就地迁移或升级。"""

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

        # 模拟旧切片留下的占位版本行，其余字段不动。
        with upload_schema.begin() as connection:
            connection.execute(
                text(
                    "UPDATE document_version SET parser_version = 'markdown-v1' "
                    "WHERE id = :id"
                ),
                {"id": version_id},
            )

        second = await upload(client, kb_id, idempotency_key=key, csrf=csrf)

    assert second.status_code == 202, second.text
    assert first.json() == second.json()
    assert document_count_for_kb(upload_schema, kb_id) == 1
    assert job_count_for_kb(upload_schema, kb_id) == 1
    # 旧行版本保持原样：回放只复用 job，不假称旧数据已按新解析器处理。
    assert version_row(upload_schema, version_id)["parser_version"] == "markdown-v1"


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
