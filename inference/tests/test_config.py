"""inference 配置校验：token 必填、embedding 上限与交叉约束、且不泄露明文。"""

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError
from support import build_settings

from citemind_inference.config import (
    DEVELOPMENT_INFERENCE_TOKEN,
    EMBEDDING_MAX_TOKENS,
    FROZEN_EMBEDDING_REVISION,
    Settings,
    derived_request_byte_limit,
)

# ---------------------------------------------------------------- token


def test_token_is_required() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_empty_token_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, inference_token=SecretStr(""))  # type: ignore[call-arg]


def test_development_placeholder_is_allowed_in_development() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        environment="development",
        inference_token=SecretStr(DEVELOPMENT_INFERENCE_TOKEN),
    )

    assert settings.inference_token.get_secret_value() == DEVELOPMENT_INFERENCE_TOKEN


def test_production_rejects_development_placeholder_and_names_the_variable() -> None:
    with pytest.raises(ValidationError) as error_info:
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            environment="production",
            inference_token=SecretStr(DEVELOPMENT_INFERENCE_TOKEN),
        )

    assert "INFERENCE_TOKEN" in str(error_info.value)
    # hide_input_in_errors 应确保校验错误不回显 token 明文。
    assert DEVELOPMENT_INFERENCE_TOKEN not in str(error_info.value)


def test_production_accepts_independent_token() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        environment="production",
        inference_token=SecretStr("production-secret"),
    )

    assert settings.inference_token.get_secret_value() == "production-secret"


def test_repr_and_str_do_not_leak_token() -> None:
    settings = build_settings(inference_token=SecretStr("super-secret-token"))

    assert "super-secret-token" not in repr(settings)
    assert "super-secret-token" not in str(settings)


# ---------------------------------------------------------------- 默认值


def test_defaults_match_the_frozen_encoding_contract() -> None:
    settings = build_settings()

    assert settings.embedding_model_revision == FROZEN_EMBEDDING_REVISION
    assert settings.embedding_max_tokens_per_text == EMBEDDING_MAX_TOKENS
    assert settings.embedding_max_batch_size >= 1
    assert settings.embedding_max_concurrency >= 1
    assert settings.embedding_torch_threads >= 1
    assert settings.embedding_queue_wait_seconds > 0
    assert settings.embedding_queue_depth >= 0
    assert settings.embedding_max_request_bytes is None
    assert settings.request_byte_limit > settings.embedding_max_total_bytes


