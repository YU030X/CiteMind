"""queue-probe supervisor 的纯逻辑测试：只使用临时 marker 目录，不连接 Redis。"""

import json
from pathlib import Path

import pytest
from rag_backend.queue_probe import (
    QueueProbeError,
    read_probe_marker,
    verify_probe_marker,
    wait_for_probe_marker,
)

PAYLOAD = {"probeId": "abc", "source": "queue-probe"}


def write_marker(marker_directory: Path, task_id: str, record: dict[str, object]) -> Path:
    marker_directory.mkdir(parents=True, exist_ok=True)
    path = marker_directory / f"{task_id}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_read_probe_marker_fails_when_absent(tmp_path: Path) -> None:
    with pytest.raises(QueueProbeError, match="未找到"):
        read_probe_marker(str(tmp_path), "missing")


def test_read_probe_marker_rejects_invalid_json(tmp_path: Path) -> None:
    (tmp_path / "abc.json").write_text("not-json", encoding="utf-8")

    with pytest.raises(QueueProbeError, match="合法 JSON"):
        read_probe_marker(str(tmp_path), "abc")


def test_verify_probe_marker_accepts_matching_record() -> None:
    record = {"taskId": "abc", "hostname": "worker@test", "pid": 42, "payload": PAYLOAD}

    verify_probe_marker(record, "abc", PAYLOAD)


@pytest.mark.parametrize(
    "record",
    [
        {"taskId": "other", "hostname": "worker@test", "pid": 42, "payload": PAYLOAD},
        {"taskId": "abc", "hostname": "worker@test", "pid": 42, "payload": {}},
        {"taskId": "abc", "hostname": "", "pid": 42, "payload": PAYLOAD},
        {"taskId": "abc", "hostname": "worker@test", "pid": 0, "payload": PAYLOAD},
    ],
    ids=["task-id", "payload", "hostname", "pid"],
)
def test_verify_probe_marker_rejects_mismatch(record: dict[str, object]) -> None:
    with pytest.raises(QueueProbeError):
        verify_probe_marker(record, "abc", PAYLOAD)


def test_wait_for_probe_marker_returns_verified_record(tmp_path: Path) -> None:
    record = {"taskId": "abc", "hostname": "worker@test", "pid": 42, "payload": PAYLOAD}
    write_marker(tmp_path, "abc", record)

    assert wait_for_probe_marker(str(tmp_path), "abc", PAYLOAD, timeout=1.0) == record


def test_wait_for_probe_marker_times_out_when_absent(tmp_path: Path) -> None:
    with pytest.raises(QueueProbeError, match="未收到"):
        wait_for_probe_marker(str(tmp_path), "abc", PAYLOAD, timeout=0.1, poll_interval=0.01)
