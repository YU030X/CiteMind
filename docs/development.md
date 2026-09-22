# 开发约定

> 当前仓库已建立 Phase 0 工程骨架：FastAPI 健康检查与 OpenAPI、Vue 3/Vite/TypeScript 页面、SQLAlchemy 异步数据库会话、Alembic 的 pgvector 扩展迁移与两片业务表迁移、Jev 开发期判断脚本及对应聚焦检查已经可运行，Docker 与真实 PostgreSQL/pgvector 迁移也已完成实测。本地数据与服务切片已建立：`deploy/compose/compose.yml` 除 PostgreSQL + pgvector 与 Redis 外增加了独立 Celery worker；其静态插值、真实容器启动、initdb 角色/ACL、迁移、Redis 配置（`PING` 与参数）与无鉴权 `PING` 的 `NOAUTH` 分支均已实测。worker 只注册无业务副作用的诊断任务 `evidencehub.probe`，使用带认证的 Redis broker 且不配置 result backend；probe 默认只回显，只有显式设置受信目录 `CITEMIND_PROBE_MARKER_DIRECTORY` 时才原子写入诊断 marker，作为执行的确定性证据（`deploy/compose/queue.yml` 的 queue-probe 验收即按退出码判定）。它仍不是可用的业务系统：六服务 Compose 已补齐（api、inference、frontend-gateway 容器已建立并完成一次真实启动验收），但推理进程目前只支持 `kind=document` 编码（`query` 与 rerank 未实现），认证、入库、检索与问答仍未实现。两片业务表已落地并在专用测试库上通过真实迁移验收：第一片（`index_profile`、`knowledge_base`、`document`、`document_version`、`ingest_job`、`outbox_event`）由 `20260922_0002` 创建，第二片（`index_generation`、`chunk`、`chunk_embedding` 与 `chunk_embedding VECTOR(512)`）由 `20260922_0003` 创建，但 worker 写入事务、认证授权、检索与问答仍未实现，因此尚未达到 Phase 0 退出条件。

## 验收实测结果

本节只记录已真实运行并核对过的结果，不是目标值；命令与范围见下文验证规则。

