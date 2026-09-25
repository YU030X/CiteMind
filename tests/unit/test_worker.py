"""worker 配置、接收任务与 probe marker 的纯逻辑测试：不连接 Redis 或数据库。"""

import json
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from celery import Celery
from kombu import Connection
from kombu.exceptions import OperationalError
from pydantic import ValidationError
from rag_backend.config import DEFAULT_REDIS_PASSWORD, Settings
from rag_backend.database import SyncSessionFactory
from rag_backend.dispatch import protocol
from rag_backend.dispatch.publisher import (
    BROKER_PUBLISH_TIMEOUT_SECONDS,
    CeleryPublisher,
)
from rag_backend.worker import (
    BROKER_VISIBILITY_TIMEOUT_SECONDS,
    INGEST_QUEUE_NAME,
    INGEST_TASK_NAME,
    LEGACY_PARSER_VERSIONS,
    PROBE_TASK_NAME,
    RECEIVE_STATUS_ALREADY_RECEIVED,
    RECEIVE_STATUS_DELETED,
    RECEIVE_STATUS_EXISTING_DIAGNOSTIC,
    RECEIVE_STATUS_INVALID_PAYLOAD,
    RECEIVE_STATUS_LEGACY_UNSUPPORTED,
    RECEIVE_STATUS_NOT_QUEUED,
    RECEIVE_STATUS_RECEIVED,
    RECEIVE_STATUS_VERSION_MISMATCH,
    IngestJobFacts,
    ReceiveAction,
    create_celery_app,
    decide_receive_action,
    parse_ingest_payload,
    probe_marker_path,
    receive_ingest_event,
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


# --- 入库接收任务 --------------------------------------------------------------


def test_ingest_task_is_registered_and_routed_to_ingest_queue() -> None:
    app = worker_app()

    assert INGEST_TASK_NAME in app.tasks
    assert app.conf.task_routes[INGEST_TASK_NAME]["queue"] == INGEST_QUEUE_NAME
    # probe 没有专门路由，仍走默认 celery 队列，与 ingest 专用队列隔离。
    assert PROBE_TASK_NAME not in app.conf.task_routes


def test_celery_app_acknowledges_failed_tasks_without_broker_retry() -> None:
    app = worker_app()

    assert app.conf.task_acks_late is True
    assert app.conf.task_acks_on_failure_or_timeout is True
    assert app.conf.result_backend is None


QUEUED_PARSER_VERSION = "markdown-it-py-4.2.0-v1"
LEGACY_PARSER_VERSION = "markdown-v1"


def job_facts(
    *,
    status: str = "QUEUED",
    deleted: bool = False,
    version_matches: bool = True,
    marker: bool = False,
    profile_bound: bool = True,
    parser_version: str = QUEUED_PARSER_VERSION,
    error_code: str | None = None,
) -> IngestJobFacts:
    """按优先级矩阵需要的字段构造 job 事实，默认是可直接接收的新 job。"""

    return IngestJobFacts(
        status, deleted, version_matches, marker, profile_bound, parser_version, error_code
    )


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        # 新 job：profile 已绑定、parser 真实、无诊断 → 写接收 marker。
        (job_facts(), ReceiveAction.SET_MARKER),
        # 已有接收 marker 优先于诊断与 legacy（含旧 job 上的 HANDLER_NOT_READY）。
        (job_facts(marker=True), ReceiveAction.ALREADY_RECEIVED),
        (
            job_facts(
                marker=True, profile_bound=False, parser_version=LEGACY_PARSER_VERSION
            ),
            ReceiveAction.ALREADY_RECEIVED,
        ),
        (
            job_facts(
                marker=True,
                profile_bound=False,
                parser_version=LEGACY_PARSER_VERSION,
                error_code=protocol.DELIVERY_UNCONFIRMED,
            ),
            ReceiveAction.ALREADY_RECEIVED,
        ),
        # deleted / version-mismatch / 非 QUEUED 先于诊断与 legacy。
        (
            job_facts(
                deleted=True, profile_bound=False, parser_version=LEGACY_PARSER_VERSION
            ),
            ReceiveAction.DELETED,
        ),
        (
            job_facts(
                deleted=True,
                profile_bound=False,
                parser_version=LEGACY_PARSER_VERSION,
                error_code=protocol.DELIVERY_UNCONFIRMED,
            ),
            ReceiveAction.DELETED,
        ),
        (
            job_facts(
                version_matches=False,
                profile_bound=False,
                parser_version=LEGACY_PARSER_VERSION,
            ),
            ReceiveAction.VERSION_MISMATCH,
        ),
        (job_facts(status="READY"), ReceiveAction.NOT_QUEUED),
        (
            job_facts(
                status="CANCELLED",
                profile_bound=False,
                parser_version=LEGACY_PARSER_VERSION,
            ),
            ReceiveAction.NOT_QUEUED,
        ),
        # 无 marker 但已有非 NULL 诊断：保持 QUEUED 原样，旧/新 profile 都是只读状态。
        (
            job_facts(
                profile_bound=False,
                parser_version=LEGACY_PARSER_VERSION,
                error_code=protocol.DELIVERY_UNCONFIRMED,
            ),
            ReceiveAction.EXISTING_DIAGNOSTIC,
        ),
        (
            job_facts(
                profile_bound=False,
                parser_version=LEGACY_PARSER_VERSION,
                error_code=protocol.UNSUPPORTED_EVENT_TYPE,
            ),
            ReceiveAction.EXISTING_DIAGNOSTIC,
        ),
        (
            job_facts(error_code=protocol.UNSUPPORTED_EVENT_TYPE),
            ReceiveAction.EXISTING_DIAGNOSTIC,
        ),
        (
            job_facts(
                parser_version=LEGACY_PARSER_VERSION,
                error_code=protocol.DELIVERY_UNCONFIRMED,
            ),
            ReceiveAction.EXISTING_DIAGNOSTIC,
        ),
        # 无诊断的 legacy：profile 未绑定或 parser 为已知占位版本（或两者）。
        (job_facts(profile_bound=False), ReceiveAction.LEGACY_UNSUPPORTED),
        (job_facts(parser_version=LEGACY_PARSER_VERSION), ReceiveAction.LEGACY_UNSUPPORTED),
        (
            job_facts(profile_bound=False, parser_version=LEGACY_PARSER_VERSION),
            ReceiveAction.LEGACY_UNSUPPORTED,
        ),
    ],
    ids=[
        "queued",
        "received",
        "received-legacy",
        "received-with-diagnostic",
        "deleted",
        "deleted-with-diagnostic",
        "version-mismatch",
        "ready",
        "cancelled",
        "existing-diagnostic-legacy-both",
        "existing-diagnostic-legacy-unsupported-event",
        "existing-diagnostic-new-profile",
        "existing-diagnostic-legacy-parser",
        "legacy-unbound-profile",
        "legacy-placeholder-parser",
        "legacy-both",
    ],
)
def test_decide_receive_action(facts: IngestJobFacts, expected: ReceiveAction) -> None:
    assert decide_receive_action(facts) is expected


