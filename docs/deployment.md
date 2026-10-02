# 部署与运行设计

> 资源数字是初始预算，尚无实测。Phase 0 已锁定依赖、第三方镜像 digest、模型 revision 与 CPU wheel 来源；真实文档与进程 RSS 的验证仍未进行。一次真实计费云 LLM 探针已由用户执行成功并以 `citemind_api` 回读账本，Phase 0 工程底座退出条件据此达成（仅一次正常调用，价目与费用未测，见 [开发约定](development.md)）。

## 服务与 profile

最小部署拟有 `frontend-gateway`、`api`、`worker`、`redis`、`postgres`、`inference` 六类服务。API 与 worker 共用业务代码但使用不同入口和数据库账号；inference 独立安装和加载模型。前端生产为静态产物，网关同源代理 API；不常驻 Node 开发服务器。dispatcher 在单 API profile 的 lifespan 中按 `DISPATCHER_ENABLED` 运行（Compose 的 api 服务显式设为 `true`，宿主 Settings 默认 `false`）；多 API 实例时拆为唯一独立服务。

当前仓库只实现本地数据与服务切片：`deploy/compose/compose.yml` 用 digest 固定 `pgvector/pgvector:pg17` 与 `redis:7.4.9`，并增加由 `deploy/compose/Dockerfile` 构建的 `api`/`worker`（同镜像基础按 digest 固定、非 root 运行）、独立 `inference` 进程与 `frontend-gateway`，worker 依赖 postgres 与 redis 的 `service_healthy` 后启动，healthcheck 用 `inspect ping` 确认消费；数据服务宿主端口默认只绑定 `127.0.0.1:55432` 与 `127.0.0.1:56379`（可用 `POSTGRES_PORT`、`REDIS_PORT` 覆盖），数据放命名卷。宿主机进程用 `127.0.0.1:55432` 连接；同网络内的 api/worker 容器现已改用服务名 `postgres:5432`。Redis 启用 AOF `everysec`、`maxmemory 128mb` 与 `noeviction`，密码从环境注入，healthcheck 通过 `REDISCLI_AUTH` 读取。PostgreSQL 集群以 `citemind_migrator` 初始化并固定 `--encoding=UTF8 --locale=C.UTF-8 --data-checksums`；initdb 脚本创建 `citemind_api`、`citemind_worker` 与 `citemind_test`，并收回两个库中 PUBLIC 的数据库与 `public` schema 权限。initdb 只在空数据卷上跑一次，所有步骤成功后才写入 healthcheck 完成标记；半初始化卷不会变成 healthy。Compose 插值值可出现在 `docker compose config` 和容器环境中，Redis 密码还会常驻容器命令参数，initdb 密码会短暂出现在 psql 参数中；这些开发占位值不属于生产密钥方案。该切片已在 Docker 29.4.3、Compose 5.1.4 上完成真实启动、健康检查、角色 ACL、迁移、Redis 参数与鉴权验收，Linux Compose worker 已真实接收并执行 `evidencehub.probe`（当时旧任务名，现为 `rag_backend.probe`）（`deploy/compose/queue.yml` 的 queue-probe 按受信 marker 目录校验并按退出码判定）。六服务切片现已补齐：`api` 与 `worker` 复用 `deploy/compose/Dockerfile` 的 runtime stage（按 target 区分，非 root 10001，容器内 DSN 用 `postgres:5432`）；`api` 读写挂载 `api-documents` 命名卷到 `/var/lib/citemind/documents`，`worker` 以同一 `DOCUMENT_STORAGE_DIRECTORY` 只读（`:ro`）挂载该卷，`inference` 不挂载，worker final stage 额外安装 worker 组 `tokenizers` 并通过 Compose `additional_contexts` 的 `model_assets: service:inference` 复制四个固定 BGE tokenizer 文件，API 镜像不含 model/tokenizers/torch；worker 冷缓存构建须先构建完整 inference 镜像、构建期需网络与固定 96MB 模型，运行时不联网且 worker 不加载权重。worker 复用与 inference 相同的必填 `INFERENCE_TOKEN`（Compose inference 用 `${INFERENCE_TOKEN:?}`），但当前不设 `depends_on: inference`：inference 未启动时安全接收壳仍可启动与消费。关键词分析器固定 `jieba==0.42.1`，jieba 基础 `dict.txt` 与领域词典作为包内资源随 wheel 进入 API/worker 镜像，运行期离线、不下载词典；镜像运行时必须可写私有临时目录（POSIX 0700、Windows per-user TEMP ACL），只读根文件系统部署须为分析器提供安全 tmpfs，否则构造期静态 `KeywordAnalyzerError` fail closed。`jieba` 依赖只有源包 sdist（约 19MB），构建期需联网/缓存，运行期不需要。`inference` 用 `inference/` 独立项目构建（非 root 10002，内部 9000 不发布，构建期把固定 revision 的 BAAI/bge-small-zh-v1.5 配置、tokenizer 与 safetensors 校验证后烘入镜像，运行期以 `HF_HUB_OFFLINE` 与 `TRANSFORMERS_OFFLINE` 离线加载（加载前先按包内钉死摘要与 `model-manifest.json` 核对六个产物，`AutoModel` 显式 safetensors 且禁用 remote code）；构建期另把模型许可与来源说明 `inference/third_party/bge-small-zh-v1.5-LICENSE.txt` COPY 到 `/models/bge-small-zh-v1.5-LICENSE.txt`，它不进入模型目录，`/models/bge-small-zh-v1.5/` 仍恰为 6 个可信产物），`frontend-gateway` 用 `deploy/compose/frontend.Dockerfile` 以 Node 24 构建静态产物、由非 root `nginx-unprivileged`（101）提供 8080，是唯一发布回环宿主端口（`GATEWAY_PORT`，默认 58080）的应用入口。同一次 `up -d --build --wait` 实测六个容器均 healthy，网关同源代理 `/api/v1/health`、SPA fallback、`/healthz`、assets immutable、index no-store 与安全头均通过，停掉 api 后 `/api/v1/health` 返回 502 且 `/healthz` 仍为 200。本切片 inference 已能离线编码 `kind=document` 与 `kind=query` 文本（query 前缀契约 `bge-zh-query-v1` 已由隔离模型 tester 在真实权重与真实 HTTP 上验收），rerank 已实现但默认关闭且未做真实模型验收；检索与问答业务路由已实现（生成默认关闭）；worker 写入事务已实现但**默认关闭**，且已由独立 tester 在隔离 PG17+Redis（pipeline 15 passed + 权限迁移 3 passed）与 Linux prefork concurrency 1 真离线模型整链上端到端验收 PG `0007` READY（见 [开发约定](development.md)），按最近记录观察 dev 当时未部署该代码；dispatcher 与 worker 接收壳已在隔离 PostgreSQL/Redis/Celery 上验收（Compose 的 api 服务设 `DISPATCHER_ENABLED=true` 开启，宿主 Settings 默认关闭；物理 Redis 停启与 Windows solo worker kill 后补偿由仓库外隔离手工探针实测，不计入 pytest 自动用例，Linux prefork 业务故障未验收），**默认关闭时**合格 job 的接收壳只写 `HANDLER_NOT_READY` 标记、job 仍为 `QUEUED`（显式开启真实处理时同一任务进入真实入库管线），无接收标记但已有非 NULL 诊断码的旧 job 保持原样，只有无接收标记且 `error_code IS NULL` 的旧 job 仅置 `FAILED`+`LEGACY_JOB_UNSUPPORTED`（已由独立 tester 在隔离 PG17+Redis 与 Linux prefork worker 镜像上最终验收）；新 worker 读取 `ingest_job.profile_id` 依赖迁移 `20260925_0006`，按 [开发约定](development.md) 最近记录的观察，dev 库当时仍停在 `20260923_0005`、六服务未部署该代码（当前真实部署状态须以实际操作核对）；`llm_usage` 用量账本由 `20260923_0004` 落地，一次性真实探针 `rag_backend.llm_probe` 已提供并已由一次真实成功调用落账回读（价目与费用为 NULL）。

