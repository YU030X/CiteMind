"""frontend-gateway 的部署资产静态检查：nginx 配置、构建 Dockerfile 与专用 dockerignore。

这些检查补充但不等价于真实构建与运行；真实启动验收由 Linux Compose 完成。
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
GATEWAY_DIR = REPO_ROOT / "deploy" / "compose" / "gateway"
NGINX_CONF = GATEWAY_DIR / "nginx.conf"
FRONTEND_DOCKERFILE = REPO_ROOT / "deploy" / "compose" / "frontend.Dockerfile"
FRONTEND_DOCKERIGNORE = (
    REPO_ROOT / "deploy" / "compose" / "frontend.Dockerfile.dockerignore"
)
COMPOSE_FILE = REPO_ROOT / "deploy" / "compose" / "compose.yml"
GITATTRIBUTES = REPO_ROOT / ".gitattributes"

NODE_IMAGE = (
    "node:24-alpine@sha256:"
    "ebfe2f90462722a7a4de65e91990e97fe0d401c70e0e762c5b53302f905ec1c1"
)
NGINX_IMAGE = (
    "nginxinc/nginx-unprivileged:1.31-alpine@sha256:"
    "e75f89810bf5bfbcf58a1cfb32a1a11de55b7623d732e67735d513b720d7436a"
)

LF_ATTRIBUTE_LINES = (
    "deploy/compose/frontend.Dockerfile text eol=lf",
    "deploy/compose/frontend.Dockerfile.dockerignore text eol=lf",
    "deploy/compose/gateway/nginx.conf text eol=lf",
)


def nginx_text() -> str:
    return NGINX_CONF.read_text(encoding="utf-8")


def dockerignore_entries() -> list[str]:
    return [
        line.strip()
        for line in FRONTEND_DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_nginx_listens_on_8080_for_ipv4_and_ipv6_with_server_tokens_off() -> None:
    text = nginx_text()

    assert "listen 8080;" in text
    assert "listen [::]:8080;" in text
    assert "server_tokens off;" in text


def test_nginx_healthz_is_local_and_does_not_proxy() -> None:
    text = nginx_text()

    assert "location = /healthz" in text
    assert 'return 200 "ok' in text
    # healthz 段内不得出现代理，否则 api 故障会让网关自身不健康。
    healthz_block = text.split("location = /healthz", 1)[1].split("}", 1)[0]
    assert "proxy_pass" not in healthz_block


def test_nginx_api_proxy_uses_docker_resolver_and_preserves_request_uri() -> None:
    text = nginx_text()

    assert "resolver 127.0.0.11 ipv6=off valid=10s;" in text
    assert "set $api_upstream http://api:8000;" in text
    assert "proxy_pass $api_upstream$request_uri;" in text
    # 静态 proxy_pass 会在构建期解析 api 失败，也会在容器重建后缓存旧 IP。
    assert not re.search(r"proxy_pass\s+http://api", text)


def test_nginx_api_location_does_not_fall_back_to_spa() -> None:
    text = nginx_text()

    api_block = text.split("location /api/", 1)[1].split("location /assets/", 1)[0]
    assert "try_files" not in api_block
    assert "index.html" not in api_block


def test_nginx_serves_spa_fallback_and_immutable_assets() -> None:
    text = nginx_text()

    assert "try_files $uri $uri/ /index.html;" in text
    assert 'Cache-Control "public, max-age=31536000, immutable"' in text
    assert 'Cache-Control "no-store"' in text


def test_nginx_sets_security_headers_with_merge_inheritance() -> None:
    text = nginx_text()

    # 不加 merge 时子 location 的 add_header 会整组丢弃父级安全头。
    assert "add_header_inherit merge;" in text
    assert "Content-Security-Policy" in text
    assert 'X-Content-Type-Options "nosniff"' in text
    assert 'X-Frame-Options "DENY"' in text
    assert "Referrer-Policy" in text


def test_frontend_dockerfile_pins_node_and_nginx_by_digest() -> None:
    content = FRONTEND_DOCKERFILE.read_text(encoding="utf-8")
    pinned = [
        line
        for line in content.splitlines()
        if line.startswith("FROM ") or line.startswith("COPY --from=")
    ]

    assert any(NODE_IMAGE in line for line in pinned)
    assert any(NGINX_IMAGE in line for line in pinned)
    assert "@sha256:" in NODE_IMAGE
    assert "@sha256:" in NGINX_IMAGE


def test_frontend_dockerfile_builds_only_the_frontend_workspace_frozen() -> None:
    content = FRONTEND_DOCKERFILE.read_text(encoding="utf-8")

    assert "corepack enable" in content
    assert "pnpm install --frozen-lockfile" in content
    assert "--filter @citemind/frontend..." in content
    assert "pnpm --filter @citemind/frontend build" in content


def test_frontend_dockerfile_copies_gateway_config_and_validates_it() -> None:
    content = FRONTEND_DOCKERFILE.read_text(encoding="utf-8")

    assert "COPY deploy/compose/gateway/nginx.conf /etc/nginx/conf.d/default.conf" in content
    assert "RUN nginx -t" in content
    assert "EXPOSE 8080" in content
    # 运行镜像沿用 nginx-unprivileged 的非 root 用户，不得切回 root。
    assert "USER root" not in content


def test_frontend_dockerignore_whitelists_workspace_sources_and_blocks_artifacts() -> None:
    entries = dockerignore_entries()

    for required in (
        "*",
        "!package.json",
        "!pnpm-workspace.yaml",
        "!pnpm-lock.yaml",
        "!frontend/**",
        "!deploy/compose/gateway/nginx.conf",
    ):
        assert required in entries, f"frontend.Dockerfile.dockerignore 缺少 {required}"

    for blocked in ("frontend/node_modules", "frontend/dist", "**/.env"):
        assert blocked in entries, f"frontend.Dockerfile.dockerignore 缺少 {blocked}"


def test_gitattributes_pins_gateway_assets_to_lf() -> None:
    lines = [line.strip() for line in GITATTRIBUTES.read_text(encoding="utf-8").splitlines()]

    for attribute in LF_ATTRIBUTE_LINES:
        assert attribute in lines


def test_compose_gateway_uses_the_frontend_dockerfile() -> None:
    text = COMPOSE_FILE.read_text(encoding="utf-8")

    assert "dockerfile: deploy/compose/frontend.Dockerfile" in text
