"""inference 独立服务的部署资产静态检查：不启动容器，只校验仓库内文件。

这些检查补充但不等价于真实构建与运行；真实启动验收由 Linux Compose 完成。
"""

import ast
import hashlib
import re
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).parents[2]
INFERENCE_DIR = REPO_ROOT / "inference"
INFERENCE_DOCKERFILE = INFERENCE_DIR / "Dockerfile"
INFERENCE_DOCKERIGNORE = INFERENCE_DIR / ".dockerignore"
INFERENCE_PYPROJECT = INFERENCE_DIR / "pyproject.toml"
INFERENCE_UV_LOCK = INFERENCE_DIR / "uv.lock"
INFERENCE_MODEL_SCRIPT = INFERENCE_DIR / "scripts" / "prepare_model.py"
SHARED_DOCKERFILE = REPO_ROOT / "deploy" / "compose" / "Dockerfile"

PYTHON_IMAGE = (
    "python:3.12-slim@sha256:"
    "2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9"
)
UV_IMAGE = (
    "ghcr.io/astral-sh/uv:0.12.4@sha256:"
    "d0a6eca6c669dc7e9c51218707b8438a3d30402733d739dcc00adb3e213e8f5c"
)

FORBIDDEN_INFERENCE_DEPENDENCIES = ("sentence-transformers",)
# CPU 直连 torch + transformers；PyTorch 必须来自 CPU index，锁文件不得带 nvidia-*。
REQUIRED_INFERENCE_DEPENDENCIES = ("torch", "transformers")
PYTORCH_CPU_INDEX_URL = "https://download.pytorch.org/whl/cpu"
FROZEN_EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
FROZEN_EMBEDDING_REVISION = "7999e1d3359715c523056ef9478215996d62a620"
# 构建期实际下载并核对过的产物（来源：Hub commit 元数据的 Git blob / LFS 摘要）。
MODEL_ARTIFACT_FILES = (
    "config.json",
    "tokenizer_config.json",
    "tokenizer.json",
    "special_tokens_map.json",
    "vocab.txt",
    "model.safetensors",
)
MODEL_WEIGHTS_SHA256 = "354763b9b1357bc9c44f62c6be2276321081ed2567773608c0d0785b61d5a026"
MODEL_WEIGHTS_SIZE = 95827648
MODEL_DIR_IN_IMAGE = "/models/bge-small-zh-v1.5"
MODEL_MANIFEST_NAME = "model-manifest.json"
# 随 inference 镜像分发的模型许可与来源说明；放在模型目录之外，不参与 6 个产物集合。
NOTICE_FILE = INFERENCE_DIR / "third_party" / "bge-small-zh-v1.5-LICENSE.txt"
NOTICE_PATH_IN_IMAGE = "/models/bge-small-zh-v1.5-LICENSE.txt"
MODEL_CARD_URL = (
    f"https://huggingface.co/{FROZEN_EMBEDDING_MODEL}/blob/{FROZEN_EMBEDDING_REVISION}/README.md"
)
UPSTREAM_LICENSE_URL = (
    "https://raw.githubusercontent.com/FlagOpen/FlagEmbedding/"
    "c086741f5e117b7b8ce1745ea00b6c262f281a01/LICENSE"
)
UPSTREAM_LICENSE_SHA256 = (
    "587a673933425dbc36ec61268d3b954051b2d3ef3c9b322ede357976055ffdd5"
)
UPSTREAM_LICENSE_BYTES = 1065


def dockerfile_stages(content: str) -> list[tuple[str, str]]:
    """按出现顺序返回 ``FROM <base> AS <stage>`` 的 (base, stage) 列表。"""

    stages: list[tuple[str, str]] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line.upper().startswith("FROM "):
            continue
        parts = line.split()
        if len(parts) >= 4 and parts[2].upper() == "AS":
            stages.append((parts[1], parts[3]))
    return stages


def dockerignore_entries() -> list[str]:
    return [
        line.strip()
        for line in INFERENCE_DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def read_pyproject() -> dict[str, Any]:
    with INFERENCE_PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)