| profile | 目标环境 | 功能与限制 |
| --- | --- | --- |
| `local-mvp` | 约 4 核/16 GB RAM，预留 20～30 GB 磁盘，无 GPU | 全部 MVP 服务，导入并发 1、问答并发 1～3；Windows 用 WSL2/Linux 容器 |
| `vps-lite` | 2 vCPU/4 GB RAM/40 GB SSD | 同 MVP；关闭本地 reranker 和大型观测栈；内存、并发与延迟必须实测 |
| `local-full` | 4～8 核/16 GB RAM，容器约 8～10 GB 可用 | 增加本地 bge-reranker-base 与完整格式；重排 top-10、并发 1，峰值内存和延迟单独验收 |

vps-lite 的初始内存预算：API 0.15～0.3 GB、worker 0.3～0.6 GB、Redis 0.1～0.2 GB、数据库 0.3～0.6 GB、inference 0.5～1.0 GB、前端/网关 0.03～0.1 GB，给操作系统至少约 0.8 GB。它们是容量假设，不是最小运行要求或已测峰值。紧张时先限制并发、暂停导入或跳过重排，不靠无限 swap 隐藏问题。

`local-full` 启用可降级重排的步骤（未接入 Compose profile，需手动操作；本轮未执行、未验收）：先以 `docker build --build-arg RERANK_MODEL_ENABLED=1` 构建 inference 镜像，使构建期 `prepare_model.py --model rerank` 下载 `BAAI/bge-reranker-base` 并生成 `rerank-model-manifest.json`；再在 api 与 inference 两个服务同时设 `RERANK_ENABLED=1`（api 另可调 `RERANK_TIMEOUT_SECONDS`）。权重未烘入时 inference 开启会在启动阶段 fail fast。`vps-lite` 保持 `RERANK_ENABLED=0`。该启用路径未在真实构建或运行中验收。

