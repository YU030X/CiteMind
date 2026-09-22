"""inference 独立服务的部署资产静态检查：不启动容器，只校验仓库内文件。

这些检查补充但不等价于真实构建与运行；真实启动验收由 Linux Compose 完成。
"""

import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).parents[2]
INFERENCE_DIR = REPO_ROOT / "inference"
INFERENCE_DOCKERFILE = INFERENCE_DIR / "Dockerfile"
INFERENCE_DOCKERIGNORE = INFERENCE_DIR / ".dockerignore"
INFERENCE_PYPROJECT = INFERENCE_DIR / "pyproject.toml"
INFERENCE_UV_LOCK = INFERENCE_DIR / "uv.lock"
SHARED_DOCKERFILE = REPO_ROOT / "deploy" / "compose" / "Dockerfile"

PYTHON_IMAGE = (
    "python:3.12-slim@sha256:"
    "2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9"
)
UV_IMAGE = (
    "ghcr.io/astral-sh/uv:0.12.4@sha256:"
    "d0a6eca6c669dc7e9c51218707b8438a3d30402733d739dcc00adb3e213e8f5c"
)

FORBIDDEN_INFERENCE_DEPENDENCIES = ("torch", "sentence-transformers", "transformers")


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
    api_command = (
        'CMD ["uvicorn", "evidencehub.main:app", "--host", "0.0.0.0", "--port", "8000"]'
    )
    assert api_command in content


def test_shared_dockerfile_worker_target_keeps_celery_command() -> None:
    content = SHARED_DOCKERFILE.read_text(encoding="utf-8")

    assert (
        'CMD ["celery", "-A", "evidencehub.worker:celery_app", "worker", "--loglevel=INFO"]'
        in content
    )
    # 非 root 与 marker 目录仍由共享 runtime stage 建立并被两个 target 继承。
    assert "USER citemind" in content
    assert "/var/lib/citemind/probe-markers" in content


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
    assert "chown -R inference:inference /app" in content


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


def test_inference_dockerfile_does_not_include_models_or_forbidden_dependencies() -> None:
    content = INFERENCE_DOCKERFILE.read_text(encoding="utf-8").lower()

    for forbidden in FORBIDDEN_INFERENCE_DEPENDENCIES:
        assert forbidden not in content, f"inference 镜像不应安装 {forbidden}"


def test_inference_dockerignore_excludes_secrets_caches_and_tests() -> None:
    entries = dockerignore_entries()

    for required in (".env", ".venv", ".pytest_cache", ".mypy_cache", ".ruff_cache", "tests"):
        assert required in entries, f"inference/.dockerignore 缺少 {required}"


def test_inference_project_declares_only_expected_runtime_dependencies() -> None:
    pyproject = read_pyproject()
    project = pyproject["project"]
    dependencies = [dependency.lower() for dependency in project["dependencies"]]

    assert project["name"] == "citemind-inference"
    assert project["requires-python"] == ">=3.12,<3.13"
    assert any(dependency.startswith("fastapi") for dependency in dependencies)
    assert any(dependency.startswith("pydantic-settings") for dependency in dependencies)
    assert any(dependency.startswith("uvicorn") for dependency in dependencies)
    for forbidden in FORBIDDEN_INFERENCE_DEPENDENCIES:
        assert not any(dependency.startswith(forbidden) for dependency in dependencies)


def test_inference_lockfile_exists_and_excludes_model_dependencies() -> None:
    assert INFERENCE_UV_LOCK.is_file(), "inference/uv.lock 必须存在"
    content = INFERENCE_UV_LOCK.read_text(encoding="utf-8")

    assert content.strip(), "inference/uv.lock 不能为空"
    for forbidden in FORBIDDEN_INFERENCE_DEPENDENCIES:
        assert f'name = "{forbidden}"' not in content, f"inference 锁文件不应包含 {forbidden}"
