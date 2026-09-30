"""业务模型的纯逻辑契约检查：不连接数据库。"""

import uuid
from typing import Any, cast

import pytest
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from rag_backend.models import metadata
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
LLM_USAGE_TABLES = {"llm_usage"}
IDENTITY_TABLES = {"user_account", "auth_session", "kb_member"}
CONVERSATION_TABLES = {"conversation", "message", "query_run", "citation"}
DOCUMENT_ACL_TABLES = {"document_acl"}
EXPECTED_TABLES = (
    FIRST_SLICE_TABLES
    | SECOND_SLICE_TABLES
    | LLM_USAGE_TABLES
    | IDENTITY_TABLES
    | CONVERSATION_TABLES
    | DOCUMENT_ACL_TABLES
)

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
    "user_account",
    "auth_session",
    "kb_member",
    "conversation",
}

# ``expires_at`` 由应用按会话 TTL 显式写入，是唯一非空但没有 server_default 的时间列。
EXPLICIT_TIMESTAMPS_WITHOUT_SERVER_DEFAULT = {"expires_at"}

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
        "ck_document_acl_mode",
    },
    "document_acl": {
        "pk_document_acl",
        "fk_document_acl_document_id_document",
        "fk_document_acl_principal_id_user_account",
        "uq_document_acl_document_principal_permission",
        "ck_document_acl_principal_type",
        "ck_document_acl_permission",
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
        "fk_ingest_job_profile_id_index_profile",
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
    "llm_usage": {
        "pk_llm_usage",
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
    },
    "user_account": {
        "pk_user_account",
        "uq_user_account_organization_id_username",
    },
    "auth_session": {
        "pk_auth_session",
        "fk_auth_session_user_id_user_account",
        "uq_auth_session_token_hash",
    },
    "kb_member": {
        "pk_kb_member",
        "fk_kb_member_kb_id_knowledge_base",
        "fk_kb_member_user_id_user_account",
        "uq_kb_member_kb_id_user_id",
        "ck_kb_member_role",
    },
    "conversation": {
        "pk_conversation",
        "fk_conversation_owner_id_user_account",
    },
    "query_run": {
        "pk_query_run",
        "fk_query_run_conversation_id_conversation",
        "fk_query_run_llm_usage_id_llm_usage",
        "ck_query_run_status",
        "ck_query_run_input_token_budget_positive",
        "ck_query_run_output_token_budget_positive",
        "ck_query_run_estimated_input_tokens_non_negative",
        "ck_query_run_evidence_count_non_negative",
        "ck_query_run_provider_prompt_tokens_non_negative",
        "ck_query_run_provider_completion_tokens_non_negative",
        "ck_query_run_question_non_empty",
        "ck_query_run_generation_options_object",
    },
    "message": {
        "pk_message",
        "fk_message_conversation_id_conversation",
        "fk_message_query_run_id_query_run",
        "uq_message_conversation_id_sequence",
        "ck_message_role",
        "ck_message_sequence_positive",
    },
    "citation": {
        "pk_citation",
        "fk_citation_message_id_message",
        "fk_citation_query_run_id_query_run",
        "fk_citation_chunk_id_chunk",
        "fk_citation_version_id_document_version",
        "uq_citation_message_id_display_label",
        "ck_citation_display_label_non_empty",
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
    "chunk": {"ix_chunk_generation_id", "ix_chunk_model_input_hash", "ix_chunk_fts"},
    "llm_usage": {"ix_llm_usage_query_run_id"},
}


def test_metadata_contains_the_expected_business_tables() -> None:
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
        if column.name in EXPLICIT_TIMESTAMPS_WITHOUT_SERVER_DEFAULT:
            assert column.nullable is False
            assert column.server_default is None
            continue
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


def test_ingest_job_profile_id_is_a_nullable_restrict_foreign_key() -> None:
    column = metadata.tables["ingest_job"].columns["profile_id"]

    assert column.nullable is True
    assert column.server_default is None
    assert column.default is None
    foreign_key = next(iter(column.foreign_keys))
    assert foreign_key.column.table.name == "index_profile"
    assert foreign_key.ondelete == "RESTRICT"
    assert foreign_key.onupdate == "RESTRICT"
    assert foreign_key.constraint is not None
    assert foreign_key.constraint.name == "fk_ingest_job_profile_id_index_profile"


def test_ingest_job_request_title_is_a_nullable_text_snapshot() -> None:
    """``request_title`` 是可空的受理快照列：无 server default、旧行为 NULL。"""

    column = metadata.tables["ingest_job"].columns["request_title"]

    assert column.nullable is True
    assert isinstance(column.type, sa.Text)
    assert column.server_default is None


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


def test_llm_usage_has_only_the_frozen_columns() -> None:
    table = metadata.tables["llm_usage"]

    assert [column.name for column in table.columns] == [
        "id",
        "query_run_id",
        "provider",
        "model",
        "stage",
        "status",
        "error_code",
        "usage_source",
        "attempt",
        "prompt_tokens",
        "completion_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
        "latency_ms",
        "price_snapshot",
        "price_source",
        "price_currency",
        "cost_amount",
        "created_at",
    ]


def test_llm_usage_optional_columns_are_nullable() -> None:
    table = metadata.tables["llm_usage"]

    for name in (
        "query_run_id",
        "error_code",
        "prompt_tokens",
        "completion_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
        "latency_ms",
        "price_snapshot",
        "price_source",
        "price_currency",
        "cost_amount",
    ):
        assert table.columns[name].nullable is True, name

    for name in ("provider", "model", "stage", "status", "usage_source", "attempt"):
        assert table.columns[name].nullable is False, name


def test_llm_usage_cost_amount_is_fixed_precision_numeric() -> None:
    column = metadata.tables["llm_usage"].columns["cost_amount"]

    assert isinstance(column.type, sa.Numeric)
    assert column.type.precision == 18
    assert column.type.scale == 8


def _llm_usage_check(name: str) -> sa.CheckConstraint:
    (check,) = [
        constraint
        for constraint in metadata.tables["llm_usage"].constraints
        if constraint.name == f"ck_llm_usage_{name}"
    ]
    assert isinstance(check, sa.CheckConstraint)
    return check


def test_llm_usage_success_requires_provider_reported_usage() -> None:
    sql_text = str(_llm_usage_check("succeeded_requires_provider_usage").sqltext)

    assert "SUCCEEDED" in sql_text
    assert "PROVIDER_REPORTED" in sql_text
    assert "prompt_tokens IS NOT NULL" in sql_text
    assert "completion_tokens IS NOT NULL" in sql_text


def test_llm_usage_failure_requires_an_error_code() -> None:
    sql_text = str(_llm_usage_check("failure_has_error_code").sqltext)

    assert "SUCCEEDED" in sql_text
    assert "error_code IS NOT NULL" in sql_text


def test_llm_usage_price_snapshot_fields_are_all_or_none() -> None:
    sql_text = str(_llm_usage_check("price_consistent").sqltext)

    for column in ("price_source", "price_currency", "cost_amount"):
        assert column in sql_text


def test_llm_usage_status_and_usage_source_are_enumerated() -> None:
    status_sql = str(_llm_usage_check("status").sqltext)
    source_sql = str(_llm_usage_check("usage_source").sqltext)

    assert "SUCCEEDED" in status_sql
    assert "FAILED" in status_sql
    assert "TIMEOUT" in status_sql
    assert "PROVIDER_REPORTED" in source_sql
    assert "UNKNOWN" in source_sql


def _unique_columns(table_name: str, constraint_name: str) -> list[str]:
    table = metadata.tables[table_name]
    (constraint,) = [
        constraint for constraint in table.constraints if constraint.name == constraint_name
    ]
    assert isinstance(constraint, sa.UniqueConstraint)
    return [column.name for column in constraint.columns]


def test_user_account_has_only_the_frozen_columns() -> None:
    table = metadata.tables["user_account"]

    assert [column.name for column in table.columns] == [
        "id",
        "organization_id",
        "username",
        "password_hash",
        "enabled",
        "is_admin",
        "created_at",
        "updated_at",
    ]


def test_user_account_username_is_unique_within_organization() -> None:
    columns = _unique_columns(
        "user_account", "uq_user_account_organization_id_username"
    )

    assert columns == ["organization_id", "username"]


def test_auth_session_has_only_the_frozen_columns() -> None:
    table = metadata.tables["auth_session"]

    assert [column.name for column in table.columns] == [
        "id",
        "user_id",
        "token_hash",
        "csrf_token_hash",
        "expires_at",
        "revoked_at",
        "created_at",
        "updated_at",
    ]


def test_auth_session_token_hash_is_unique() -> None:
    columns = _unique_columns("auth_session", "uq_auth_session_token_hash")

    assert columns == ["token_hash"]


def test_auth_session_required_columns_and_soft_revoke() -> None:
    table = metadata.tables["auth_session"]

    for name in ("user_id", "token_hash", "csrf_token_hash", "expires_at"):
        assert table.columns[name].nullable is False, name
    assert table.columns["revoked_at"].nullable is True
    assert table.columns["revoked_at"].server_default is None


def test_kb_member_has_only_the_frozen_columns() -> None:
    table = metadata.tables["kb_member"]

    assert [column.name for column in table.columns] == [
        "id",
        "kb_id",
        "user_id",
        "role",
        "revoked_at",
        "created_at",
        "updated_at",
    ]


def test_kb_member_kb_and_user_are_unique() -> None:
    columns = _unique_columns("kb_member", "uq_kb_member_kb_id_user_id")

    assert columns == ["kb_id", "user_id"]


def test_kb_member_role_is_enumerated() -> None:
    table = metadata.tables["kb_member"]
    (check,) = [
        constraint
        for constraint in table.constraints
        if constraint.name == "ck_kb_member_role"
    ]
    assert isinstance(check, sa.CheckConstraint)
    sql_text = str(check.sqltext)

    for role in ("OWNER", "EDITOR", "READER"):
        assert role in sql_text


def test_kb_member_required_columns_and_soft_revoke() -> None:
    table = metadata.tables["kb_member"]

    for name in ("kb_id", "user_id", "role"):
        assert table.columns[name].nullable is False, name
    assert table.columns["revoked_at"].nullable is True
    assert table.columns["revoked_at"].server_default is None
