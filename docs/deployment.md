# 部署与运行设计

> 资源数字是初始预算，尚无实测。Phase 0 应锁定依赖、镜像 digest、模型 revision 和 CPU wheel 来源，再以真实文档和进程 RSS 验证。

## 服务与 profile

最小部署拟有 `frontend-gateway`、`api`、`worker`、`redis`、`postgres`、`inference` 六类服务。API 与 worker 共用业务代码但使用不同入口和数据库账号；inference 独立安装和加载模型。前端生产为静态产物，网关同源代理 API；不常驻 Node 开发服务器。dispatcher 在单 API profile 的 lifespan 中运行；多 API 实例时拆为唯一独立服务。

当前仓库只实现本地数据服务切片：`deploy/compose/compose.yml` 用 digest 固定 `pgvector/pgvector:pg17` 与 `redis:7.4.9`，只启动 `postgres` 与 `redis`，宿主端口默认只绑定 `127.0.0.1:55432` 与 `127.0.0.1:56379`（可用 `CITEMIND_POSTGRES_PORT`、`CITEMIND_REDIS_PORT` 覆盖），数据放命名卷。宿主机进程用 `127.0.0.1:55432` 连接；将来同网络内的 api/worker 容器改用服务名 `postgres:5432`。Redis 启用 AOF `everysec`、`maxmemory 128mb` 与 `noeviction`，密码从环境注入，healthcheck 通过 `REDISCLI_AUTH` 读取。PostgreSQL 集群以 `citemind_migrator` 初始化并固定 `--encoding=UTF8 --locale=C.UTF-8 --data-checksums`；initdb 脚本创建 `citemind_api`、`citemind_worker` 与 `citemind_test`，并收回两个库中 PUBLIC 的数据库与 `public` schema 权限。initdb 只在空数据卷上跑一次，所有步骤成功后才写入 healthcheck 完成标记；半初始化卷不会变成 healthy。Compose 插值值可出现在 `docker compose config` 和容器环境中，Redis 密码还会常驻容器命令参数，initdb 密码会短暂出现在 psql 参数中；这些开发占位值不属于生产密钥方案。该切片已在 Docker 29.4.3、Compose 5.1.4 上完成真实启动、健康检查、角色 ACL、迁移和 Redis 参数验收。api、worker、inference、frontend-gateway 容器仍未实现，worker 未运行，所以这不是六服务 Compose。

| profile | 目标环境 | 功能与限制 |
| --- | --- | --- |
| `local-mvp` | 约 4 核/16 GB RAM，预留 20～30 GB 磁盘，无 GPU | 全部 MVP 服务，导入并发 1、问答并发 1～3；Windows 用 WSL2/Linux 容器 |
| `vps-lite` | 2 vCPU/4 GB RAM/40 GB SSD | 同 MVP；关闭本地 reranker 和大型观测栈；内存、并发与延迟必须实测 |
| `local-full` | 4～8 核/16 GB RAM，容器约 8～10 GB 可用 | 增加本地 bge-reranker-base 与完整格式；重排 top-10、并发 1，峰值内存和延迟单独验收 |

vps-lite 的初始内存预算：API 0.15～0.3 GB、worker 0.3～0.6 GB、Redis 0.1～0.2 GB、数据库 0.3～0.6 GB、inference 0.5～1.0 GB、前端/网关 0.03～0.1 GB，给操作系统至少约 0.8 GB。它们是容量假设，不是最小运行要求或已测峰值。紧张时先限制并发、暂停导入或跳过重排，不靠无限 swap 隐藏问题。

初始进程数：API Uvicorn worker 1，Celery concurrency 1，inference Uvicorn worker 1。推理进程只加载一份权重，限制 CPU 线程、批量和信号量；导入与问答竞争时优先问答。Celery 完整运行及队列验收在 Linux/WSL2 容器。模型文件提前缓存并固定 revision；首次下载、离线启动与云 API 不可用的行为应分别记录。

## 配置、观测与恢复

配置集中用 pydantic-settings 读取，缺失密钥、无效模型维度、互相冲突的超时与预算应启动失败。密钥使用运行环境注入，不入库；内部服务和数据库不对公网开放。HTTPX 客户端在 API lifespan 内创建/关闭，设连接池、连接/读取超时与整体请求预算。数据库迁移由 Alembic 执行，发布前做备份，验证恢复后索引、任务与文件引用一致。

CiteMind 必须使用独占的 PostgreSQL database，不能与其他应用共享。迁移账号必须拥有 `CREATE EXTENSION` 权限，因为首迁移负责创建 `vector` 扩展；降级会删除本应用管理的 `vector` 扩展，不能在共享数据库上执行。`vector.control` 没有 `trusted = true`，所以安装与升级扩展需要超级用户；本地切片因此把高权限账号 `citemind_migrator` 只交给迁移进程，运行时用非特权的 `citemind_api`、`citemind_worker`。角色与 ACL 由 initdb 脚本建立，而它只在空数据卷上执行，需要重建时先 `docker compose --env-file .env -f deploy/compose/compose.yml down -v`（会删除本地数据库数据），再 `up -d --wait` 让 initdb 重跑。

在线迁移只从 Alembic 配置项 `sqlalchemy.url` 或 `CITEMIND_MIGRATION_DATABASE_URL` 读取 DSN，缺失时直接以非零状态失败，不回退到 API 运行用的 `CITEMIND_DATABASE_URL`：迁移账号的高权限凭据不进入 API 与 worker 的运行配置。离线 `--sql` 只生成 SQL，不读取该 DSN。当前运行角色没有表权限；业务表落地时，每个 Alembic migration 必须按 API 与 worker 的实际职责显式 `GRANT`，不能依赖 PUBLIC 或笼统的默认表权限。

JSON 结构化日志记录 requestId、queryRunId、ingestJobId、阶段耗时、候选数、版本、模型修订和失败类型，默认不记正文。`/metrics` 记录检索、模型、队列、用量和资源指标；低配 profile 不常驻完整 Grafana 栈。模型超时、broker 不可用、磁盘或 Redis 内存压力应有显式降级/拒绝新导入状态。Redis 可采用 `noeviction` 和 AOF，但仍由 PostgreSQL outbox 与 job 承担恢复事实。

CI 计划用冻结的 uv 锁文件运行 Ruff、mypy、pytest、前端检查、`pip-audit` 与 SBOM 生成；为 API、worker、inference 分别构建最小依赖镜像，计划推送 GHCR。手动发布与备份恢复需独立运行验收。当前只有两项数据服务的本地切片，确切单行命令与静态检查见 [开发约定](development.md)；六服务 Compose、分角色镜像与 GHCR 发布仍未建成。
