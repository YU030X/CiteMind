# CiteMind inference 镜像：安装 inference 自己的锁定依赖并运行独立的 uvicorn 进程。
# 由 deploy/compose/compose.yml 的 inference 服务以 inference/ 目录作为构建上下文构建。
# 基础镜像与 uv 均按 digest 固定；模型只在构建期下载，运行期完全离线。
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9 AS runtime

COPY --from=ghcr.io/astral-sh/uv:0.12.4@sha256:d0a6eca6c669dc7e9c51218707b8438a3d30402733d739dcc00adb3e213e8f5c /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2 \
    OPENBLAS_NUM_THREADS=2 \
    TOKENIZERS_PARALLELISM=false \
    EMBEDDING_TORCH_THREADS=2 \
    EMBEDDING_MODEL_PATH=/models/bge-small-zh-v1.5 \
    EMBEDDING_MODEL_REVISION=7999e1d3359715c523056ef9478215996d62a620 \
    RERANK_ENABLED=0 \
    RERANK_MODEL_PATH=/models/bge-reranker-base \
    RERANK_MODEL_REVISION=2cfc18c9415c912f9d8155881c133215df768a70

WORKDIR /app

# 依赖层只依赖锁文件，源码改动不会使依赖重新解析；严格按 uv.lock 安装且不含 dev 组。
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project

# 使用标准库下载公开文件，不读取凭据；校验 Hub commit、Git blob/LFS hash 并保存 SHA-256。
COPY scripts/prepare_model.py ./scripts/prepare_model.py
RUN python scripts/prepare_model.py

# reranker 默认不从网络下载；只有显式传入 RERANK_MODEL_ENABLED=1 才在构建期获取权重并生成
# rerank-model-manifest.json。默认关闭时镜像不含 reranker 权重，进程以 RERANK_ENABLED=0 运行。
ARG RERANK_MODEL_ENABLED=0
RUN if [ "$RERANK_MODEL_ENABLED" = "1" ]; then python scripts/prepare_model.py --model rerank; fi

# 随镜像分发模型许可与来源说明；放在模型目录之外，/models/bge-small-zh-v1.5/ 仍恰为 6 个模型产物。
COPY third_party/bge-small-zh-v1.5-LICENSE.txt /models/bge-small-zh-v1.5-LICENSE.txt
COPY third_party/bge-reranker-base-LICENSE.txt /models/bge-reranker-base-LICENSE.txt

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

# 推理进程不需要 root；uid 10002 与 api/worker 的 10001 区分开。
RUN useradd --create-home --uid 10002 inference \
    && chown inference:inference /app
USER inference

EXPOSE 9000
# 健康检查由 compose 添加，镜像本身不内置 HEALTHCHECK。
CMD ["uvicorn", "citemind_inference.main:app", "--host", "0.0.0.0", "--port", "9000"]