def test_legacy_parser_versions_only_contains_the_known_placeholder() -> None:
    assert LEGACY_PARSER_VERSIONS == frozenset({"markdown-v1"})


def test_ingest_payload_protocol_version_and_shape_are_unchanged() -> None:
    assert protocol.PROTOCOL_VERSION == 1
    payload = protocol.build_dispatch_payload(uuid.uuid4())
    assert set(payload) == {"protocolVersion", "jobId"}
    assert payload["protocolVersion"] == 1


def test_parse_ingest_payload_accepts_only_job_and_protocol_version() -> None:
    job_id = uuid.uuid4()

    assert parse_ingest_payload({"protocolVersion": 1, "jobId": str(job_id)}) == job_id


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"protocolVersion": 1},
        {"protocolVersion": 2, "jobId": str(uuid.uuid4())},
        {"protocolVersion": 1, "jobId": "not-a-uuid"},
        {"protocolVersion": 1, "jobId": 7},
    ],
    ids=["none", "list", "empty", "missing-job", "wrong-version", "bad-uuid", "non-str"],
)
def test_parse_ingest_payload_rejects_malformed(payload: object) -> None:
    with pytest.raises(ValueError):
        parse_ingest_payload(payload)


def test_ingest_task_reports_invalid_payload_without_touching_database() -> None:
    app = worker_app()
    app.conf.task_always_eager = True
    app.conf.task_eager_propagates = True

    result = app.tasks[INGEST_TASK_NAME].apply(args=[{"jobId": "x"}]).get()

    assert result["status"] == RECEIVE_STATUS_INVALID_PAYLOAD


