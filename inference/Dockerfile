# CiteMind inference 镜像：安装 inference 自己的锁定依赖并运行独立的 uvicorn 进程。
# 由 deploy/compose/compose.yml 的 inference 服务以 inference/ 目录作为构建上下文构建。
# 基础镜像与 uv 均按 digest 固定，不使用浮动 latest；不安装模型权重或缓存。
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9 AS runtime

COPY --from=ghcr.io/astral-sh/uv:0.12.4@sha256:d0a6eca6c669dc7e9c51218707b8438a3d30402733d739dcc00adb3e213e8f5c /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# 依赖层只依赖锁文件，源码改动不会使依赖重新解析；严格按 uv.lock 安装且不含 dev 组。
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

# 推理进程不需要 root；uid 10002 与 api/worker 的 10001 区分开。
RUN useradd --create-home --uid 10002 inference \
    && chown -R inference:inference /app
USER inference

EXPOSE 9000
# 健康检查由 compose 添加，镜像本身不内置 HEALTHCHECK。
CMD ["uvicorn", "citemind_inference.main:app", "--host", "0.0.0.0", "--port", "9000"]
