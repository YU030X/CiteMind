"""真实 Redis broker 与独立 Celery worker 的集成测试。

这些测试不需要 Celery result backend：probe 任务不写业务状态。执行是否发生由临时 marker
目录里与本次 ``async_result.id`` 对应的 JSON 文件证实；worker 日志与 ``inspect`` 只用于
readiness 与失败诊断，不能作为通过条件。运行前必须提供带认证的回环测试 broker DSN
并显式 opt-in，规则见 ``broker_guard``。
"""

import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from broker_guard import RedisBrokerTarget
from evidencehub.config import Settings
from evidencehub.worker import PROBE_TASK_NAME, create_celery_app
from redis import Redis
from redis.exceptions import AuthenticationError

pytestmark = [pytest.mark.integration, pytest.mark.broker]

REPO_ROOT = Path(__file__).parents[2]
WORKER_READY_TIMEOUT_SECONDS = 60.0
TASK_SUCCESS_TIMEOUT_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 0.5
PROCESS_TERMINATE_TIMEOUT_SECONDS = 10.0


def read_log(log_path: Path) -> str:
    return log_path.read_text(encoding="utf-8", errors="replace")


def wait_until(
    predicate: Callable[[], bool],
    process: "subprocess.Popen[bytes]",
    log_path: Path,
    timeout: float,
    failure_message: str,
) -> None:
    """轮询等待条件成立；worker 提前退出或超时都带着日志失败。"""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(
                f"worker 在条件满足前退出（exit={process.returncode}）：\n{read_log(log_path)}"
            )
        if predicate():
            return
        time.sleep(POLL_INTERVAL_SECONDS)
    pytest.fail(f"{failure_message}\nworker 日志：\n{read_log(log_path)}")


def terminate_process(process: "subprocess.Popen[bytes]") -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=PROCESS_TERMINATE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=PROCESS_TERMINATE_TIMEOUT_SECONDS)


@contextmanager
def running_worker(
    redis_url: str, queue: str, node_name: str, log_path: Path, marker_directory: Path
) -> Iterator["subprocess.Popen[bytes]"]:
    """以独立子进程启动 worker，并在退出时回收进程。

    只有本测试进程把 marker 目录传给 worker；probe 的 payload 无法控制该路径。
    """

    environment = os.environ.copy()
    environment["CITEMIND_REDIS_URL"] = redis_url
    environment["CITEMIND_ENVIRONMENT"] = "test"
    environment["CITEMIND_PROBE_MARKER_DIRECTORY"] = str(marker_directory)
    environment["PYTHONUNBUFFERED"] = "1"
    command = [
        sys.executable,
        "-m",
        "celery",
        "-A",
        "evidencehub.worker:celery_app",
        "worker",
        "--loglevel=INFO",
        # Windows 上不能使用默认的 prefork；本测试用 solo 作 smoke。权威验收是 Linux Compose
        # 的 queue-probe（deploy/compose/queue.yml），这里只验证 broker 与 marker 契约。
        "--pool=solo",
        "--queues",
        queue,
        "--hostname",
        node_name,
        "--without-gossip",
        "--without-mingle",
    ]

    with log_path.open("wb") as log_file:
        process = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            env=environment,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        try:
            yield process
        finally:
            terminate_process(process)


def delete_queue(redis_url: str, queue: str) -> None:
    """只删除本次测试使用的唯一队列 key 及其 binding，不清空逻辑库。"""

    with Redis.from_url(redis_url) as client:
        client.delete(queue)
        # Celery 为未知队列生成的 direct exchange 与 routing key 都等于队列名
        #（见 celery.app.amqp.Queues.new_missing）。
        client.delete(f"_kombu.binding.{queue}")


def unauthenticated_url(redis_url: str) -> str:
    parsed = urlsplit(redis_url)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    database = parsed.path or "/0"
    return f"redis://{host}{port}{database}"


def test_redis_broker_requires_authentication(test_redis: RedisBrokerTarget) -> None:
    with Redis.from_url(test_redis.url) as client:
        assert client.ping() is True

    with Redis.from_url(unauthenticated_url(test_redis.url)) as client:
        with pytest.raises(AuthenticationError):
            client.ping()


def test_probe_task_runs_on_a_separate_worker_through_the_real_broker(
    test_redis: RedisBrokerTarget, tmp_path: Path
) -> None:
    queue = f"citemind-probe-{uuid.uuid4().hex}"
    node_name = f"probe-test@{uuid.uuid4().hex}"
    log_path = tmp_path / "worker.log"
    marker_directory = tmp_path / "markers"
    marker_directory.mkdir(parents=True, exist_ok=True)
    app = create_celery_app(Settings(environment="test", redis_url=test_redis.url))

    try:
        with running_worker(
            test_redis.url, queue, node_name, log_path, marker_directory
        ) as process:
            inspector = app.control.inspect(timeout=2.0, destination=[node_name])
            wait_until(
                lambda: bool(inspector.ping()),
                process,
                log_path,
                WORKER_READY_TIMEOUT_SECONDS,
                f"worker {node_name} 未在 {WORKER_READY_TIMEOUT_SECONDS:.0f} 秒内响应 inspect ping",
            )

            registered = inspector.registered()
            assert registered is not None, "未取得 worker 已注册任务列表"
            assert PROBE_TASK_NAME in registered[node_name]

            probe_id = uuid.uuid4().hex
            payload = {
                "probeId": probe_id,
                "nested": {"value": 7},
                "items": [1, 2, 3],
            }
            async_result = app.send_task(PROBE_TASK_NAME, args=[payload], queue=queue)
            marker_path = marker_directory / f"{async_result.id}.json"
            wait_until(
                lambda: marker_path.exists(),
                process,
                log_path,
                TASK_SUCCESS_TIMEOUT_SECONDS,
                f"probe marker {marker_path} 未在 {TASK_SUCCESS_TIMEOUT_SECONDS:.0f} 秒内出现",
            )

            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            assert marker["taskId"] == async_result.id
            assert marker["payload"] == payload
            # worker 身份必须来自本次启动的节点，而不是同 broker 上的其它消费者。
            assert marker["hostname"] == node_name
            assert marker["pid"] > 0
            assert probe_id in json.dumps(marker)
    finally:
        delete_queue(test_redis.url, queue)
