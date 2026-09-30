# Phase 4 手动验收（阶段待验收）

> 本页只给可由授权操作者手动执行的命令与判据，**不含任何已跑结果**：Phase 4 仍未验收。本轮 agent 未读真实 `.env`、未运行真实 HTTP、PostgreSQL、Redis、Docker、备份/恢复、付费 LLM 或 inference；操作者需自行准备并读取**仓库外**的隔离配置，但**不得回显、粘贴或提交**任何凭据。以下命令均从仓库根目录以**单行 PowerShell** 执行（不换行、不反斜杠续行），默认值均为非秘密安全值。

先在一行里定义本页所有命令使用的变量（安全默认值）：

```powershell
$fixtureProject='myrag-phase4-fixtures'; $sourceProject='myrag-phase4-source'; $restoreProject='myrag-phase4-restore'; $phase4Root=Join-Path $env:USERPROFILE 'citemind-phase4'; $fixtureEnvFile=Join-Path $phase4Root 'fixtures.env'; $sourceEnvFile=Join-Path $phase4Root 'source.env'; $restoreEnvFile=Join-Path $phase4Root 'restore.env'; $snapshotDir=Join-Path $phase4Root ('snapshot-' + (Get-Date -Format 'yyyyMMdd-HHmmss')); $reportPath=Join-Path $phase4Root ('perf-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.json'); $qaReportPath=Join-Path $phase4Root ('qa-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.json'); $sourceAdmin='phase4-admin'; $apiBaseURL='http://127.0.0.1:58580'; $kbId=''; $requestCount=100
```

真实标识由操作者按自己的源测试应用填写：`$kbId` 从源应用 GUI 里已 READY 的 KB 复制（留空则真实采集会静态失败，这是有意为之）；`$apiBaseURL` 默认 `http://127.0.0.1:58580`，若改了源 `GATEWAY_PORT` 需同步修改。

## 1. 前置隔离与环境

集成夹具会对它指向的库执行 `alembic upgrade` 与 `downgrade base` 并清空数据，因此本节只使用**破坏性测试专用**的 `$fixtureProject`，其 `citemind_test` 库必须独占且为空，不能与运行中的应用、源样本或恢复演练数据混用。守卫源码在 `tests/integration/database_guard.py`、`database_roles_guard.py`、`broker_guard.py`，要求的键与规则（由操作者用自己受管的方式注入进程环境，本页不提供在 shell 历史里明文赋值的命令）：

- `TEST_DATABASE_URL` 使用 `postgresql+psycopg`、库名以 `_test` 结尾；同时提供 `TEST_MIGRATOR_DATABASE_URL`/`TEST_API_DATABASE_URL`/`TEST_WORKER_DATABASE_URL`，用户名分别为 `citemind_migrator`/`citemind_api`/`citemind_worker`，且与 `TEST_DATABASE_URL` 同 host、port、database；并设 `ALLOW_DESTRUCTIVE_TEST_DB=1`。
- `TEST_REDIS_URL` 为回环地址、带密码、显式非 0 逻辑库（如 `/15`），并设 `ALLOW_TEST_REDIS=1`。
- 旧 `CITEMIND_TEST_*` 键会被直接拒绝；残留旧名先改为上面的裸名。

外部配置在**资源管理器**里手工准备：创建目录 `$phase4Root`（例如 `%USERPROFILE%\citemind-phase4`），首次从 `.env.example` 手工复制出 `fixtures.env`、`source.env`、`restore.env` 三份到该目录，再用编辑器替换成独立合成密码与独立宿主端口。**已有文件绝不覆盖**，只手动核对本页列出的必要字段；不要读取或覆盖仓库内的真实 `.env`。`fixtures.env` 供本节夹具，`source.env` 供第 3、4 节源样本，`restore.env` 供第 3 节恢复目标。

用独立 project 名启动夹具 PostgreSQL 与 Redis（`-p` 会覆盖文件里的 `name: citemind`，不触碰 dev 卷与网络）；`fixtures.env` 的 `POSTGRES_PORT`/`REDIS_PORT` 必须是独立于 dev `55432`/`56379` 的端口：

```powershell
docker compose --env-file $fixtureEnvFile -p $fixtureProject -f deploy/compose/compose.yml up -d --wait postgres redis
docker compose --env-file $fixtureEnvFile -p $fixtureProject -f deploy/compose/compose.yml port postgres 5432
```

**判据**：`port` 输出只绑定 `127.0.0.1` 且端口与 `fixtures.env` 一致；夹具 `TEST_*`/`ALLOW_*` 键已安全注入；未出现真实 `.env` 内容或真实密码回显。

## 2. 聚焦手动集成测试

判据统一为：命令**0 skipped** 且收集到的用例**全部 passed**；首次因守卫缺值而 skip **不是**通过。不要用全量 `-m integration` 当必须入口。四个聚焦组各有独立用途：

