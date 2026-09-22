"""业务模型的纯逻辑契约检查：不连接数据库。"""

import uuid
from typing import Any, cast

import pytest
import sqlalchemy as sa
from evidencehub.models import metadata
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

FIRST_SLICE_TABLES = {
    "index_profile",
    "knowledge_base",
    "document",
    "document_version",
    "ingest_job",
    "outbox_event",
}
SECOND_SLICE_TABLES = {"index_generation", "chunk", "chunk_embedding"}
EXPECTED_TABLES = FIRST_SLICE_TABLES | SECOND_SLICE_TABLES

# chunk_embedding 的主键来自 chunk，不是应用新生成的 UUID。
UUID_PK_TABLES = EXPECTED_TABLES - {"chunk_embedding"}
CREATED_AT_TABLES = EXPECTED_TABLES - {"chunk_embedding"}
UPDATED_AT_TABLES = {
    "knowledge_base",
    "document",
    "document_version",
    "ingest_job",
    "outbox_event",
    "index_generation",
}

EXPECTED_NAMED_CONSTRAINTS = {
    "index_profile": {
        "pk_index_profile",
        "uq_index_profile_config_hash",
        "ck_index_profile_dimension_is_512",
    },
    "knowledge_base": {
        "pk_knowledge_base",
        "fk_knowledge_base_active_index_profile_id_index_profile",
        "ck_knowledge_base_kb_revision_non_negative",
        "ck_knowledge_base_acl_revision_non_negative",
    },
    "document": {
        "pk_document",
        "fk_document_kb_id_knowledge_base",
        "fk_document_active_version_id_document_version",
        "ck_document_source_type",
        "ck_document_lifecycle_status",
    },
    "document_version": {
        "pk_document_version",
        "fk_document_version_document_id_document",
        "uq_document_version_document_id_version_no",
        "ck_document_version_version_no_positive",
        "ck_document_version_status",
    },
    "ingest_job": {
        "pk_ingest_job",
        "fk_ingest_job_document_id_document",
        "fk_ingest_job_version_id_document_version",
        "fk_ingest_job_generation_id_index_generation",
        "uq_ingest_job_dedupe_key",
        "ck_ingest_job_status",
        "ck_ingest_job_attempt_non_negative",
        "ck_ingest_job_lease_consistent",
    },
    "outbox_event": {
        "pk_outbox_event",
        "fk_outbox_event_job_id_ingest_job",
        "ck_outbox_event_status",
        "ck_outbox_event_dispatch_attempt_non_negative",
        "ck_outbox_event_lease_consistent",
    },
    "index_generation": {
        "pk_index_generation",
        "fk_index_generation_version_id_document_version",
        "fk_index_generation_profile_id_index_profile",
        "ck_index_generation_status",
        "ck_index_generation_expected_chunks_non_negative",
        "ck_index_generation_actual_chunks_non_negative",
        "ck_index_generation_actual_chunks_within_expected",
    },
    "chunk": {
        "pk_chunk",
        "fk_chunk_generation_id_index_generation",
        "fk_chunk_kb_id_knowledge_base",
        "fk_chunk_document_id_document",
        "fk_chunk_version_id_document_version",
        "uq_chunk_generation_id_chunk_index",
        "ck_chunk_chunk_index_non_negative",
        "ck_chunk_text_non_empty",
        "ck_chunk_token_count_non_negative",
    },
    "chunk_embedding": {
        "pk_chunk_embedding",
        "fk_chunk_embedding_chunk_id_chunk",
        "fk_chunk_embedding_profile_id_index_profile",
    },
}

EXPECTED_INDEXES = {
    "document": {"ix_document_kb_id_lifecycle_status"},
    "ingest_job": {"ix_ingest_job_status_next_run_at"},
    "outbox_event": {"ix_outbox_event_status_next_send_at"},
    "index_generation": {
        "ix_index_generation_version_id_profile_id_status",
        "uq_index_generation_version_id_profile_id_ready",
    },
    "chunk": {"ix_chunk_generation_id", "ix_chunk_fts"},
}


def test_metadata_contains_the_nine_business_tables() -> None:
    assert set(metadata.tables) == EXPECTED_TABLES


@pytest.mark.parametrize("table_name", sorted(UUID_PK_TABLES))
def test_primary_key_is_application_generated_uuid(table_name: str) -> None:
    table = metadata.tables[table_name]
    primary_key_columns = list(table.primary_key.columns)

    assert len(primary_key_columns) == 1
    column = primary_key_columns[0]
    assert isinstance(column.type, sa.Uuid)
    # 数据库不提供 UUID server default；应用层 uuid4 是唯一来源。
    assert column.server_default is None
    assert column.default is not None
    assert column.default.is_callable
    assert isinstance(cast(Any, column.default).arg(None), uuid.UUID)


def test_chunk_embedding_primary_key_is_the_chunk_foreign_key() -> None:
    table = metadata.tables["chunk_embedding"]
    primary_key_columns = list(table.primary_key.columns)

    assert [column.name for column in primary_key_columns] == ["chunk_id"]
    assert primary_key_columns[0].default is None
    foreign_key = next(iter(primary_key_columns[0].foreign_keys))
    assert foreign_key.column.table.name == "chunk"


@pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES))
def test_no_sequences_or_identity_columns(table_name: str) -> None:
    table = metadata.tables[table_name]

    for column in table.columns:
        assert column.identity is None
        assert not isinstance(column.server_default, sa.Sequence)
        assert not isinstance(column.default, sa.Sequence)
        assert not isinstance(column.type, sa.Enum)


