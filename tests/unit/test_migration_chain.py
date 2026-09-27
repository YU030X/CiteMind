"""迁移链与离线 SQL 的纯逻辑检查：不连接数据库。"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from rag_backend.config import DEFAULT_DATABASE_URL

REPO_ROOT = Path(__file__).parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
PGVECTOR_REVISION = "20260921_0001"
CORE_REVISION = "20260922_0002"
SECOND_SLICE_REVISION = "20260922_0003"
LLM_USAGE_REVISION = "20260923_0004"
IDENTITY_REVISION = "20260923_0005"
INGEST_JOB_PROFILE_REVISION = "20260925_0006"
WORKER_KB_PUBLISH_REVISION = "20260925_0007"
INGEST_JOB_REQUEST_TITLE_REVISION = "20260926_0008"
CONVERSATION_REVISION = "20260927_0009"

CORE_TABLES = (
    "index_profile",
    "knowledge_base",
    "document",
    "document_version",
    "ingest_job",
    "outbox_event",
)
SECOND_SLICE_TABLES = ("index_generation", "chunk", "chunk_embedding")
LLM_USAGE_TABLES = ("llm_usage",)
IDENTITY_TABLES = ("user_account", "auth_session", "kb_member")
CONVERSATION_TABLES = ("conversation", "message", "query_run", "citation")
ALL_TABLES = (
    CORE_TABLES
    + SECOND_SLICE_TABLES
    + LLM_USAGE_TABLES
    + IDENTITY_TABLES
    + CONVERSATION_TABLES
)

EXPECTED_CHECK_CONSTRAINTS = (
    "ck_index_profile_dimension_is_512",
    "ck_knowledge_base_kb_revision_non_negative",
    "ck_knowledge_base_acl_revision_non_negative",
    "ck_document_source_type",
    "ck_document_lifecycle_status",
    "ck_document_version_version_no_positive",
    "ck_document_version_status",
    "ck_ingest_job_status",
    "ck_ingest_job_attempt_non_negative",
    "ck_ingest_job_lease_consistent",
    "ck_outbox_event_status",
    "ck_outbox_event_dispatch_attempt_non_negative",
    "ck_outbox_event_lease_consistent",
    "ck_index_generation_status",
    "ck_index_generation_expected_chunks_non_negative",
    "ck_index_generation_actual_chunks_non_negative",
    "ck_index_generation_actual_chunks_within_expected",
    "ck_chunk_chunk_index_non_negative",
    "ck_chunk_text_non_empty",
    "ck_chunk_token_count_non_negative",
    "ck_llm_usage_status",
    "ck_llm_usage_usage_source",
    "ck_llm_usage_attempt_positive",
    "ck_llm_usage_prompt_tokens_non_negative",
    "ck_llm_usage_completion_tokens_non_negative",
    "ck_llm_usage_cache_hit_tokens_non_negative",
    "ck_llm_usage_cache_miss_tokens_non_negative",
    "ck_llm_usage_latency_ms_non_negative",
    "ck_llm_usage_cost_amount_non_negative",
    "ck_llm_usage_succeeded_requires_provider_usage",
    "ck_llm_usage_failure_has_error_code",
    "ck_llm_usage_price_consistent",
    "ck_kb_member_role",
    "ck_query_run_status",
    "ck_query_run_input_token_budget_positive",
    "ck_query_run_output_token_budget_positive",
    "ck_query_run_estimated_input_tokens_non_negative",
    "ck_query_run_evidence_count_non_negative",
    "ck_query_run_provider_prompt_tokens_non_negative",
    "ck_query_run_provider_completion_tokens_non_negative",
    "ck_query_run_question_non_empty",
    "ck_message_role",
    "ck_message_sequence_positive",
    "ck_citation_display_label_non_empty",
)

SECOND_SLICE_GRANTS = (
    "GRANT SELECT ON TABLE index_generation TO citemind_api;",
    "GRANT SELECT, INSERT, UPDATE ON TABLE index_generation TO citemind_worker;",
    "GRANT SELECT ON TABLE chunk TO citemind_api;",
    "GRANT SELECT, INSERT ON TABLE chunk TO citemind_worker;",
    "GRANT SELECT ON TABLE chunk_embedding TO citemind_api;",
    "GRANT SELECT, INSERT ON TABLE chunk_embedding TO citemind_worker;",
)
FIRST_SLICE_GRANT_COUNT = 11
LLM_USAGE_GRANTS = ("GRANT SELECT, INSERT ON TABLE llm_usage TO citemind_api;",)
IDENTITY_GRANTS = (
    "GRANT SELECT, INSERT, UPDATE ON TABLE user_account TO citemind_api;",
    "GRANT SELECT, INSERT, UPDATE ON TABLE auth_session TO citemind_api;",
    "GRANT SELECT, INSERT, UPDATE ON TABLE kb_member TO citemind_api;",
)
WORKER_KB_PUBLISH_GRANTS = (
    "GRANT UPDATE (active_index_profile_id, kb_revision) ON TABLE knowledge_base "
    "TO citemind_worker;",
)
CONVERSATION_GRANTS = tuple(
    f"GRANT SELECT, INSERT ON TABLE {table} TO citemind_api;"
    for table in CONVERSATION_TABLES
)


def alembic_config() -> Config:
    # 显式提供离线 DSN，避免离线 SQL 测试回退去读取仓库根 .env。
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", DEFAULT_DATABASE_URL.replace("%", "%%"))
    return config


@pytest.fixture(scope="module")
def script_directory() -> ScriptDirectory:
    return ScriptDirectory.from_config(alembic_config())


def test_migration_chain_has_a_single_linear_head(script_directory: ScriptDirectory) -> None:
    assert script_directory.get_heads() == [CONVERSATION_REVISION]
    assert script_directory.get_bases() == [PGVECTOR_REVISION]

    conversation = script_directory.get_revision(CONVERSATION_REVISION)
    request_title = script_directory.get_revision(INGEST_JOB_REQUEST_TITLE_REVISION)
    worker_kb_publish = script_directory.get_revision(WORKER_KB_PUBLISH_REVISION)
    ingest_job_profile = script_directory.get_revision(INGEST_JOB_PROFILE_REVISION)
    identity = script_directory.get_revision(IDENTITY_REVISION)
    llm_usage = script_directory.get_revision(LLM_USAGE_REVISION)
    second_slice = script_directory.get_revision(SECOND_SLICE_REVISION)
    core = script_directory.get_revision(CORE_REVISION)
    legacy = script_directory.get_revision(PGVECTOR_REVISION)

    assert conversation.down_revision == INGEST_JOB_REQUEST_TITLE_REVISION
    assert conversation.nextrev == set()
    assert request_title.down_revision == WORKER_KB_PUBLISH_REVISION
    assert request_title.nextrev == {CONVERSATION_REVISION}
    assert worker_kb_publish.down_revision == INGEST_JOB_PROFILE_REVISION
    assert worker_kb_publish.nextrev == {INGEST_JOB_REQUEST_TITLE_REVISION}
    assert ingest_job_profile.down_revision == IDENTITY_REVISION
    assert ingest_job_profile.nextrev == {WORKER_KB_PUBLISH_REVISION}
    assert identity.down_revision == LLM_USAGE_REVISION
    assert identity.nextrev == {INGEST_JOB_PROFILE_REVISION}
    assert llm_usage.down_revision == SECOND_SLICE_REVISION
    assert llm_usage.nextrev == {IDENTITY_REVISION}
    assert second_slice.down_revision == CORE_REVISION
    assert second_slice.nextrev == {LLM_USAGE_REVISION}
    assert core.down_revision == PGVECTOR_REVISION
    assert core.nextrev == {SECOND_SLICE_REVISION}
    assert legacy.down_revision is None
    assert legacy.nextrev == {CORE_REVISION}


def test_legacy_pgvector_migration_test_still_only_covers_its_own_revision() -> None:
    source = (REPO_ROOT / "tests" / "integration" / "test_pgvector_migration.py").read_text(
        encoding="utf-8"
    )

    assert f'PGVECTOR_REVISION = "{PGVECTOR_REVISION}"' in source


def test_online_upgrade_requires_migration_database_url_without_root_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只有 DATABASE_URL 时在线迁移必须在连接前失败，不回退也不读仓库根 .env。

    工作目录隔离到没有 .env 的临时目录；即使实现错误地去构造 Settings，也读不到仓库根
    .env。DATABASE_URL 使用自制无效 DSN：一旦意外尝试连接就会先报连接错误而非 CommandError。
    """

    monkeypatch.chdir(tmp_path)
    config = Config(str(ALEMBIC_INI))
    monkeypatch.delenv("MIGRATION_DATABASE_URL", raising=False)
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://invalid:invalid@127.0.0.1:1/citemind"
    )

    with pytest.raises(CommandError, match="MIGRATION_DATABASE_URL"):
        command.upgrade(config, "head")


