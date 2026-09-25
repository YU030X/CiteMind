"""第五切片 ``ingest_job.profile_id`` 迁移的真实 PostgreSQL 验收。

从 ``20260923_0005`` 干净状态升级到 ``20260925_0006``，核对 ``ingest_job`` 新增的
可空 ``profile_id`` 列与指向 ``index_profile(id)`` 的具名 RESTRICT 外键，证明：

- 升级前插入的旧 ``QUEUED`` 任务（真实引用已存在的 KB/document/document_version，
  不冒充处理完成）在升级后 ``profile_id`` 仍为 NULL，且列可空、无默认值、无回填；
- 合法 profile 引用被接受，不存在的 profile 被外键拒绝，被引用 profile 的删除被
  RESTRICT 拒绝；
- 升级未新增任何表、索引或 PUBLIC 权限，api/worker 的表级授权未被扩大；
- 降级只删除该外键与列，旧任务行仍存在，其它表与 ACL 不受影响；
- SQLAlchemy 模型与迁移结构一致。

只使用破坏性测试库与三角色 DSN 守卫；缺少 DSN 时按既有契约跳过。

清理由所有权把手：只有确认升级前 ``current_database`` 正确、无 ``alembic_version``、
public schema 没有任何业务表之后才允许降级；前置条件不干净时绝不降级，避免破坏
不属于本 fixture 的对象。
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from alembic import command
from database_guard import DestructiveTestDatabase
from database_roles_guard import API_ROLE, WORKER_ROLE, RoleTestDatabases
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import IntegrityError
from test_core_migration import (
    alembic_config,
    alembic_revision,
    assert_statement_denied,
    business_tables,
    foreign_key_behaviours,
    named_constraints,
    named_indexes,
    public_grant_counts,
    role_grants,
    sequence_count,
)
from test_second_slice_migration import (
    CORE_TABLES,
    EXPECTED_INGEST_JOB_CONSTRAINTS,
    SECOND_SLICE_TABLES,
    ann_index_count,
    enum_type_count,
    forbidden_privilege_count,
    ingest_job_columns,
)

pytestmark = pytest.mark.integration

IDENTITY_REVISION = "20260923_0005"
INGEST_JOB_PROFILE_REVISION = "20260925_0006"

LLM_USAGE_TABLES = ("llm_usage",)
IDENTITY_TABLES = ("user_account", "auth_session", "kb_member")
# 本迁移只给 ingest_job 加列，不新增任何业务表。
ALL_TABLES = (
    CORE_TABLES + SECOND_SLICE_TABLES + LLM_USAGE_TABLES + IDENTITY_TABLES
)

INGEST_JOB_PROFILE_FOREIGN_KEY = "fk_ingest_job_profile_id_index_profile"
INGEST_JOB_CONSTRAINTS_WITH_PROFILE = EXPECTED_INGEST_JOB_CONSTRAINTS | {
    INGEST_JOB_PROFILE_FOREIGN_KEY
}

PROFILE_ID_COLUMN_QUERY = text(
    "SELECT is_nullable, column_default FROM information_schema.columns "
    "WHERE table_schema = 'public' AND table_name = 'ingest_job' "
    "AND column_name = 'profile_id'"
)
SELECT_JOB_PROFILE_SQL = text("SELECT profile_id FROM ingest_job WHERE id = :id")
UPDATE_JOB_PROFILE_SQL = text(
    "UPDATE ingest_job SET profile_id = :profile_id WHERE id = :id"
)

INSERT_KB_SQL = text(
    "INSERT INTO knowledge_base (id, organization_id, name, kb_revision, acl_revision) "
    "VALUES (:id, :organization_id, 'kb', 0, 0)"
)
INSERT_DOCUMENT_SQL = text(
    "INSERT INTO document (id, kb_id, title, source_type, lifecycle_status) "
    "VALUES (:id, :kb_id, 'doc', 'markdown', 'CREATED')"
)
INSERT_VERSION_SQL = text(
    "INSERT INTO document_version (id, document_id, version_no, file_ref, file_hash, mime, "
    "parser_version, status) VALUES (:id, :document_id, 1, 'ref', 'file-hash', "
    "'text/markdown', 'parser', 'READY')"
)
# 0005 结构下的旧任务：状态保持 QUEUED，不写 READY、不冒充处理完成。
INSERT_JOB_SQL = text(
    "INSERT INTO ingest_job (id, document_id, version_id, status, dedupe_key) "
    "VALUES (:id, :document_id, :version_id, 'QUEUED', :dedupe_key)"
)
INSERT_JOB_WITH_PROFILE_SQL = text(
    "INSERT INTO ingest_job (id, document_id, version_id, status, dedupe_key, profile_id) "
    "VALUES (:id, :document_id, :version_id, 'QUEUED', :dedupe_key, :profile_id)"
)
INSERT_PROFILE_SQL = text(
    "INSERT INTO index_profile (id, embedding_model, model_revision, dimension, normalize, "
    "tokenizer_revision, chunker_version, keyword_analyzer_version, config_hash) "
    "VALUES (:id, 'model', 'rev', 512, true, 'tok', 'chunk', 'kw', :config_hash)"
)


@dataclass(frozen=True)
class LegacyJob:
    """升级前插入的真实合法任务链 id；用于核对旧行 NULL 与降级不删除。"""

    organization_id: uuid.UUID
    kb_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    job_id: uuid.UUID


def column_info(connection: Connection) -> tuple[str | None, str | None]:
    row = connection.execute(PROFILE_ID_COLUMN_QUERY).one_or_none()
    if row is None:
        return (None, None)
    return (None if row[0] is None else str(row[0]), None if row[1] is None else str(row[1]))


def seed_legacy_job(engine: Engine) -> LegacyJob:
    chain = LegacyJob(
        organization_id=uuid.uuid4(),
        kb_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
    )
    with engine.begin() as connection:
        connection.execute(
            INSERT_KB_SQL, {"id": chain.kb_id, "organization_id": chain.organization_id}
        )
        connection.execute(
            INSERT_DOCUMENT_SQL, {"id": chain.document_id, "kb_id": chain.kb_id}
        )
        connection.execute(
            INSERT_VERSION_SQL,
            {"id": chain.version_id, "document_id": chain.document_id},
        )
        connection.execute(
            INSERT_JOB_SQL,
            {
                "id": chain.job_id,
                "document_id": chain.document_id,
                "version_id": chain.version_id,
                "dedupe_key": chain.job_id.hex,
            },
        )
    return chain


def insert_profile(engine: Engine) -> uuid.UUID:
    profile_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            INSERT_PROFILE_SQL, {"id": profile_id, "config_hash": profile_id.hex}
        )
    return profile_id


def insert_job_with_profile(
    engine: Engine, chain: LegacyJob, *, profile_id: uuid.UUID
) -> uuid.UUID:
    job_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            INSERT_JOB_WITH_PROFILE_SQL,
            {
                "id": job_id,
                "document_id": chain.document_id,
                "version_id": chain.version_id,
                "dedupe_key": job_id.hex,
                "profile_id": profile_id,
            },
        )
    return job_id


def verify_identity_stage(engine: Engine, legacy: LegacyJob | None) -> None:
    """核对已降回 ``20260923_0005``：结构、旧任务与 ACL 未被降级破坏。"""

    with engine.connect() as connection:
        assert alembic_revision(connection) == IDENTITY_REVISION
        assert business_tables(connection) == set(ALL_TABLES)
        assert "profile_id" not in ingest_job_columns(connection)
        if legacy is not None:
            # 降级只删外键与列，旧任务行仍在。
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM ingest_job WHERE id = :id"),
                    {"id": legacy.job_id},
                )
                == 1
            )
        # 其它表结构与 ACL 未被降级触碰。
        assert named_constraints(connection)["ingest_job"] == (
            EXPECTED_INGEST_JOB_CONSTRAINTS
        )
        assert role_grants(connection, "ingest_job", API_ROLE) == {
            "SELECT",
            "INSERT",
            "UPDATE",
        }
        assert role_grants(connection, "ingest_job", WORKER_ROLE) == {
            "SELECT",
            "UPDATE",
        }


def verify_base_stage(engine: Engine) -> None:
    """核对降回 base 后无残留：无 ``alembic_version`` 且 public schema 无业务表。"""

    with engine.connect() as connection:
        assert alembic_revision(connection) is None
        assert business_tables(connection) == set()


def open_ingest_job_profile_schema(
    destructive_test_database: DestructiveTestDatabase,
) -> Iterator[tuple[Engine, LegacyJob]]:
    """真实 schema 生命周期；fixture 只是它的薄包装，单元测试直接驱动本函数。

    ``owns_schema`` 初始为 False，只有升级前的库名、``alembic_version`` 与 public
    schema 表集合全部核对通过后才置 True。前置条件不干净（库不是本 fixture 独占的
    空库）时不做任何 downgrade。
    """

    config = alembic_config(destructive_test_database.url)
    engine = create_engine(destructive_test_database.url, pool_pre_ping=True)
    owns_schema = False
    legacy: LegacyJob | None = None
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT current_database()"))
                == destructive_test_database.database_name
            )
            assert alembic_revision(connection) is None
            # business_tables 只排除 alembic_version，空集即 public schema 无表。
            assert business_tables(connection) == set()
        owns_schema = True

        command.upgrade(config, IDENTITY_REVISION)
        with engine.connect() as connection:
            assert alembic_revision(connection) == IDENTITY_REVISION
            assert business_tables(connection) == set(ALL_TABLES)
            assert "profile_id" not in ingest_job_columns(connection)

        # 升级前插入旧任务，验证新列对旧行保持 NULL。
        legacy = seed_legacy_job(engine)
        command.upgrade(config, INGEST_JOB_PROFILE_REVISION)
        yield engine, legacy
    finally:
        try:
            if owns_schema:
                # 即使降回 0005 或随后的结构核对失败，也必须继续尝试降回 base。
                try:
                    command.downgrade(config, IDENTITY_REVISION)
                    verify_identity_stage(engine, legacy)
                finally:
                    command.downgrade(config, "base")
                    verify_base_stage(engine)
        finally:
            # dispose 在任何分支都必须执行。
            engine.dispose()


@pytest.fixture(scope="module")
def ingest_job_profile_schema(
    destructive_test_database: DestructiveTestDatabase,
) -> Iterator[tuple[Engine, LegacyJob]]:
    """模块级 fixture；实现见 ``open_ingest_job_profile_schema``（单测直接驱动它）。"""

    yield from open_ingest_job_profile_schema(destructive_test_database)


def test_upgrade_adds_nullable_profile_id_leaving_legacy_rows_null(
    ingest_job_profile_schema: tuple[Engine, LegacyJob],
) -> None:
    engine, legacy = ingest_job_profile_schema
    with engine.connect() as connection:
        assert alembic_revision(connection) == INGEST_JOB_PROFILE_REVISION
        assert business_tables(connection) == set(ALL_TABLES)
        assert "profile_id" in ingest_job_columns(connection)
        assert column_info(connection) == ("YES", None)
        assert (
            connection.scalar(SELECT_JOB_PROFILE_SQL, {"id": legacy.job_id}) is None
        )
        assert sequence_count(connection) == 0
        assert enum_type_count(connection) == 0
        assert ann_index_count(connection) == 0


def test_profile_id_is_a_named_restrict_foreign_key_without_new_indexes(
    ingest_job_profile_schema: tuple[Engine, LegacyJob],
) -> None:
    engine, _ = ingest_job_profile_schema
    with engine.connect() as connection:
        constraints = named_constraints(connection)
        indexes = named_indexes(connection)
        behaviours = foreign_key_behaviours(connection)

    assert constraints["ingest_job"] == INGEST_JOB_CONSTRAINTS_WITH_PROFILE
    assert indexes["ingest_job"] == {"ix_ingest_job_status_next_run_at"}
    assert behaviours[INGEST_JOB_PROFILE_FOREIGN_KEY] == ("r", "r")


def test_upgrade_does_not_expand_public_or_role_grants(
    ingest_job_profile_schema: tuple[Engine, LegacyJob],
) -> None:
    engine, _ = ingest_job_profile_schema
    with engine.connect() as connection:
        counts = public_grant_counts(connection)
        api_grants = role_grants(connection, "ingest_job", API_ROLE)
        worker_grants = role_grants(connection, "ingest_job", WORKER_ROLE)
        forbidden = forbidden_privilege_count(connection)

    for table in ALL_TABLES:
        assert counts.get(table, 0) == 0, f"{table} 仍向 PUBLIC 授权"
    assert api_grants == {"SELECT", "INSERT", "UPDATE"}
    assert worker_grants == {"SELECT", "UPDATE"}
    assert forbidden == 0


def test_foreign_key_accepts_valid_profile_and_rejects_unknown_and_delete(
    ingest_job_profile_schema: tuple[Engine, LegacyJob],
) -> None:
    engine, legacy = ingest_job_profile_schema
    profile_id = insert_profile(engine)

    job_id = insert_job_with_profile(engine, legacy, profile_id=profile_id)
    with engine.connect() as connection:
        assert (
            connection.scalar(SELECT_JOB_PROFILE_SQL, {"id": job_id}) == profile_id
        )

    with pytest.raises(IntegrityError):
        insert_job_with_profile(engine, legacy, profile_id=uuid.uuid4())

    # RESTRICT：删除被引用的 profile 被拒绝。
    with engine.connect() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(
                text("DELETE FROM index_profile WHERE id = :id"), {"id": profile_id}
            )
        connection.rollback()


def test_new_column_is_covered_by_existing_update_grants(
    ingest_job_profile_schema: tuple[Engine, LegacyJob],
    role_test_databases: RoleTestDatabases,
) -> None:
    engine, legacy = ingest_job_profile_schema
    profile_id = insert_profile(engine)
    job_id = insert_job_with_profile(engine, legacy, profile_id=profile_id)

    # 新列由既有表级 UPDATE 授权覆盖：api 与 worker 都可改 pin，无需新增 GRANT。
    api_engine = create_engine(role_test_databases.api_url, pool_pre_ping=True)
    try:
        with api_engine.begin() as connection:
            connection.execute(
                UPDATE_JOB_PROFILE_SQL, {"id": job_id, "profile_id": None}
            )
    finally:
        api_engine.dispose()

    worker_engine = create_engine(role_test_databases.worker_url, pool_pre_ping=True)
    try:
        with worker_engine.begin() as connection:
            connection.execute(
                UPDATE_JOB_PROFILE_SQL, {"id": job_id, "profile_id": profile_id}
            )
    finally:
        worker_engine.dispose()

    with engine.connect() as connection:
        assert connection.scalar(SELECT_JOB_PROFILE_SQL, {"id": job_id}) == profile_id

    # 授权未扩大：两个运行角色仍不能 DELETE/TRUNCATE ingest_job。
    assert_statement_denied(role_test_databases.api_url, "DELETE FROM ingest_job")
    assert_statement_denied(role_test_databases.api_url, "TRUNCATE ingest_job")
    assert_statement_denied(role_test_databases.worker_url, "DELETE FROM ingest_job")
    assert_statement_denied(role_test_databases.worker_url, "TRUNCATE ingest_job")


def test_sqlalchemy_model_matches_the_migrated_profile_column(
    ingest_job_profile_schema: tuple[Engine, LegacyJob],
) -> None:
    from rag_backend.models import IngestJob

    column = IngestJob.__table__.columns["profile_id"]
    assert column.nullable is True
    assert column.server_default is None
    assert column.default is None
    foreign_key = next(iter(column.foreign_keys))
    assert foreign_key.column.table.name == "index_profile"
    assert foreign_key.ondelete == "RESTRICT"
    assert foreign_key.onupdate == "RESTRICT"
    assert foreign_key.constraint is not None
    assert foreign_key.constraint.name == INGEST_JOB_PROFILE_FOREIGN_KEY

    engine, _ = ingest_job_profile_schema
    with engine.connect() as connection:
        assert "profile_id" in ingest_job_columns(connection)
        assert named_constraints(connection)["ingest_job"] == (
            INGEST_JOB_CONSTRAINTS_WITH_PROFILE
        )