@pytest.mark.parametrize("table_name", sorted(CREATED_AT_TABLES))
def test_timestamps_are_timezone_aware(table_name: str) -> None:
    table = metadata.tables[table_name]
    timestamp_columns = [
        column for column in table.columns if isinstance(column.type, sa.DateTime)
    ]

    assert timestamp_columns, f"{table_name} 缺少时间列"
    for column in timestamp_columns:
        assert cast(sa.DateTime, column.type).timezone is True, (
            f"{table_name}.{column.name} 必须带时区"
        )
        assert column.nullable == (column.server_default is None), (
            f"{table_name}.{column.name} 的可空性与 server_default 不匹配"
        )


@pytest.mark.parametrize("table_name", sorted(CREATED_AT_TABLES))
def test_created_at_has_server_default_now(table_name: str) -> None:
    assert metadata.tables[table_name].columns["created_at"].server_default is not None


@pytest.mark.parametrize("table_name", sorted(UPDATED_AT_TABLES))
def test_updated_at_has_server_default_now(table_name: str) -> None:
    assert metadata.tables[table_name].columns["updated_at"].server_default is not None


@pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES))
def test_named_constraints_match_the_frozen_contract(table_name: str) -> None:
    table = metadata.tables[table_name]
    names = {constraint.name for constraint in table.constraints}

    assert names == EXPECTED_NAMED_CONSTRAINTS[table_name]


def test_constraint_names_follow_the_shared_naming_convention() -> None:
    for table in metadata.tables.values():
        for constraint in table.constraints:
            assert constraint.name is not None
            prefix = {
                sa.PrimaryKeyConstraint: "pk_",
                sa.ForeignKeyConstraint: "fk_",
                sa.UniqueConstraint: "uq_",
                sa.CheckConstraint: "ck_",
            }[type(constraint)]
            assert str(constraint.name).startswith(prefix)


def test_indexes_match_the_frozen_contract() -> None:
    for table_name, table in metadata.tables.items():
        expected = EXPECTED_INDEXES.get(table_name, set())
        assert {index.name for index in table.indexes} == expected


def test_foreign_keys_default_to_restrict() -> None:
    for table in metadata.tables.values():
        for foreign_key in table.foreign_keys:
            constraint = foreign_key.constraint
            assert constraint is not None
            assert foreign_key.ondelete == (
                "SET NULL"
                if constraint.name == "fk_document_active_version_id_document_version"
                else "RESTRICT"
            )
            assert foreign_key.onupdate == "RESTRICT"


def test_ingest_job_lease_check_covers_all_three_lease_columns() -> None:
    table = metadata.tables["ingest_job"]
    (check,) = [
        constraint
        for constraint in table.constraints
        if constraint.name == "ck_ingest_job_lease_consistent"
    ]
    assert isinstance(check, sa.CheckConstraint)
    sql_text = str(check.sqltext)

    for column in ("lease_owner", "lease_token", "lease_until"):
        assert column in sql_text


def test_ingest_job_generation_id_is_a_nullable_restrict_foreign_key() -> None:
    column = metadata.tables["ingest_job"].columns["generation_id"]

    assert column.nullable is True
    foreign_key = next(iter(column.foreign_keys))
    assert foreign_key.column.table.name == "index_generation"
    assert foreign_key.ondelete == "RESTRICT"


def test_chunk_embedding_vector_dimension_is_fixed_at_512() -> None:
    column = metadata.tables["chunk_embedding"].columns["embedding"]

    assert column.nullable is False
    assert isinstance(column.type, Vector)
    assert column.type.dim == 512


def test_chunk_jsonb_and_tsvector_columns() -> None:
    table = metadata.tables["chunk"]

    assert isinstance(table.columns["heading_path"].type, JSONB)
    assert isinstance(table.columns["source_locator"].type, JSONB)
    assert isinstance(table.columns["fts"].type, TSVECTOR)


def test_chunk_embedding_has_only_the_frozen_columns() -> None:
    table = metadata.tables["chunk_embedding"]

    assert [column.name for column in table.columns] == [
        "chunk_id",
        "profile_id",
        "embedding",
    ]


def test_vector_columns_only_exist_on_chunk_embedding() -> None:
    for table in metadata.tables.values():
        for column in table.columns:
            if table.name == "chunk_embedding" and column.name == "embedding":
                continue
            assert not isinstance(column.type, Vector)


def test_index_generation_partial_unique_index_predicate() -> None:
    (index,) = [
        index
        for index in metadata.tables["index_generation"].indexes
        if index.name == "uq_index_generation_version_id_profile_id_ready"
    ]

    assert index.unique is True
    assert [column.name for column in index.columns] == ["version_id", "profile_id"]
    where = str(index.dialect_options["postgresql"]["where"])
    assert "status" in where
    assert "READY" in where


def test_no_ann_indexes_are_defined() -> None:
    for table in metadata.tables.values():
        for index in table.indexes:
            using = index.dialect_options["postgresql"].get("using")
            assert using not in {"hnsw", "ivfflat"}, f"{table.name}.{index.name} 不应是 ANN 索引"


def test_chunk_business_columns_are_not_nullable() -> None:
    for column in metadata.tables["chunk"].columns:
        assert column.nullable is False, column.name
