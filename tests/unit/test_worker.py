"""worker 配置与 probe marker 的纯逻辑测试：不连接 Redis。"""

import json
from pathlib import Path
from typing import Any

import pytest
from celery import Celery
from kombu.exceptions import OperationalError
from pydantic import ValidationError
from rag_backend.config import DEFAULT_REDIS_PASSWORD, Settings
from rag_backend.worker import (
    BROKER_VISIBILITY_TIMEOUT_SECONDS,
    PROBE_TASK_NAME,
    create_celery_app,
    probe_marker_path,
    require_redis_url,
    write_probe_marker,
)

REDIS_URL = "redis://:secret@127.0.0.1:56379/0"
PRODUCTION_DATABASE_URL = "postgresql+psycopg://citemind_app:strong-password@postgres:5432/citemind"


def settings(**overrides: Any) -> Settings:
    """构造不读取本地 .env 的配置，避免测试受开发环境变量影响。"""

    values: dict[str, Any] = {"_env_file": None, "environment": "test"}
    values.update(overrides)
    return Settings(**values)


def test_settings_accepts_redis_broker_url() -> None:
    assert settings(redis_url=REDIS_URL).redis_url == REDIS_URL


def test_settings_leaves_redis_url_optional_for_the_api_process() -> None:
    assert settings().redis_url is None


@pytest.mark.parametrize(
    "redis_url",
    [
        "http://:secret@127.0.0.1:56379/0",
        "redis+unix:///tmp/redis.sock",
        "postgresql://citemind:secret@127.0.0.1:56379/0",
    ],
    ids=["http", "unix-socket", "postgresql"],
)
def test_settings_rejects_non_redis_scheme(redis_url: str) -> None:
    with pytest.raises(ValidationError, match="redis 或 rediss"):
        settings(redis_url=redis_url)


def test_settings_rejects_redis_url_without_host() -> None:
    with pytest.raises(ValidationError, match="非空 host"):
        settings(redis_url="redis://:secret@:56379/0")


@pytest.mark.parametrize(
    "redis_url",
    [
        "redis://127.0.0.1:56379/0",
        "redis://:secret@127.0.0.1:56379/0",
    ],
    ids=["missing-password", "non-default-password"],
)
def test_settings_accepts_non_default_redis_credentials_in_development(redis_url: str) -> None:
    assert settings(redis_url=redis_url).redis_url == redis_url


def test_settings_rejects_default_redis_credentials_in_production() -> None:
    with pytest.raises(ValidationError, match="默认 Redis 凭据"):
        settings(
            environment="production",
            database_url=PRODUCTION_DATABASE_URL,
            redis_url=f"redis://:{DEFAULT_REDIS_PASSWORD}@redis:6379/0",
        )


def test_settings_requires_redis_password_in_production() -> None:
    with pytest.raises(ValidationError, match="非空 password"):
        settings(
            environment="production",
            database_url=PRODUCTION_DATABASE_URL,
            redis_url="redis://redis:6379/0",
        )


def test_require_redis_url_fails_explicitly_without_configuration() -> None:
    with pytest.raises(ValueError, match="REDIS_URL"):
        require_redis_url(settings())


def test_create_celery_app_rejects_missing_broker_configuration() -> None:
    with pytest.raises(ValueError, match="REDIS_URL"):
        create_celery_app(settings())


def test_dispatch_fails_explicitly_when_broker_is_unreachable() -> None:
    """不可达 broker 必须报错，而不是静默丢弃消息。"""

    app = create_celery_app(settings(redis_url="redis://:secret@127.0.0.1:1/0"))

    with pytest.raises(OperationalError):
        app.send_task(PROBE_TASK_NAME, args=[{}], queue="unreachable")


def worker_app() -> Celery:
    return create_celery_app(settings(redis_url=REDIS_URL))


def test_celery_app_uses_authenticated_broker_and_no_result_backend() -> None:
    app = worker_app()

    assert app.conf.broker_url == REDIS_URL
    assert app.conf.result_backend is None
    assert app.conf.task_ignore_result is True


def test_celery_app_uses_json_protocol_and_safe_acknowledgement_defaults() -> None:
    app = worker_app()

    assert app.conf.task_serializer == "json"
    assert app.conf.result_serializer == "json"
    assert app.conf.accept_content == ["json"]
    assert app.conf.worker_prefetch_multiplier == 1
    assert app.conf.task_acks_late is True


def test_celery_app_fails_fast_and_does_not_send_events() -> None:
    app = worker_app()

    assert app.conf.broker_connection_retry_on_startup is False
    assert (
        app.conf.broker_transport_options["visibility_timeout"]
        == BROKER_VISIBILITY_TIMEOUT_SECONDS
    )
    # 事件不再作为执行证据；marker 才是确定性证据，因此两处事件发布都关闭。
    assert app.conf.task_send_sent_event is False
    assert app.conf.worker_send_task_events is False


def test_probe_task_is_registered_on_the_worker_app() -> None:
    assert PROBE_TASK_NAME in worker_app().tasks


def test_probe_task_returns_json_serializable_identity_and_payload() -> None:
    app = worker_app()
    app.conf.task_always_eager = True
    app.conf.task_eager_propagates = True
    payload = {"probeId": "abc", "nested": {"value": 7}, "items": [1, 2, 3]}

    result = app.tasks[PROBE_TASK_NAME].apply(args=[payload]).get()

    assert result["payload"] == payload
    assert result["hostname"]
    assert result["pid"] > 0
    assert result["taskId"]
    assert json.loads(json.dumps(result)) == result


def test_probe_task_without_marker_directory_writes_no_file() -> None:
    app = worker_app()
    app.conf.task_always_eager = True
    app.conf.task_eager_propagates = True

    assert app.conf.probe_marker_directory is None
    result = app.tasks[PROBE_TASK_NAME].apply(args=[{"probeId": "abc"}]).get()

    assert result["payload"] == {"probeId": "abc"}


def test_probe_marker_path_is_derived_from_trusted_directory_and_task_id() -> None:
    assert probe_marker_path(str(Path("markers")), "abc-123") == Path("markers") / "abc-123.json"


@pytest.mark.parametrize(
    "task_id",
    ["../escape", "a/b", "a\\b", "", ".", "..", "a" * 129],
    ids=["parent", "slash", "backslash", "empty", "dot", "dotdot", "too-long"],
)
def test_probe_marker_path_rejects_unsafe_task_id(task_id: str) -> None:
    with pytest.raises(ValueError):
        probe_marker_path("markers", task_id)


def test_probe_task_writes_marker_when_directory_is_configured(tmp_path: Path) -> None:
    marker_directory = tmp_path / "markers"
    app = create_celery_app(
        settings(redis_url=REDIS_URL, probe_marker_directory=str(marker_directory))
    )
    app.conf.task_always_eager = True
    app.conf.task_eager_propagates = True
    payload = {"probeId": "abc", "nested": {"value": 7}}

    result = app.tasks[PROBE_TASK_NAME].apply(args=[payload]).get()

    marker = json.loads((marker_directory / f"{result['taskId']}.json").read_text(encoding="utf-8"))
    assert marker == result
    assert marker["payload"] == payload


def test_probe_task_cleans_up_temporary_files(tmp_path: Path) -> None:
    marker_directory = tmp_path / "markers"
    record = {"taskId": "abc", "hostname": "worker@test", "pid": 1, "payload": {}}

    write_probe_marker(str(marker_directory), record)

    assert sorted(path.name for path in marker_directory.iterdir()) == ["abc.json"]