初始进程数：API Uvicorn worker 1，Celery concurrency 1，inference Uvicorn worker 1。推理进程只加载一份权重，限制 CPU 线程、批量和信号量；导入与问答竞争时优先问答。Celery 完整运行及队列验收在 Linux/WSL2 容器。模型文件在构建期按固定 revision 下载、校验并烘入 inference 镜像，运行期不联网也不挂宿主模型卷，且每次加载前都按包内钉死摘要与 `model-manifest.json` 重新校验实际字节；首次下载发生在构建阶段，离线启动已实测，云 API 不可用的行为仍待记录。问答生成的输入预算另需 api 镜像内的 DeepSeek V4.1 tokenizer 产物（`deepseek-ai/DeepSeek-V4-Flash-Vision-Exp` 固定 revision `6821d6ad3681a4b137b066b76094fa82ebd0a380` 的 `tokenizer.json`，按字节大小与 SHA-256 校验后离线加载）；该产物已由 api 镜像在构建期用 `backend/scripts/prepare_generation_tokenizer.py` 按固定 revision 下载，并以 Hub commit 与钉死字节大小/SHA-256（非 LFS 文件另核 Git blob SHA-1）双重校验后烘入 `/models/deepseek-v41/tokenizer.json`，运行期不联网也不读凭据，计数器不会退化为字符/字节估算（输入/输出预算由 `LLM_INPUT_TOKEN_BUDGET`/`LLM_OUTPUT_TOKEN_BUDGET` 配置，Compose 的 api 服务已透传；命令与实测见 [开发约定](development.md)）。

## 配置、观测与恢复

配置集中用 pydantic-settings 读取，进程环境优先于根 `.env`；缺失密钥、无效模型维度、互相冲突的超时与预算应启动失败。密钥使用运行环境注入（容器 env、CI secret 等，不要求只写在 `.env`），不入库；内部服务和数据库不对公网开放。HTTPX 客户端在 API lifespan 内创建/关闭，设连接池、连接/读取超时与整体请求预算。数据库迁移由 Alembic 执行，发布前做备份，验证恢复后索引、任务与文件引用一致。一次性云 LLM 探针不随 api/worker 容器启动：它在仓库根目录手动执行单行 `uv run python -m rag_backend.llm_probe`，从进程环境或根 `.env` 读取 `ALLOW_LLM_PROBE=1` 与独立 `LLM_API_KEY`（pydantic-settings 进程环境优先，进程环境值同样会用于向固定 `https://api.deepseek.com` 请求，且绝不要求回显或上传密钥；不复用开发期 Node Jev 的 `AI_GATEWAY_API_KEY`）；缺失任一项时快速失败、不联网、不写 `llm_usage`。探针在触网前对 `DATABASE_URL` 指向的目标库做只读预检：角色必须是 `citemind_api`，`public.llm_usage` 存在且必要列可读，SELECT+INSERT 可用；未迁移、角色不符、权限不足或库不可达均以退出码 6 失败且零网络。运行前需先对目标库执行在线迁移（`uv run --env-file .env alembic upgrade head`，会改写目标库；生产须先备份、使用独立库与 migrator DSN）。云调用与 PostgreSQL 提交无法原子化，写入失败返回非零且可能没有账本行。endpoint 固定在 `https://api.deepseek.com`，模型名默认 `deepseek-flash` 且可配置，网络重试为 0，prompt、响应正文与密钥不落库、不入日志。

