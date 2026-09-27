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

# api 与 worker 都不需要 root；marker 与文档存储目录先在镜像内创建并归属 citemind，
# 这样首次挂载命名卷时会继承非 root 所有权（uid 10001 可直接写入）。
# USER 会被下面的 final stage 继承。
RUN useradd --create-home --uid 10001 citemind \
    && mkdir -p /var/lib/citemind/probe-markers /var/lib/citemind/documents \
    && chown -R citemind:citemind /app /var/lib/citemind
USER citemind

# api final target：只提供 HTTP 入口，不运行 Celery worker。
# --no-proxy-headers：由应用自己按 TRUSTED_PROXY_CIDRS 解析 X-Real-IP，
# 不让 uvicorn 改写 request.client，避免两套代理信任规则并存。
FROM runtime AS api
# 构建期下载需要写入镜像根下的 /models；与 inference 构建期一致，只在构建阶段以 root 执行，
# 随后立即恢复非 root 运行用户。
USER root
# 问答生成的输入预算需要 DeepSeek V4.1 tokenizer 产物：只在构建期从固定 revision 下载，
# 并用 Hub commit 与包内钉死的字节大小/SHA-256 双重校验后烘入 /models/deepseek-v41；
# 运行期完全离线、不读凭据、不挂载宿主模型卷。
RUN python backend/scripts/prepare_generation_tokenizer.py
# 随镜像分发上游许可与来源说明；放在模型目录之外，/models/deepseek-v41/ 仍恰为一个产物。
COPY backend/third_party/deepseek-v4-flash-vision-exp-LICENSE.txt /models/deepseek-v41-LICENSE.txt
USER citemind
EXPOSE 8000
CMD ["uvicorn", "rag_backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]

# worker final target：必须是最后一个 stage，未指定 --target 时默认构建它。
FROM runtime AS worker
# worker 额外安装 pypdf（PDF 解析，只在 worker 依赖组）。tokenizers 自 generation 切片起属主依赖，
# api 与 worker 阶段都会安装；DeepSeek 生成预算所需 tokenizer 产物只在 api 阶段烘入（worker
# 不做问答生成），依赖与镜像计划见 docs/development.md。
RUN --mount=type=cache,target=/home/citemind/.cache/uv,uid=10001,gid=10001 uv sync --frozen --no-dev --group worker
# BGE tokenizer 产物只在 inference 构建期生成，通过 Compose 具名构建上下文 model_assets 复制。
# 只取计数所需的四个文件，使目录内恰为这四个，不含 config.json 与 model.safetensors。
COPY --from=model_assets /models/bge-small-zh-v1.5/tokenizer.json /models/bge-small-zh-v1.5/tokenizer.json
COPY --from=model_assets /models/bge-small-zh-v1.5/vocab.txt /models/bge-small-zh-v1.5/vocab.txt
COPY --from=model_assets /models/bge-small-zh-v1.5/tokenizer_config.json /models/bge-small-zh-v1.5/tokenizer_config.json
COPY --from=model_assets /models/bge-small-zh-v1.5/special_tokens_map.json /models/bge-small-zh-v1.5/special_tokens_map.json
# 构建期用标准库逐字节复核四个产物；失败即构建失败，运行期不联网、不重新下载模型。
RUN python -c "from pathlib import Path; from rag_backend.ingestion.token_counting import verify_tokenizer_directory; verify_tokenizer_directory(Path('/models/bge-small-zh-v1.5'))"
CMD ["celery", "-A", "rag_backend.worker:celery_app", "worker", "--loglevel=INFO"]
