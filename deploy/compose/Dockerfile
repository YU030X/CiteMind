# CiteMind worker 镜像：安装锁定的 Python 依赖与后端包，运行 Celery worker。
# 由 deploy/compose/compose.yml 的 worker 服务用仓库根目录作为构建上下文构建。
# 基础镜像与 uv 均按 digest 固定，不使用浮动 latest。
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9 AS runtime

COPY --from=ghcr.io/astral-sh/uv:0.12.4@sha256:d0a6eca6c669dc7e9c51218707b8438a3d30402733d739dcc00adb3e213e8f5c /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# 依赖层只依赖锁文件，源码改动不会使依赖重新解析。
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY backend ./backend
RUN uv sync --frozen --no-dev

# worker 与 queue-probe 不需要 root；marker 目录先在镜像内创建并归属 citemind，
# 这样首次挂载命名卷时会继承非 root 所有权。
RUN useradd --create-home --uid 10001 citemind \
    && mkdir -p /var/lib/citemind/probe-markers \
    && chown -R citemind:citemind /app /var/lib/citemind
USER citemind

CMD ["celery", "-A", "evidencehub.worker:celery_app", "worker", "--loglevel=INFO"]