def test_ingest_task_passes_task_id_and_job_id_to_receiver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = worker_app()
    app.conf.task_always_eager = True
    app.conf.task_eager_propagates = True
    recorded: dict[str, object] = {}

    def fake_receiver(
        session_factory: SyncSessionFactory, *, job_id: uuid.UUID, event_id: str
    ) -> str:
        recorded["job_id"] = job_id
        recorded["event_id"] = event_id
        return RECEIVE_STATUS_RECEIVED

    monkeypatch.setattr("rag_backend.worker.receive_ingest_event", fake_receiver)
    job_id = uuid.uuid4()

    result = app.tasks[INGEST_TASK_NAME].apply(
        args=[{"protocolVersion": 1, "jobId": str(job_id)}]
    ).get()

    assert result["status"] == RECEIVE_STATUS_RECEIVED
    assert result["jobId"] == str(job_id)
    assert recorded["job_id"] == job_id
    assert recorded["event_id"] == result["eventId"]


def test_receive_status_constants_are_distinct() -> None:
    statuses = {
        RECEIVE_STATUS_RECEIVED,
        RECEIVE_STATUS_ALREADY_RECEIVED,
        RECEIVE_STATUS_NOT_QUEUED,
        RECEIVE_STATUS_DELETED,
        RECEIVE_STATUS_VERSION_MISMATCH,
        RECEIVE_STATUS_INVALID_PAYLOAD,
        RECEIVE_STATUS_LEGACY_UNSUPPORTED,
        RECEIVE_STATUS_EXISTING_DIAGNOSTIC,
    }
    assert len(statuses) == 8
    assert RECEIVE_STATUS_LEGACY_UNSUPPORTED == "legacy_unsupported"
    assert RECEIVE_STATUS_EXISTING_DIAGNOSTIC == "existing_diagnostic"


def test_receive_ingest_event_rejects_invalid_event_id_before_any_database_work() -> None:
    opened: list[str] = []

    def exploding_factory() -> Any:
        opened.append("session")
        raise AssertionError("非法 event id 不得打开数据库会话")

    factory = cast(SyncSessionFactory, exploding_factory)
    with pytest.raises(ValueError, match="event id"):
        receive_ingest_event(factory, job_id=uuid.uuid4(), event_id="not-a-uuid")
    assert opened == []


class _FakeRowResult:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    def first(self) -> tuple[Any, ...] | None:
        return self._row


class _RecordingSession:
    """只实现接收逻辑用到的最小接口，记录执行过的语句与是否回滚。"""

    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row
        self.executed: list[str] = []
        self.rolled_back = False

    def execute(self, statement: Any, parameters: Any = None) -> _FakeRowResult:
        self.executed.append(str(statement))
        return _FakeRowResult(self._row)

    def rollback(self) -> None:
        self.rolled_back = True

    def __enter__(self) -> "_RecordingSession":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _diagnostic_row(
    *, profile_bound: bool, parser_version: str
) -> tuple[Any, ...]:
    # (status, deleted_at, dv.document_id, j.document_id, lease_owner, heartbeat_at,
    #  profile_id, parser_version, error_code)：无 marker 且两条 document_id 相同。
    document_id = uuid.uuid4()
    profile_id = uuid.uuid4() if profile_bound else None
    return (
        "QUEUED",
        None,
        document_id,
        document_id,
        None,
        None,
        profile_id,
        parser_version,
        "DELIVERY_UNCONFIRMED",
    )