def final_dockerfile_stage_text() -> str:
    """返回最后一个 ``FROM`` 起的文本，即默认构建目标（最终 runtime 镜像）。"""

    lines = INFERENCE_DOCKERFILE.read_text(encoding="utf-8").splitlines()
    last_from = max(index for index, line in enumerate(lines) if line.startswith("FROM "))
    return "\n".join(lines[last_from:])


def test_shared_dockerfile_has_reusable_runtime_and_api_worker_final_targets() -> None:
    content = SHARED_DOCKERFILE.read_text(encoding="utf-8")
    stages = dockerfile_stages(content)
    names = [stage for _, stage in stages]

    assert names == ["runtime", "api", "worker"], (
        "共享 Dockerfile 必须是 runtime 基础 stage 加 api/worker 两个 final target"
    )
    assert all(base == "runtime" for base, _ in stages[1:])


def test_shared_dockerfile_default_target_is_worker() -> None:
    stages = dockerfile_stages(SHARED_DOCKERFILE.read_text(encoding="utf-8"))

    # 最后一个 stage 是默认构建目标；worker 必须保持最后，未指定 --target 时行为不变。
    assert stages[-1][1] == "worker"


def test_shared_dockerfile_api_target_serves_uvicorn_on_8000() -> None:
    content = SHARED_DOCKERFILE.read_text(encoding="utf-8")

    assert "EXPOSE 8000" in content
    # --no-proxy-headers：客户端 IP 由应用按可信代理 CIDR 自行解析，避免两套信任规则。
    api_command = (
        'CMD ["uvicorn", "rag_backend.main:app", "--host", "0.0.0.0", "--port", "8000", '
        '"--no-proxy-headers"]'
    )
    assert api_command in content


def test_shared_dockerfile_worker_target_keeps_celery_command() -> None:
    content = SHARED_DOCKERFILE.read_text(encoding="utf-8")

    assert (
        'CMD ["celery", "-A", "rag_backend.worker:celery_app", "worker", "--loglevel=INFO"]'
        in content
    )
    # 非 root 与 marker 目录仍由共享 runtime stage 建立并被两个 target 继承。
    assert "USER citemind" in content
    assert "/var/lib/citemind/probe-markers" in content


def model_script_text() -> str:
    return INFERENCE_MODEL_SCRIPT.read_text(encoding="utf-8")


def test_inference_model_script_pins_frozen_model_and_revision() -> None:
    content = model_script_text()

    assert f'MODEL = "{FROZEN_EMBEDDING_MODEL}"' in content
    assert f'REVISION = "{FROZEN_EMBEDDING_REVISION}"' in content
    assert MODEL_DIR_IN_IMAGE in content, "构建期模型目录必须与运行期默认路径一致"
    # revision 必须实际用于拼 URL：只在文件里写一遍字符串不构成固定。
    assert "/revision/{spec.revision}" in content
    assert "/{spec.model}/resolve/{spec.revision}/{name}" in content
    # reranker 的模型与 revision 同样必须钉死；它与 embedding 共用参数化的 URL 构造。
    assert 'RERANK_MODEL = "BAAI/bge-reranker-base"' in content
    assert (
        'RERANK_REVISION = "2cfc18c9415c912f9d8155881c133215df768a70"' in content
    )


def test_inference_model_script_requires_only_config_tokenizer_and_safetensors() -> None:
    content = model_script_text()
    tree = ast.parse(content)
    pinned = next(
        node
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "EXPECTED_FILES"
    )
    pinned_value = pinned.value
    assert isinstance(pinned_value, ast.Dict)
    keys = [key.value for key in pinned_value.keys if isinstance(key, ast.Constant)]

    assert tuple(keys) == MODEL_ARTIFACT_FILES
    assert "pytorch_model.bin" not in keys, "不应把冗余的 pytorch_model.bin 烘入镜像"
    assert "model.safetensors" in keys
    for name in MODEL_ARTIFACT_FILES:
        assert f'"{name}"' in content, f"构建期必须下载并校验 {name}"