- Dispatcher 恢复语义（真实 PostgreSQL + fake publisher，不依赖 broker）：`uv run --no-sync pytest tests/integration/test_dispatcher_flow.py -q`
- 真实 Redis broker 与独立 worker 子进程（应用层故障注入，**不是**物理 kill）：`uv run --no-sync pytest tests/integration/test_dispatcher_broker.py tests/integration/test_worker_broker.py -q`
- 身份与 KB 授权（真实 DB 行锁与受控交错，**不是**多副本压测）：`uv run --no-sync pytest tests/integration/test_auth_flow.py tests/integration/test_kb_flow.py -q`
- 文档 ACL 与问答主流程（provider 为 mock，**不证明**真实 provider 超时）：`uv run --no-sync pytest tests/integration/test_document_acl_flow.py tests/integration/test_conversation_flow.py -q`

这些夹具测试最后会把 schema 降回 base 并清空夹具库，**不在这里做物理故障**：`$fixtureProject` 没有业务 schema、worker 也没起。物理 Redis 停启与 worker kill 属真实任务场景，改到第 3 节已准备好样本的 `$sourceProject` 上执行。

## 3. 源样本、物理故障、备份与隔离恢复

本节使用**三个物理隔离的 project**：`$fixtureProject` 只跑第 2 节的破坏性测试，其夹具库会被降回 base 清空，不能用于备份；`$sourceProject` 是单独准备好的**非空真实 MVP 样本应用**（默认业务库 `citemind`，project 隔离使备份安全，但真实备份仍必须 `--execute`+`--confirm $snapshotDir`，并额外用 `--quiesced` 声明已暂停写入，不是只声明 quiesced 即可）；`$restoreProject` 是恢复目标，恢复到它自己的 `citemind_test`。源若用 `_test` 库也合法，但不要拿第 2 节已被清空的夹具库备份。

先按 [部署设计](deployment.md) 与 [开发约定](development.md) 准备 `$sourceProject`：启动前就配置好 `source.env` 全部 flags（含两个门控），再按“数据服务 → 迁移/开户 → 六服务 overlay”顺序推进；`--build` 会在构建期下载固定的 inference 模型资产，需网络且仅由操作者手动执行。

先只启动数据服务（此时还没有业务 schema，不能先等 API/worker 健康）：

```powershell
docker compose --env-file $sourceEnvFile -p $sourceProject -f deploy/compose/compose.yml up -d --wait postgres redis
```

`source.env` 必须手工核对并设置（改端口不会自动改 DSN，每个 DSN 的 host 端口与密码都要与新 PG 一致）：`POSTGRES_PORT`/`REDIS_PORT`/`GATEWAY_PORT=58580`；四个密码变量；`MIGRATION_DATABASE_URL`（migrator，宿主 `127.0.0.1` 加 `POSTGRES_PORT`）与宿主 `DATABASE_URL`（api 角色，同端口）；`ALLOWED_ORIGINS=http://127.0.0.1:58580,http://localhost:58580`（key 见 `.env.example`，compose 内随 `GATEWAY_PORT` 自动插值，宿主进程读 env-file）；启用**两个**门控 `DISPATCHER_ENABLED=1` 与 `INGEST_PROCESSING_ENABLED=1`（只开后者会让任务永远停在 `QUEUED`）；`INFERENCE_TOKEN`。用 migrator 的 `MIGRATION_DATABASE_URL`（**不是** api DML 角色，也不回退到 `DATABASE_URL`）对源库在线迁移，再由运维 CLI 开户（普通用户不能建 KB，必须 `--admin`）：

```powershell
uv run --env-file $sourceEnvFile alembic upgrade head
uv run --env-file $sourceEnvFile python -m rag_backend.auth.cli --username $sourceAdmin --admin --password-env SOURCE_ADMIN_PASSWORD
```

schema 就绪后再叠加 overlay 拉起六服务（准备样本时就叠加，使第 4 节资源测量针对同一栈）：

```powershell
docker compose --env-file $sourceEnvFile -p $sourceProject -f deploy/compose/compose.yml -f deploy/compose/phase4-limits.yml up -d --build --wait
```

最后从浏览器在 `$apiBaseURL` 上传自制 Markdown/PDF，等索引全部 `READY`。

**物理故障（仅 `$sourceProject`）**：在全 READY 样本上再上传一份自制资料，让某个任务处于处理中，然后 kill worker 或停止 Redis，等租约过期由 api 内 dispatcher 有界恢复，再拉起 worker 消费；停/恢复措施如下，不要 `down -v`，也不要作用于 dev：

