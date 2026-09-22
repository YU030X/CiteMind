# CiteMind 业务镜像：安装锁定的 Python 依赖与后端包，供 api 与 worker 两种入口复用。
# 由 deploy/compose/compose.yml 用仓库根目录作为构建上下文构建；runtime 是共享基础
# stage，api 与 worker 只在其上覆盖暴露端口与启动命令。
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

# api 与 worker 都不需要 root；marker 目录先在镜像内创建并归属 citemind，
# 这样首次挂载命名卷时会继承非 root 所有权。USER 会被下面的 final stage 继承。
RUN useradd --create-home --uid 10001 citemind \
    && mkdir -p /var/lib/citemind/probe-markers \
    && chown -R citemind:citemind /app /var/lib/citemind
USER citemind

# api final target：只提供 HTTP 入口，不运行 Celery worker。
FROM runtime AS api
EXPOSE 8000
CMD ["uvicorn", "evidencehub.main:app", "--host", "0.0.0.0", "--port", "8000"]

# worker final target：必须是最后一个 stage，未指定 --target 时默认构建它。
FROM runtime AS worker
CMD ["celery", "-A", "evidencehub.worker:celery_app", "worker", "--loglevel=INFO"]