CiteMind 必须使用独占的 PostgreSQL database，不能与其他应用共享。迁移账号必须拥有 `CREATE EXTENSION` 权限，因为首迁移负责创建 `vector` 扩展；降级会删除本应用管理的 `vector` 扩展，不能在共享数据库上执行。`vector.control` 没有 `trusted = true`，所以安装与升级扩展需要超级用户；本地切片因此把高权限账号 `citemind_migrator` 只交给迁移进程，运行时用非特权的 `citemind_api`、`citemind_worker`。角色与 ACL 由 initdb 脚本建立，而它只在空数据卷上执行，需要重建时先 `docker compose --env-file .env -f deploy/compose/compose.yml down -v`（会删除本地数据库数据），再 `up -d --wait` 让 initdb 重跑。

在线迁移只从 Alembic 配置项 `sqlalchemy.url` 或 `MIGRATION_DATABASE_URL` 读取 DSN，缺失时直接以非零状态失败，不回退到 API 运行用的 `DATABASE_URL`：迁移账号的高权限凭据不进入 API 与 worker 的运行配置。离线 `--sql` 只生成 SQL，不读取该 DSN。当前运行角色没有表权限；业务表落地时，每个 Alembic migration 必须按 API 与 worker 的实际职责显式 `GRANT`，不能依赖 PUBLIC 或笼统的默认表权限。

JSON 结构化日志记录 requestId、queryRunId、ingestJobId、阶段耗时、候选数、版本、模型修订和失败类型，默认不记正文。生产诊断不要打印 `Settings.model_dump()`/`model_dump_json()`，它会输出未脱敏的 `database_url`/`redis_url` 等连接串；需要排查配置时只核对存在性与一致性，不回显。`/metrics` 记录检索、模型、队列、用量和资源指标；低配 profile 不常驻完整 Grafana 栈。模型超时、broker 不可用、磁盘或 Redis 内存压力应有显式降级/拒绝新导入状态。Redis 可采用 `noeviction` 和 AOF，但仍由 PostgreSQL outbox 与 job 承担恢复事实。

CI 已在 `.github/workflows/ci.yml` 落地静态与单元门禁：`push`（仅 main 分支）、`pull_request` 与 `workflow_dispatch` 触发，只申请 `contents: read`，按 workflow+ref 并发取消；`changes` 判断 job 用 `git diff` 判断 inference 相关改动（PR 用 `origin/<base>...HEAD` merge-base 三点 diff，push 用 `before..sha`，零 `before` SHA 保守视为全量，`--no-renames` 保留改名两侧路径），命中 `inference/` 或 `.github/workflows/ci.yml` 才运行 inference job，手动触发恒运行，backend/frontend 恒跑；`changes` 判断 job 之外，backend/inference/frontend 三个 ubuntu-24.04 检查 job 分别覆盖 backend（Python 3.12 + uv 0.12.4，`uv lock --check`、`uv sync --frozen`、Ruff、mypy、非集成 pytest）、inference（`working-directory: inference` 与独立锁/缓存，`uv lock --check`、`uv sync --frozen --group dev`、`ruff`、`mypy`、`pytest -m "not model"`，并设 `HF_HUB_OFFLINE=1`/`TRANSFORMERS_OFFLINE=1`/`RUN_MODEL_TESTS=0`）与 frontend（Node 24 + pnpm 11.22.0，`pnpm install --frozen-lockfile`、`pnpm test:jev`、前端测试与构建；其中 `pnpm --dir frontend test` 会经 `uv run python -c` 调用后端模块，故该 job 另装 uv 0.12.4 + Python 3.12、先显式 `uv sync --frozen`，并在测试 step 设 `UV_NO_SYNC=1` 禁止嵌套 `uv run` 隐式安装）。该 workflow 不注入 secrets、不读 `.env`、不连数据库/Redis/broker/真实模型、不构建镜像或部署；backend 单测在 runner 存在 `docker` 时会调用 `docker compose config --format json` 做静态渲染，不启动容器（本仓不声称 CI 完全不执行 Docker CLI）；它**尚未在 GitHub 上真实运行**，仓库内只做过 YAML 与 Actions 引用的静态解析。`pip-audit`、SBOM 生成、为 API/worker/inference 构建最小依赖镜像并推送 GHCR 仍是后续计划；手动发布与备份恢复需独立运行验收。六服务 Compose 本地切片已建成并完成一次真实启动与网关验收，确切单行命令与静态检查见 [开发约定](development.md)；分角色生产镜像与 GHCR 发布仍未建成；本片备份恢复入口已实现但尚未真实执行验收。

