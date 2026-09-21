# 开发约定

> 当前仓库已建立 Phase 0 工程骨架：FastAPI 健康检查与 OpenAPI、Vue 3/Vite/TypeScript 页面、SQLAlchemy 异步数据库会话、Alembic 的 pgvector 扩展迁移、Jev 开发期判断脚本及对应聚焦检查已经可运行，Docker 与真实 PostgreSQL/pgvector 迁移也已完成实测。业务表、Compose、worker、inference 和产品功能仍未实现，因此尚未达到 Phase 0 退出条件。

## 数据库验收实测结果

本节只记录已真实运行并核对过的结果，不是目标值；命令与范围见下文验证规则。

| 项 | 实测值 |
| --- | --- |
| 环境 | Windows 11 + Docker 29.4.3；镜像 `pgvector/pgvector:pg17`（digest `sha256:cf134a767f474095eeba57e0117be8e568e011a63f33fbf252f14c9b760f8e6f`） |
| 服务端 | PostgreSQL 17.11 (Debian 17.11-1.pgdg12+2)，pgvector 0.8.6 |
| upgrade | 在线 `uv run alembic upgrade head` 应用 `20260921_0001`，`alembic_version.version_num` 与该 revision 一致，`pg_extension` 中出现 `vector` 0.8.6 |
| 512 维字面量 | `SELECT vector_dims(CAST('[0,...0]' AS vector))`（512 个元素）返回 512 |
| downgrade | `uv run alembic downgrade base` 后 `pg_extension` 无 `vector` 行，且 `CAST(... AS vector)` 报 `type "vector" does not exist` |

这轮验收只覆盖 `vector` 扩展的启用与回收、512 维字面量的解析和迁移的降级，不是 `chunk_embedding VECTOR(512)` 列约束验收：业务表、`Vector(512)` 列和 pgvector-python 的类型绑定都尚未落地，因此不能据此声称向量列维度契约已通过。

## 环境和依赖

当前骨架已在 Windows 11、Node.js 24.16.0、pnpm 11.22.0、uv 0.12.4 和由 uv 管理的 CPython 3.12.13 上验证。完整队列运行仍以 Linux 容器或 Windows WSL2 为验收环境。

- 后端使用 Python 3.12、`uv`、`pyproject.toml` 和 `uv.lock`。当前已锁定 FastAPI、Pydantic v2、pydantic-settings、Uvicorn、SQLAlchemy 2.x、psycopg 3、Alembic 和 pgvector-python；HTTPX 当前仅用于 ASGI 接口测试。psycopg 只安装 `[binary]` extra，连接池使用 SQLAlchemy 自带实现，`psycopg_pool` 未被使用；pgvector-python 已锁定，但业务的 `Vector` 类型与向量列尚未落地，当前迁移只创建 `vector` 扩展。worker 计划使用独立同步 Session；推理进程计划单独安装 sentence-transformers/PyTorch CPU。
- Windows 上 psycopg 异步模式不能使用默认的 `ProactorEventLoop`：应用启动用 `--loop evidencehub.event_loop:create_event_loop` 提供自定义事件循环工厂（Windows 返回 `SelectorEventLoop`，其他平台返回 `asyncio.new_event_loop`），不修改全局事件循环 policy；Alembic 在线迁移在 Windows 内部同样切换到 `SelectorEventLoop`。
- 前端已使用 Vue 3、Vite 和 TypeScript，并由根目录 pnpm workspace 管理。Element Plus 在出现实际组件需求后再加入，不为骨架预装。
- 开发期 Jev 判断使用 Node 脚本、Vercel AI SDK 和 `typesafe-ai/jev`，只从服务端 `AI_GATEWAY_API_KEY` 读取凭据，不进入前端产物或产品运行时。
- 文档处理计划在 MVP 加入 markdown-it-py、pypdf、jieba；完整范围再加 pdfplumber、python-docx、BeautifulSoup4/lxml 和受限网页抓取。
- 数据服务：PostgreSQL 17 + pgvector 0.8.x、Redis；云生成默认选 DeepSeek API 的 `deepseek-flash` 非思考模式，模型名保持配置化。迁移验收已实测 PostgreSQL 17.11 + pgvector 0.8.6（镜像 `pgvector/pgvector:pg17`）；其余版本、接口行为与镜像 digest 在 Phase 0 验证后固定，不使用浮动 `latest`。

## 目录

```text
backend/src/evidencehub/   # 已建立：API、配置与数据库会话入口
frontend/                  # 已建立：Vue 控制台骨架
scripts/                   # 已建立：开发辅助和质量门禁实现
tests/tooling/             # 已建立：开发工具的 Node 测试
tests/unit/                # 已建立：后端纯逻辑和 API 骨架测试
inference/                 # 待建：独立模型服务，仅共享协议
migrations/                # 已建立：pgvector 扩展迁移；业务迁移待建
deploy/compose/            # 待建：单机服务配置
tests/integration/         # 已建立：迁移测试与不连库的破坏性守卫测试；broker 与 worker 测试待建
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
- 当前可执行 `uv sync --frozen`、Ruff、mypy、非集成 pytest、Jev 脚本测试、Alembic 离线 SQL 检查和前端构建。真实 PostgreSQL/pgvector 集成测试需要 `CITEMIND_TEST_DATABASE_URL`（`postgresql+psycopg` 驱动、数据库名以 `_test` 结尾）并显式确认 `CITEMIND_ALLOW_DESTRUCTIVE_TEST_DB=1`，因为测试会执行 upgrade 和 downgrade；测试数据库必须由 CiteMind 独占、不能与其他应用共享，迁移账号必须拥有 `CREATE EXTENSION` 权限。运行前测试先断言 `current_database()` 与 URL 中的库名一致、`vector` 扩展不存在且 Alembic 处于 base，任何一项不满足都会失败而不是继续。未设置测试 DSN 时仅明确跳过。分角色镜像、依赖扫描、SBOM、GHCR 发布与恢复演练仍是后续计划。
- 在线迁移只接受显式 DSN：Alembic 配置项 `sqlalchemy.url` 优先，否则必须设置 `CITEMIND_MIGRATION_DATABASE_URL`，缺失或不符合 SQLAlchemy URL 规则时直接以非零状态失败，不会回退到 `CITEMIND_DATABASE_URL` 或开发默认 URL。该 DSN 需要 `CREATE EXTENSION` 权限，只用于迁移进程，不进入 API 与 worker 的运行配置。
- 离线 `uv run alembic upgrade head --sql` 只生成 SQL、不连接数据库，仍可使用开发默认 URL。

## 当前命令

以下命令均从仓库根目录执行：

```powershell
uv sync --frozen
uv run uvicorn evidencehub.main:app --reload --loop evidencehub.event_loop:create_event_loop
uv run ruff check backend/src migrations tests
uv run mypy
uv run pytest -m "not integration"
uv run alembic heads
uv run alembic upgrade head --sql
uv run pytest -m integration
pnpm install --frozen-lockfile
pnpm test:jev
pnpm jev -- scripts/jev-request.example.json
pnpm --dir frontend dev
pnpm frontend:build
```

`pnpm jev` 从标准输入或首个参数指定的 JSON 文件读取 `{ state, questions }`，固定调用 `typesafe-ai/jev`；输入只应包含完成当前判断所需的非敏感状态。提交前检查差异、生成文件和密钥，只报告实际运行结果与未运行的验证。