def test_inference_model_script_verifies_upstream_digests_not_self_reported_revision() -> None:
    content = model_script_text()

    # 大小 + 上游 Git blob SHA-1，LFS 文件改用上游 sha256：三者都来自 Hub 元数据。
    assert "blobId" in content
    assert 'upstream["sha"]' in content, "必须核对 Hub 返回的 commit 与冻结 revision 一致"
    assert 'metadata.get("lfs")' in content
    assert 'lfs["sha256"]' in content
    assert "hashlib.sha256" in content and "hashlib.sha1" in content
    assert "模型文件大小不符" in content
    assert "上游 LFS SHA-256 不符" in content
    assert "上游 Git blob 不符" in content
    # 产物清单与再校验入口必须存在，且拒绝覆盖已有目录。
    assert MODEL_MANIFEST_NAME in content
    assert "--verify" in content
    assert "FileExistsError" in content


def test_inference_model_script_pins_verified_weight_digest() -> None:
    """权重摘要来自实际下载核对结果，测试锁定它以防静默换成别的产物。"""

    assert MODEL_WEIGHTS_SHA256 == (
        "354763b9b1357bc9c44f62c6be2276321081ed2567773608c0d0785b61d5a026"
    )
    assert f"\"sha256\": \"{MODEL_WEIGHTS_SHA256}\"" in model_script_text()
    assert f"\"size\": {MODEL_WEIGHTS_SIZE}" in model_script_text()


def test_inference_dockerfile_bakes_model_at_build_time_and_stays_offline() -> None:
    content = INFERENCE_DOCKERFILE.read_text(encoding="utf-8")

    assert "COPY scripts/prepare_model.py ./scripts/prepare_model.py" in content
    assert "RUN python scripts/prepare_model.py" in content
    for variable in ("HF_HUB_OFFLINE=1", "TRANSFORMERS_OFFLINE=1", "HF_HUB_DISABLE_TELEMETRY=1"):
        assert variable in content, f"运行期必须设置 {variable}"
    # CPU 线程变量在 torch 导入前由进程环境生效，不靠运行期再设置。
    for variable in ("OMP_NUM_THREADS=2", "MKL_NUM_THREADS=2", "OPENBLAS_NUM_THREADS=2"):
        assert variable in content, f"镜像必须固定 {variable}"
    assert f"EMBEDDING_MODEL_PATH={MODEL_DIR_IN_IMAGE}" in content
    assert f"EMBEDDING_MODEL_REVISION={FROZEN_EMBEDDING_REVISION}" in content
    # 模型烘入镜像：不允许镜像声明模型卷或运行期下载入口。
    assert "VOLUME" not in content
    assert "huggingface-cli download" not in content
    assert "snapshot_download" not in content


def test_inference_notice_exists_with_fixed_provenance() -> None:
    assert NOTICE_FILE.is_file(), "inference/third_party 下必须分发模型许可与来源 notice"
    content = NOTICE_FILE.read_text(encoding="utf-8")

    assert FROZEN_EMBEDDING_MODEL in content
    assert FROZEN_EMBEDDING_REVISION in content
    assert MODEL_CARD_URL in content, "notice 必须给出固定 revision 的模型卡 URL"
    assert "license: mit" in content, "notice 必须保留模型卡的 MIT 声明原文"
    assert (
        "The released models can be used for commercial purposes free of charge."
        in content
    ), "notice 必须保留模型可商用的英文原文"
    assert "没有单独的 LICENSE 文件" in content, "notice 必须说明该 revision 无独立 LICENSE"
    assert UPSTREAM_LICENSE_URL in content, "notice 必须给出固定上游许可 URL"
    assert UPSTREAM_LICENSE_SHA256 in content
    assert f"文件字节数：{UPSTREAM_LICENSE_BYTES}" in content
    # 原文保留：不得改写原始署名，也不得虚构 BAAI 版权年。
    assert "MIT License" in content
    assert "Copyright (c) 2022 staoxiao" in content
    assert "Permission is hereby granted, free of charge" in content
    assert 'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND' in content
    assert "OUT OF OR IN CONNECTION WITH THE SOFTWARE" in content


