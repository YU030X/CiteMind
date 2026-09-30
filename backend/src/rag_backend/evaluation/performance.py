"""``python -m rag_backend.evaluation.performance``：Phase 4 最小性能手动采集入口。

默认 **dry-run**：只做参数与护栏校验并打印计划，**不联网、不读环境变量、不执行 Docker、不写文件**。
真实采集必须显式 ``--execute``；``retrieval`` 模式只调用既有检索 HTTP 接口，``qa`` 模式可能产生
付费 LLM 调用，因此另外必须显式 ``--allow-paid-llm``。本入口不是通用压测平台，只实现最小串行
并发 1（``--concurrency`` 目前只接受 1）。

口径边界：

- 延迟是每请求在客户端测得的端到端墙钟时间（含请求发送与响应读取，单位毫秒）；失败与超时仍计入
  成功率分母和延迟样本，不剔除。``p50``/``p95`` 复用既有评估的 nearest-rank 定义，不插值。
- 吞吐是时间窗口吞吐：``count / elapsedSeconds``，窗口为从首个请求开始到最后一个请求结束的墙钟；
  它不是稳态 QPS，也不是容量测试结论。
- 凭据（用户名/密码）、问题、答案、Cookie 与 CSRF 令牌都不写入报告、不回显；
  只从显式命名的环境变量读取。
- 内存观察只针对指定 Compose project 的容器，区分采样峰值与观察到的容器内存上限；缺值一律记为
  ``null``（未知）而非 0。观察到的上限是 Docker 实际报告的生效配置，不是本仓 overlay 的声明目标，
  也不代表性能达标。
- 真实 BGE/完整问答 p95 仍需在真实 inference 与显式付费授权下另行测量；本切片只提供离线可测的
  纯函数与 fake 运行时。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from rag_backend.auth.tokens import CSRF_HEADER_NAME
from rag_backend.evaluation.analysis import summarize_latency

LOGIN_ENDPOINT = "/api/v1/auth/login"
RETRIEVAL_ENDPOINT = "/api/v1/retrieval/search"
CONVERSATIONS_ENDPOINT = "/api/v1/conversations"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MEMORY_SAMPLE_INTERVAL_SECONDS = 1.0
MEMORY_SAMPLER_JOIN_TIMEOUT_SECONDS = 30.0
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})
VALID_MODES = ("retrieval", "qa")

# 报告里固定的口径说明；不得把计划目标写成已测结论。
MEASURED_OPERATION_BY_MODE = {
    "retrieval": "one POST /api/v1/retrieval/search request",
    "qa": "one conversation creation plus one POST /api/v1/conversations/{id}/messages ask",
}
ENDPOINT_BY_MODE = {
    "retrieval": RETRIEVAL_ENDPOINT,
    "qa": "/api/v1/conversations/{conversationId}/messages",
}
LATENCY_DEFINITION = (
    "end-to-end client wall-clock per measured request, in milliseconds; "
    "failures and timeouts are included and not removed from the denominator; "
    "p50/p95 use nearest-rank (ceil(p*n)-1, 0-based) without interpolation"
)
THROUGHPUT_DEFINITION = (
    "count / elapsedSeconds over the wall-clock window from the first request start to the "
    "last request end; a time-window figure, not a steady-state capacity result"
)
RESOURCE_DEFINITION = (
    "per-service container observations for the named Compose project: sampled peak of reported "
    "container memory usage and the memory/CPU limits Docker actually reports, sampled on a "
    "bounded background interval during the HTTP window; missing values are null (unknown), "
    "never 0; sampling failures are reported in errors; container memory is not host RSS, the "
    "sampled peak is not an exact RSS and excludes the runtime outside these containers"
)


class PerformanceError(Exception):
    """静态错误；消息不回显凭据、问题、答案、Cookie、CSRF 或响应正文。"""


# ---------------------------------------------------------------------------
# 内存观察


@dataclass(frozen=True, slots=True)
class ContainerMemorySnapshot:
    """一次采样的单个服务容器观察；任一项缺失都是 ``None``（未知）。"""

    service: str
    mem_usage_bytes: int | None
    mem_limit_bytes: int | None
    cpu_limit: float | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryObservation:
    """汇总到服务粒度的内存观察；``sampled_peak_bytes`` 为各次采样非空使用量的最大值。"""

    service: str
    sample_count: int
    sampled_peak_bytes: int | None
    observed_limit_bytes: int | None
    cpu_limit: float | None
    errors: tuple[str, ...]


class MemorySampler(Protocol):
    """按 Compose project 返回一轮容器观察。

    ``stop_event`` 是取消契约：实现必须在各只读命令之间检查它，已置位时不得再发新命令，
    可直接返回空序列丢弃未完成样本（该空结果不会计为采样）。实现仍须自行捕获真实采样失败
    并写入 ``error``。
    """

    def __call__(
        self, compose_project: str, stop_event: threading.Event
    ) -> Sequence[ContainerMemorySnapshot]: ...


def aggregate_memory(
    snapshots: Sequence[Sequence[ContainerMemorySnapshot]],
) -> tuple[MemoryObservation, ...]:
    """把多轮采样聚合为按服务名升序的观察；未知保持 ``None``，不填 0。"""

    services: dict[str, dict[str, Any]] = {}
    for round_snapshots in snapshots:
        for snapshot in round_snapshots:
            entry = services.setdefault(
                snapshot.service,
                {"count": 0, "peak": None, "limit": None, "cpu": None, "errors": []},
            )
            entry["count"] = int(entry["count"]) + 1
            usage = snapshot.mem_usage_bytes
            if usage is not None:
                peak = entry["peak"]
                entry["peak"] = usage if peak is None else max(int(peak), usage)
            if snapshot.mem_limit_bytes is not None:
                entry["limit"] = snapshot.mem_limit_bytes
            if snapshot.cpu_limit is not None:
                entry["cpu"] = snapshot.cpu_limit
            if snapshot.error:
                entry["errors"].append(snapshot.error)

    observations: list[MemoryObservation] = []
    for service in sorted(services):
        entry = services[service]
        errors = tuple(sorted(set(entry["errors"])))
        observations.append(
            MemoryObservation(
                service=service,
                sample_count=int(entry["count"]),
                sampled_peak_bytes=entry["peak"],
                observed_limit_bytes=entry["limit"],
                cpu_limit=entry["cpu"],
                errors=errors,
            )
        )
    return tuple(observations)


_DOCKER_BYTE_SUFFIXES: tuple[tuple[str, int], ...] = (
    ("kib", 1024),
    ("mib", 1024**2),
    ("gib", 1024**3),
    ("tib", 1024**4),
    ("kb", 1000),
    ("mb", 1000**2),
    ("gb", 1000**3),
    ("tb", 1000**4),
    ("b", 1),
)


def parse_docker_bytes(value: str) -> int | None:
    """解析 ``docker stats`` 的 ``12.3MiB``/``1GiB`` 等大小；无法解析返回 ``None``。"""

    text = value.strip()
    if not text:
        return None
    lowered = text.lower()
    for suffix, factor in _DOCKER_BYTE_SUFFIXES:
        if lowered.endswith(suffix):
            number = lowered[: -len(suffix)].strip()
            try:
                amount = float(number)
            except ValueError:
                return None
            if not math.isfinite(amount) or amount < 0:
                return None
            return int(amount * factor)
    return None


def _run_docker(args: Sequence[str], *, timeout_seconds: float) -> str | None:
    """执行只读 docker 命令并返回 stdout；任何失败返回 ``None``，不回显 stderr。"""

    try:
        completed = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def _docker_container_services(
    compose_project: str, *, timeout_seconds: float
) -> tuple[dict[str, str], str | None]:
    """返回 ``(container_id -> service, error)``；只按 project 标签过滤。

    ``--no-trunc`` 保证 ps 返回 64 位容器 ID，与 ``docker inspect .Id`` 可直接匹配；命令失败或
    该 project 没有容器时返回显式 error，绝不静默当作空采样成功。
    """

    output = _run_docker(
        [
            "ps",
            "--no-trunc",
            "--filter",
            f"label=com.docker.compose.project={compose_project}",
            "--format",
            '{{.ID}}\t{{.Label "com.docker.compose.service"}}',
        ],
        timeout_seconds=timeout_seconds,
    )
    if output is None:
        return {}, "docker ps did not report containers"
    mapping: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        container_id, service = parts[0].strip(), parts[1].strip()
        if container_id and service:
            mapping[container_id] = service
    if not mapping:
        return {}, "no containers found for the Compose project"
    return mapping, None


def _docker_limits(
    container_ids: Sequence[str], *, timeout_seconds: float
) -> tuple[dict[str, tuple[int | None, float | None]], str | None]:
    """返回 ``(container_id -> (memory_limit_bytes, cpu_limit), error)``。"""

    if not container_ids:
        return {}, None
    output = _run_docker(
        [
            "inspect",
            "--format",
            "{{.Id}}\t{{.HostConfig.Memory}}\t{{.HostConfig.NanoCpus}}",
            *container_ids,
        ],
        timeout_seconds=timeout_seconds,
    )
    if output is None:
        return {}, "docker inspect did not report container limits"
    limits: dict[str, tuple[int | None, float | None]] = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        container_id = parts[0].strip()
        try:
            memory_bytes = int(parts[1])
        except ValueError:
            memory_bytes = 0
        try:
            nano_cpus = int(parts[2])
        except ValueError:
            nano_cpus = 0
        limits[container_id] = (
            memory_bytes if memory_bytes > 0 else None,
            (nano_cpus / 1_000_000_000) if nano_cpus > 0 else None,
        )
    return limits, None


def docker_memory_snapshot(
    compose_project: str,
    stop_event: threading.Event | None = None,
    *,
    timeout_seconds: float = 8.0,
) -> Sequence[ContainerMemorySnapshot]:
    """真实默认采样器：对指定 Compose project 的容器执行只读 ``docker ps``/``inspect``/``stats``。

    取消契约：在各只读命令之间检查 ``stop_event``；已置位时立即返回空序列丢弃本次未完成样本，
    不再发下一条命令。单条命令仍由 ``subprocess.run(timeout=...)`` 严格限时（默认 8 秒，超时会
    kill+wait），因此停止后最多只余下一条命令的耗时。真实采样失败（非取消）仍返回带 ``error``
    的观察，不静默成功。
    """

    event = stop_event if stop_event is not None else threading.Event()
    if event.is_set():
        return ()
    services, ps_error = _docker_container_services(
        compose_project, timeout_seconds=timeout_seconds
    )
    if event.is_set():
        return ()
    if ps_error is not None:
        return (
            ContainerMemorySnapshot(
                service=compose_project,
                mem_usage_bytes=None,
                mem_limit_bytes=None,
                cpu_limit=None,
                error=ps_error,
            ),
        )
    limits, limits_error = _docker_limits(list(services), timeout_seconds=timeout_seconds)
    if event.is_set():
        return ()
    output = _run_docker(
        [
            "stats",
            "--no-stream",
            "--no-trunc",
            "--format",
            "{{.ID}}\t{{.MemUsage}}",
            *services.keys(),
        ],
        timeout_seconds=timeout_seconds,
    )
    if event.is_set():
        return ()
    usage_by_id: dict[str, int | None] = {}
    stats_error: str | None = None
    if output is None:
        stats_error = "docker stats did not report container memory usage"
    else:
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) != 2:
                continue
            usage_part = parts[1].split("/", 1)[0].strip()
            usage_by_id[parts[0].strip()] = parse_docker_bytes(usage_part)

    snapshots: list[ContainerMemorySnapshot] = []
    for container_id, service in sorted(services.items(), key=lambda item: item[1]):
        memory_limit, cpu_limit = limits.get(container_id, (None, None))
        usage = usage_by_id.get(container_id)
        errors: list[str] = []
        if limits_error is not None or container_id not in limits:
            errors.append("container limits were not observed")
        if stats_error is not None or container_id not in usage_by_id:
            errors.append("container memory usage was not observed")
        elif usage is None:
            errors.append("container memory usage could not be parsed")
        snapshots.append(
            ContainerMemorySnapshot(
                service=service,
                mem_usage_bytes=usage,
                mem_limit_bytes=memory_limit,
                cpu_limit=cpu_limit,
                error="; ".join(errors) or None,
            )
        )
    return snapshots


# ---------------------------------------------------------------------------
# 请求测量


@dataclass(frozen=True, slots=True)
class RequestRecord:
    """一次测量样本；``latency_ms`` 为 ``None`` 表示连墙钟都无法测得。"""

    index: int
    ok: bool
    latency_ms: float | None
    status_code: int | None
    failure_stage: str | None


@dataclass(frozen=True, slots=True)
class LatencyStats:
    sample_count: int
    min_ms: float | None
    mean_ms: float | None
    p50_ms: float | None
    p95_ms: float | None
    max_ms: float | None


@dataclass(frozen=True, slots=True)
class PerformanceReport:
    mode: str
    api_base_url: str
    count: int
    concurrency: int
    success_count: int
    failure_count: int
    failure_rate: float
    elapsed_seconds: float
    throughput_rps: float | None
    latency: LatencyStats
    memory_sampled: bool
    memory: tuple[MemoryObservation, ...]
    memory_sample_interval_seconds: float | None
    created_at: str
    write_cloud_llm: bool


def summarize_latencies(records: Sequence[RequestRecord]) -> LatencyStats:
    """对全部测得延迟（含失败）计算 min/mean/p50/p95/max；无样本返回全 ``None``。"""

    values = [record.latency_ms for record in records if record.latency_ms is not None]
    if not values:
        return LatencyStats(0, None, None, None, None, None)
    summary = summarize_latency(values)
    return LatencyStats(
        sample_count=len(values),
        min_ms=min(values),
        mean_ms=summary.mean_ms,
        p50_ms=summary.p50_ms,
        p95_ms=summary.p95_ms,
        max_ms=summary.max_ms,
    )


def build_report(
    *,
    mode: str,
    api_base_url: str,
    count: int,
    concurrency: int,
    records: Sequence[RequestRecord],
    elapsed_seconds: float,
    memory_sampled: bool,
    memory: Sequence[MemoryObservation],
    memory_sample_interval_seconds: float | None,
    created_at: str,
    write_cloud_llm: bool,
) -> PerformanceReport:
    """纯函数：由测量记录构造报告对象；不发起任何请求、不读环境。"""

    if count <= 0:
        raise PerformanceError("count 必须为正数")
    if len(records) != count:
        raise PerformanceError("测量记录数量与 count 不一致")
    success_count = sum(1 for record in records if record.ok)
    failure_count = count - success_count
    elapsed = max(0.0, elapsed_seconds)
    throughput = (count / elapsed) if elapsed > 0 else None
    return PerformanceReport(
        mode=mode,
        api_base_url=api_base_url,
        count=count,
        concurrency=concurrency,
        success_count=success_count,
        failure_count=failure_count,
        failure_rate=failure_count / count,
        elapsed_seconds=elapsed,
        throughput_rps=throughput,
        latency=summarize_latencies(records),
        memory_sampled=memory_sampled,
        memory=tuple(memory),
        memory_sample_interval_seconds=memory_sample_interval_seconds,
        created_at=created_at,
        write_cloud_llm=write_cloud_llm,
    )


def report_payload(report: PerformanceReport) -> dict[str, Any]:
    """把报告转成 camelCase JSON payload；不含任何凭据、问题、答案或 Cookie。"""

    latency = report.latency
    return {
        "generatedAt": report.created_at,
        "mode": report.mode,
        "endpoint": ENDPOINT_BY_MODE[report.mode],
        "measuredOperation": MEASURED_OPERATION_BY_MODE[report.mode],
        "apiBaseUrl": report.api_base_url,
        "count": report.count,
        "concurrency": report.concurrency,
        "successCount": report.success_count,
        "failureCount": report.failure_count,
        "failureRate": report.failure_rate,
        "latencyMs": {
            "sampleCount": latency.sample_count,
            "min": latency.min_ms,
            "mean": latency.mean_ms,
            "p50": latency.p50_ms,
            "p95": latency.p95_ms,
            "max": latency.max_ms,
        },
        "window": {
            "elapsedSeconds": report.elapsed_seconds,
            "throughputRequestsPerSecond": report.throughput_rps,
        },
        "resourceObservation": {
            "sampled": report.memory_sampled,
            "sampleIntervalSeconds": report.memory_sample_interval_seconds,
            "scope": (
                "named Compose project containers only"
                if report.memory_sampled
                else "not sampled (no --compose-project)"
            ),
            "services": [
                {
                    "service": observation.service,
                    "sampleCount": observation.sample_count,
                    "sampledPeakBytes": observation.sampled_peak_bytes,
                    "observedLimitBytes": observation.observed_limit_bytes,
                    "cpuLimit": observation.cpu_limit,
                    "errors": list(observation.errors),
                }
                for observation in report.memory
            ],
        },
        "definitions": {
            "latency": LATENCY_DEFINITION,
            "throughput": THROUGHPUT_DEFINITION,
            "resource": RESOURCE_DEFINITION,
        },
        "llm": {
            "cloudLlmCallsAuthorized": report.write_cloud_llm,
        },
        "notes": [
            "deployment limits are caps, not measured performance; this report is not a "
            "pass/fail verdict",
            "cloudLlmCallsAuthorized reflects the executed path only and is not an "
            "access-control or authorization basis",
            "real BGE/complete QA p95 still requires a real model stack and explicit paid "
            "authorization",
            "sampled memory peak is a bounded-interval observation, not an exact host RSS or "
            "a guarantee of in-container peak",
            "memory outside the named Compose project containers (VM kernel, host, build) "
            "is out of scope",
        ],
    }


def write_report(path: Path, payload: Mapping[str, Any]) -> None:
    """原子写入报告：拒绝覆盖已存在文件；失败时清理唯一临时文件。"""

    if path.exists():
        raise PerformanceError("报告文件已存在，拒绝覆盖")
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    temp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
    except OSError as error:
        try:
            temp.unlink()
        except OSError:
            pass
        raise PerformanceError("报告写入失败") from error


# ---------------------------------------------------------------------------
# HTTP 执行


def _origin_of(base_url: str) -> str:
    """校验并返回 origin（scheme://host[:port]）；拒绝凭据、路径前缀、query 与 fragment。

    IPv6 主机在 Origin / base URL 中必须带方括号，因此这里补回方括号。
    """

    try:
        url = httpx.URL(base_url)
    except httpx.InvalidURL as error:
        raise PerformanceError("api-base-url 不是合法 URL") from error
    if not url.scheme or not url.host:
        raise PerformanceError("api-base-url 必须是带 scheme 与 host 的绝对地址")
    if url.username or url.password:
        raise PerformanceError("api-base-url 不得内嵌凭据；凭据只从环境变量读取")
    if url.path not in ("", "/"):
        raise PerformanceError("api-base-url 只支持 origin，不接受路径前缀")
    if url.query or url.fragment:
        raise PerformanceError("api-base-url 不接受 query 或 fragment")
    host = url.host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = "" if url.port is None else f":{url.port}"
    return f"{url.scheme}://{host}{port}"


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as error:
        raise PerformanceError("响应不是合法 JSON") from error
    if not isinstance(payload, dict):
        raise PerformanceError("响应结构非法")
    return payload


def _login(client: httpx.Client, *, origin: str, username: str, password: str) -> str:
    try:
        response = client.post(
            LOGIN_ENDPOINT,
            json={"username": username, "password": password},
            headers={"Origin": origin},
        )
    except httpx.HTTPError as error:
        raise PerformanceError("登录请求失败") from error
    if response.status_code != 200:
        raise PerformanceError("登录失败")
    payload = _json_object(response)
    csrf = payload.get("csrfToken")
    if not isinstance(csrf, str) or not csrf:
        raise PerformanceError("登录响应缺少 csrfToken")
    return csrf


def _measure_retrieval(
    client: httpx.Client, *, origin: str, query: str, kb_ids: Sequence[str]
) -> int | None:
    try:
        response = client.post(
            RETRIEVAL_ENDPOINT,
            json={"query": query, "kbIds": list(kb_ids)},
            headers={"Origin": origin},
        )
    except httpx.HTTPError:
        return None
    return response.status_code


def _measure_qa(
    client: httpx.Client,
    *,
    origin: str,
    csrf: str,
    question: str,
    kb_ids: Sequence[str],
) -> tuple[int | None, str | None]:
    """返回 ``(状态码, 失败阶段)``；会话创建失败时为 ``(None, "setup")``。"""

    headers = {"Origin": origin, CSRF_HEADER_NAME: csrf}
    try:
        created = client.post(
            CONVERSATIONS_ENDPOINT,
            json={"kbIds": list(kb_ids)},
            headers=headers,
        )
    except httpx.HTTPError:
        return None, "setup"
    if created.status_code != 201:
        return created.status_code, "setup"
    payload = _json_object(created)
    conversation_id = payload.get("conversationId")
    if not isinstance(conversation_id, str) or not conversation_id:
        return None, "setup"
    try:
        answered = client.post(
            f"{CONVERSATIONS_ENDPOINT}/{conversation_id}/messages",
            json={"question": question},
            headers=headers,
        )
    except httpx.HTTPError:
        return None, "request"
    return answered.status_code, None


def run_measurement(
    *,
    mode: str,
    api_base_url: str,
    count: int,
    concurrency: int,
    kb_ids: Sequence[str],
    username: str,
    password: str,
    retrieval_query: str,
    question: str,
    timeout_seconds: float,
    created_at: str,
    transport: httpx.BaseTransport | None,
    clock: Callable[[], float],
    sampler: MemorySampler | None,
    compose_project: str | None,
    memory_sample_interval_seconds: float,
) -> PerformanceReport:
    """执行最小测量；调用方负责在此之前完成全部护栏校验。

    生命周期：先构造 HTTP 客户端，登录成功后才取一次窗口前样本并启动单个有界后台采样线程，
    使线程样本确实覆盖 HTTP 测量窗口而不是登录；``finally`` 保证 stop+join+close，构造/登录/
    请求异常都不遗留线程。HTTP 延迟与 elapsed 只用 ``clock`` 计量，不含 Docker 采样墙钟。
    采样线程在 join 限额内未停止时明确失败，不写后置长采样、也不把迟到样本写入结果。
    """

    origin = _origin_of(api_base_url)
    try:
        client = httpx.Client(
            base_url=api_base_url.rstrip("/"),
            timeout=timeout_seconds,
            transport=transport,
            follow_redirects=False,
            # 凭据与 Cookie 只发往显式目标，不读代理/证书环境变量。
            trust_env=False,
            verify=True,
        )
    except (httpx.HTTPError, ValueError) as error:
        raise PerformanceError("HTTP 客户端构造失败") from error

    snapshots: list[ContainerMemorySnapshot] = []
    lock = threading.Lock()
    stop_event = threading.Event()
    thread: threading.Thread | None = None
    thread_stuck = False
    memory_sampled = sampler is not None and compose_project is not None
    try:
        csrf = _login(client, origin=origin, username=username, password=password)
        if memory_sampled and sampler is not None and compose_project is not None:
            project = compose_project
            sample = sampler
            # 窗口前样本在测量窗口之外，不计入 elapsed。
            snapshots.extend(sample(project, stop_event))

            def _sample_loop() -> None:
                while not stop_event.wait(memory_sample_interval_seconds):
                    observed = sample(project, stop_event)
                    if stop_event.is_set():
                        break
                    with lock:
                        snapshots.extend(observed)

            thread = threading.Thread(
                target=_sample_loop, name="perf-memory-sampler", daemon=True
            )
            thread.start()

        records: list[RequestRecord] = []
        start = clock()
        for index in range(count):
            attempt_start = clock()
            status_code: int | None
            failure_stage: str | None
            if mode == "qa":
                status_code, failure_stage = _measure_qa(
                    client,
                    origin=origin,
                    csrf=csrf,
                    question=question,
                    kb_ids=kb_ids,
                )
            else:
                status_code = _measure_retrieval(
                    client, origin=origin, query=retrieval_query, kb_ids=kb_ids
                )
                failure_stage = None
            latency_ms = (clock() - attempt_start) * 1000.0
            ok = status_code == 200
            if not ok and failure_stage is None:
                failure_stage = "request"
            records.append(
                RequestRecord(
                    index=index,
                    ok=ok,
                    latency_ms=latency_ms,
                    status_code=status_code,
                    failure_stage=failure_stage,
                )
            )
        elapsed = clock() - start
    finally:
        stop_event.set()
        if thread is not None:
            thread.join(timeout=MEMORY_SAMPLER_JOIN_TIMEOUT_SECONDS)
            thread_stuck = thread.is_alive()
        client.close()

    if thread_stuck:
        raise PerformanceError("采样线程未按取消契约在限额内停止")

    with lock:
        observed_snapshots = list(snapshots)
    observations = aggregate_memory([observed_snapshots]) if memory_sampled else ()
    return build_report(
        mode=mode,
        api_base_url=api_base_url,
        count=count,
        concurrency=concurrency,
        records=records,
        elapsed_seconds=elapsed,
        memory_sampled=memory_sampled,
        memory=observations,
        memory_sample_interval_seconds=(
            memory_sample_interval_seconds if memory_sampled else None
        ),
        created_at=created_at,
        write_cloud_llm=mode == "qa",
    )


# ---------------------------------------------------------------------------
# CLI


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m rag_backend.evaluation.performance",
        description=(
            "Phase 4 最小性能采集（默认 dry-run：不联网、不读环境变量、不执行 Docker、不写文件）。"
        ),
    )
    parser.add_argument("--mode", choices=VALID_MODES, default="retrieval")
    parser.add_argument("--api-base-url", default=None, help="真实采集必填；默认只允许回环地址")
    parser.add_argument(
        "--kb-id", action="append", default=[], help="检索/问答使用的 KB UUID，可重复"
    )
    parser.add_argument("--count", type=int, default=1, help="测量请求数（必须为正）")
    parser.add_argument("--concurrency", type=int, default=1, help="本最小入口只接受并发 1")
    parser.add_argument(
        "--out", type=Path, default=None, help="报告输出路径；真实采集必填且拒绝覆盖"
    )
    parser.add_argument("--compose-project", default=None, help="要观察内存的 Compose project 名")
    parser.add_argument(
        "--memory-sample-interval-seconds",
        type=float,
        default=DEFAULT_MEMORY_SAMPLE_INTERVAL_SECONDS,
        help="指定 --compose-project 时的后台采样间隔秒数",
    )
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--username-env", default="PERF_USERNAME")
    parser.add_argument("--password-env", default="PERF_PASSWORD")
    parser.add_argument("--query-env", default="PERF_QUERY", help="retrieval 查询文本的环境变量名")
    parser.add_argument("--question-env", default="PERF_QUESTION", help="qa 问题文本的环境变量名")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="显式开启真实采集（会联网并按需调用 Docker 采样）",
    )
    parser.add_argument(
        "--allow-paid-llm",
        action="store_true",
        help="qa 模式必须显式给出；授权可能产生付费 LLM 调用",
    )
    parser.add_argument(
        "--allow-non-loopback-api",
        action="store_true",
        help="api-base-url 非回环时必须显式重申",
    )
    return parser.parse_args(argv)


def _validate_plan(args: argparse.Namespace) -> None:
    if args.count <= 0:
        raise PerformanceError("--count 必须为正数")
    if args.concurrency != 1:
        raise PerformanceError("本最小入口只实现并发 1，拒绝 --concurrency 非 1")
    if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
        raise PerformanceError("--timeout-seconds 必须是有限正数")
    if (
        not math.isfinite(args.memory_sample_interval_seconds)
        or args.memory_sample_interval_seconds <= 0
    ):
        raise PerformanceError("--memory-sample-interval-seconds 必须是有限正数")
    for kb_id in args.kb_id:
        try:
            uuid.UUID(kb_id)
        except (ValueError, AttributeError) as error:
            raise PerformanceError("--kb-id 必须是合法 UUID") from error
    if args.mode == "qa":
        if not args.allow_paid_llm:
            raise PerformanceError("qa 模式可能产生付费 LLM 调用，必须显式 --allow-paid-llm")
    elif args.allow_paid_llm:
        raise PerformanceError("--allow-paid-llm 只在 qa 模式有效")
    if args.api_base_url:
        _origin_of(args.api_base_url)


def _assert_api_target(args: argparse.Namespace) -> str:
    if not args.api_base_url:
        raise PerformanceError("真实采集必须提供 --api-base-url")
    origin = _origin_of(args.api_base_url)
    host = httpx.URL(origin).host or ""
    if host not in LOOPBACK_HOSTS and not args.allow_non_loopback_api:
        raise PerformanceError(
            "api-base-url 非回环；如确认不是线上环境，请显式 --allow-non-loopback-api"
        )
    return origin


def _required_env(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if not value:
        raise PerformanceError(f"真实采集需要环境变量 {name}")
    return value


def _default_clock() -> float:
    return time.perf_counter()


def _default_created_at() -> str:
    return datetime.now(UTC).isoformat()


def _print_plan(args: argparse.Namespace) -> None:
    target = args.api_base_url or "<execute 必填>"
    print(
        f"计划：mode={args.mode} endpoint={ENDPOINT_BY_MODE[args.mode]} "
        f"count={args.count} concurrency={args.concurrency}"
    )
    print(
        f"目标：apiBaseUrl={target} knowledgeBases={len(args.kb_id)} "
        f"composeProject={args.compose_project or 'none'}"
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
    clock: Callable[[], float] | None = None,
    sampler: MemorySampler | None = None,
    created_at: str | None = None,
) -> int:
    args = _parse_args(argv)
    try:
        _validate_plan(args)
    except PerformanceError as error:
        print(f"准备失败：{error}", file=sys.stderr)
        return 1

    if not args.execute:
        _print_plan(args)
        print("dry-run：未开启 --execute，不联网、不读环境变量、不执行 Docker、不写文件。")
        return 0

    try:
        api_base_url = _assert_api_target(args)
        if not args.kb_id:
            raise PerformanceError("真实采集必须至少提供一个 --kb-id")
        if args.out is None:
            raise PerformanceError("真实采集必须提供 --out 报告路径")
        if args.out.exists():
            raise PerformanceError("报告文件已存在，拒绝覆盖")
        env = os.environ if environ is None else environ
        username = _required_env(env, args.username_env)
        password = _required_env(env, args.password_env)
        retrieval_query = ""
        question = ""
        if args.mode == "qa":
            question = _required_env(env, args.question_env)
        else:
            retrieval_query = _required_env(env, args.query_env)
        active_sampler: MemorySampler | None = sampler
        if args.compose_project is not None and active_sampler is None:
            active_sampler = docker_memory_snapshot
        report = run_measurement(
            mode=args.mode,
            api_base_url=api_base_url,
            count=args.count,
            concurrency=args.concurrency,
            kb_ids=args.kb_id,
            username=username,
            password=password,
            retrieval_query=retrieval_query,
            question=question,
            timeout_seconds=args.timeout_seconds,
            created_at=created_at or _default_created_at(),
            transport=transport,
            clock=clock or _default_clock,
            sampler=active_sampler,
            compose_project=args.compose_project,
            memory_sample_interval_seconds=args.memory_sample_interval_seconds,
        )
        write_report(args.out, report_payload(report))
    except PerformanceError as error:
        print(f"采集失败：{error}", file=sys.stderr)
        return 1
    except httpx.HTTPError:
        print("采集失败：HTTP 请求失败", file=sys.stderr)
        return 1

    print(
        f"报告已写出：{args.out}（count={report.count} "
        f"success={report.success_count} failure={report.failure_count}）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