def test_offline_upgrade_sql_covers_the_business_tables(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.upgrade(alembic_config(), "head", sql=True)
    output = capsys.readouterr().out

    for table in ALL_TABLES:
        assert f"CREATE TABLE {table} (" in output


def test_offline_downgrade_sql_drops_the_core_business_tables(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.downgrade(alembic_config(), f"{CORE_REVISION}:base", sql=True)
    output = capsys.readouterr().out

    for table in CORE_TABLES:
        assert f"DROP TABLE {table}" in output


def test_offline_downgrade_sql_removes_the_second_slice(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.downgrade(alembic_config(), f"{SECOND_SLICE_REVISION}:{CORE_REVISION}", sql=True)
    output = capsys.readouterr().out

    assert "DROP COLUMN generation_id" in output
    for table in ("chunk", "chunk_embedding", "index_generation"):
        assert f"DROP TABLE {table}" in output


def test_offline_upgrade_sql_has_vector_gin_partial_unique_and_ingest_job_fk(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.upgrade(alembic_config(), "head", sql=True)
    output = capsys.readouterr().out

    assert "embedding VECTOR(512) NOT NULL" in output
    assert "CREATE INDEX ix_chunk_fts ON chunk USING gin (fts);" in output
    assert "CREATE UNIQUE INDEX uq_index_generation_version_id_profile_id_ready" in output
    assert "WHERE status = 'READY';" in output
    assert (
        "ADD CONSTRAINT fk_ingest_job_generation_id_index_generation "
        "FOREIGN KEY(generation_id) REFERENCES index_generation (id)" in output
    )
    assert (
        "ADD CONSTRAINT fk_ingest_job_profile_id_index_profile "
        "FOREIGN KEY(profile_id) REFERENCES index_profile (id)" in output
    )


def test_offline_upgrade_sql_adds_only_a_nullable_profile_id_column(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.upgrade(
        alembic_config(), f"{IDENTITY_REVISION}:{INGEST_JOB_PROFILE_REVISION}", sql=True
    )
    output = capsys.readouterr().out

    assert "ALTER TABLE ingest_job ADD COLUMN profile_id UUID;" in output
    assert "ADD CONSTRAINT fk_ingest_job_profile_id_index_profile" in output
    # 不加 server default、不回填、不 seed、不建索引、不改授权。
    assert "DEFAULT" not in output
    assert "UPDATE ingest_job" not in output
    assert "CREATE INDEX" not in output
    assert "GRANT " not in output
    assert "REVOKE " not in output


def test_offline_downgrade_sql_removes_only_the_profile_id_fk_and_column(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.downgrade(
        alembic_config(), f"{INGEST_JOB_PROFILE_REVISION}:{IDENTITY_REVISION}", sql=True
    )
    output = capsys.readouterr().out

    assert "DROP CONSTRAINT fk_ingest_job_profile_id_index_profile" in output
    assert "DROP COLUMN profile_id" in output
    # 降级只删外键与列，不对其它表或 ACL 做任何事。
    assert "DROP TABLE" not in output
    assert "GRANT " not in output
    assert "REVOKE " not in output


def test_offline_upgrade_sql_adds_only_a_nullable_request_title_column(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.upgrade(
        alembic_config(),
        f"{WORKER_KB_PUBLISH_REVISION}:{INGEST_JOB_REQUEST_TITLE_REVISION}",
        sql=True,
    )
    output = capsys.readouterr().out

    assert "ALTER TABLE ingest_job ADD COLUMN request_title TEXT;" in output
    # 不加 server default、不回填、不建索引、不改授权。
    assert "DEFAULT" not in output
    assert "UPDATE ingest_job" not in output
    assert "CREATE INDEX" not in output
    assert "GRANT " not in output
    assert "REVOKE " not in output


def test_offline_downgrade_sql_removes_only_the_request_title_column(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.downgrade(
        alembic_config(),
        f"{INGEST_JOB_REQUEST_TITLE_REVISION}:{WORKER_KB_PUBLISH_REVISION}",
        sql=True,
    )
    output = capsys.readouterr().out

    assert "DROP COLUMN request_title" in output
    # 降级只删本列，不对其它表或 ACL 做任何事。
    assert "DROP TABLE" not in output
    assert "GRANT " not in output
    assert "REVOKE " not in output


def test_offline_upgrade_sql_grants_the_business_tables_exactly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.upgrade(alembic_config(), "head", sql=True)
    output = capsys.readouterr().out

    for table in SECOND_SLICE_TABLES:
        assert f"REVOKE ALL ON TABLE {table} FROM PUBLIC;" in output
        for statement in ("DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            assert f"GRANT {statement} ON TABLE {table}" not in output
    assert output.count("GRANT ") == (
        FIRST_SLICE_GRANT_COUNT
        + len(SECOND_SLICE_GRANTS)
        + len(LLM_USAGE_GRANTS)
        + len(IDENTITY_GRANTS)
        + len(WORKER_KB_PUBLISH_GRANTS)
        + len(CONVERSATION_GRANTS)
    )
    for grant in SECOND_SLICE_GRANTS:
        assert grant in output
    for grant in WORKER_KB_PUBLISH_GRANTS:
        assert grant in output


def test_offline_upgrade_sql_grants_only_kb_publish_columns_to_worker(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.upgrade(
        alembic_config(), f"{INGEST_JOB_PROFILE_REVISION}:{WORKER_KB_PUBLISH_REVISION}", sql=True
    )
    output = capsys.readouterr().out

    assert WORKER_KB_PUBLISH_GRANTS[0] in output
    # 不给全表 UPDATE，也不新增结构或其它对象的授权。
    assert "GRANT UPDATE ON TABLE knowledge_base" not in output
    assert "GRANT SELECT" not in output
    assert "GRANT INSERT" not in output
    assert "ALTER TABLE" not in output
    assert "CREATE " not in output
    assert "REVOKE " not in output


def test_offline_downgrade_sql_revokes_only_kb_publish_columns(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.downgrade(
        alembic_config(), f"{WORKER_KB_PUBLISH_REVISION}:{INGEST_JOB_PROFILE_REVISION}", sql=True
    )
    output = capsys.readouterr().out

    assert (
        "REVOKE UPDATE (active_index_profile_id, kb_revision) ON TABLE knowledge_base "
        "FROM citemind_worker;" in output
    )
    assert "DROP TABLE" not in output
    assert "ALTER TABLE" not in output
    assert "GRANT " not in output


def test_offline_upgrade_sql_grants_llm_usage_only_to_api(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.upgrade(alembic_config(), "head", sql=True)
    output = capsys.readouterr().out

    assert "REVOKE ALL ON TABLE llm_usage FROM PUBLIC;" in output
    for grant in LLM_USAGE_GRANTS:
        assert grant in output
    for statement in ("DELETE", "TRUNCATE", "REFERENCES", "TRIGGER", "UPDATE"):
        assert f"GRANT {statement} ON TABLE llm_usage" not in output
    assert "llm_usage TO citemind_worker" not in output
    assert "ON llm_usage" not in output


def test_offline_downgrade_sql_removes_llm_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.downgrade(alembic_config(), f"{LLM_USAGE_REVISION}:{SECOND_SLICE_REVISION}", sql=True)
    output = capsys.readouterr().out

    assert "DROP TABLE llm_usage;" in output


def test_offline_upgrade_sql_grants_identity_tables_only_to_api(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.upgrade(alembic_config(), "head", sql=True)
    output = capsys.readouterr().out

    for table in IDENTITY_TABLES:
        assert f"REVOKE ALL ON TABLE {table} FROM PUBLIC;" in output
        for statement in ("DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            assert f"GRANT {statement} ON TABLE {table}" not in output
        assert f"{table} TO citemind_worker" not in output
    for grant in IDENTITY_GRANTS:
        assert grant in output


def test_offline_downgrade_sql_removes_identity_tables(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.downgrade(alembic_config(), f"{IDENTITY_REVISION}:{LLM_USAGE_REVISION}", sql=True)
    output = capsys.readouterr().out

    for table in IDENTITY_TABLES:
        assert f"DROP TABLE {table};" in output


def test_offline_upgrade_sql_creates_conversation_tables_with_exact_grants(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.upgrade(
        alembic_config(),
        f"{INGEST_JOB_REQUEST_TITLE_REVISION}:{CONVERSATION_REVISION}",
        sql=True,
    )
    output = capsys.readouterr().out

    for table in CONVERSATION_TABLES:
        assert f"CREATE TABLE {table} (" in output
        assert f"REVOKE ALL ON TABLE {table} FROM PUBLIC;" in output
        assert f"GRANT SELECT, INSERT ON TABLE {table} TO citemind_api;" in output
        for statement in ("UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            assert f"GRANT {statement} ON TABLE {table}" not in output
        assert f"{table} TO citemind_worker" not in output
    assert "ARRAY" not in output
    assert "SERIAL" not in output.upper()
    assert "CREATE SEQUENCE" not in output.upper()
    assert "NEXTVAL" not in output.upper()


def test_offline_downgrade_sql_removes_conversation_tables(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.downgrade(
        alembic_config(),
        f"{CONVERSATION_REVISION}:{INGEST_JOB_REQUEST_TITLE_REVISION}",
        sql=True,
    )
    output = capsys.readouterr().out

    for table in CONVERSATION_TABLES:
        assert f"DROP TABLE {table};" in output
    assert "GRANT " not in output
    assert "REVOKE " not in output


def test_offline_upgrade_sql_has_no_ann_indexes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.upgrade(alembic_config(), "head", sql=True)
    output = capsys.readouterr().out.lower()

    assert "hnsw" not in output
    assert "ivfflat" not in output
    assert "create index" in output


def test_offline_upgrade_sql_names_every_check_constraint_once(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.upgrade(alembic_config(), "head", sql=True)
    output = capsys.readouterr().out

    for name in EXPECTED_CHECK_CONSTRAINTS:
        assert output.count(f"CONSTRAINT {name} CHECK") == 1, f"{name} 的具名 CHECK 缺失或重复"
    # 共享 naming convention 会补 ck_<table>_ 前缀，迁移不能再传全名，否则会双前缀。
    for table in ALL_TABLES:
        assert f"ck_{table}_ck_{table}_" not in output