def test_inference_notice_preserves_upstream_license_verbatim() -> None:
    """notice 内嵌的上游许可全文必须逐字不变：锁定字节数与 SHA-256。"""

    content = NOTICE_FILE.read_text(encoding="utf-8")
    marker = "\nMIT License\n\nCopyright (c) 2022 staoxiao"
    embedded = content[content.index(marker) + 1 :].encode("utf-8")

    assert len(embedded) == UPSTREAM_LICENSE_BYTES
    assert hashlib.sha256(embedded).hexdigest() == UPSTREAM_LICENSE_SHA256


def test_inference_dockerfile_copies_notice_into_final_image_only() -> None:
    stage = final_dockerfile_stage_text()
    copy_line = f"COPY third_party/bge-small-zh-v1.5-LICENSE.txt {NOTICE_PATH_IN_IMAGE}"

    assert copy_line in stage, "最终镜像必须 COPY notice 到 /models 下的分发路径"
    # notice 不得进模型目录，否则该目录不再恰为 6 个可信产物。
    assert f"{MODEL_DIR_IN_IMAGE}/" not in copy_line
    assert f"{MODEL_DIR_IN_IMAGE}/bge-small-zh-v1.5-LICENSE.txt" not in stage


def test_inference_dockerfile_pins_python_and_uv_by_digest() -> None:
    content = INFERENCE_DOCKERFILE.read_text(encoding="utf-8")
    pinned = [
        line
        for line in content.splitlines()
        if line.startswith("FROM ") or line.startswith("COPY --from=")
    ]

    assert any(PYTHON_IMAGE in line for line in pinned)
    assert any(UV_IMAGE in line for line in pinned)
    assert "@sha256:" in PYTHON_IMAGE
    assert "@sha256:" in UV_IMAGE


def test_inference_dockerfile_installs_from_lock_without_dev_and_runs_non_root() -> None:
    content = INFERENCE_DOCKERFILE.read_text(encoding="utf-8")

    assert "uv sync --frozen --no-dev --no-install-project" in content
    assert "uv sync --frozen --no-dev" in content
    assert "useradd --create-home --uid 10002 inference" in content
    assert "USER inference" in content
    # 只改 /app 自身属主：递归 chown 会把整个 venv 复制进新层，白白增大镜像。
    assert "chown inference:inference /app" in content
    assert "chown -R inference:inference" not in content


def test_inference_dockerfile_exposes_9000_and_starts_uvicorn() -> None:
    content = INFERENCE_DOCKERFILE.read_text(encoding="utf-8")
    instructions = [
        line.strip()
        for line in content.splitlines()
        if not line.lstrip().startswith("#")
    ]

    assert "EXPOSE 9000" in instructions
    assert (
        'CMD ["uvicorn", "citemind_inference.main:app", "--host", "0.0.0.0", "--port", "9000"]'
        in instructions
    )
    # 健康检查由 compose 添加，镜像不内置 HEALTHCHECK 指令。
    assert not any(line.upper().startswith("HEALTHCHECK") for line in instructions)


def test_inference_dockerfile_does_not_install_sentence_transformers() -> None:
    content = INFERENCE_DOCKERFILE.read_text(encoding="utf-8").lower()

    for forbidden in FORBIDDEN_INFERENCE_DEPENDENCIES:
        assert forbidden not in content, f"inference 镜像不应安装 {forbidden}"


def test_inference_dockerignore_excludes_secrets_caches_and_tests() -> None:
    entries = dockerignore_entries()

    for required in (".env", ".venv", ".pytest_cache", ".mypy_cache", ".ruff_cache", "tests"):
        assert required in entries, f"inference/.dockerignore 缺少 {required}"


