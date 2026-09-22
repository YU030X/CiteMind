# 知据 CiteMind

知据 CiteMind 是一个规划中的企业知识库应用。它面向需要查阅内部制度、产品说明和技术文档的小团队，让用户用自然语言提问，并查看回答依据对应的原文与版本。知识管理员负责导入和更新资料、分配访问权限，以及检查检索与引用质量。

项目重点是让回答有可核对的来源，并让文档权限、更新和删除在检索与历史访问中持续生效。资料不足时，系统应明确拒答；对模型输出的引用仍需验证其是否真正支持回答。

## 当前进度

仓库已进入 Phase 0 工程底座建设：当前有锁定依赖的 FastAPI 健康检查与 OpenAPI、Vue 3/Vite/TypeScript 前端骨架、SQLAlchemy 异步数据库会话、Alembic 的 pgvector 扩展迁移与两片业务表迁移，以及通过 Vercel AI Gateway 调用 Jev 的开发期判断脚本。六服务本地切片已建立并完成一次真实启动验收（`deploy/compose/compose.yml` 编排 PostgreSQL + pgvector、Redis、worker、api、inference 与 frontend-gateway；`up -d --build --wait` 后六容器均 healthy，第三方镜像按 digest 固定，只有 gateway 对宿主发布回环端口，api/inference/worker 不发布；前端网关同源代理 `/api/v1/health` 成功，SPA fallback、`/healthz`、assets immutable 与 index no-store、安全头、API 故障返回 502 均已核对；api 以 10001、inference 以 10002、gateway 以 101 非 root 运行，inference 镜像无 torch，`/capabilities` 报告 embedding.ready=false、受保护 `/internal/embed` 在正确令牌下 503 且无 vectors；Linux Compose queue-probe 限定 service 仍以 0 退出；此前已验证的 initdb 角色/ACL、迁移、Redis 鉴权与 worker 执行 `evidencehub.probe` 保持通过）。但 inference 目前只有进程/能力边界：真实 embedding、worker 写入事务、dispatcher、登录授权、入库、检索和问答均尚未实现。第一片六张事实表（`index_profile`、`knowledge_base`、`document`、`document_version`、`ingest_job`、`outbox_event`）由迁移 `20260922_0002` 落地，第二片三张索引表（`index_generation`、`chunk`、`chunk_embedding` 含 `VECTOR(512)` 向量列）由迁移 `20260922_0003` 落地，均已在 PostgreSQL 17.11 + pgvector 0.8.6 专用测试库上通过真实迁移验收（`uv run pytest -m integration -q` 为 25 passed）；`chunk_embedding VECTOR(512)` 列约束已用真实插入（512 维成功、非 512 维失败）验收，但向量检索、ANN 索引、授权过滤与问答仍未实现，worker 写入事务和跨表来源一致性也尚未落地。文档中的资源预算与质量指标仍是待测目标。实际命令与已验证范围见 [开发约定](docs/development.md)。

## 文档入口

- [docs/AGENTS.md](docs/AGENTS.md)：技术文档索引与写作规则。
- [docs/architecture.md](docs/architecture.md)：计划中的系统组成和主要数据流。
- [docs/roadmap.md](docs/roadmap.md)：分阶段的技术范围与验收条件。
- [AGENTS.md](AGENTS.md)：仓库级开发规则。

演示和测试资料应使用自制或许可明确的无敏感内容。使用云模型处理真实资料前，需确认片段允许外发。
