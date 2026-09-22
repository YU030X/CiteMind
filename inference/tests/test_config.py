"""inference 配置校验：token 必填非空、生产拒绝占位值、且不泄露明文。"""

import pytest
from pydantic import SecretStr, ValidationError

from citemind_inference.config import DEVELOPMENT_INFERENCE_TOKEN, Settings


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

    assert "CITEMIND_INFERENCE_TOKEN" in str(error_info.value)
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
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        inference_token=SecretStr("super-secret-token"),
    )

    assert "super-secret-token" not in repr(settings)
    assert "super-secret-token" not in str(settings)