| 项 | 实测值 |
| --- | --- |
| 环境 | Windows 11 + Docker 29.4.3；镜像 `pgvector/pgvector:pg17`（digest `sha256:cf134a767f474095eeba57e0117be8e568e011a63f33fbf252f14c9b760f8e6f`） |
| 服务端 | PostgreSQL 17.11 (Debian 17.11-1.pgdg12+2)，pgvector 0.8.6 |
| upgrade | 在线 `uv run alembic upgrade head` 当时应用 `20260921_0001`，`alembic_version.version_num` 与该 revision 一致，`pg_extension` 中出现 `vector` 0.8.6 |
| 512 维字面量 | `SELECT vector_dims(CAST('[0,...0]' AS vector))`（512 个元素）返回 512 |
| downgrade | `uv run alembic downgrade base` 后 `pg_extension` 无 `vector` 行，且 `CAST(... AS vector)` 报 `type "vector" does not exist` |
| Compose 数据与服务 | Docker Compose 5.1.4 的隔离项目中，postgres、redis 与 worker 均进入 healthy；数据服务端口只绑定 `127.0.0.1`，验收后用 `down` 回收容器与网络 |
| 角色与 ACL | `uv run pytest -m integration -q` 在 Compose 测试库上为 9 passed：迁移 upgrade/downgrade 以及 migrator/api/worker 角色、主库/测试库 PUBLIC ACL 和运行角色权限均通过 |
| 第一切片业务迁移 | 在 PostgreSQL 17.11 (Debian 17.11-1.pgdg12+2) + pgvector 0.8.6 的独立专用测试库（`127.0.0.1:55433`）上，`uv run pytest tests/integration/test_core_migration.py -q` 为 7 passed，随后 `uv run pytest -m integration -q` 为 16 passed（0 failed/0 skipped）。核对 6 张业务表、13 个具名 CHECK、独立索引、无 sequence、PUBLIC 无授权、api/worker 授权差异与 `document.active_version_id` 的 `SET NULL`；finally 降级回 base 后 `citemind_test` 仅剩空的 `alembic_version`，无业务表、无 sequence、无 `vector` 扩展 |
| 第二切片业务迁移 | 在同一专用测试库上，`uv run pytest tests/integration/test_second_slice_migration.py -q` 为 9 passed，随后 `uv run pytest -m integration -q` 为 25 passed（0 failed/0 skipped）。从 `20260922_0002` 升级到 `20260922_0003`，核对 9 张表、`ingest_job.generation_id` 外键、具名约束、GIN 与部分唯一索引、无 sequence/ENUM/ANN、PUBLIC 收权、api/worker 精确授权、512 维可插入与非 512 维被拒、同 chunk 第二条向量被主键拒绝、同 version/profile 第二个 READY 被部分唯一索引拒绝且非 READY 可并存；finally 降级回 `20260922_0002` 后第一切片 6 表仍完整、新表/新列/授权无残留，再降回 base |
| Redis 鉴权 | 容器内移除 `REDISCLI_AUTH` 后执行 `redis-cli -e ping`，stderr 输出 `NOAUTH Authentication required.` 且退出码为 1，确认 `--requirepass` 生效 |
| 独立 worker（Linux Compose） | `up -d --wait` 后 postgres、redis、worker 三个容器均 healthy；worker 以 prefork、concurrency 1 启动，日志显示 `results: disabled://`、`[tasks] . evidencehub.probe` 与 `celery@<container> ready.` |
| worker 接收并执行 probe | worker 配置 `CITEMIND_PROBE_MARKER_DIRECTORY` 并挂载共享 marker 卷后，从宿主机派发 `evidencehub.probe` 会在卷内生成与本次 task id 同名的 `<taskId>.json`，可解析出 `taskId`/`hostname`/`pid`/`payload`；不写业务表、不改 job 状态 |
| Linux Compose 确定性验收 | `docker compose --env-file .env -f deploy/compose/compose.yml -f deploy/compose/queue.yml up --build --abort-on-container-exit --exit-code-from queue-probe queue-probe` 退出码为 0：只显式启动 queue-probe 与自动依赖（redis/worker/postgres），queue-probe 派发唯一 probe、等待并校验 marker 后打印 `queue-probe 成功: taskId=... hostname=celery@<container> pid=...`；未引入 result backend |
| broker 集成测试 | `uv run --env-file .env pytest tests/integration/test_worker_broker.py -q` 为 2 passed：真实 Redis db 15 上独立 worker 子进程在超时内响应 `inspect ping`、注册 probe，并按本次 `async_result.id` 生成通过校验的 marker（日志仅用于诊断）；同一 Redis 无密码连接被拒 |
| 本切片全量集成 | `uv run --env-file .env pytest -m integration -q` 为 27 passed（25 个迁移/角色 + 2 个 broker）、0 failed/0 skipped |
| 本切片静态检查 | `uv run ruff check backend/src tests`、`uv run mypy`、`uv run pytest -m "not integration"` 与 `docker compose --env-file .env.example -f deploy/compose/compose.yml config --quiet` 均通过 |
| 六服务 Compose 启动 | `docker compose --env-file .env -f deploy/compose/compose.yml up -d --build --wait` 退出码 0：postgres、redis、api、inference、worker、frontend-gateway 六个容器全部 `running (healthy)`；`ps` 显示只有 gateway（`127.0.0.1:58080->8080`）、postgres（`127.0.0.1:55432->5432`）、redis（`127.0.0.1:56379->6379`）发布宿主端口，api/inference/worker 无 published ports |
| 网关同源入口 | `curl.exe --noproxy "*" -fsS http://127.0.0.1:58080/api/v1/health` 返回 `{"status":"ok","service":"api","environment":"development"}`；`/` 与 `/kb/1/documents` 均 200（SPA fallback），`/healthz` 返回 `ok`；assets 返回 `Cache-Control: public, max-age=31536000, immutable`，index 返回 `no-store`，CSP/nosniff/X-Frame/Referrer 安全头齐备；在网关运行镜像内 `nginx -t` successful；停止 api 后 `/api/v1/health` 为 502 而 `/healthz` 仍 200，重启 api 后恢复 200 |
| 容器身份 | `exec -T api id -u`=10001、`inference id -u`=10002、`frontend-gateway id -u`=101；只有 gateway 发布回环宿主端口，api/inference/worker 无 published ports |
| inference 模型产物与离线真实编码（上一轮镜像实测，保留为历史） | `docker run --rm --network none --entrypoint python citemind-inference:latest scripts/prepare_model.py --verify` 输出 `模型校验通过：BAAI/bge-small-zh-v1.5@7999e1d3359715c523056ef9478215996d62a620，6 个文件`；产物共 96,377,581 字节，`model.safetensors` SHA-256 `354763b9b1357bc9c44f62c6be2276321081ed2567773608c0d0785b61d5a026` 与该 revision 的 Hub LFS 摘要一致；无网络容器内以 uid 10002 加载后 `/ready` 为 200 且 `embedding.ready=true`、dimension=512、modelRevision 等于冻结 revision，`/internal/embed` 对 `kind=document` 返回 512 维有限向量（float32 舍入内 L2 范数为 1、同一输入两次逐元素相同），`kind=query` 为 422、无 token 为 401，rerank.ready=false；`torch 2.14.0+cpu` 且 `torch.version.cuda` 为 null、无 nvidia-* 包，`HF_HUB_OFFLINE`/`TRANSFORMERS_OFFLINE`/`HF_HUB_DISABLE_TELEMETRY` 均为 1 |
| inference 镜像与冷启动（上一轮镜像历史，非最终修复验收） | `docker build --progress=plain -t citemind-inference:embedding-check inference` 成功；`docker history` 显示模型层 97.8MB、依赖层 1.22GB（torch CPU），`docker images` 显示 1.96GB，容器内 `du -sh /app/.venv /models` 为 1.2G 与 92M；冷启动到 `/ready` 200 两次独立运行分别为 5.2s 与 5.8s，Compose healthcheck 保持 `/health` 与 `start_period: 20s` |
| inference Compose 运行（上一轮镜像历史，非最终修复验收） | `docker compose --env-file .env -f deploy/compose/compose.yml up -d --build --wait inference` 后容器 `Up (healthy)`，`compose ps` 只显示 `9000/tcp` 而无 published ports；用容器自身 `CITEMIND_INFERENCE_TOKEN` 在容器内调用 `/internal/embed` 返回 512 维向量（范数 0.99999999）；验收后用 `rm -sf inference` 回收容器，未删除数据卷 |
| 六服务静态检查（上一轮，保留为历史） | 两个 Compose `config --quiet`（base 与 queue override）均通过；`uv run ruff check backend/src tests`、`uv run mypy`、`uv run pytest -m "not integration"`（238 passed）、inference 的 `uv run --frozen pytest`（当次 14 passed）与 `pnpm frontend:build` 均通过 |
| inference 本切片静态检查（本地测试环境，保留为历史/本地上下文，非镜像内 124） | `cd inference` 下 `uv lock --check`、`uv run --frozen ruff check src tests`、`uv run --frozen ruff check scripts`、`uv run --frozen mypy`、`uv run --frozen pytest -q`（117 passed, 7 skipped）均通过；加载前字节校验、golden 结构审计与拒绝回归已纳入常驻测试，真实权重与 golden 比较为显式 opt-in，本轮无本地权重因而不运行 |
| 仓库本轮静态检查（本地测试环境，保留为历史/本地上下文） | `uv run ruff check backend/src tests`、`uv run mypy`、`uv run pytest -m "not integration"`（254 passed, 27 deselected）、`docker compose --env-file .env.example -f deploy/compose/compose.yml config --quiet`（base 与 queue override）与 `git diff --check` 均通过 |
| inference 最终镜像内真实测试（最终修复验收，离线） | 产品镜像 `citemind-inference:final-verify-20260923T010823`（ID `sha256:4be274ae00986c9a70e165abe4fa4d51d006e6c056727ce89334ae573b9ebed4`）之上用 dev 测试镜像 `citemind-inference:final-verify-test-20260923T010823` 挂载当前 `inference/tests` 只读执行，单行命令：`docker run --rm --network none -v "D:/Projects/MyRAG/inference/tests:/app/tests:ro" -e CITEMIND_RUN_MODEL_TESTS=1 citemind-inference:final-verify-test-20260923T010823 python -m pytest -q -p no:cacheprovider tests`；日志 `D:/tmp/myrag-embed-verify/inference_full_tests.txt` 为 **124 passed, 0 skipped**，7 个真实模型 golden 测试见 `real_model_tests.txt`（7 passed），即 117+7=124 且无 opt-in 跳过 |
| 篡改拒绝在导入 torch 之前（最终修复验收） | 5 个篡改用例（tokenizer.json 字节、model.safetensors 字节、额外 pytorch_model.bin、清单错 revision、清单错 digest）各自用真实 `uvicorn citemind_inference.main:app` 启动；日志 `tamper_runs.log` 读回全部 `uvicorn_rc=3` 且 `Application startup failed. Exiting.`，聚焦检查为 `torch_imported=False`、`transformers_imported=False`、`TORCH_NOT_IMPORTED_BEFORE_REJECTION`，且原模型目录 SHA-256 未被改动 |
| 六服务最终启动与容器内检查（最终修复验收） | `docker compose --env-file .env -f deploy/compose/compose.yml up -d --build --wait` 退出码 0，日志 `compose_up.log` 读回 postgres/redis/api/inference/worker/frontend-gateway 六个容器全部 Healthy；`incontainer_results.txt` 读回 `/health`、`/ready`（dimension=512、modelRevision=7999e1d3359715c523056ef9478215996d62a620）、`/internal/embed`（512 维有限、L2 范数 1、无回显）与离线变量检查全部 PASS，`RESULT_FAILURES=[]` |
| 仓库最终静态检查（最终修复验收） | 宿主 `uv run ruff check backend/src tests`、`uv run mypy` 通过，`uv run pytest -m "not integration"` 为 254 passed、27 deselected，`git diff --check` 通过；本轮未跑 root 集成测试，也未测 RSS/p95 |
| 最终镜像与当前树差异 | 最终镜像内产品源码哈希见 `inimage_source_hashes.txt`，与冻结树 `source_hashes.txt` 逐文件一致；当前树相对该镜像仅 `src/citemind_inference/config.py` 派生上限注释文字修正（常量、算法、默认值不变，行为等价）与 `tests/test_real_model.py` 新增一条 golden token 数断言，因此未重建产品镜像 |

