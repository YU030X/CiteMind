# 知据 CiteMind

CiteMind 是一个面向小团队的企业知识库应用：用自然语言提问，回答带可点击的来源引用，并能回到对应原文与版本。它同时把知识库权限、文档更新与删除贯通到检索和后续访问中——资料不足时明确拒答，被过滤文档的存在性不被泄露。

> [!IMPORTANT]
> Phase 1 的六项退出条件已达成（证据与未验边界见[评估计划](docs/evaluation.md)与[开发约定](docs/development.md)），但仓库仍处于开发阶段、并非开箱可用的成品；Phase 2 及后续仍为计划。按默认配置启动六服务后，**真实入库与问答生成都是关闭的**：上传返回 `202` 只表示文件与任务已持久化，文档尚不可检索；问答端点返回静态 `503` 且不联网。要跑通完整问答，需要显式开启入库与生成开关并提供云模型密钥，见[快速开始](#快速开始)与[个人演示流程](docs/personal-demo.md)。

## 核心能力

- **有来源的回答**：回答逐句附带可点击的内联引用，引用详情由服务端从已保存片段映射，可定位到 Markdown 行区间或 PDF 页码，并标明文档版本是否为当前版本。
- **权限与版本持续生效**：KB 成员授权（OWNER / EDITOR / READER）；文档更新时旧版本继续服务直到新版本就绪；删除或撤权后，检索候选、历史消息与引用入口一起失效。
- **受限异步入库**：上传在单事务登记文档、版本、任务与 outbox 事件，由 dispatcher 投递、Celery worker 解析切分、编码并发布索引；处理中任务的过期租约由后台周期做有界恢复。
- **中文混合检索**：pgvector 精确向量检索（`BAAI/bge-small-zh-v1.5`，512 维）与中文关键词 FTS 两路，经 RRF 融合排序；两路都在 SQL 内做授权与版本过滤，权限不一致时 fail closed。
- **明确拒答**：没有授权证据时直接拒答、不调用生成模型，也不透露被过滤文档。
- **本地优先、可复现**：六服务本地 Compose；第三方镜像按 digest 固定，本地 embedding 与 tokenizer 在构建期烘入镜像、运行期离线（云生成需显式开启并联网），应用容器全部非 root 运行。

## 架构与技术栈

单仓库、Python 模块化单体，按三种进程角色运行，外加数据服务与前端网关：

| 服务 | 职责 |
| --- | --- |
| `frontend-gateway` | Vue 3 控制台静态资源，非 root nginx 同源代理 `/api`；唯一应用入口（api / inference / worker 不发布宿主端口，postgres / redis 仅回环映射） |
| `api` | FastAPI：认证与会话、KB 授权、上传受理、混合检索、证据问答 |
| `worker` | Celery：消费 outbox 任务，异步解析 / 切分 / 编码 / 索引发布 |
| `inference` | 独立进程：固定 revision 的中文 embedding（当前 512 维），不判断用户权限 |
| `postgres` | PostgreSQL 17 + pgvector：用户、文档、任务、版本、向量与关键词索引的事实来源 |
| `redis` | Celery broker 投递与登录限流，非事实来源 |

- **后端**：Python 3.12、FastAPI、Pydantic v2、SQLAlchemy 2、Alembic、Celery、Redis，依赖由 `uv.lock` 锁定。
- **前端**：Vue 3、Vite、TypeScript、Tailwind CSS v4、shadcn-vue（reka-ui）。
- **数据与模型**：PostgreSQL 17 + pgvector、Redis 7；本地推理使用 PyTorch CPU 与 Hugging Face Transformers，云生成固定端点 `https://api.deepseek.com`。
- **编排**：Docker Compose（`deploy/compose/compose.yml`）。进程职责与数据流详见[架构设计](docs/architecture.md)。

## 快速开始

### 先决条件

- **Docker Engine + Docker Compose v2**：启动六服务必需；队列与 worker 的权威运行环境是 Linux 容器或 Windows WSL2（Windows 宿主只用 `--pool=solo` 做自检）。
- **uv（管理 Python 3.12）**：执行数据库迁移与运维开户 CLI 时需要。
- **Node.js 24 + pnpm 11.22.0**：仅在本地开发前端时需要；启动六服务由镜像内构建前端，宿主无需安装。

### 启动本地六服务

1. 准备配置（`.env.example` 内是开发占位密码，生产必须替换）：

   ```powershell
   Copy-Item .env.example .env
   ```

   随后在 `.env` 中取消注释 `MIGRATION_DATABASE_URL`，并确认 `MIGRATION_DB_PASSWORD` / `API_DB_PASSWORD` / `WORKER_DB_PASSWORD` / `REDIS_PASSWORD` / `INFERENCE_TOKEN` 已设置。

2. 安装后端工具链与依赖：

   ```bash
   uv sync --frozen
   ```

3. 先启动数据服务并等待 healthy：

   ```bash
   docker compose --env-file .env -f deploy/compose/compose.yml up -d --wait postgres redis
   ```

4. 对目标库应用迁移（新 api / worker 依赖较新的迁移）：

   ```bash
   uv run --env-file .env alembic upgrade head
   ```

5. 构建并启动六服务，等待全部 healthy：

   ```bash
   docker compose --env-file .env -f deploy/compose/compose.yml up -d --build --wait
   ```

6. 创建一个登录账号（密码隐藏输入，不进入命令行历史）：

   ```bash
   uv run --env-file .env python -m rag_backend.auth.cli --username demo-admin --admin
   ```

7. 打开工作台 <http://127.0.0.1:58080> 并登录（只有网关提供应用入口；postgres / redis 另有回环宿主端口，api / inference / worker 不发布）。

> [!NOTE]
> 以上是从零走通**服务与登录**的最小路径。上传到检索、问答到引用、版本更新与删除的完整演示见[个人演示流程](docs/personal-demo.md)；部署、密钥与迁移的完整约束见[部署](docs/deployment.md)与[开发约定](docs/development.md)。

### 开启真实入库与问答

默认配置下 `INGEST_PROCESSING_ENABLED=0`、`LLM_ENABLED=0`。要真正入库和生成回答，需要在 `.env` 中开启两个开关并配置非占位密钥，然后重建重启 `api` 与 `worker`：

- 开启真实入库前，目标库必须先成功迁移到最新 head（发布事务依赖较新的迁移），并按[部署](docs/deployment.md)要求先备份。
- 开启生成需要非空 `LLM_API_KEY`，否则启动校验失败；问答会把经授权的片段发往云模型并产生费用。

## 当前状态

> 区分「已实现与验收范围」与「尚未实现」；各条目注明本机或隔离环境验收边界。

### 已实现与验收范围

- **工程底座**：锁定依赖、FastAPI 健康检查与 OpenAPI、Vue/Vite 前端骨架、SQLAlchemy 异步会话、Alembic 的 pgvector 扩展与业务表迁移；真实 PostgreSQL 17 + pgvector 迁移在本机实测通过。
- **六服务本地切片**：Compose 编排 postgres / redis / api / worker / inference / frontend-gateway，真实启动后六容器全部 healthy；网关同源代理、SPA fallback、安全头与 API 故障时的 `502` 均已核对。
- **身份与 KB 授权**：登录 / 注销 / `GET /me`、Argon2 密码、Redis 跨进程登录限流、运维开户 CLI，以及 KB 成员读取与全量替换；已在真实 PostgreSQL + Redis 与经网关联的端到端流程上验收。
- **推理**：`BAAI/bge-small-zh-v1.5` 固定 revision 烘入镜像、运行期离线加载；`kind=document` 与 `kind=query`（具名前缀契约 `bge-zh-query-v1`）编码已在真实权重与真实 HTTP 上验收。
- **检索与问答**：授权混合检索首片（`POST /api/v1/retrieval/search`）、证据问答主流程（会话、历史、追问改写、引用、拒答）以及文档新版本与逻辑删除；已在隔离 PostgreSQL / Redis 上验收，尚未部署到当前开发栈。
- **真实入库管线**（`rag_backend.ingestion.indexing_worker`）：实现了解析 / 切分 / 编码 / 索引发布与 READY 切换，但**默认关闭**；已在隔离 PostgreSQL 17 + Redis 与真离线模型整链上验收首次入库 READY。
- **证据问答生成**：支持白名单模型与思考强度，每次真实调用写 `llm_usage` 账本；但**默认关闭**，隔离 Demo 之外的真实 provider 连通性与失败 / 超时路径尚未作为例行验收。
- **2026-09-28 隔离六服务真实 Demo**：用真浏览器经真网关、真实本地 BGE、真实 worker/Celery 与真实 DeepSeek provider 走通上传 → 引用 → 追问 → 版本更新 → 删除闭环。该结果只覆盖这条演示路线，不代表生产部署或 Phase 1 整阶段退出。
- **开发评估集**：40 道自制开发题、离线结构校验与最小 runner 已实现，并已在 2026-09-28 对真实隔离链路运行一次（恰好 40 题结果与五项确定性指标，精简归档见[评估计划](docs/evaluation.md)）；开发集不是留出集，`citationSourceValidity` 不等于句子级引用支持率。

### 尚未实现

- 重排（rerank）及其降级标记（既有 `degraded_stages` 只覆盖 `unsupported_text` / `source_retry`）、相似度阈值标定、多轮检索之外的重排策略。
- 完整文档级 ACL（当前为 KB 成员授权）、DOCX / 静态网页等格式、OCR、文件 GC 与 KB 配额。
- 生产级 CI、GHCR 发布与备份恢复演练；Phase 2～4 的整体范围仍为计划，阶段划分见[技术实施顺序](docs/roadmap.md)。

## 限制

- 上传返回 `202` **只代表文件与任务已持久化**，不代表解析、索引或可检索已完成。
- 真实入库与问答生成默认关闭；关闭时分别表现为「文档停在接收状态」与「问答返回静态 503」。
- 单组织隔离，不具备多租户 SaaS 能力；跨租户安全尚未验收。
- 前端不提供流式输出、PDF 阅读器、成员管理界面和任务后台；文档与会话列表无分页。
- 删除会话只做软删、不可恢复；删除文档为逻辑删除，不物理回收共享 blob。
- 拒答与降级状态未在界面单独标注。
- 推理在 CPU 上运行，无 GPU；资源预算与性能目标尚未实测。
- 使用云模型处理真实资料前需确认片段允许外发；仓库内样本须为自制或许可明确的无敏感内容。

## 文档导航

| 文档 | 内容 |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | 系统组成、进程职责、主要数据流与边界 |
| [docs/roadmap.md](docs/roadmap.md) | 分阶段实现范围与退出条件 |
| [docs/development.md](docs/development.md) | 开发环境、目录、命令与已实测结果 |
| [docs/deployment.md](docs/deployment.md) | 服务、资源配置、迁移依赖、观测与恢复 |
| [docs/ingestion.md](docs/ingestion.md) | 解析、切分、异步任务、索引发布与恢复 |
| [docs/retrieval.md](docs/retrieval.md) | 检索、融合、重排、回答、引用与会话 |
| [docs/data-model.md](docs/data-model.md) | 实体、关系、数据库约束与保留策略 |
| [docs/api.md](docs/api.md) | 外部 API 与内部推理接口 |
| [docs/security.md](docs/security.md) | 身份、授权、文件与网页输入、模型外发 |
| [docs/evaluation.md](docs/evaluation.md) | 题集、指标、故障、性能与端到端验收 |
| [docs/personal-demo.md](docs/personal-demo.md) | 个人工作台的手工演示流程 |
| [AGENTS.md](AGENTS.md) | 仓库级开发规则 |

演示与测试语料位于 [`tests/evaluation/corpus/`](tests/evaluation/corpus/)，使用说明与写作规则见 [docs/AGENTS.md](docs/AGENTS.md)。
