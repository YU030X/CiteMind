"""queue-probe：Linux Compose 的确定性验收入口。

在容器内派发一次带唯一 task id 的 ``evidencehub.probe``，然后只在受信 marker 目录里
等待并校验由该 task id 推导出的同名 marker 文件；不使用 Celery 日志、事件或 result
backend 作为执行证据。成功以 0 退出，超时或校验失败以非零退出，从而让
``docker compose up --abort-on-container-exit --exit-code-from queue-probe`` 给出硬退出码。
"""

import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from evidencehub.config import Settings, get_settings
from evidencehub.worker import PROBE_TASK_NAME, create_celery_app, probe_marker_path

POLL_INTERVAL_SECONDS = 0.5


class QueueProbeError(RuntimeError):
    """queue-probe 未能在时限内取得并通过校验的 probe marker。"""


def read_probe_marker(marker_directory: str, task_id: str) -> dict[str, Any]:
    """读取并解析已校验路径上的 probe marker。"""

    path = probe_marker_path(marker_directory, task_id)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise QueueProbeError(f"未找到 probe marker: {path}") from error
    try:
        record = json.loads(text)
    except json.JSONDecodeError as error:
        raise QueueProbeError(f"probe marker 不是合法 JSON: {path}") from error
    if not isinstance(record, dict):
        raise QueueProbeError(f"probe marker 顶层必须是 JSON 对象: {path}")
    return record


def verify_probe_marker(
    record: dict[str, Any], task_id: str, expected_payload: dict[str, Any]
) -> None:
    """校验 marker 确实来自本次派发，并带有可辨认的 worker 身份。"""

    if record.get("taskId") != task_id:
        raise QueueProbeError(
            f"marker taskId 与本次派发不符: 期望 {task_id}，实际 {record.get('taskId')!r}"
        )
    if record.get("payload") != expected_payload:
        raise QueueProbeError(
            f"marker payload 与本次派发不符: "
            f"期望 {expected_payload}，实际 {record.get('payload')!r}"
        )
    hostname = record.get("hostname")
    if not isinstance(hostname, str) or not hostname:
        raise QueueProbeError("probe marker 缺少非空 hostname，无法确认 worker 身份")
    pid = record.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise QueueProbeError("probe marker 缺少有效 pid，无法确认 worker 身份")


def wait_for_probe_marker(
    marker_directory: str,
    task_id: str,
    expected_payload: dict[str, Any],
    timeout: float,
    poll_interval: float = POLL_INTERVAL_SECONDS,
) -> dict[str, Any]:
    """轮询等待 marker 出现并校验；超时或校验失败抛 ``QueueProbeError``。"""

    path: Path = probe_marker_path(marker_directory, task_id)
    deadline = time.monotonic() + timeout
    while True:
        if path.exists():
            record = read_probe_marker(marker_directory, task_id)
            verify_probe_marker(record, task_id, expected_payload)
            return record
        if time.monotonic() >= deadline:
            raise QueueProbeError(f"在 {timeout:.0f} 秒内未收到 probe marker: {path}")
        time.sleep(poll_interval)


def run(settings: Settings, timeout: float | None = None) -> dict[str, Any]:
    """派发唯一 probe 并等待 marker；返回已校验的 marker。"""

    marker_directory = settings.probe_marker_directory
    if not marker_directory:
        raise QueueProbeError(
            "必须设置 CITEMIND_PROBE_MARKER_DIRECTORY 才能用 marker 验收 probe 执行"
        )
    app = create_celery_app(settings)
    payload = {"probeId": uuid.uuid4().hex, "source": "queue-probe"}
    async_result = app.send_task(PROBE_TASK_NAME, args=[payload])
    effective_timeout = settings.queue_probe_timeout_seconds if timeout is None else timeout
    return wait_for_probe_marker(marker_directory, async_result.id, payload, effective_timeout)


def main() -> int:
    try:
        record = run(get_settings())
    except Exception as error:  # CLI 边界：任何失败都必须映射为非零退出码
        print(f"queue-probe 失败: {error}", file=sys.stderr, flush=True)
        return 1
    print(
        "queue-probe 成功: "
        f"taskId={record['taskId']} hostname={record['hostname']} pid={record['pid']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