```powershell
docker compose --env-file $sourceEnvFile -p $sourceProject -f deploy/compose/compose.yml kill worker
docker compose --env-file $sourceEnvFile -p $sourceProject -f deploy/compose/compose.yml stop redis
docker compose --env-file $sourceEnvFile -p $sourceProject -f deploy/compose/compose.yml start redis
docker compose --env-file $sourceEnvFile -p $sourceProject -f deploy/compose/compose.yml up -d --wait worker
```

用 [文档入库](ingestion.md) 的只读控制查询核对 job 的 `status`/`error_code`/`attempt`/`next_run_at`/`lease_owner`/`lease_until`/`heartbeat_at` 与 `outbox_event` 的 `event_type`/`status`，确认租约过期任务可被重新领取并最终 `READY`、同一 version/profile 不得重复 READY 索引发布，观察重投事件被幂等收敛（补偿可合法新建 `PENDING`，不是禁止补投），并手工记录；不新建 fault 脚本或 SQL。自然 3600 秒 visibility 重投、多 API 副本与灾备不属于本阶段强制项。

**备份**：样本全 READY 后只停 `$sourceProject` 的 api 与 worker（`postgres` 继续运行），再用 `--quiesced` 声明已暂停写入执行真实备份；`backup` 默认 dry-run，`--confirm` 必须与 `--output` 完全一致：

```powershell
docker compose --env-file $sourceEnvFile -p $sourceProject -f deploy/compose/compose.yml stop api worker
uv run --no-sync python -m rag_backend.operations.backup backup --project $sourceProject --database citemind --output $snapshotDir
uv run --no-sync python -m rag_backend.operations.backup backup --project $sourceProject --database citemind --output $snapshotDir --execute --confirm $snapshotDir --quiesced
docker compose --env-file $sourceEnvFile -p $sourceProject -f deploy/compose/compose.yml start api worker
```

**隔离恢复**：目标必须是**不同** project 里已存在的空 `_test` 库与空 `api-documents` 卷；`restore.env` 的 `POSTGRES_PORT`/`REDIS_PORT`/`GATEWAY_PORT` 也必须与 source 和 dev 都不同，避免端口冲突；`restore` 默认 dry-run，真实执行需要 `--execute` 且 `--confirm` 与 `--target-database` 完全一致；`verify` 只读，只需 `--execute`、**不支持也不接受** `--confirm`，它按应用 UID 只读导出目标实际 blob 并逐文件核对 SHA-256：

```powershell
docker compose --env-file $restoreEnvFile -p $restoreProject -f deploy/compose/compose.yml up -d --wait postgres
docker volume create "${restoreProject}_api-documents"
uv run --no-sync python -m rag_backend.operations.backup restore --snapshot $snapshotDir --source-project $sourceProject --target-project $restoreProject --target-database citemind_test
uv run --no-sync python -m rag_backend.operations.backup restore --snapshot $snapshotDir --source-project $sourceProject --target-project $restoreProject --target-database citemind_test --execute --confirm citemind_test
uv run --no-sync python -m rag_backend.operations.backup verify --snapshot $snapshotDir --execute
uv run --no-sync python -m rag_backend.operations.backup verify --snapshot $snapshotDir --target-project $restoreProject --target-database citemind_test --execute
docker compose --env-file $restoreEnvFile -p $restoreProject -f deploy/compose/compose.yml -f deploy/compose/phase4-restore.yml up -d --wait
```

`deploy/compose/phase4-restore.yml` 是真实入口文件：它只把 api/worker 的 `DATABASE_URL` 库名从 `citemind` 改成 `citemind_test`（driver、角色、必填密码表达式与 base 完全一致），因此目标应用不必改 base 或 initdb；**绝不可用于 dev 或生产**。启动前先确认 restore 后目标 migrator schema 与源一致（都为 HEAD，可用 `uv run --no-sync alembic heads` 与目标 `alembic_version` 对照；源可较旧时只在**目标库**用 migrator DSN 升到与快照/代码一致）；如需宿主进程验证也可显式设置 `DATABASE_URL` 指向恢复库。目标半失败或损坏时只重建隔离 target 自己的空库/卷，工具不会 drop/clean；`down -v` 仅在上下文明确是隔离 target 且单独确认后使用。手工成功判据需同时满足：目标库版本/profile/任务引用正确、blob checksum 一致、应用读写正常（至少 target 上新 KB 上传与旧 KB 新版本）；代码层 fake 用例不证明 Docker owner 真的生效。

## 4. 性能与资源测量

源栈必须真正叠加限额 overlay（六服务）；下列命令读取并应用 `deploy/compose/phase4-limits.yml`，仅由操作者手动执行：

```powershell
docker compose --env-file $sourceEnvFile -p $sourceProject -f deploy/compose/compose.yml -f deploy/compose/phase4-limits.yml up -d --build --wait
docker compose --env-file $sourceEnvFile -p $sourceProject -f deploy/compose/compose.yml -f deploy/compose/phase4-limits.yml config --quiet
docker compose --env-file $restoreEnvFile -p $restoreProject -f deploy/compose/compose.yml -f deploy/compose/phase4-restore.yml stop
```