早期这轮验收只覆盖 `vector` 扩展的启用与回收、512 维字面量的解析和迁移的降级；`chunk_embedding VECTOR(512)` 列约束已由第二切片迁移的真实插入（512 维成功、非 512 维失败）另行验收，但 ANN 索引、向量检索与授权过滤仍未实现，因此不能据此声称检索维度契约已经端到端通过。

## 环境和依赖

当前骨架已在 Windows 11、Node.js 24.16.0、pnpm 11.22.0、uv 0.12.4 和由 uv 管理的 CPython 3.12.13 上验证。完整队列运行仍以 Linux 容器或 Windows WSL2 为验收环境。

- 后端使用 Python 3.12、`uv`、`pyproject.toml` 和 `uv.lock`。当前已锁定 FastAPI、Pydantic v2、pydantic-settings、Uvicorn、SQLAlchemy 2.x、psycopg 3、Alembic、pgvector-python、Celery 5.x 与 Redis 客户端（`celery[redis]`）；HTTPX 当前仅用于 ASGI 接口测试。psycopg 只安装 `[binary]` extra，连接池使用 SQLAlchemy 自带实现，`psycopg_pool` 未被使用；pgvector-python 与业务的 `Vector(512)` 已在 `chunk_embedding` 落地；当前迁移创建 `vector` 扩展、第一片六张事实表和第二片三张索引表。worker 计划使用独立同步 Session；推理进程已单独安装 PyTorch CPU 与 transformers（不用 sentence-transformers）。
- Windows 上 psycopg 异步模式不能使用默认的 `ProactorEventLoop`：应用启动用 `--loop evidencehub.event_loop:create_event_loop` 提供自定义事件循环工厂（Windows 返回 `SelectorEventLoop`，其他平台返回 `asyncio.new_event_loop`），不修改全局事件循环 policy；Alembic 在线迁移在 Windows 内部同样切换到 `SelectorEventLoop`。
- 前端已使用 Vue 3、Vite 和 TypeScript，并由根目录 pnpm workspace 管理。Element Plus 在出现实际组件需求后再加入，不为骨架预装。
- 开发期 Jev 判断使用 Node 脚本、Vercel AI SDK 和 `typesafe-ai/jev`，只从服务端 `AI_GATEWAY_API_KEY` 读取凭据，不进入前端产物或产品运行时。
- 文档处理计划在 MVP 加入 markdown-it-py、pypdf、jieba；完整范围再加 pdfplumber、python-docx、BeautifulSoup4/lxml 和受限网页抓取。
- 数据服务：PostgreSQL 17 + pgvector 0.8.x、Redis；云生成默认选 DeepSeek API 的 `deepseek-flash` 非思考模式，模型名保持配置化。迁移验收已实测 PostgreSQL 17.11 + pgvector 0.8.6（镜像 `pgvector/pgvector:pg17`）；其余版本、接口行为与镜像 digest 在 Phase 0 验证后固定，不使用浮动 `latest`。
- 本地数据与服务用 `deploy/compose/compose.yml` 起六服务：`postgres`、`redis`、`api`、`inference`、`worker` 与 `frontend-gateway`。第三方镜像按 digest 固定（`pgvector/pgvector:pg17`、`redis:7.4.9`），宿主端口默认只绑定 `127.0.0.1:55432`、`127.0.0.1:56379` 与 `127.0.0.1:58080`，可分别用 `CITEMIND_POSTGRES_PORT`、`CITEMIND_REDIS_PORT`、`CITEMIND_GATEWAY_PORT` 覆盖，且只有 gateway 是应用入口；数据放命名卷。`api` 与 `worker` 由 `deploy/compose/Dockerfile` 构建（基础镜像与 uv 按 digest 固定，非 root 10001，用 target `api`/`worker` 区分入口；api 容器内 DSN 用 `postgres:5432`，不透传宿主回环 DSN）。`inference` 用 `inference/` 独立项目构建（非 root 10002，内部 9000 不发布，`CITEMIND_INFERENCE_TOKEN` 必填；构建期下载固定 revision 的 BAAI/bge-small-zh-v1.5 配置、tokenizer 与 safetensors，并用 Hub commit、Git blob/LFS 摘要与脚本内钉死 SHA-256 校验证，运行期 `HF_HUB_OFFLINE`/`TRANSFORMERS_OFFLINE` 离线加载，加载前先按包内钉死摘要与产物清单离线核对六个可信产物，`AutoModel` 显式走 safetensors 且所有加载禁用 remote code，CPU 线程变量在 torch 导入前由进程环境固定）。`frontend-gateway` 用 `deploy/compose/frontend.Dockerfile`（Node 24 构建静态产物，运行镜像 `nginxinc/nginx-unprivileged` 非 root 101，内部 8080）并把 `/api/` 动态代理到 `api:8000`。Redis 使用 AOF `everysec`、`maxmemory 128mb`、`noeviction`，密码由环境注入，healthcheck 通过 `REDISCLI_AUTH` 读取。PostgreSQL 集群以超级用户 `citemind_migrator` 初始化并固定 `--encoding=UTF8 --locale=C.UTF-8 --data-checksums`；initdb 脚本创建非特权角色 `citemind_api`、`citemind_worker` 与测试库 `citemind_test`，并收回 `citemind`、`citemind_test` 中 PUBLIC 的数据库权限与 `public` schema 权限，只给两个运行角色 CONNECT 与 schema USAGE。inference 已能以固定 revision 模型编码 `kind=document` 文本（rerank 与 `query` 编码未实现），worker 写入事务与认证、入库、检索、问答仍未实现。

