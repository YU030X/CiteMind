"""本地数据服务切片的静态检查：不启动容器，只校验仓库内的 Compose 与 initdb 文件。"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
COMPOSE_FILE = REPO_ROOT / "deploy" / "compose" / "compose.yml"
QUEUE_COMPOSE_FILE = REPO_ROOT / "deploy" / "compose" / "queue.yml"
DOCKERFILE = REPO_ROOT / "deploy" / "compose" / "Dockerfile"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
INITDB_SCRIPTS = sorted((REPO_ROOT / "deploy" / "compose" / "initdb").glob("*.sh"))
GITATTRIBUTES = REPO_ROOT / ".gitattributes"

# 项目自有变量去前缀后不再有统一前缀，正则只能按形态抓取，因此显式排除第三方环境变量名，
# 避免未来第三方必填插值被误判成项目变量；当前 compose 的必填插值全部是项目变量。
THIRD_PARTY_ENV_VARIABLES = frozenset(
    {
        "POSTGRES_USER",
        "POSTGRES_DB",
        "POSTGRES_PASSWORD",
        "POSTGRES_INITDB_ARGS",
        "REDISCLI_AUTH",
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
    }
)
REQUIRED_VARIABLE_PATTERN = re.compile(r"\$\{([A-Z][A-Z0-9_]*):\?")

LF_ATTRIBUTE_LINE = "deploy/compose/**/*.sh text eol=lf"
PINNED_IMAGES = (
    "pgvector/pgvector:pg17@sha256:cf134a767f474095eeba57e0117be8e568e011a63f33fbf252f14c9b760f8e6f",
    "redis:7.4.9@sha256:a8f08480e1f88f2647fed492d1178c06abb0d0c1fbf02c682a61e2f483fb3954",
)
# 与 inference 代码/构建脚本保持一致的冻结模型契约；不一致时插值会静默失配。
INFERENCE_MODEL = "BAAI/bge-small-zh-v1.5"
INFERENCE_REVISION = "7999e1d3359715c523056ef9478215996d62a620"
INFERENCE_MODEL_DIR = "/models/bge-small-zh-v1.5"


def compose_text() -> str:
    return COMPOSE_FILE.read_text(encoding="utf-8")


def queue_compose_text() -> str:
    return QUEUE_COMPOSE_FILE.read_text(encoding="utf-8")


def compose_entries(key: str) -> list[str]:
    """收集 compose 文件中某个 key 的标量值，支持行内值与列表项两种写法。"""

    entries: list[str] = []
    collecting = False
    for line in compose_text().splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{key}:"):
            inline = stripped[len(key) + 1 :].strip()
            if inline:
                entries.append(inline.strip('"'))
                collecting = False
            else:
                collecting = True
            continue
        if not collecting:
            continue
        if stripped.startswith("- "):
            entries.append(stripped[2:].strip().strip('"'))
        elif stripped:
            collecting = False
    return entries


SERVICE_HEADER = re.compile(r"^  ([a-zA-Z0-9_-]+):\s*$")


def compose_service_names() -> list[str]:
    """返回 services: 块下的顶层服务名（两空格缩进）。"""

    names: list[str] = []
    in_services = False
    for line in compose_text().splitlines():
        if line.startswith("services:"):
            in_services = True
            continue
        if not in_services:
            continue
        if line and not line.startswith(" "):
            break
        match = SERVICE_HEADER.match(line)
        if match:
            names.append(match.group(1))
    return names


def compose_service_block(service: str) -> str:
    """返回单个 service 的缩进块文本，用于按服务断言而不是全局字符串计数。"""

    block: list[str] = []
    capturing = False
    for line in compose_text().splitlines():
        if capturing and (SERVICE_HEADER.match(line) or line.startswith("volumes:")):
            break
        if line.startswith(f"  {service}:"):
            capturing = True
        if capturing:
            block.append(line)
    return "\n".join(block)


def test_initdb_scripts_exist() -> None:
    assert INITDB_SCRIPTS, "deploy/compose/initdb 下缺少 *.sh 初始化脚本"


@pytest.mark.parametrize("script", INITDB_SCRIPTS, ids=lambda path: path.name)
def test_initdb_script_uses_lf_line_endings(script: Path) -> None:
    content = script.read_bytes()

    assert b"\r" not in content, f"{script.name} 含 CR；容器内执行必须使用 LF"
    assert content.endswith(b"\n"), f"{script.name} 缺少结尾换行"


def test_gitattributes_pins_initdb_scripts_to_lf() -> None:
    assert INITDB_SCRIPTS, "initdb 脚本缺失时该属性无法验证"

    lines = [line.strip() for line in GITATTRIBUTES.read_text(encoding="utf-8").splitlines()]

    assert LF_ATTRIBUTE_LINE in lines


def test_env_example_provides_non_empty_values_for_required_compose_variables() -> None:
    """compose 的必填插值变量必须在 .env.example 中以未注释的非空值提供，否则首次启动直接失败。"""

    captured = set(REQUIRED_VARIABLE_PATTERN.findall(compose_text()))
    assert not (captured & THIRD_PARTY_ENV_VARIABLES), (
        "第三方变量不应以必填插值出现，否则会污染该检查"
    )
    required = captured - THIRD_PARTY_ENV_VARIABLES
    assert required, "compose 未声明必填的项目变量时该检查失去意义"

    values: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip()

    missing = sorted(name for name in required if name not in values)
    empty = sorted(name for name in required if values.get(name) == "")

    assert not missing, f".env.example 缺少必填变量：{missing}"
    assert not empty, f".env.example 中必填变量为空值：{empty}"


def test_compose_declares_the_six_expected_services() -> None:
    assert sorted(compose_service_names()) == sorted(
        ["api", "frontend-gateway", "inference", "postgres", "redis", "worker"]
    )


def test_compose_pins_the_two_third_party_images_by_digest() -> None:
    images = compose_entries("image")

    assert len(images) == 2, "只有 postgres 与 redis 是第三方镜像，自建服务不得写 image:"
    assert sorted(images) == sorted(PINNED_IMAGES)
    for image in images:
        assert "@sha256:" in image, f"{image} 未固定 digest"


def test_compose_publishes_only_loopback_ports_by_default() -> None:
    published_ports = compose_entries("ports")

    assert len(published_ports) == 3, "只有 postgres、redis 与 frontend-gateway 发布宿主端口"
    for mapping in published_ports:
        assert mapping.startswith("127.0.0.1:"), f"{mapping} 必须只绑定回环地址"

    text = compose_text()
    assert "POSTGRES_PORT:-55432" in text
    assert "REDIS_PORT:-56379" in text
    assert "GATEWAY_PORT:-58080" in text


def test_compose_keeps_internal_services_unpublished() -> None:
    for service in ("api", "inference", "worker"):
        assert "ports:" not in compose_service_block(service), f"{service} 不应发布宿主端口"


def test_compose_mounts_api_private_document_volume() -> None:
    """上传原文件只落在 api 专用命名卷；worker 只读同一卷，inference 无权访问。"""

    api_block = compose_service_block("api")
    assert "DOCUMENT_STORAGE_DIRECTORY: /var/lib/citemind/documents" in api_block
    # api 是唯一写入者：挂载不带 `:ro`。
    assert "- api-documents:/var/lib/citemind/documents" in api_block
    assert "api-documents:/var/lib/citemind/documents:ro" not in api_block
    # 顶层必须声明该命名卷，否则 compose config 会因未定义卷失败。
    assert "\n  api-documents:\n" in compose_text()

    worker_block = compose_service_block("worker")
    # worker 读同一容器路径且必须只读；仍由服务端配置固定存储根。
    assert "DOCUMENT_STORAGE_DIRECTORY: /var/lib/citemind/documents" in worker_block
    assert "- api-documents:/var/lib/citemind/documents:ro" in worker_block
    assert "- api-documents:/var/lib/citemind/documents\n" not in worker_block

    # inference 不需要也不得访问上传文档卷。
    assert "api-documents" not in compose_service_block("inference")
    # 只有 worker 以上述只读形式挂载该卷；api 保持读写。
    assert compose_text().count("api-documents:/var/lib/citemind/documents:ro") == 1


def test_compose_keeps_declared_service_contracts() -> None:
    text = compose_text()

    assert text.count("restart: unless-stopped") == 6
    assert text.count("healthcheck:") == 6
    assert "--locale=C.UTF-8 --data-checksums" in text
    assert text.count(".citemind-init-complete") == 1
    assert all(
        ".citemind-init-complete" in script.read_text(encoding="utf-8")
        for script in INITDB_SCRIPTS
    )
    assert "REDISCLI_AUTH" in text
    assert "--appendfsync" in text
    assert "everysec" in text
    assert "128mb" in text
    assert "noeviction" in text


def test_compose_builds_api_and_worker_from_shared_dockerfile_targets() -> None:
    assert "target: api" in compose_service_block("api")
    assert "target: worker" in compose_service_block("worker")

    api_block = compose_service_block("api")
    assert "context: ../.." in api_block
    assert "dockerfile: deploy/compose/Dockerfile" in api_block
    # api 在容器网络内使用服务名 DSN，绝不透传宿主 127.0.0.1 DSN。
    assert "@postgres:5432/citemind" in api_block
    assert "127.0.0.1:55432" not in api_block
    assert "urlopen('http://127.0.0.1:8000/api/v1/health'" in api_block


def test_compose_inference_service_is_self_contained_and_token_protected() -> None:
    block = compose_service_block("inference")

    assert "context: ../../inference" in block
    assert "depends_on" not in block, "本切片 inference 不依赖 api 或数据服务"
    assert "INFERENCE_TOKEN" in block
    assert "urlopen('http://127.0.0.1:9000/health'" in block


def test_compose_inference_bakes_model_and_never_downloads_at_runtime() -> None:
    block = compose_service_block("inference")

    # 无模型卷：权重烘入镜像，启动时不联网下载。
    assert "volumes:" not in block, "inference 不得挂载宿主模型卷"
    assert f"EMBEDDING_MODEL_PATH: {INFERENCE_MODEL_DIR}" in block
    assert f"EMBEDDING_MODEL_REVISION: {INFERENCE_REVISION}" in block
    for variable in (
        'HF_HUB_OFFLINE: "1"',
        'TRANSFORMERS_OFFLINE: "1"',
        'HF_HUB_DISABLE_TELEMETRY: "1"',
        'TOKENIZERS_PARALLELISM: "false"',
    ):
        assert variable in block, f"inference 容器必须设置 {variable}"
    # OMP/MKL/OpenBLAS 必须在 torch 导入前由进程环境固定，只靠 torch.set_num_threads 不够。
    for variable in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        assert f"{variable}: ${{EMBEDDING_TORCH_THREADS:-2}}" in block, (
            f"inference 必须固定 {variable}"
        )
    assert "EMBEDDING_TORCH_THREADS: ${EMBEDDING_TORCH_THREADS:-2}" in block
    # 健康检查仍以 /health liveness 为准，不因模型就绪与否改变。
    assert "urlopen('http://127.0.0.1:9000/health', timeout=3)" in block
    assert "start_period" in block


def test_compose_inference_identity_matches_frozen_model_contract() -> None:
    """模型路径与 revision 必须与 inference 代码及构建脚本里的冻结契约一致。"""

    config = (REPO_ROOT / "inference" / "src" / "citemind_inference" / "config.py").read_text(
        encoding="utf-8"
    )
    script = (REPO_ROOT / "inference" / "scripts" / "prepare_model.py").read_text(
        encoding="utf-8"
    )
    block = compose_service_block("inference")

    assert INFERENCE_MODEL in config and INFERENCE_MODEL in script
    assert INFERENCE_REVISION in config and INFERENCE_REVISION in script
    assert INFERENCE_REVISION in block
    assert all(
        INFERENCE_MODEL_DIR in text for text in (config, script, block)
    ), "模型目录必须在代码、构建脚本与 Compose 中保持一致"


def test_env_example_documents_inference_thread_budget() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")

    # 线程数有 Compose 默认值，.env.example 只作为可调项出现，不强制填值。
    assert "EMBEDDING_TORCH_THREADS=2" in text
    assert "INFERENCE_TOKEN=citemind-inference" in text


def test_compose_gateway_is_the_only_app_entrypoint_depending_on_api() -> None:
    block = compose_service_block("frontend-gateway")

    assert "dockerfile: deploy/compose/frontend.Dockerfile" in block
    assert "127.0.0.1:${GATEWAY_PORT:-58080}:8080" in block
    assert "condition: service_healthy" in block
    assert "healthz" in block


def test_compose_wires_the_gateway_proxy_trust_boundary() -> None:
    api_block = compose_service_block("api")
    gateway_block = compose_service_block("frontend-gateway")

    # api 只信任 gateway 专用网段，且默认值与顶层网络子网一致。
    trust_setting = (
        "TRUSTED_PROXY_CIDRS: ${TRUSTED_PROXY_CIDRS:-172.28.10.0/24}"
    )
    assert trust_setting in api_block
    assert "      - gateway" in api_block
    # 网关只接 gateway 网络，因此它到 api 的连接一定来自可信网段。
    assert "      - gateway" in gateway_block
    assert "subnet: 172.28.10.0/24" in compose_text()


def test_compose_worker_service_runs_celery_and_depends_on_data_services() -> None:
    text = compose_text()

    assert "rag_backend.worker:celery_app" in text
    assert "--concurrency=1" in text
    # 四个 service_healthy：worker 等 postgres/redis，api 等 postgres，gateway 等 api。
    assert text.count("condition: service_healthy") == 4
    assert "REDIS_URL" in text
    assert "@redis:6379/0" in text
    assert "citemind_worker" in text
    # worker 不应因 inference 未就绪而被阻塞（注释里的全角冒号不算依赖键）。
    assert "inference:" not in compose_service_block("worker")


def test_compose_worker_consumes_ingest_and_default_probe_queues() -> None:
    worker_block = compose_service_block("worker")

    # 同一 worker 同时消费 ingest 专用队列与 probe 的默认队列，两类任务只按队列隔离。
    assert "--queues=celery,ingest" in worker_block


def test_compose_api_enables_dispatcher_without_blocking_on_redis_health() -> None:
    api_block = compose_service_block("api")

    assert 'DISPATCHER_ENABLED: "true"' in api_block
    # dispatcher 启用后仍只等 redis 启动（而非 healthy），慢启动不阻塞 API。
    assert "condition: service_started" in api_block


def test_dockerfile_pins_base_images_and_runs_the_worker_as_non_root() -> None:
    content = DOCKERFILE.read_text(encoding="utf-8")
    pinned = [
        line
        for line in content.splitlines()
        if line.startswith("FROM ") or line.startswith("COPY --from=")
    ]

    assert any("python:3.12-slim@sha256:" in line for line in pinned)
    assert any("ghcr.io/astral-sh/uv:0.12.4@sha256:" in line for line in pinned)
    assert "uv sync --frozen --no-dev" in content
    assert "USER citemind" in content
    # 非 root marker 目录必须在镜像里预先创建并归属运行用户，命名卷首次挂载才能继承。
    assert "/var/lib/citemind/probe-markers" in content
    # 上传文档目录同理，必须预先创建并归属 uid 10001。
    assert "/var/lib/citemind/documents" in content
    assert "chown -R citemind:citemind /app /var/lib/citemind" in content


def test_base_compose_does_not_run_queue_probe_or_set_marker_directory() -> None:
    text = compose_text()

    assert "queue-probe" not in text
    assert "PROBE_MARKER_DIRECTORY" not in text


def test_queue_override_adds_shared_marker_volume_and_one_shot_probe() -> None:
    text = queue_compose_text()

    assert "queue-probe" in text
    assert "PROBE_MARKER_DIRECTORY: /var/lib/citemind/probe-markers" in text
    # base worker 与 queue-probe 必须挂载同一个非 root marker 卷。
    assert text.count("probe-markers:/var/lib/citemind/probe-markers") == 2
    assert "rag_backend.queue_probe" in text
    assert "restart: \"no\"" in text
    # 确定性验收使用硬退出码，不引入 result backend。
    assert "--abort-on-container-exit" in text
    # 验收命令必须显式只启动 queue-probe service，避免六服务其它容器干扰退出码。
    assert "--exit-code-from queue-probe queue-probe" in text
    assert "result_backend" not in text
