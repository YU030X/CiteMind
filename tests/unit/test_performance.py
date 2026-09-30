"""Phase 4 最小性能入口与受限 Compose overlay 的聚焦单测。

全部离线：HTTP 用 ``httpx.MockTransport``，内存用 fake 采样器；**不联网、不读 .env、不调用
真实 Docker、不启动容器、不写真实报告**。Compose overlay 只做静态 YAML 结构断言，不运行
``docker compose``。
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from rag_backend.evaluation import performance as performance_module
from rag_backend.evaluation.performance import (
    ContainerMemorySnapshot,
    LatencyStats,
    PerformanceError,
    RequestRecord,
    _origin_of,
    aggregate_memory,
    build_report,
    docker_memory_snapshot,
    parse_docker_bytes,
    report_payload,
    summarize_latencies,
    write_report,
)
from rag_backend.evaluation.performance import (
    main as performance_main,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PHASE4_LIMITS = REPO_ROOT / "deploy" / "compose" / "phase4-limits.yml"
BASE_COMPOSE = REPO_ROOT / "deploy" / "compose" / "compose.yml"

EXPECTED_SERVICES = {
    "api",
    "frontend-gateway",
    "inference",
    "postgres",
    "redis",
    "worker",
}
EXPECTED_TOTAL_MIB = 4096
EXPECTED_TOTAL_CPUS = 2.0


# ---------------------------------------------------------------------------
# Compose overlay 静态断言


def parse_overlay_limits(text: str) -> dict[str, dict[str, str]]:
    """只解析本 overlay 的 ``services.<name>.deploy.resources.limits`` 标量。"""

    limits: dict[str, dict[str, str]] = {}
    service: str | None = None
    in_limits = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent == 2 and stripped.endswith(":"):
            service = stripped[:-1].strip()
            in_limits = False
            continue
        if indent <= 4:
            in_limits = False
        if stripped == "limits:":
            in_limits = True
            continue
        if in_limits and service is not None and indent == 10 and ":" in stripped:
            key, _, value = stripped.partition(":")
            limits.setdefault(service, {})[key.strip()] = value.strip().strip('"')
    return limits


def _memory_mib(value: str) -> int:
    assert re.fullmatch(r"\d+M", value), f"内存必须是无符号 M(MiB) 标量：{value}"
    return int(value[:-1])


def test_overlay_declares_limits_for_all_six_services() -> None:
    limits = parse_overlay_limits(PHASE4_LIMITS.read_text(encoding="utf-8"))

    assert set(limits) == EXPECTED_SERVICES


def test_overlay_cpu_and_memory_sum_to_two_vcpu_and_four_gib() -> None:
    limits = parse_overlay_limits(PHASE4_LIMITS.read_text(encoding="utf-8"))

    total_cpus = sum(float(limits[service]["cpus"]) for service in EXPECTED_SERVICES)
    total_mib = sum(_memory_mib(limits[service]["memory"]) for service in EXPECTED_SERVICES)

    assert total_mib == EXPECTED_TOTAL_MIB, "六服务内存上限之和必须是 4096 MiB(4 GiB)"
    assert total_cpus == pytest.approx(EXPECTED_TOTAL_CPUS)


def test_overlay_limits_are_positive_scalars() -> None:
    limits = parse_overlay_limits(PHASE4_LIMITS.read_text(encoding="utf-8"))

    for service in EXPECTED_SERVICES:
        assert float(limits[service]["cpus"]) > 0
        assert _memory_mib(limits[service]["memory"]) > 0


def _base_service_names(text: str) -> set[str]:
    """只收集 base ``services:`` 块下缩进两空格的服务名。"""

    names: set[str] = set()
    in_services = False
    for line in text.splitlines():
        if line.startswith("services:"):
            in_services = True
            continue
        if not in_services:
            continue
        if line and not line.startswith(" "):
            break
        match = re.match(r"^  ([a-z][a-z0-9-]*):\s*$", line)
        if match:
            names.add(match.group(1))
    return names


def test_overlay_matches_base_compose_services_and_does_not_touch_base_file() -> None:
    base_text = BASE_COMPOSE.read_text(encoding="utf-8")
    overlay_services = set(parse_overlay_limits(PHASE4_LIMITS.read_text(encoding="utf-8")))

    assert overlay_services == _base_service_names(base_text)
    # overlay 不是 base 的替代：base 仍声明全部六服务且未自行写 deploy 限额。
    assert "deploy:" not in base_text


# ---------------------------------------------------------------------------
# 纯函数：统计与内存聚合


def test_summarize_latencies_uses_nearest_rank_and_keeps_failures() -> None:
    records = [
        RequestRecord(0, True, 10.0, 200, None),
        RequestRecord(1, False, 20.0, 500, "request"),
        RequestRecord(2, True, 30.0, 200, None),
        RequestRecord(3, False, 40.0, 500, "request"),
    ]

    stats = summarize_latencies(records)

    assert stats.sample_count == 4
    assert stats.min_ms == 10.0
    assert stats.max_ms == 40.0
    # nearest-rank：ceil(0.5*4)-1 = 1 -> 20；ceil(0.95*4)-1 = 3 -> 40。
    assert stats.p50_ms == 20.0
    assert stats.p95_ms == 40.0


def test_summarize_latencies_without_samples_is_all_none() -> None:
    stats = summarize_latencies([RequestRecord(0, False, None, None, "request")])

    assert stats == LatencyStats(0, None, None, None, None, None)


def test_aggregate_memory_never_turns_unknown_into_zero() -> None:
    snapshots = [
        [
            ContainerMemorySnapshot("api", 100, 512 * 1024**2, 0.5),
            ContainerMemorySnapshot("worker", None, None, None, "not observed"),
        ],
        [
            ContainerMemorySnapshot("api", 300, None, None),
            ContainerMemorySnapshot("worker", None, None, None, "not observed"),
        ],
    ]

    observations = {item.service: item for item in aggregate_memory(snapshots)}

    api = observations["api"]
    assert api.sample_count == 2
    assert api.sampled_peak_bytes == 300
    assert api.observed_limit_bytes == 512 * 1024**2
    assert api.cpu_limit == 0.5
    worker = observations["worker"]
    assert worker.sampled_peak_bytes is None
    assert worker.observed_limit_bytes is None
    assert worker.cpu_limit is None
    assert worker.errors == ("not observed",)


def test_parse_docker_bytes_handles_suffixes_and_rejects_garbage() -> None:
    assert parse_docker_bytes("1KiB") == 1024
    assert parse_docker_bytes("1.5MiB") == int(1.5 * 1024**2)
    assert parse_docker_bytes("2GiB") == 2 * 1024**3
    assert parse_docker_bytes("100MB") == 100 * 1000**2
    assert parse_docker_bytes("") is None
    assert parse_docker_bytes("not-a-size") is None
    assert parse_docker_bytes("-5MiB") is None


def test_build_report_rejects_record_count_mismatch() -> None:
    with pytest.raises(PerformanceError):
        build_report(
            mode="retrieval",
            api_base_url="http://127.0.0.1:58080",
            count=2,
            concurrency=1,
            records=[RequestRecord(0, True, 1.0, 200, None)],
            elapsed_seconds=1.0,
            memory_sampled=False,
            memory=(),
            memory_sample_interval_seconds=None,
            created_at="2026-09-30T00:00:00+00:00",
            write_cloud_llm=False,
        )


def test_build_report_throughput_is_none_for_zero_window() -> None:
    report = build_report(
        mode="retrieval",
        api_base_url="http://127.0.0.1:58080",
        count=1,
        concurrency=1,
        records=[RequestRecord(0, True, 1.0, 200, None)],
        elapsed_seconds=0.0,
        memory_sampled=False,
        memory=(),
        memory_sample_interval_seconds=None,
        created_at="2026-09-30T00:00:00+00:00",
        write_cloud_llm=False,
    )

    assert report.throughput_rps is None
    assert report.failure_rate == 0.0


def test_write_report_refuses_to_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "report.json"
    target.write_text("original", encoding="utf-8")

    with pytest.raises(PerformanceError):
        write_report(target, {"a": 1})

    assert target.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.iterdir()) == [target]


def test_write_report_is_atomic_and_leaves_no_temp(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "report.json"
    target.parent.mkdir()

    write_report(target, {"count": 1})

    assert json.loads(target.read_text(encoding="utf-8")) == {"count": 1}
    assert list(target.parent.iterdir()) == [target]


# ---------------------------------------------------------------------------
# CLI dry-run 护栏


class StepClock:
    """确定性时钟：每次调用前进固定步长。"""

    def __init__(self, step: float) -> None:
        self._value = 0.0
        self._step = step

    def __call__(self) -> float:
        self._value += self._step
        return self._value


def _exploding_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("dry-run 不得联网")

    return httpx.MockTransport(handler)


def _exploding_sampler(
    compose_project: str, stop_event: threading.Event
) -> Sequence[ContainerMemorySnapshot]:
    raise AssertionError("dry-run 不得执行 Docker 采样")


class FakeSampler:
    def __init__(self, snapshots: Sequence[ContainerMemorySnapshot]) -> None:
        self._snapshots = snapshots
        self.projects: list[str] = []

    def __call__(
        self, compose_project: str, stop_event: threading.Event
    ) -> Sequence[ContainerMemorySnapshot]:
        self.projects.append(compose_project)
        return self._snapshots


def test_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as excinfo:
        performance_main(["--help"])

    assert excinfo.value.code == 0


def test_dry_run_uses_no_network_env_or_docker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "report.json"

    rc = performance_main(
        ["--mode", "retrieval", "--count", "2", "--compose-project", "demo", "--out", str(out)],
        environ={"PERF_USERNAME": "SHOULD_NOT_BE_READ"},
        transport=_exploding_transport(),
        sampler=_exploding_sampler,
    )

    assert rc == 0
    assert not out.exists()
    output = capsys.readouterr().out
    assert "dry-run" in output
    assert "count=2" in output


def test_execute_rejects_non_loopback_without_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = performance_main(
        [
            "--execute",
            "--api-base-url",
            "http://example.com:58080",
            "--kb-id",
            "11111111-1111-1111-1111-111111111111",
            "--out",
            str(tmp_path / "report.json"),
        ]
    )

    assert rc == 1
    assert "回环" in capsys.readouterr().err


def test_execute_rejects_credentials_embedded_in_api_url(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = performance_main(
        [
            "--execute",
            "--api-base-url",
            "http://user:pass@127.0.0.1:58080",
            "--kb-id",
            "11111111-1111-1111-1111-111111111111",
            "--out",
            str(tmp_path / "report.json"),
        ]
    )

    assert rc == 1
    assert "凭据" in capsys.readouterr().err


def test_qa_requires_explicit_paid_llm_flag(capsys: pytest.CaptureFixture[str]) -> None:
    rc = performance_main(["--mode", "qa", "--count", "1"])

    assert rc == 1
    assert "allow-paid-llm" in capsys.readouterr().err


def test_paid_llm_flag_is_invalid_for_retrieval(capsys: pytest.CaptureFixture[str]) -> None:
    rc = performance_main(["--mode", "retrieval", "--allow-paid-llm"])

    assert rc == 1
    assert "allow-paid-llm" in capsys.readouterr().err


def test_concurrency_other_than_one_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    rc = performance_main(["--mode", "retrieval", "--concurrency", "2"])

    assert rc == 1
    assert "并发 1" in capsys.readouterr().err


def test_invalid_kb_id_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    rc = performance_main(["--mode", "retrieval", "--kb-id", "not-a-uuid"])

    assert rc == 1
    assert "UUID" in capsys.readouterr().err


def test_retrieval_execute_rejects_missing_query_env_before_network(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = performance_main(
        [
            "--execute",
            "--api-base-url",
            "http://127.0.0.1:58080",
            "--kb-id",
            "11111111-1111-1111-1111-111111111111",
            "--out",
            str(tmp_path / "report.json"),
        ],
        environ={"PERF_USERNAME": "u", "PERF_PASSWORD": "p"},
        transport=_exploding_transport(),
    )

    assert rc == 1
    assert "PERF_QUERY" in capsys.readouterr().err


def test_execute_rejects_existing_report_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "report.json"
    out.write_text("keep-me", encoding="utf-8")

    rc = performance_main(
        [
            "--execute",
            "--api-base-url",
            "http://127.0.0.1:58080",
            "--kb-id",
            "11111111-1111-1111-1111-111111111111",
            "--out",
            str(out),
        ],
        environ={"PERF_USERNAME": "u", "PERF_PASSWORD": "p", "PERF_QUERY": "q"},
    )

    assert rc == 1
    assert out.read_text(encoding="utf-8") == "keep-me"


# ---------------------------------------------------------------------------
# 真实（fake HTTP）执行路径


SECRET_QUERY = "secret-query-should-not-appear"
SECRET_QUESTION = "secret-question-should-not-appear"
SECRET_PASSWORD = "secret-password-should-not-appear"


def _login_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/api/v1/auth/login"
    assert request.headers["Origin"] == "http://127.0.0.1:58080"
    body = json.loads(request.content)
    assert body["username"] == "perf-user"
    assert body["password"] == SECRET_PASSWORD
    return httpx.Response(200, json={"csrfToken": "csrf-token-value"})


def _read_report(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_retrieval_execute_records_success_metrics(tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    channels: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/login":
            return _login_handler(request)
        channels.append(request.url.path)
        assert request.headers["Origin"] == "http://127.0.0.1:58080"
        return httpx.Response(200, json={"candidates": []})

    rc = performance_main(
        [
            "--execute",
            "--count",
            "4",
            "--api-base-url",
            "http://127.0.0.1:58080",
            "--kb-id",
            "11111111-1111-1111-1111-111111111111",
            "--out",
            str(out),
        ],
        environ={
            "PERF_USERNAME": "perf-user",
            "PERF_PASSWORD": SECRET_PASSWORD,
            "PERF_QUERY": SECRET_QUERY,
        },
        transport=httpx.MockTransport(handler),
        clock=StepClock(0.01),
    )

    assert rc == 0
    payload = _read_report(out)
    assert payload["mode"] == "retrieval"
    assert payload["endpoint"] == "/api/v1/retrieval/search"
    assert payload["count"] == 4
    assert payload["successCount"] == 4
    assert payload["failureCount"] == 0
    assert payload["failureRate"] == 0.0
    latency = payload["latencyMs"]
    assert isinstance(latency, dict)
    assert latency["sampleCount"] == 4
    assert latency["p50"] is not None and latency["p95"] is not None
    window = payload["window"]
    assert isinstance(window, dict)
    assert window["throughputRequestsPerSecond"] is not None
    assert channels == ["/api/v1/retrieval/search"] * 4
    text = out.read_text(encoding="utf-8")
    for secret in (SECRET_QUERY, SECRET_PASSWORD, "csrf-token-value"):
        assert secret not in text


def test_retrieval_execute_keeps_failures_in_denominator(tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/login":
            return _login_handler(request)
        calls["n"] += 1
        status = 500 if calls["n"] <= 2 else 200
        return httpx.Response(status, json={})

    rc = performance_main(
        [
            "--execute",
            "--count",
            "3",
            "--api-base-url",
            "http://127.0.0.1:58080",
            "--kb-id",
            "11111111-1111-1111-1111-111111111111",
            "--out",
            str(out),
        ],
        environ={
            "PERF_USERNAME": "perf-user",
            "PERF_PASSWORD": SECRET_PASSWORD,
            "PERF_QUERY": SECRET_QUERY,
        },
        transport=httpx.MockTransport(handler),
        clock=StepClock(0.02),
    )

    assert rc == 0
    payload = _read_report(out)
    assert payload["count"] == 3
    assert payload["successCount"] == 1
    assert payload["failureCount"] == 2
    assert payload["failureRate"] == pytest.approx(2 / 3)
    latency = payload["latencyMs"]
    assert isinstance(latency, dict)
    assert latency["sampleCount"] == 3, "失败也计入延迟分母"


def test_qa_execute_creates_conversation_and_asks_with_csrf(tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/login":
            return _login_handler(request)
        seen.append((request.url.path, request.headers.get("X-CSRF-Token")))
        if request.url.path == "/api/v1/conversations":
            return httpx.Response(
                201, json={"conversationId": "22222222-2222-2222-2222-222222222222"}
            )
        return httpx.Response(200, json={"answer": "hidden", "citations": []})

    rc = performance_main(
        [
            "--execute",
            "--mode",
            "qa",
            "--allow-paid-llm",
            "--count",
            "2",
            "--api-base-url",
            "http://127.0.0.1:58080",
            "--kb-id",
            "11111111-1111-1111-1111-111111111111",
            "--out",
            str(out),
        ],
        environ={
            "PERF_USERNAME": "perf-user",
            "PERF_PASSWORD": SECRET_PASSWORD,
            "PERF_QUESTION": SECRET_QUESTION,
        },
        transport=httpx.MockTransport(handler),
        clock=StepClock(0.01),
    )

    assert rc == 0
    payload = _read_report(out)
    assert payload["mode"] == "qa"
    assert payload["successCount"] == 2
    llm = payload["llm"]
    assert isinstance(llm, dict)
    assert llm["cloudLlmCallsAuthorized"] is True
    assert seen == [
        ("/api/v1/conversations", "csrf-token-value"),
        ("/api/v1/conversations/22222222-2222-2222-2222-222222222222/messages", "csrf-token-value"),
        ("/api/v1/conversations", "csrf-token-value"),
        ("/api/v1/conversations/22222222-2222-2222-2222-222222222222/messages", "csrf-token-value"),
    ]
    text = out.read_text(encoding="utf-8")
    for secret in (SECRET_QUESTION, SECRET_PASSWORD, "csrf-token-value", "hidden"):
        assert secret not in text


def test_qa_setup_failure_is_counted_and_no_ask_is_sent(tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/login":
            return _login_handler(request)
        paths.append(request.url.path)
        return httpx.Response(500, json={})

    rc = performance_main(
        [
            "--execute",
            "--mode",
            "qa",
            "--allow-paid-llm",
            "--count",
            "2",
            "--api-base-url",
            "http://127.0.0.1:58080",
            "--kb-id",
            "11111111-1111-1111-1111-111111111111",
            "--out",
            str(out),
        ],
        environ={
            "PERF_USERNAME": "perf-user",
            "PERF_PASSWORD": SECRET_PASSWORD,
            "PERF_QUESTION": SECRET_QUESTION,
        },
        transport=httpx.MockTransport(handler),
        clock=StepClock(0.01),
    )

    assert rc == 0
    payload = _read_report(out)
    assert payload["successCount"] == 0
    assert payload["failureCount"] == 2
    assert paths == ["/api/v1/conversations", "/api/v1/conversations"]


def test_memory_observation_is_sampled_only_when_project_given(tmp_path: Path) -> None:
    sampler = FakeSampler(
        [
            ContainerMemorySnapshot("api", 1234, 512 * 1024**2, 0.35),
            ContainerMemorySnapshot("worker", None, None, None, "container memory usage absent"),
        ]
    )
    out = tmp_path / "report.json"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/login":
            return _login_handler(request)
        return httpx.Response(200, json={"candidates": []})

    rc = performance_main(
        [
            "--execute",
            "--count",
            "1",
            "--api-base-url",
            "http://127.0.0.1:58080",
            "--kb-id",
            "11111111-1111-1111-1111-111111111111",
            "--compose-project",
            "myrag-perf",
            "--out",
            str(out),
        ],
        environ={
            "PERF_USERNAME": "perf-user",
            "PERF_PASSWORD": SECRET_PASSWORD,
            "PERF_QUERY": SECRET_QUERY,
        },
        transport=httpx.MockTransport(handler),
        clock=StepClock(0.01),
        sampler=sampler,
    )

    assert rc == 0
    payload = _read_report(out)
    resource = payload["resourceObservation"]
    assert isinstance(resource, dict)
    assert resource["sampled"] is True
    services = {item["service"]: item for item in resource["services"]}
    assert services["api"]["sampledPeakBytes"] == 1234
    assert services["worker"]["sampledPeakBytes"] is None
    assert services["worker"]["observedLimitBytes"] is None
    assert len(sampler.projects) >= 1
    assert set(sampler.projects) == {"myrag-perf"}


def test_memory_observation_absent_without_project(tmp_path: Path) -> None:
    out = tmp_path / "report.json"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/login":
            return _login_handler(request)
        return httpx.Response(200, json={"candidates": []})

    rc = performance_main(
        [
            "--execute",
            "--count",
            "1",
            "--api-base-url",
            "http://127.0.0.1:58080",
            "--kb-id",
            "11111111-1111-1111-1111-111111111111",
            "--out",
            str(out),
        ],
        environ={
            "PERF_USERNAME": "perf-user",
            "PERF_PASSWORD": SECRET_PASSWORD,
            "PERF_QUERY": SECRET_QUERY,
        },
        transport=httpx.MockTransport(handler),
        clock=StepClock(0.01),
        sampler=_exploding_sampler,
    )

    assert rc == 0
    resource = _read_report(out)["resourceObservation"]
    assert isinstance(resource, dict)
    assert resource["sampled"] is False
    assert resource["services"] == []


def test_report_payload_defines_metrics_and_limits() -> None:
    report = build_report(
        mode="retrieval",
        api_base_url="http://127.0.0.1:58080",
        count=2,
        concurrency=1,
        records=[
            RequestRecord(0, True, 5.0, 200, None),
            RequestRecord(1, False, 7.0, 500, "request"),
        ],
        elapsed_seconds=1.0,
        memory_sampled=False,
        memory=(),
        memory_sample_interval_seconds=None,
        created_at="2026-09-30T00:00:00+00:00",
        write_cloud_llm=False,
    )

    payload = report_payload(report)

    definitions = payload["definitions"]
    assert isinstance(definitions, dict)
    assert "nearest-rank" in str(definitions["latency"])
    assert "denominator" in str(definitions["latency"])
    assert payload["failureCount"] == 1
    llm = payload["llm"]
    assert isinstance(llm, dict)
    assert set(llm) == {"cloudLlmCallsAuthorized"}
    # 报告必须能被严格 JSON 序列化（无 NaN/Infinity）。
    encoded = json.dumps(payload, allow_nan=False)
    assert "NaN" not in encoded


# ---------------------------------------------------------------------------
# 真实 collector 路径（fake `_run_docker`，不执行真 Docker）


FULL_CONTAINER_ID = "a" * 64
OTHER_CONTAINER_ID = "b" * 64


def _fake_docker_run(
    outputs: dict[str, str | None], *, require_no_trunc: bool = True
) -> Callable[..., str | None]:
    def fake(args: Sequence[str], *, timeout_seconds: float) -> str | None:
        command = args[0]
        if require_no_trunc and command in ("ps", "stats"):
            assert "--no-trunc" in args, "ps/stats 必须用 --no-trunc 保证容器 ID 不被截断"
        return outputs.get(command)

    return fake


def test_docker_collector_matches_full_ids_for_limits_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ps_output = f"{FULL_CONTAINER_ID}\tapi\n{OTHER_CONTAINER_ID}\tworker\n"
    inspect_output = (
        f"{FULL_CONTAINER_ID}\t536870912\t350000000\n"
        f"{OTHER_CONTAINER_ID}\t671088640\t250000000\n"
    )
    stats_output = (
        f"{FULL_CONTAINER_ID}\t120MiB / 512MiB\n"
        f"{OTHER_CONTAINER_ID}\t300MiB / 640MiB\n"
    )
    monkeypatch.setattr(
        performance_module,
        "_run_docker",
        _fake_docker_run({"ps": ps_output, "inspect": inspect_output, "stats": stats_output}),
    )

    snapshots = {item.service: item for item in docker_memory_snapshot("proj")}

    api = snapshots["api"]
    assert api.mem_usage_bytes == 120 * 1024**2
    assert api.mem_limit_bytes == 536870912
    assert api.cpu_limit == pytest.approx(0.35)
    assert api.error is None
    worker = snapshots["worker"]
    assert worker.mem_usage_bytes == 300 * 1024**2
    assert worker.mem_limit_bytes == 671088640
    assert worker.cpu_limit == pytest.approx(0.25)
    assert worker.error is None


def test_docker_collector_surfaces_ps_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        performance_module,
        "_run_docker",
        _fake_docker_run({"ps": None}, require_no_trunc=False),
    )

    snapshots = docker_memory_snapshot("proj")

    assert len(snapshots) == 1
    assert snapshots[0].service == "proj"
    assert snapshots[0].mem_usage_bytes is None
    assert snapshots[0].error is not None


def test_docker_collector_surfaces_project_without_containers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        performance_module,
        "_run_docker",
        _fake_docker_run({"ps": ""}, require_no_trunc=False),
    )

    snapshots = docker_memory_snapshot("wrong-project")

    assert len(snapshots) == 1
    assert snapshots[0].service == "wrong-project"
    assert snapshots[0].error is not None


def test_docker_collector_surfaces_inspect_and_stats_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ps_output = f"{FULL_CONTAINER_ID}\tapi\n"
    monkeypatch.setattr(
        performance_module,
        "_run_docker",
        _fake_docker_run({"ps": ps_output, "inspect": None, "stats": None}),
    )

    api = docker_memory_snapshot("proj")[0]

    assert api.mem_usage_bytes is None
    assert api.mem_limit_bytes is None
    assert api.cpu_limit is None
    assert api.error is not None
    assert "limits" in api.error
    assert "usage" in api.error


def test_docker_collector_does_not_silently_succeed_on_id_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 模拟 ps/stats 回报短 ID、inspect 回报长 ID 的错配：必须显式记错而不是静默把 limit 当 unknown。
    short_id = FULL_CONTAINER_ID[:12]
    monkeypatch.setattr(
        performance_module,
        "_run_docker",
        _fake_docker_run(
            {
                "ps": f"{short_id}\tapi\n",
                "inspect": f"{FULL_CONTAINER_ID}\t536870912\t350000000\n",
                "stats": f"{short_id}\t120MiB / 512MiB\n",
            },
            require_no_trunc=False,
        ),
    )

    api = docker_memory_snapshot("proj")[0]

    assert api.mem_limit_bytes is None
    assert api.error is not None
    assert "limits" in api.error


# ---------------------------------------------------------------------------
# origin 护栏与 IPv6


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://user:pass@127.0.0.1:58080",
        "http://127.0.0.1:58080/api/v1",
        "http://127.0.0.1:58080?token=abc",
        "http://127.0.0.1:58080#frag",
    ],
)
def test_origin_guard_rejects_credentials_path_query_fragment(bad_url: str) -> None:
    with pytest.raises(PerformanceError):
        _origin_of(bad_url)


def test_origin_of_brackets_ipv6_host() -> None:
    assert _origin_of("http://[::1]:58080") == "http://[::1]:58080"
    assert _origin_of("http://127.0.0.1:58080") == "http://127.0.0.1:58080"


# ---------------------------------------------------------------------------
# 代理环境隔离与窗口内采样


def test_http_client_disables_env_proxy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}
    real_client = httpx.Client

    def factory(**kwargs: Any) -> httpx.Client:
        captured.update(kwargs)
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "Client", factory)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/login":
            return _login_handler(request)
        return httpx.Response(200, json={"candidates": []})

    rc = performance_main(
        [
            "--execute",
            "--count",
            "1",
            "--api-base-url",
            "http://127.0.0.1:58080",
            "--kb-id",
            "11111111-1111-1111-1111-111111111111",
            "--out",
            str(tmp_path / "report.json"),
        ],
        environ={
            "PERF_USERNAME": "perf-user",
            "PERF_PASSWORD": SECRET_PASSWORD,
            "PERF_QUERY": SECRET_QUERY,
        },
        transport=httpx.MockTransport(handler),
        clock=StepClock(0.01),
    )

    assert rc == 0
    assert captured["trust_env"] is False
    assert captured["verify"] is True


class WindowSampler:
    """第 2 次调用（窗口内首次）置事件，供 HTTP handler 事件等待。"""

    def __init__(self) -> None:
        self.calls = 0
        self.projects: list[str] = []
        self.first_window = threading.Event()

    def __call__(
        self, compose_project: str, stop_event: threading.Event
    ) -> Sequence[ContainerMemorySnapshot]:
        self.projects.append(compose_project)
        self.calls += 1
        if self.calls == 2:
            self.first_window.set()
        return [ContainerMemorySnapshot("api", 1000 + self.calls, 512 * 1024**2, 0.35)]


def test_window_sampling_runs_during_http_window(tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    sampler = WindowSampler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/login":
            return _login_handler(request)
        assert sampler.first_window.wait(timeout=5.0), "后台窗口采样未在 HTTP 窗口内发生"
        return httpx.Response(200, json={"candidates": []})

    rc = performance_main(
        [
            "--execute",
            "--count",
            "1",
            "--api-base-url",
            "http://127.0.0.1:58080",
            "--kb-id",
            "11111111-1111-1111-1111-111111111111",
            "--compose-project",
            "myrag-perf",
            "--memory-sample-interval-seconds",
            "0.001",
            "--out",
            str(out),
        ],
        environ={
            "PERF_USERNAME": "perf-user",
            "PERF_PASSWORD": SECRET_PASSWORD,
            "PERF_QUERY": SECRET_QUERY,
        },
        transport=httpx.MockTransport(handler),
        clock=StepClock(0.01),
        sampler=sampler,
    )

    assert rc == 0
    resource = _read_report(out)["resourceObservation"]
    assert isinstance(resource, dict)
    assert resource["sampled"] is True
    assert resource["sampleIntervalSeconds"] == 0.001
    api_items = [item for item in resource["services"] if item["service"] == "api"]
    assert api_items
    assert api_items[0]["sampleCount"] == sampler.calls
    assert api_items[0]["sampledPeakBytes"] == 1000 + sampler.calls
    assert sampler.calls >= 2, "应包含窗口前与窗口内采样"
    assert set(sampler.projects) == {"myrag-perf"}


# ---------------------------------------------------------------------------
# 采样取消契约与线程生命周期


def _sampler_thread_count() -> int:
    return sum(1 for item in threading.enumerate() if item.name == "perf-memory-sampler")


def test_default_sampler_cancels_between_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    stop_event = threading.Event()

    def fake(args: Sequence[str], *, timeout_seconds: float) -> str | None:
        calls.append(args[0])
        if args[0] == "ps":
            stop_event.set()  # 模拟 ps 期间收到停止信号
            return f"{FULL_CONTAINER_ID}\tapi\n"
        return None

    monkeypatch.setattr(performance_module, "_run_docker", fake)

    snapshots = docker_memory_snapshot("proj", stop_event)

    assert snapshots == ()
    assert calls == ["ps"], "停止后不得再执行 inspect/stats"


def test_running_default_sampler_can_be_stopped_and_joined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_event = threading.Event()
    entered = threading.Event()

    def fake(args: Sequence[str], *, timeout_seconds: float) -> str | None:
        if args[0] == "ps":
            entered.set()
            stop_event.wait(timeout=5.0)  # 可被取消唤醒的长命令
            return f"{FULL_CONTAINER_ID}\tapi\n"
        return None

    monkeypatch.setattr(performance_module, "_run_docker", fake)
    result: list[Sequence[ContainerMemorySnapshot]] = []

    def run() -> None:
        result.append(docker_memory_snapshot("proj", stop_event))

    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(timeout=5.0)
    stop_event.set()
    thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert result == [()]


def test_client_construction_failure_leaves_no_sampler_thread(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def factory(**kwargs: Any) -> httpx.Client:
        raise httpx.HTTPError("synthetic construction failure")

    monkeypatch.setattr(httpx, "Client", factory)

    rc = performance_main(
        [
            "--execute",
            "--count",
            "1",
            "--api-base-url",
            "http://127.0.0.1:58080",
            "--kb-id",
            "11111111-1111-1111-1111-111111111111",
            "--compose-project",
            "myrag-perf",
            "--out",
            str(tmp_path / "report.json"),
        ],
        environ={
            "PERF_USERNAME": "perf-user",
            "PERF_PASSWORD": SECRET_PASSWORD,
            "PERF_QUERY": SECRET_QUERY,
        },
        sampler=_exploding_sampler,
    )

    assert rc == 1
    assert "构造失败" in capsys.readouterr().err
    assert _sampler_thread_count() == 0


def test_login_failure_leaves_no_sampler_thread(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    out = tmp_path / "report.json"
    rc = performance_main(
        [
            "--execute",
            "--count",
            "1",
            "--api-base-url",
            "http://127.0.0.1:58080",
            "--kb-id",
            "11111111-1111-1111-1111-111111111111",
            "--compose-project",
            "myrag-perf",
            "--out",
            str(out),
        ],
        environ={
            "PERF_USERNAME": "perf-user",
            "PERF_PASSWORD": SECRET_PASSWORD,
            "PERF_QUERY": SECRET_QUERY,
        },
        transport=httpx.MockTransport(handler),
        clock=StepClock(0.01),
        sampler=_exploding_sampler,
    )

    assert rc == 1
    assert "登录失败" in capsys.readouterr().err
    assert not out.exists()
    assert _sampler_thread_count() == 0


def test_stuck_sampler_fails_without_report_or_late_samples(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(performance_module, "MEMORY_SAMPLER_JOIN_TIMEOUT_SECONDS", 0.05)
    release = threading.Event()

    class BlockingSampler:
        def __init__(self) -> None:
            self.calls = 0
            self.entered = threading.Event()

        def __call__(
            self, compose_project: str, stop_event: threading.Event
        ) -> Sequence[ContainerMemorySnapshot]:
            self.calls += 1
            if self.calls >= 2:
                self.entered.set()
                release.wait(timeout=5.0)  # 故意忽略取消契约
            return [ContainerMemorySnapshot("api", 1, 1, 1.0)]

    out = tmp_path / "report.json"
    sampler = BlockingSampler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/login":
            return _login_handler(request)
        assert sampler.entered.wait(timeout=5.0), "后台采样未进入窗口内调用"
        return httpx.Response(200, json={"candidates": []})

    try:
        rc = performance_main(
            [
                "--execute",
                "--count",
                "1",
                "--api-base-url",
                "http://127.0.0.1:58080",
                "--kb-id",
                "11111111-1111-1111-1111-111111111111",
                "--compose-project",
                "myrag-perf",
                "--memory-sample-interval-seconds",
                "0.001",
                "--out",
                str(out),
            ],
            environ={
                "PERF_USERNAME": "perf-user",
                "PERF_PASSWORD": SECRET_PASSWORD,
                "PERF_QUERY": SECRET_QUERY,
            },
            transport=httpx.MockTransport(handler),
            clock=StepClock(0.01),
            sampler=sampler,
        )
        assert rc == 1
        assert "停止" in capsys.readouterr().err
        assert not out.exists()
    finally:
        release.set()
        for item in threading.enumerate():
            if item.name == "perf-memory-sampler":
                item.join(timeout=5.0)
    assert _sampler_thread_count() == 0


def test_conforming_cancelled_sampler_stops_cleanly(tmp_path: Path) -> None:
    class ConformingSampler:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(
            self, compose_project: str, stop_event: threading.Event
        ) -> Sequence[ContainerMemorySnapshot]:
            self.calls += 1
            if self.calls >= 2:
                stop_event.wait(timeout=5.0)  # 遵从取消契约
                return ()
            return [ContainerMemorySnapshot("api", 7, 8, 0.1)]

    out = tmp_path / "report.json"
    sampler = ConformingSampler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/login":
            return _login_handler(request)
        return httpx.Response(200, json={"candidates": []})

    rc = performance_main(
        [
            "--execute",
            "--count",
            "1",
            "--api-base-url",
            "http://127.0.0.1:58080",
            "--kb-id",
            "11111111-1111-1111-1111-111111111111",
            "--compose-project",
            "myrag-perf",
            "--memory-sample-interval-seconds",
            "0.001",
            "--out",
            str(out),
        ],
        environ={
            "PERF_USERNAME": "perf-user",
            "PERF_PASSWORD": SECRET_PASSWORD,
            "PERF_QUERY": SECRET_QUERY,
        },
        transport=httpx.MockTransport(handler),
        clock=StepClock(0.01),
        sampler=sampler,
    )

    assert rc == 0
    resource = _read_report(out)["resourceObservation"]
    assert isinstance(resource, dict)
    services = {item["service"]: item for item in resource["services"]}
    assert services["api"]["sampleCount"] == 1, "取消后的空样本不得计入"
    assert _sampler_thread_count() == 0