@pytest.mark.parametrize(
    ("profile_bound", "parser_version"),
    [(False, LEGACY_PARSER_VERSION), (True, QUEUED_PARSER_VERSION)],
    ids=["old-legacy", "new-profile"],
)
def test_receive_ingest_event_with_existing_diagnostic_never_writes(
    profile_bound: bool, parser_version: str
) -> None:
    fake = _RecordingSession(
        _diagnostic_row(profile_bound=profile_bound, parser_version=parser_version)
    )
    factory = cast(SyncSessionFactory, lambda: fake)

    status = receive_ingest_event(
        factory, job_id=uuid.uuid4(), event_id=str(uuid.uuid4())
    )

    assert status == RECEIVE_STATUS_EXISTING_DIAGNOSTIC
    # 只读了 FOR UPDATE 行锁，没有执行任何 UPDATE/marker 语句，也没提交。
    assert len(fake.executed) == 1
    assert "FOR UPDATE" in fake.executed[0]
    assert fake.rolled_back is True


# --- dispatcher 配置与 API lifespan 接线 -----------------------------------------


def test_dispatcher_is_disabled_by_default() -> None:
    assert settings().dispatcher_enabled is False


def test_dispatcher_enabled_requires_redis_broker() -> None:
    with pytest.raises(ValidationError, match="dispatcher"):
        settings(dispatcher_enabled=True)


def test_dispatcher_enabled_accepts_redis_broker() -> None:
    assert settings(dispatcher_enabled=True, redis_url=REDIS_URL).dispatcher_enabled is True


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_lifespan_starts_and_cancels_dispatcher_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from rag_backend import app as app_module

    events: list[str] = []

    class FakePublisher:
        def __init__(self, celery_app: object) -> None:
            events.append("publisher")

        async def publish(self, **kwargs: object) -> None:
            raise AssertionError("unit test 不应真正投递")

        async def aclose(self) -> None:
            events.append("publisher_closed")

    class FakeDispatcher:
        def __init__(self, *, session_factory: object, publisher: object) -> None:
            events.append("dispatcher")

        async def run(self) -> None:
            events.append("run")
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                events.append("cancelled")
                raise

    monkeypatch.setattr(app_module, "create_celery_app", lambda resolved: object())
    monkeypatch.setattr(app_module, "CeleryPublisher", FakePublisher)
    monkeypatch.setattr(app_module, "OutboxDispatcher", FakeDispatcher)

    resolved = settings(
        dispatcher_enabled=True,
        redis_url=REDIS_URL,
        session_cookie_secure=False,
    )
    application = app_module.create_app(resolved)
    async with application.router.lifespan_context(application):
        await asyncio.sleep(0)
        assert "run" in events

    assert "cancelled" in events
    assert events[-1] == "publisher_closed"


@pytest.mark.anyio
async def test_lifespan_does_not_start_dispatcher_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rag_backend import app as app_module

    created: list[str] = []
    monkeypatch.setattr(
        app_module, "CeleryPublisher", lambda celery_app: created.append("publisher")
    )

    resolved = settings(session_cookie_secure=False)
    application = app_module.create_app(resolved)
    async with application.router.lifespan_context(application):
        pass

    assert created == []


# --- Celery publisher：不重试 + 有限超时 ---------------------------------------


def test_celery_publisher_disables_retry_and_bounds_broker_timeout() -> None:
    app = create_celery_app(settings(redis_url=REDIS_URL))

    CeleryPublisher(app)

    assert app.conf.task_publish_retry is False
    assert app.conf.broker_connection_timeout == BROKER_PUBLISH_TIMEOUT_SECONDS
    options = app.conf.broker_transport_options
    assert options["socket_connect_timeout"] == BROKER_PUBLISH_TIMEOUT_SECONDS
    assert options["socket_timeout"] == BROKER_PUBLISH_TIMEOUT_SECONDS
    # 既有 visibility_timeout 不被发布超时改写。
    assert options["visibility_timeout"] == BROKER_VISIBILITY_TIMEOUT_SECONDS