## 目录

```text
backend/src/evidencehub/   # 已建立：API、配置与数据库会话入口
frontend/                  # 已建立：Vue 控制台骨架
scripts/                   # 已建立：开发辅助和质量门禁实现
tests/tooling/             # 已建立：开发工具的 Node 测试
tests/unit/                # 已建立：后端纯逻辑、API 骨架与部署文件静态测试
inference/                 # 已建立：独立推理进程（构建期烘入固定 revision 模型，自带 pyproject/uv.lock/Dockerfile）
  scripts/prepare_model.py # 已建立：构建期下载模型并以 Hub blob/LFS 摘要与钉死 SHA-256 校验证，支持 --verify
  src/citemind_inference/model_identity.py # 已建立：加载前复用同一组钉死摘要与产物清单做离线字节校验
  tests/golden/            # 已建立：独立断网 CPU 环境生成的 golden 参考（JSON + 取证脚本，原样保留）
migrations/                # 已建立：pgvector 扩展迁移与两片业务表迁移（`20260921_0001`…`20260922_0003`）
deploy/compose/            # 已建立：本地六服务 Compose 切片（postgres/redis/api/inference/worker/frontend-gateway）
  Dockerfile               # 已建立：api/worker 共享 runtime stage + api/worker final target（python:3.12 + uv，非 root）
  frontend.Dockerfile      # 已建立：Node 24 构建前端 + 非 root nginx 网关运行镜像
  frontend.Dockerfile.dockerignore # 已建立：网关构建上下文白名单（放行根 workspace 与 frontend 源）
  gateway/nginx.conf       # 已建立：SPA fallback + /api 动态代理 + 安全头 + /healthz
  queue.yml                # 已建立：Linux Compose 确定性验收 override（显式限定 queue-probe service）
tests/integration/         # 已建立：迁移、角色 DSN、broker guard 与真实 Redis worker 测试
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

- 对纯逻辑运行聚焦单测；授权 SQL、迁移和向量维度使用真实 PostgreSQL/pgvector；任务恢复使用真实 Redis 与独立 Celery worker。`uv run` 不会把根 `.env` 注入 `os.environ`：依赖 `os.environ` 的命令（Alembic 在线迁移、集成测试守卫、Celery worker CLI）必须写成 `uv run --env-file .env ...`。pydantic Settings 另行通过 `env_file=".env"` 读取同一文件，但统一使用 `--env-file` 以保持两套来源一致。
- 对入库验证重复投递、Redis 断连或重启、worker 强制退出、租约过期后的迟到回写、旧版本继续可查和单次发布。
- 对问答验证越权内容从候选至引用全链不可见、撤权后的历史/下载、结构化回答和模型超时。性能与质量按 [评估与验收](evaluation.md) 的数据集和硬件条件实测。
- 当前可执行 `uv sync --frozen`、Ruff、mypy、非集成 pytest、Jev 脚本测试、Alembic 离线 SQL 检查和前端构建。真实 PostgreSQL/pgvector 集成测试需要 `CITEMIND_TEST_DATABASE_URL`（`postgresql+psycopg` 驱动、数据库名以 `_test` 结尾）并显式确认 `CITEMIND_ALLOW_DESTRUCTIVE_TEST_DB=1`，因为测试会执行 upgrade 和 downgrade；测试数据库必须由 CiteMind 独占、不能与其他应用共享，迁移账号必须拥有 `CREATE EXTENSION` 权限。运行前测试先断言 `current_database()` 与 URL 中的库名一致、`vector` 扩展不存在且 Alembic 处于 base，任何一项不满足都会失败而不是继续。未设置测试 DSN 时仅明确跳过。分角色镜像、依赖扫描、SBOM、GHCR 发布与恢复演练仍是后续计划。
- 在线迁移只接受显式 DSN：Alembic 配置项 `sqlalchemy.url` 优先，否则必须设置 `CITEMIND_MIGRATION_DATABASE_URL`，缺失或不符合 SQLAlchemy URL 规则时直接以非零状态失败，不会回退到 `CITEMIND_DATABASE_URL` 或开发默认 URL。该 DSN 需要 `CREATE EXTENSION` 权限，只用于迁移进程，不进入 API 与 worker 的运行配置。
- 离线 `uv run alembic upgrade head --sql` 只生成 SQL、不连接数据库，仍可使用开发默认 URL。
- 本地数据与服务切片用 `docker compose --env-file .env.example -f deploy/compose/compose.yml config --quiet` 做权威静态插值检查：不需要 Docker daemon，也不启动容器；pytest 中的部署文件测试只补充检查 digest、回环端口、初始化标记、必填变量在 `.env.example` 中以非空值提供和 LF 等源码不变量，不能替代 Compose CLI。首次真实启动前配置已忽略的根 `.env`，分两种情况：根 `.env` 不存在时用 `Copy-Item .env.example .env` 创建它并替换四个数据库/Redis 开发密码；根 `.env` 已存在时不得覆盖，只把 `.env.example` 中 Compose 需要的 `CITEMIND_MIGRATION_DB_PASSWORD`、`CITEMIND_API_DB_PASSWORD`、`CITEMIND_WORKER_DB_PASSWORD`、`CITEMIND_REDIS_PASSWORD` 四个密码变量、本地 worker/派发需要的 `CITEMIND_REDIS_URL`、inference 需要的开发占位 `CITEMIND_INFERENCE_TOKEN` 与可选的 `CITEMIND_POSTGRES_PORT`、`CITEMIND_REDIS_PORT`、`CITEMIND_GATEWAY_PORT` 追加进去，已保存的 `AI_GATEWAY_API_KEY` 等本地配置保持原样。
- inference 镜像的模型产物由 `inference/scripts/prepare_model.py` 在构建期从固定 revision 下载并校验证（Hub commit、Git blob/LFS 摘要与脚本内钉死 SHA-256），不在运行期下载。加载端 `load_embedder` 在导入 torch/transformers 之前复用包内 `citemind_inference.model_identity` 的同一组钉死摘要，离线核对六个产物的大小、SHA-256 与目录集合，并交叉核对模型目录父目录的 `model-manifest.json`；清单只作为待核对对象，不能自报身份，覆盖 `CITEMIND_EMBEDDING_MODEL_PATH` 时同样走这套校验，任何偏差都使 lifespan 启动失败。离线复核用 `docker run --rm --network none --entrypoint python citemind-inference:latest scripts/prepare_model.py --verify`，离线真实编码用 `docker run --rm --network none --entrypoint python citemind-inference:latest -c "from citemind_inference.config import Settings; from citemind_inference.embeddings import load_embedder; e=load_embedder(Settings(inference_token='x')); v=e.embed(['离线样本'])[0]; print(e.dimension, e.model_revision, e.token_counts(['离线样本']), len(v), round(sum(x*x for x in v)**0.5, 9))"`。两者均不打印 token、不读取宿主模型卷。真实模型与独立 golden 的比较是显式 opt-in：在模型目录存在且清单校验通过时设 `CITEMIND_RUN_MODEL_TESTS=1`（可用 `CITEMIND_TEST_EMBEDDING_MODEL_PATH` 覆盖目录）后运行 `uv run --frozen pytest -q -m model tests/test_real_model.py`，token IDs 要求与 `tests/golden/golden-reference.json` 严格相等、512 维向量按每元素最大绝对差 ≤ 1e-4 比较；未 opt-in 时跳过，显式 opt-in 但模型缺失、损坏或 golden 不一致都必须失败。
- 根 `.env` 就绪后必须逐个核对连接串与 Compose 变量的完整一致性，任一项不符都会导致连接失败：`CITEMIND_DATABASE_URL` 以及所有已启用的 migration/test DSN（`CITEMIND_MIGRATION_DATABASE_URL`、`CITEMIND_TEST_DATABASE_URL`、`CITEMIND_TEST_MIGRATOR_DATABASE_URL`、`CITEMIND_TEST_API_DATABASE_URL`、`CITEMIND_TEST_WORKER_DATABASE_URL`）都要检查对应用户名、数据库名、host=`127.0.0.1`、端口=`CITEMIND_POSTGRES_PORT` 与对应角色的密码，只有全部一致才能保持原样。允许在本地安全比对，但不得把真实密码输出到终端、日志或命令历史，只报告是否一致。
- `CITEMIND_POSTGRES_PORT` 变化只改变 PostgreSQL 的宿主端口，因此只需把上述每个 DSN 的端口同步为新值，用户名、数据库名和密码无需改动。同步后重新执行已有单行 `docker compose --env-file .env -f deploy/compose/compose.yml up -d --wait` 以重建容器；命名卷保留数据，不需要 `down -v`。
- PostgreSQL 初始化流程写入 `CITEMIND_MIGRATION_DB_PASSWORD`、`CITEMIND_API_DB_PASSWORD`、`CITEMIND_WORKER_DB_PASSWORD` 三个角色密码（分别对应 `citemind_migrator`、`citemind_api`、`citemind_worker`）。initdb 脚本只在空命名卷上执行一次，所以卷已初始化后再改动其中任一密码，重启不会更新已有角色：必须先执行会删除本地数据库数据的 `docker compose --env-file .env -f deploy/compose/compose.yml down -v`，再执行 `docker compose --env-file .env -f deploy/compose/compose.yml up -d --wait`，让 initdb 重建角色、密码与 ACL。角色或 ACL 需要重建时同样先跑 `down -v`。
- Redis 密码 `CITEMIND_REDIS_PASSWORD` 不进入任何 PostgreSQL DSN，也不参与上面的 DSN 同步：它只需在 Compose 的 redis 环境变量与 `--requirepass` 之间保持一致，重建 redis 容器即可生效，不需要 `down -v`。worker 与本地派发使用的 `CITEMIND_REDIS_URL` 只与 `CITEMIND_REDIS_PASSWORD`、`CITEMIND_REDIS_PORT`（宿主）或 Compose 内 `redis:6379`（容器）一致；缺失时 worker 启动直接失败，不回退到 localhost。
- 缺少任一密码变量时 `docker compose --env-file .env -f deploy/compose/compose.yml up -d --wait` 会以 `required variable ... is missing a value` 退出，这是必填插值的预期快速失败，不是 Compose 故障。
- `docker compose config` 会把插值后的密码明文打印，Redis 密码也会出现在容器命令中；不要分享这些输出，生产部署必须改用独立密钥方案。
- 验证 Redis 是否真的要求鉴权时必须临时移除 `REDISCLI_AUTH`：该变量由 Compose 注入 redis 容器，容器内 `redis-cli` 会继承它并自动完成 AUTH，继承环境下 `PING` 返回 `PONG` 不能证明 Redis 没有密码。在 PowerShell 中执行 `docker compose --env-file .env -f deploy/compose/compose.yml exec -T redis env -u REDISCLI_AUTH redis-cli -e ping`，`env -u` 只对这一次 `redis-cli` 子进程取消 `REDISCLI_AUTH`（不要把变量设为空值），`-e` 让 `redis-cli` 在收到错误回复时以非零状态退出；预期 `NOAUTH Authentication required.` 文本写入 stderr（不是 stdout）且退出码非零，只有观察到二者才能确认鉴权生效。
- 角色权限集成测试是只读的，但要求三个角色 DSN 同时提供并指向同一个 `_test` 库：`CITEMIND_TEST_MIGRATOR_DATABASE_URL`、`CITEMIND_TEST_API_DATABASE_URL`、`CITEMIND_TEST_WORKER_DATABASE_URL`，用户名固定为 `citemind_migrator`、`citemind_api`、`citemind_worker`。若测试库名为 `<app>_test`，验收同时要求对应的 `<app>` 应用库存在并核对两个库的 ACL，避免范围静默缩小。三者都未设置时明确跳过；只设置部分、或驱动、用户名、host、port、database 任一项不符时在连接前失败，不尝试连接。
- 真实 Redis broker 集成测试需要 `CITEMIND_TEST_REDIS_URL`（`redis`/`rediss` scheme、host 必须是回环地址、必须带密码、必须显式指定非 0 逻辑库如 `.../15`）并显式设置 `CITEMIND_ALLOW_TEST_REDIS=1`。未设置 DSN 时明确跳过；DSN 非法或缺少 opt-in 时在连接前失败，不尝试连接。测试以唯一队列名派发 `evidencehub.probe`，把临时 marker 目录传给 worker 子进程，只在 `finally` 删除该队列 key 及其 `_kombu.binding.<queue>`、不 `FLUSHDB`，并回收 worker 子进程。执行证据来自与本次 `async_result.id` 对应的 marker 文件；worker 日志与 `inspect` 只用于 readiness 与失败诊断，不使用 Celery result backend。
- Linux Compose 的确定性 worker 验收走 override：`docker compose --env-file .env -f deploy/compose/compose.yml -f deploy/compose/queue.yml up --build --abort-on-container-exit --exit-code-from queue-probe queue-probe`。命令显式只启动 `queue-probe` service，Compose 自动带上它依赖的 worker/redis/postgres，避免六服务里其它容器干扰 `--abort-on-container-exit`。它给 base worker 与一次性 `queue-probe` 挂载同一个非 root marker 卷并设置 `CITEMIND_PROBE_MARKER_DIRECTORY`；`queue-probe` 派发唯一 probe 后只在受信目录里等待并校验 marker，成功退出 0、失败/超时退出非 0，因此可用 `--exit-code-from queue-probe` 得到硬证据。该 override 不引入 result backend，base compose 默认不运行 queue-probe。验收后回收容器与网络（保留所有命名卷）：`docker compose --env-file .env -f deploy/compose/compose.yml -f deploy/compose/queue.yml down --remove-orphans`；如需单独删除 queue-probe 的 marker 卷，用 `docker volume rm citemind_probe-markers`，不要用 `down -v`，它会一并删除数据库与 Redis 数据。
- 宿主上验收回环地址（如 `http://127.0.0.1:58080/api/v1/health`）的 HTTP 命令必须绕过代理：PowerShell 用 `curl.exe --noproxy "*" -fsS http://127.0.0.1:58080/api/v1/health`。若 shell 设置了 `HTTP_PROXY`/`HTTPS_PROXY`，回环请求可能被送到代理并返回代理自身的 502，形成伪证据；用 `curl.exe` 而不是 `curl`，因为 PowerShell 的 `curl` 是 `Invoke-WebRequest` 别名。

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
uv run --env-file .env pytest -m integration
uv run --env-file .env celery -A evidencehub.worker:celery_app worker --pool=solo --loglevel=INFO
docker compose --env-file .env -f deploy/compose/compose.yml -f deploy/compose/queue.yml config --quiet
docker compose --env-file .env.example -f deploy/compose/compose.yml config --quiet
docker compose --env-file .env -f deploy/compose/compose.yml up -d --build --wait
docker compose --env-file .env -f deploy/compose/compose.yml exec -T worker celery -A evidencehub.worker:celery_app inspect ping
docker compose --env-file .env -f deploy/compose/compose.yml -f deploy/compose/queue.yml up --build --abort-on-container-exit --exit-code-from queue-probe queue-probe
docker compose --env-file .env -f deploy/compose/compose.yml -f deploy/compose/queue.yml down --remove-orphans
pnpm install --frozen-lockfile
pnpm test:jev
pnpm jev -- scripts/jev-request.example.json
pnpm --dir frontend dev
pnpm frontend:build
```

`pnpm jev` 从标准输入或首个参数指定的 JSON 文件读取 `{ state, questions }`，固定调用 `typesafe-ai/jev`；输入只应包含完成当前判断所需的非敏感状态。提交前检查差异、生成文件和密钥，只报告实际运行结果与未运行的验证。

Celery worker 的权威验收环境是 Linux 容器或 WSL2：`docker compose --env-file .env -f deploy/compose/compose.yml up -d --wait` 会启动独立 `worker` 容器，其 healthcheck 用 `inspect ping` 确认消费。确定性执行验收用 `deploy/compose/queue.yml` 的 queue-probe 退出码，而不是日志或事件。Windows 本机只能用 `--pool=solo`（默认 prefork 不受支持），适合开发自检，不等同于 Linux 上的并发与故障恢复验收。