### 数据库与原文件备份/隔离恢复（已实现入口，未真实执行验收）

入口为 `uv run python -m rag_backend.operations.backup`，只使用标准库与既有 Compose 中运行中的 `postgres` 容器内的 `pg_dump`/`pg_restore`/`psql`（`docker exec`，走容器内本地 socket，不传 DSN/password），并可从容器镜像复用 `tar` 导出/导入只装原文件的 `api-documents` 命名卷；不依赖宿主 `pg` 客户端，也不新增依赖或第二套实现。

备份用 `pg_dump --format=custom` 生成二进制 `database.dump`，只复制内容寻址原文件，写入同目录自建临时目录后原子改名；目标目录已存在时拒绝覆盖，失败只清理本次临时资源；`manifest.json` 记录 `database.dump` 与每个 blob 的 SHA-256/大小、`alembic_version`、关键表计数与 `document_version.file_ref` 关联事实，不记录 DSN/password/cookie/密钥。

数据库与文件是顺序分拷贝、不是原子快照：执行备份前必须暂停 API/worker 写入并用 `--quiesced` 声明，拷贝后按 `document_version.file_ref` 与快照 blob 交叉核对，缺任何一个即整体失败。命令默认 dry-run，不连接任何服务；真实操作必须 `--execute`，并用与写入对象完全一致的 `--confirm` 二次确认。

恢复只接受与源 project 不同、库名严格以 `_test` 结尾的隔离目标（拒绝 `test`/`mytest`/`test_db` 这类宽松匹配），要求目标库已由隔离项目 initdb 建好且不含业务表、目标 `api-documents` 卷已存在且为空，否则拒绝；不同 project 中同名 `_test` 库是允许的隔离演练场景，同 project 内恢复一律拒绝。实现不使用 `--clean`/DROP，不覆盖开发/生产库，不重置用户数据。恢复后由固定脚本把空 target 卷的卷根与 KB/blob 目录归运行时用户 `10001:10001` 并给 owner 写权限，使 API 能继续写入新 blob；该步骤只对本次显式恢复的空 target 卷执行，不改源卷，真实 owner/chmod 行为仍需 Docker 真验。恢复角色与权限沿 init/migrator/api/worker 约定（`pg_restore` 以 `citemind_migrator` 运行并 `--no-owner`，保留 GRANT，兼容 pgvector），不备份外部角色密码。

恢复半途失败时不要自动 drop/clean 或对任何共享/开发数据卷执行 `down -v`：只有隔离 target 项目可能留下未完成的库或 partial 卷；先手工核对 target 项目（示例隔离名 `myrag-restore`），确认确属本次隔离演练后，再用本文已建成的 Docker Compose 命令重建自己的空 target，然后重试。`down -v` 会删除该 project 的数据卷，仅在明确指定隔离 project 且有单独确认后使用，不得用于 dev/生产。

只读校验入口 `verify` 先离线重算清单 SHA-256 并校验内容寻址不变量（拒绝符号链接与非普通文件），有目标时还核对目标库的 `alembic_version`/`document.active_version_id`/`ingest_job.generation_id` 引用、READY 版本与生成及 chunk 计数，并把目标 `api-documents` 卷只读导出后逐文件校验实际 SHA-256、确认目标库引用的文件都在目标卷中且与快照一致；`--expected-head` 无目标时核对快照记录的 `alembicRevision`，有目标时还核对目标库 revision。它只证明 schema 与引用级一致性，**不证明**全表字节完全同一、向量数值等价、pgvector ANN 索引重建或 WAL/跨文件原子性。

本轮未真实执行 `pg_dump`/`pg_restore`/卷导出：容器内本地 socket 认证、`pg_restore --single-transaction` 行为、跨 project 具名卷命名与大 dump/大卷流式内存都仍需真实验收。以下为单行命令示例，真实执行前请先备份好并暂停写入；示例 `verify` 不传 `--expected-head`，若要核对具体版本请填 `manifest.json` 里 `alembicRevision` 的实际值。