@pytest.mark.anyio
async def test_celery_publisher_sends_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_celery_app(settings(redis_url=REDIS_URL))
    publisher = CeleryPublisher(app)
    recorded: dict[str, Any] = {}
    payload: dict[str, object] = {
        "protocolVersion": 1,
        "jobId": "00000000-0000-0000-0000-000000000001",
    }

    def fake_send_task(
        name: str,
        *,
        args: list[dict[str, object]],
        task_id: str,
        queue: str,
        connection: object,
        retry: bool,
    ) -> None:
        recorded.update(
            name=name,
            args=args,
            task_id=task_id,
            queue=queue,
            connection=connection,
            retry=retry,
        )

    monkeypatch.setattr(app, "send_task", fake_send_task)

    await publisher.publish(
        task_name=INGEST_TASK_NAME,
        payload=payload,
        task_id="event-1",
        queue=INGEST_QUEUE_NAME,
    )

    assert recorded["name"] == INGEST_TASK_NAME
    assert recorded["retry"] is False
    assert recorded["task_id"] == "event-1"
    assert recorded["queue"] == INGEST_QUEUE_NAME
    assert recorded["args"] == [payload]
    # 发布使用单次受控连接，而不是从全局 producer pool 取连接。
    assert recorded["connection"] is not None


class _RecordingConnection:
    """最小连接上下文，记录 `__enter__`/`__exit__` 次数。"""

    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    def __enter__(self) -> "_RecordingConnection":
        self.entered += 1
        return self

    def __exit__(self, *exc: object) -> None:
        self.exited += 1


@pytest.mark.anyio
async def test_celery_publisher_releases_connection_on_success_and_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_celery_app(settings(redis_url=REDIS_URL))
    publisher = CeleryPublisher(app)
    connection = _RecordingConnection()
    monkeypatch.setattr(app, "connection_for_write", lambda: connection)
    payload: dict[str, object] = {"protocolVersion": 1, "jobId": "job-1"}

    monkeypatch.setattr(app, "send_task", lambda *args, **kwargs: None)
    await publisher.publish(
        task_name=INGEST_TASK_NAME, payload=payload, task_id="e1", queue=INGEST_QUEUE_NAME
    )
    assert (connection.entered, connection.exited) == (1, 1)

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("broker down")

    monkeypatch.setattr(app, "send_task", boom)
    with pytest.raises(RuntimeError, match="broker down"):
        await publisher.publish(
            task_name=INGEST_TASK_NAME, payload=payload, task_id="e2", queue=INGEST_QUEUE_NAME
        )
    # 失败路径同样释放连接，且原始异常未被吞掉。
    assert (connection.entered, connection.exited) == (2, 2)


@pytest.mark.anyio
async def test_celery_publisher_sends_two_messages_in_order_over_memory_transport() -> None:
    app = Celery("publisher-test", broker="memory://")
    publisher = CeleryPublisher(app)

    await publisher.publish(
        task_name=INGEST_TASK_NAME,
        payload={"protocolVersion": 1, "jobId": "job-1"},
        task_id="event-1",
        queue=INGEST_QUEUE_NAME,
    )
    await publisher.publish(
        task_name=INGEST_TASK_NAME,
        payload={"protocolVersion": 1, "jobId": "job-2"},
        task_id="event-2",
        queue=INGEST_QUEUE_NAME,
    )

    with Connection("memory://") as connection:
        channel = connection.default_channel
        first = channel.basic_get(queue=INGEST_QUEUE_NAME, no_ack=True)
        second = channel.basic_get(queue=INGEST_QUEUE_NAME, no_ack=True)

    assert first is not None and second is not None
    assert first.headers["id"] == "event-1"
    assert second.headers["id"] == "event-2"


@pytest.mark.anyio
async def test_celery_publisher_closes_single_use_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Celery("publisher-close-test", broker="memory://")
    publisher = CeleryPublisher(app)
    seen: list[Connection] = []

    def fake_send_task(
        name: str,
        *,
        args: list[dict[str, object]],
        task_id: str,
        queue: str,
        connection: Connection,
        retry: bool,
    ) -> None:
        seen.append(connection)

    monkeypatch.setattr(app, "send_task", fake_send_task)

    await publisher.publish(
        task_name=INGEST_TASK_NAME,
        payload={"protocolVersion": 1, "jobId": "j"},
        task_id="event-1",
        queue=INGEST_QUEUE_NAME,
    )

    assert len(seen) == 1
    # 传给 send_task 的受控连接在 publish 返回时已被关闭：
    # kombu Connection.__exit__ → release() → _close()。
    assert seen[0]._closed is True
