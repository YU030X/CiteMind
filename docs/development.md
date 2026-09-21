# 开发约定

> 当前仓库已建立 Phase 0 工程骨架：FastAPI 健康检查与 OpenAPI、Vue 3/Vite/TypeScript 页面、Jev 开发期判断脚本及对应聚焦检查已经可运行。数据库、迁移、Compose、worker、inference 和产品功能仍未实现。

## 环境和依赖

当前骨架已在 Windows 11、Node.js 24.16.0、pnpm 11.22.0、uv 0.12.4 和由 uv 管理的 CPython 3.12.13 上验证。完整队列运行仍以 Linux 容器或 Windows WSL2 为验收环境。

- 后端使用 Python 3.12、`uv`、`pyproject.toml` 和 `uv.lock`。当前已锁定 FastAPI、Pydantic v2、pydantic-settings、Uvicorn 及开发检查依赖；HTTPX 当前仅用于 ASGI 接口测试。SQLAlchemy 2.x、psycopg 3、Alembic 和 pgvector-python 在数据库入口实现时再加入。worker 计划使用独立同步 Session；推理进程计划单独安装 sentence-transformers/PyTorch CPU。
- 前端已使用 Vue 3、Vite 和 TypeScript，并由根目录 pnpm workspace 管理。Element Plus 在出现实际组件需求后再加入，不为骨架预装。
- 开发期 Jev 判断使用 Node 脚本、Vercel AI SDK 和 `typesafe-ai/jev`，只从服务端 `AI_GATEWAY_API_KEY` 读取凭据，不进入前端产物或产品运行时。
- 文档处理计划在 MVP 加入 markdown-it-py、pypdf、jieba；完整范围再加 pdfplumber、python-docx、BeautifulSoup4/lxml 和受限网页抓取。
- 数据服务：PostgreSQL 17 + pgvector 0.8.x、Redis；云生成默认选 DeepSeek API 的 `deepseek-flash` 非思考模式，模型名保持配置化。具体版本、接口行为与镜像 digest 在 Phase 0 验证后固定，不使用浮动 `latest`。

## 目录

```text
backend/src/evidencehub/   # 已建立：API 应用与配置入口
frontend/                  # 已建立：Vue 控制台骨架
scripts/                   # 已建立：开发辅助和质量门禁
tests/unit/                # 已建立：纯逻辑和 API 骨架测试
inference/                 # 待建：独立模型服务，仅共享协议
migrations/                # 待建：Alembic 数据库迁移
deploy/compose/            # 待建：单机服务配置
tests/integration/         # 待建：真实数据库、broker 与 worker
fixtures/documents/        # 待建：无敏感样本
eval/datasets/             # 待建：固定题集及版本
eval/results/              # 待建：可复算的评估产物
```

只在对应实现落地时创建待建目录，不创建空目录充数。迁移使用 Alembic；运行时 `create_all` 不负责更新生产 schema。前端类型从 OpenAPI 契约生成；Python 内部字段用 snake_case，外部 JSON 用 camelCase alias。

## 建设顺序

1. 验证 Python、数据库、Celery、embedding 模型和云 LLM 的确切兼容版本，建立锁文件、服务入口、首个迁移和两个不同权限的测试账号。
2. 从上传到暂存 generation 的真实队列链路开始，先保留来源和任务事实，再做聊天 UI。
3. 在同一授权作用域实现两路检索与 RRF；建立可定位引用、无证据拒答和版本切换。
4. 扩展格式、文档 ACL、重排、增量缓存和评估。每一步更新对应文档和可复现测试。

## 验证规则

- 对纯逻辑运行聚焦单测；授权 SQL、迁移和向量维度使用真实 PostgreSQL/pgvector；任务恢复使用真实 Redis 与独立 Celery worker。
- 对入库验证重复投递、Redis 断连或重启、worker 强制退出、租约过期后的迟到回写、旧版本继续可查和单次发布。
- 对问答验证越权内容从候选至引用全链不可见、撤权后的历史/下载、结构化回答和模型超时。性能与质量按 [评估与验收](evaluation.md) 的数据集和硬件条件实测。
- 当前可执行 `uv sync --frozen`、Ruff、mypy、pytest、Jev 脚本测试和前端构建。分角色镜像、集成测试、依赖扫描、SBOM、GHCR 发布与恢复演练仍是后续计划。

## 当前命令

以下命令均从仓库根目录执行：

```powershell
uv sync --frozen
uv run uvicorn evidencehub.main:app --reload
uv run ruff check backend/src tests/unit
uv run mypy
uv run pytest tests/unit/test_health.py
pnpm install --frozen-lockfile
pnpm test:jev
pnpm jev -- scripts/jev-request.example.json
pnpm --dir frontend dev
pnpm frontend:build
```

`pnpm jev` 从标准输入或首个参数指定的 JSON 文件读取 `{ state, questions }`，固定调用 `typesafe-ai/jev`；输入只应包含完成当前判断所需的非敏感状态。提交前检查差异、生成文件和密钥，只报告实际运行结果与未运行的验证。
