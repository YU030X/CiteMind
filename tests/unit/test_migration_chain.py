"""迁移链与离线 SQL 的纯逻辑检查：不连接数据库。"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
PGVECTOR_REVISION = "20260921_0001"
CORE_REVISION = "20260922_0002"
SECOND_SLICE_REVISION = "20260922_0003"

CORE_TABLES = (
    "index_profile",
    "knowledge_base",
    "document",
    "document_version",
    "ingest_job",
    "outbox_event",
)
SECOND_SLICE_TABLES = ("index_generation", "chunk", "chunk_embedding")
ALL_TABLES = CORE_TABLES + SECOND_SLICE_TABLES

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


def alembic_config() -> Config:
    return Config(str(ALEMBIC_INI))


@pytest.fixture(scope="module")
def script_directory() -> ScriptDirectory:
    return ScriptDirectory.from_config(alembic_config())


def test_migration_chain_has_a_single_linear_head(script_directory: ScriptDirectory) -> None:
    assert script_directory.get_heads() == [SECOND_SLICE_REVISION]
    assert script_directory.get_bases() == [PGVECTOR_REVISION]

    core = script_directory.get_revision(CORE_REVISION)
    second_slice = script_directory.get_revision(SECOND_SLICE_REVISION)
    legacy = script_directory.get_revision(PGVECTOR_REVISION)

    assert second_slice.down_revision == CORE_REVISION
    assert core.down_revision == PGVECTOR_REVISION
    assert core.nextrev == {SECOND_SLICE_REVISION}
    assert legacy.down_revision is None
    assert legacy.nextrev == {CORE_REVISION}


def test_legacy_pgvector_migration_test_still_only_covers_its_own_revision() -> None:
    source = (REPO_ROOT / "tests" / "integration" / "test_pgvector_migration.py").read_text(
        encoding="utf-8"
    )

    assert f'PGVECTOR_REVISION = "{PGVECTOR_REVISION}"' in source


def test_offline_upgrade_sql_covers_the_nine_business_tables(
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


def test_offline_upgrade_sql_grants_the_second_slice_exactly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command.upgrade(alembic_config(), "head", sql=True)
    output = capsys.readouterr().out

    for table in SECOND_SLICE_TABLES:
        assert f"REVOKE ALL ON TABLE {table} FROM PUBLIC;" in output
        for statement in ("DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            assert f"GRANT {statement} ON TABLE {table}" not in output
    assert output.count("GRANT ") == FIRST_SLICE_GRANT_COUNT + len(SECOND_SLICE_GRANTS)
    for grant in SECOND_SLICE_GRANTS:
        assert grant in output


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
