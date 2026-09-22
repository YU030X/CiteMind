"""本地数据服务切片的静态检查：不启动容器，只校验仓库内的 Compose 与 initdb 文件。"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
COMPOSE_FILE = REPO_ROOT / "deploy" / "compose" / "compose.yml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
INITDB_SCRIPTS = sorted((REPO_ROOT / "deploy" / "compose" / "initdb").glob("*.sh"))
GITATTRIBUTES = REPO_ROOT / ".gitattributes"

REQUIRED_VARIABLE_PATTERN = re.compile(r"\$\{(CITEMIND_[A-Z0-9_]+):\?")

LF_ATTRIBUTE_LINE = "deploy/compose/**/*.sh text eol=lf"
PINNED_IMAGES = (
    "pgvector/pgvector:pg17@sha256:cf134a767f474095eeba57e0117be8e568e011a63f33fbf252f14c9b760f8e6f",
    "redis:7.4.9@sha256:a8f08480e1f88f2647fed492d1178c06abb0d0c1fbf02c682a61e2f483fb3954",
)


def compose_text() -> str:
    return COMPOSE_FILE.read_text(encoding="utf-8")


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

    required = set(REQUIRED_VARIABLE_PATTERN.findall(compose_text()))
    assert required, "compose 未声明必填的 CITEMIND 变量时该检查失去意义"

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


def test_compose_pins_the_two_data_service_images_by_digest() -> None:
    images = compose_entries("image")

    assert len(images) == 2, "本地数据服务切片只应有 postgres 与 redis 两个镜像"
    assert sorted(images) == sorted(PINNED_IMAGES)
    for image in images:
        assert "@sha256:" in image, f"{image} 未固定 digest"


def test_compose_publishes_only_loopback_ports_by_default() -> None:
    published_ports = compose_entries("ports")

    assert len(published_ports) == 2
    for mapping in published_ports:
        assert mapping.startswith("127.0.0.1:"), f"{mapping} 必须只绑定回环地址"

    text = compose_text()
    assert "CITEMIND_POSTGRES_PORT:-55432" in text
    assert "CITEMIND_REDIS_PORT:-56379" in text


def test_compose_keeps_declared_service_contracts() -> None:
    text = compose_text()

    assert text.count("restart: unless-stopped") == 2
    assert text.count("healthcheck:") == 2
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