@pytest.mark.parametrize(
    "overrides",
    [
        {"embedding_max_batch_size": 0},
        {"embedding_max_total_bytes": 0},
        {"embedding_max_chars_per_text": 0},
        {"embedding_max_tokens_per_text": 0},
        {"embedding_max_total_tokens": 0},
        {"embedding_max_concurrency": 0},
        {"embedding_torch_threads": 0},
        {"embedding_max_request_bytes": 0},
        {"embedding_queue_depth": -1},
    ],
)
def test_non_positive_limits_are_rejected(overrides: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        build_settings(**overrides)


def test_zero_queue_depth_is_allowed_but_no_waiting_is_not() -> None:
    settings = build_settings(embedding_queue_depth=0)

    assert settings.embedding_queue_depth == 0


def test_tokens_above_model_limit_are_rejected() -> None:
    with pytest.raises(ValidationError) as error_info:
        build_settings(
            embedding_max_tokens_per_text=EMBEDDING_MAX_TOKENS + 1,
            embedding_max_total_tokens=EMBEDDING_MAX_TOKENS + 1,
        )

    assert "EMBEDDING_MAX_TOKENS_PER_TEXT" in str(error_info.value)


def test_chars_above_total_bytes_are_rejected() -> None:
    with pytest.raises(ValidationError) as error_info:
        build_settings(embedding_max_chars_per_text=2048, embedding_max_total_bytes=1024)

    assert "EMBEDDING_MAX_CHARS_PER_TEXT" in str(error_info.value)


def test_total_tokens_below_per_text_limit_is_rejected() -> None:
    with pytest.raises(ValidationError) as error_info:
        build_settings(embedding_max_total_tokens=256, embedding_max_tokens_per_text=512)

    assert "EMBEDDING_MAX_TOTAL_TOKENS" in str(error_info.value)


def test_total_tokens_equal_to_per_text_limit_is_allowed() -> None:
    settings = build_settings(embedding_max_total_tokens=512, embedding_max_tokens_per_text=512)

    assert settings.embedding_max_total_tokens == 512


@pytest.mark.parametrize(
    "revision",
    [
        "main",
        "7999e1d",
        "7999E1D3359715C523056EF9478215996D62A620",
        "not-a-sha",
        "",
        # 格式合法但并非冻结 revision 的 sha 也必须拒绝。
        "0" * 40,
        "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
    ],
)
def test_only_the_frozen_revision_is_accepted(revision: str) -> None:
    with pytest.raises(ValidationError) as error_info:
        build_settings(embedding_model_revision=revision)

    assert "EMBEDDING_MODEL_REVISION" in str(error_info.value)


def test_frozen_revision_is_the_default_and_accepted() -> None:
    settings = build_settings(embedding_model_revision=FROZEN_EMBEDDING_REVISION)

    assert settings.embedding_model_revision == FROZEN_EMBEDDING_REVISION


@pytest.mark.parametrize("wait", [0.0, -1.0, -0.5, float("nan"), float("inf"), float("-inf")])
def test_queue_wait_must_be_finite_and_positive(wait: float) -> None:
    with pytest.raises(ValidationError):
        build_settings(embedding_queue_wait_seconds=wait)


def test_queue_wait_accepts_a_small_positive_value() -> None:
    settings = build_settings(embedding_queue_wait_seconds=0.01)

    assert settings.embedding_queue_wait_seconds == 0.01


def test_request_byte_limit_must_cover_the_text_byte_budget() -> None:
    with pytest.raises(ValidationError) as error_info:
        build_settings(
            embedding_max_total_bytes=4096,
            embedding_max_chars_per_text=512,
            embedding_max_request_bytes=1024,
        )

    assert "EMBEDDING_MAX_REQUEST_BYTES" in str(error_info.value)


def test_request_byte_limit_defaults_from_the_text_budget() -> None:
    # 调紧文本预算必须同时收紧传输预算，否则小预算配置仍会先缓存大请求体。
    tight = build_settings(embedding_max_total_bytes=32, embedding_max_chars_per_text=32)
    roomy = build_settings()

    assert tight.request_byte_limit == derived_request_byte_limit(32)
    assert tight.request_byte_limit < 10000
    assert roomy.request_byte_limit == derived_request_byte_limit(roomy.embedding_max_total_bytes)


def test_explicit_request_byte_limit_is_used_verbatim() -> None:
    settings = build_settings(
        embedding_max_total_bytes=4096,
        embedding_max_chars_per_text=512,
        embedding_max_request_bytes=65536,
    )

    assert settings.request_byte_limit == 65536
    assert settings.request_byte_limit != derived_request_byte_limit(4096)


def test_environment_overrides_take_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_MAX_BATCH_SIZE", "3")
    monkeypatch.setenv("EMBEDDING_TORCH_THREADS", "4")

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        environment="test",
        inference_token=SecretStr("env-token"),
    )

    assert settings.embedding_max_batch_size == 3
    assert settings.embedding_torch_threads == 4


# ---------------------------------------------------------------- 旧 CITEMIND_* 前缀


def test_legacy_citemind_process_env_var_is_rejected_without_leaking_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CITEMIND_INFERENCE_TOKEN", "legacy-process-token")

    with pytest.raises(ValueError) as error_info:
        build_settings()

    message = str(error_info.value)
    assert "CITEMIND_INFERENCE_TOKEN" in message
    assert "legacy-process-token" not in message


def test_unrelated_legacy_citemind_var_is_still_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "CITEMIND_DATABASE_URL", "postgresql+psycopg://user:db-secret@127.0.0.1/legacy"
    )

    with pytest.raises(ValueError) as error_info:
        build_settings()

    message = str(error_info.value)
    assert "CITEMIND_DATABASE_URL" in message
    assert "db-secret" not in message


def test_legacy_citemind_field_var_is_rejected_case_insensitively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("citemind_embedding_torch_threads", "4")

    with pytest.raises(ValueError) as error_info:
        build_settings()

    assert "CITEMIND_EMBEDDING_TORCH_THREADS" in str(error_info.value)


def test_legacy_citemind_dotenv_var_is_rejected_without_leaking_value(tmp_path: Path) -> None:
    env_file = tmp_path / "legacy.env"
    env_file.write_text("CITEMIND_INFERENCE_TOKEN=legacy-dotenv-token\n", encoding="utf-8")

    with pytest.raises(ValueError) as error_info:
        Settings(_env_file=env_file, environment="test", inference_token=SecretStr("x"))  # type: ignore[call-arg]

    message = str(error_info.value)
    assert "CITEMIND_INFERENCE_TOKEN" in message
    assert "legacy-dotenv-token" not in message


def test_bare_dotenv_names_are_loaded(tmp_path: Path) -> None:
    env_file = tmp_path / "bare.env"
    env_file.write_text(
        "ENVIRONMENT=test\nINFERENCE_TOKEN=dotenv-token\nEMBEDDING_TORCH_THREADS=7\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]

    assert settings.environment == "test"
    assert settings.inference_token.get_secret_value() == "dotenv-token"
    assert settings.embedding_torch_threads == 7