```
uv run python -m rag_backend.operations.backup backup --project citemind --database citemind --output D:/backups/2026-09-30
uv run python -m rag_backend.operations.backup backup --project citemind --database citemind --output D:/backups/2026-09-30 --execute --confirm D:/backups/2026-09-30 --quiesced
uv run python -m rag_backend.operations.backup verify --snapshot D:/backups/2026-09-30 --execute
uv run python -m rag_backend.operations.backup restore --snapshot D:/backups/2026-09-30 --source-project citemind --target-project myrag-restore --target-database citemind_test --execute --confirm citemind_test
uv run python -m rag_backend.operations.backup verify --snapshot D:/backups/2026-09-30 --target-project myrag-restore --target-database citemind_test --execute
```

## 部署顺序与迁移依赖

新 worker 接收路径会读取 `ingest_job.profile_id`（以及 `document_version.parser_version` 与 `error_code`），其中 `profile_id` 由迁移 `20260925_0006` 增加；因此新 worker 与依赖同一 schema 的新 API 只能在目标库**成功升级到 `20260925_0006` 之后**部署。本文不声称任何环境已部署；按 [开发约定](development.md) 最近记录的观察，dev 库当时仍停在 `20260923_0005`、六服务未部署该代码（当前真实部署状态须以实际操作核对）。本片最终验收覆盖隔离 PG17+Redis（pipeline 15 passed + 权限迁移 3 passed）与 Linux prefork 单 worker 真离线模型整链（PG `0007` READY）；完整身份工厂已由默认关闭的真实入库管线接线；物理 Redis 停启、父 worker kill 后子进程回收与租约恢复、自然 3600 秒 visibility 重投、多 worker、人工恢复 SQL 与 p95 未测，GC 未实现。

正确顺序：先备份目标库并确认可用恢复点，再由授权运维以独立的 migrator DSN 通过 Alembic 在线迁移把目标库升到 `20260925_0006` 并核对结果，迁移成功后才上线新 API/worker。本文不提供直接对真实 `.env` 执行迁移的单行命令，真实迁移由授权运维在确认目标库后单独执行，不在仓库内代跑。

不允许把新 worker 先于迁移上线：旧 schema（`20260923_0005`）没有 `ingest_job.profile_id` 列，新接收壳的行锁 `SELECT` 会以未定义列的 `ProgrammingError` 失败；而当前 Celery 配置 `task_acks_on_failure_or_timeout=True`（失败任务也确认、不无限重投），该消息会被确认，dispatcher 补偿随后按 `MAX_DELIVERY_ATTEMPTS` 重试并最终写 `DELIVERY_UNCONFIRMED`，因此这种顺序不会稍后自愈，只会耗尽补偿并留下诊断错误码。该顺序不得记录为允许操作。

### 真实入库管线（默认关闭）的迁移依赖

真实入库管线 `rag_backend.ingestion.indexing_worker` 额外需要迁移 `20260925_0007`：它只给
`citemind_worker` 增加 `knowledge_base(active_index_profile_id, kb_revision)` 两列 UPDATE
（**无全表 UPDATE**，也不新增结构/索引/其它授权）。因此在 `20260925_0006` 上开启
`INGEST_PROCESSING_ENABLED=1` 后，发布事务会因缺少这两列 UPDATE 失败；`20260923_0005`
则连 `ingest_job.profile_id` 都不存在。真实处理**默认关闭**：`INGEST_PROCESSING_ENABLED`
缺省为 `0`，行为与既有安全接收壳完全一致，不迁移也能保持现状。显式开启时启动校验要求
`INFERENCE_TOKEN` 存在，且必须先备份目标库、由授权运维把目标库成功迁移到 `20260925_0007`
并核对，独立 tester 已在隔离 PG17+Redis 与真离线模型整链上验收 READY/发布/权限端到端；目标库完成备份与迁移后才上线开启真实处理的 worker。本文不声称任何环境已部署；按 [开发约定](development.md) 最近记录的观察，dev 库当时仍停在 `20260923_0005`、六服务未部署该代码（当前真实部署状态须以实际操作核对）。

解析硬时限由受控 `subprocess` 入口实现（`python -m rag_backend.ingestion.parse_subprocess`），
父进程 `communicate` 超时后 `kill`+`wait` 真实终止并回收；这是设计上的进程隔离，不依赖
`multiprocessing`。已归档的超时终止证据来自隔离 Linux 容器：对受控子进程强制超时后
`kill`+`wait` 连续 5 次均回收、`/proc` 无残留；单元层用假 `Popen` 断言 kill 调用，与真实
子进程解析测试分开。未实际等待生产 60 秒时限，也未测父 worker 被 kill 后对子进程的清理；
Linux prefork 下（含该清理）尚未验收，不声称跨平台行为一致。