def test_inference_dockerignore_excludes_host_model_artifacts() -> None:
    entries = dockerignore_entries()

    for required in ("models", ".cache", "**/*.safetensors", "**/pytorch_model.bin"):
        assert required in entries, f"宿主机模型或缓存不得进入构建上下文：{required}"


def test_inference_project_declares_only_expected_runtime_dependencies() -> None:
    pyproject = read_pyproject()
    project = pyproject["project"]
    dependencies = [dependency.lower() for dependency in project["dependencies"]]

    assert project["name"] == "citemind-inference"
    assert project["requires-python"] == ">=3.12,<3.13"
    assert any(dependency.startswith("fastapi") for dependency in dependencies)
    assert any(dependency.startswith("pydantic-settings") for dependency in dependencies)
    assert any(dependency.startswith("uvicorn") for dependency in dependencies)
    for required in REQUIRED_INFERENCE_DEPENDENCIES:
        assert any(dependency.startswith(required) for dependency in dependencies), (
            f"inference 必须直接依赖 {required}"
        )
    for forbidden in FORBIDDEN_INFERENCE_DEPENDENCIES:
        assert not any(dependency.startswith(forbidden) for dependency in dependencies)


def test_inference_pyproject_routes_torch_to_cpu_index() -> None:
    tool_uv = read_pyproject()["tool"]["uv"]

    assert any(
        index["name"] == "pytorch-cpu"
        and index["url"] == PYTORCH_CPU_INDEX_URL
        and index["explicit"] is True
        for index in tool_uv["index"]
    ), "inference 必须声明显式的 PyTorch CPU index"
    assert tool_uv["sources"]["torch"] == {"index": "pytorch-cpu"}


def test_inference_freezes_bge_model_and_dimension() -> None:
    config = (INFERENCE_DIR / "src" / "citemind_inference" / "config.py").read_text(
        encoding="utf-8"
    )

    assert FROZEN_EMBEDDING_MODEL in config
    assert FROZEN_EMBEDDING_REVISION in config
    assert f"DEFAULT_EMBEDDING_MODEL_PATH = Path(\"{MODEL_DIR_IN_IMAGE}\")" in config
    assert "EMBEDDING_DIMENSION = 512" in config
    assert "EMBEDDING_MAX_TOKENS = 512" in config


def test_inference_source_never_imports_sentence_transformers() -> None:
    pattern = re.compile(r"^\s*(?:import|from)\s+sentence_transformers", re.MULTILINE)

    for source in (INFERENCE_DIR / "src").rglob("*.py"):
        assert not pattern.search(source.read_text(encoding="utf-8")), (
            f"{source} 不得导入 sentence_transformers"
        )


def test_inference_lockfile_exists_and_excludes_model_dependencies() -> None:
    assert INFERENCE_UV_LOCK.is_file(), "inference/uv.lock 必须存在"
    content = INFERENCE_UV_LOCK.read_text(encoding="utf-8")

    assert content.strip(), "inference/uv.lock 不能为空"
    for forbidden in FORBIDDEN_INFERENCE_DEPENDENCIES:
        assert f'name = "{forbidden}"' not in content, f"inference 锁文件不应包含 {forbidden}"


def test_inference_lockfile_installs_cpu_torch_without_cuda() -> None:
    lock = tomllib.loads(INFERENCE_UV_LOCK.read_text(encoding="utf-8"))
    packages = lock["package"]
    names = [package["name"] for package in packages]

    assert not any(name.startswith("nvidia-") for name in names), (
        "CPU 构建的锁文件不得包含 nvidia-* 依赖"
    )
    assert "transformers" in names

    torch_packages = [package for package in packages if package["name"] == "torch"]
    assert torch_packages, "inference 锁文件必须包含 torch"
    for package in torch_packages:
        assert package["source"] == {"registry": PYTORCH_CPU_INDEX_URL}, (
            "torch 必须只来自 PyTorch CPU index"
        )
        for dependency in package.get("dependencies", []):
            assert not dependency["name"].startswith("nvidia-")