`--build` 在构建期下载已固定模型资产（人工、需网络，非运行期下载）。测量期间停掉 `$restoreProject`（上一条最后一行），也不要并行跑 `$fixtureProject` 应用，以免抢占资源。overlay 六容器上限之和恰为 `2.00` vCPU 与 `4096M`（二进制 MiB，即 4 GiB），**不包含**宿主操作系统与 Docker Desktop 虚拟机整体、镜像磁盘层、构建期资源与其它项目；worker 的 `640M` 是整容器内存限额，可能先由 cgroup OOM 终止 worker。容器内存限额按 cgroup 记账，可能包含容器内 page cache，不能笼统排除；采样是**有界间隔观察**，不是精确 RSS。`$apiBaseURL` 取源 `GATEWAY_PORT`，`$kbId` 取源应用里已 READY 的 KB，`$requestCount` 至少 `100`（不要用 `--count 1` smoke 当容量验收）。

入口 `python -m rag_backend.evaluation.performance`（已用 `--help` 核对 argparse）默认 dry-run，不联网、不读环境变量、不执行 Docker、不写文件；`--mode retrieval` 只调既有 `POST /api/v1/retrieval/search`，不产生付费调用；`--mode qa` 必须额外显式 `--allow-paid-llm`，但该 flag 只是授权 CLI，**不是**启用服务端生成：源 api 还需在 `source.env` 显式 `LLM_ENABLED=1` 与非占位 `LLM_API_KEY` 并重启，否则 qa 只会静态 503，不能算真实 QA 验收通过。`--api-base-url` 只接受 origin（拒绝 userinfo/path/query/fragment），默认只允许回环；秘密只从 `PERF_USERNAME`/`PERF_PASSWORD`/`PERF_QUERY`/`PERF_QUESTION`（可用 `--username-env` 等覆盖）注入，报告不含秘密与问题文本：

```powershell
uv run --no-sync python -m rag_backend.evaluation.performance --mode retrieval --count 1
uv run --no-sync python -m rag_backend.evaluation.performance --mode retrieval --api-base-url $apiBaseURL --kb-id $kbId --count $requestCount --out $reportPath --compose-project $sourceProject --execute
docker inspect $(docker compose --env-file $sourceEnvFile -p $sourceProject -f deploy/compose/compose.yml ps -q) --format "{{.Name}} memory={{.HostConfig.Memory}} nanocpus={{.HostConfig.NanoCpus}}"
```

付费问答容量若要测量，由操作者先启用服务端生成再另行人工授权执行（本页不代跑，且用独立的 `$qaReportPath` 避免覆写 retrieval 报告）：`uv run --no-sync python -m rag_backend.evaluation.performance --mode qa --api-base-url $apiBaseURL --kb-id $kbId --count $requestCount --out $qaReportPath --compose-project $sourceProject --allow-paid-llm --execute`。**判据与边界**：记录真实 `docker inspect` 限额、窗口内采样峰值（每服务取最大，只是有界间隔观察，非精确 RSS）、p50/p95/吞吐与失败（失败计入固定 `--count`、不剔除）；报告里资源观察的 `errors`/`null` 不能算容量验收通过。`5000` chunks 是计划负载，需操作者准备实际授权的有效语料并记录真实数量，不得用 QA 40 题或数量未知的语料代替；串行重复 query 测量不是混合流量，也不代表多并发稳定性。CI workflow 本轮不改；如用户愿意手动 push 后再运行 Actions，请记录 run URL；本轮无 push。成功退出码不等于阶段达标，必须读真实指标并逐条核对退出条件。

## 5. 阶段判据与未验收界限

Phase 4 退出条件逐条核对：worker/broker 中断可恢复；Alembic 和备份可恢复；模型超时明确；记录峰值内存、吞吐、p95 与实际限制。本轮只提供上述入口与判据，**阶段仍未验收且未提交真实运行**。

已明确的未验收界限，任何一条都不能被上面的命令“跑通”自动替代：真实模型/网络/DB/Redis/Docker/备份恢复/性能数值均未产生；连接 pin 只是应用层，真实网络握手与完整 SSRF 仍未验收；`RLIMIT_AS` 不保证覆盖所有合法最大输入，也不是 RSS/cgroup 硬限；640 MiB overlay 不代表资源达标；模型错误客户端单测只是 mock，不能声称真实 provider 超时（Phase 1 已有 2026-09-28 真实 40 题问答链路成功实证，不是只有一次探针成功，但真实失败/超时仍待验收）；多 API 副本、灾备平台与自然 3600 秒重投不是本阶段强制项。所有输出以真实运行与结果文件为准，不把计划数字写成成果。
