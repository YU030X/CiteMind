# 知据 CiteMind

知据 CiteMind 是一个规划中的企业知识库应用。它面向需要查阅内部制度、产品说明和技术文档的小团队，让用户用自然语言提问，并查看回答依据对应的原文与版本。知识管理员负责导入和更新资料、分配访问权限，以及检查检索与引用质量。

项目重点是让回答有可核对的来源，并让文档权限、更新和删除在检索与历史访问中持续生效。资料不足时，系统应明确拒答；对模型输出的引用仍需验证其是否真正支持回答。

## 当前进度

Phase 0 工程底座已验收，Phase 1 已完成身份/KB 授权及 Markdown 上传受理切片：当前有锁定依赖的 FastAPI 健康检查与 OpenAPI、Vue 3/Vite/TypeScript 前端骨架、SQLAlchemy 异步数据库会话、Alembic 的 pgvector 扩展迁移与三片业务表迁移，以及通过 Vercel AI Gateway 调用 Jev 的开发期判断脚本。六服务本地切片已建立并完成一次真实启动验收（`deploy/compose/compose.yml` 编排 PostgreSQL + pgvector、Redis、worker、api、inference 与 frontend-gateway；`up -d --build --wait` 后六容器均 healthy，第三方镜像按 digest 固定，只有 gateway 对宿主发布回环端口，api/inference/worker 不发布；前端网关同源代理 `/api/v1/health` 成功，SPA fallback、`/healthz`、assets immutable 与 index no-store、安全头、API 故障返回 502 均已核对；api 以 10001、inference 以 10002、gateway 以 101 非 root 运行；Linux Compose queue-probe 限定 service 仍以 0 退出；此前已验证的 initdb 角色/ACL、迁移、Redis 鉴权与 worker 执行 `rag_backend.probe`（当时旧入口 `evidencehub.probe`）保持通过）。推理切片已把固定 revision 的 BAAI/bge-small-zh-v1.5 配置、tokenizer 与 safetensors 在构建期下载并校验证后烘入镜像（核对 Hub commit、Git blob/LFS 摘要与脚本内钉死 SHA-256），运行期不联网、不挂宿主模型卷；已在无网络容器内实测 `/internal/embed` 对 `kind=document` 返回 512 维、L2 范数在 float32 舍入内为 1 的有限向量（uid 10002、`torch 2.14.0+cpu`、冷启动就绪约 5 秒，命令与数值见 [开发约定](docs/development.md)）。但内部 embedding 目前只接受 `kind=document`（`query` 返回 422）、rerank 未实现。第一片六张事实表（`index_profile`、`knowledge_base`、`document`、`document_version`、`ingest_job`、`outbox_event`）由迁移 `20260922_0002` 落地，第二片三张索引表（`index_generation`、`chunk`、`chunk_embedding` 含 `VECTOR(512)` 向量列）由迁移 `20260922_0003` 落地，均已在 PostgreSQL 17.11 + pgvector 0.8.6 专用测试库上通过真实迁移验收（`uv run pytest -m integration -q` 为 25 passed）；第三片 append-only 云 LLM 用量账本 `llm_usage` 由迁移 `20260923_0004` 落地，并新增一次性真实探针 `rag_backend.llm_probe`（api 仅 SELECT+INSERT，worker 无权限；探针默认关闭，需显式 opt-in 与独立密钥），其真实数据库验收已在隔离的专用测试库上通过（17 focused + 42 full integration passed，2 个 broker 用例因未配置 Redis 跳过）；一次性真实 DeepSeek 探针已执行成功并以 `citemind_api` 回读一行 `SUCCEEDED`/`PROVIDER_REPORTED`（价目与费用为 NULL，详见 [开发约定](docs/development.md)）。`chunk_embedding VECTOR(512)` 列约束已用真实插入（512 维成功、非 512 维失败）验收，但向量检索、ANN 索引、授权过滤与问答仍未实现。登录、会话与 KB 成员授权已在开发库经网关验收；Markdown 上传已由提交 `81ce084` 落地并在隔离 PostgreSQL/Redis 中验收，但尚未部署到当前开发栈，返回 202 仅表示文件与任务持久化、不可检索。dispatcher 与 worker 接收壳已在隔离 PostgreSQL/Redis/Celery 上通过真实投递与故障恢复（应用层故障注入来自 pytest 自动集成；物理 Redis 停启与 worker 被 kill 后补偿补投由仓库外隔离手工探针实测，不在 pytest 自动用例内），但仅在隔离测试中运行、未部署到当前 dev 栈；Markdown 纯解析与切分已实现但未落库；文档 blob 读取器（严格 ref/符号链接/摘要校验）、worker 本地真实 token 计数器（按固定 BGE tokenizer 文件大小+SHA256 校验后离线加载）、受限内部 embedding 客户端（同步 httpx/Bearer、无代理继承、无自动重试）与纯函数中文关键词分析器（固定 `jieba==0.42.1`、NFKC+casefold、词项流参数绑定 `to_tsvector('simple')`）已作为未接线前置能力实现，上传与 worker 仍未调用；worker 编码/索引发布、PDF、检索和问答仍未实现，Phase 1 未退出；Phase 0 工程底座退出条件已达成（唯一缺口「一次真实计费云 LLM 探针」已补上，但仅证明一次正常调用与 provider usage 事实落账，价目与费用未测）。文档中的资源预算与质量指标仍是待测目标。实际命令与已验证范围见 [开发约定](docs/development.md)。

## 文档入口

- [docs/AGENTS.md](docs/AGENTS.md)：技术文档索引与写作规则。
- [docs/architecture.md](docs/architecture.md)：系统组成、已实现边界与计划中的数据流。
- [docs/roadmap.md](docs/roadmap.md)：分阶段的技术范围与验收条件。
- [AGENTS.md](AGENTS.md)：仓库级开发规则。

演示和测试资料应使用自制或许可明确的无敏感内容。使用云模型处理真实资料前，需确认片段允许外发。
