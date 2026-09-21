# 知据 CiteMind

知据 CiteMind 是一个规划中的企业知识库应用。它面向需要查阅内部制度、产品说明和技术文档的小团队，让用户用自然语言提问，并查看回答依据对应的原文与版本。知识管理员负责导入和更新资料、分配访问权限，以及检查检索与引用质量。

项目重点是让回答有可核对的来源，并让文档权限、更新和删除在检索与历史访问中持续生效。资料不足时，系统应明确拒答；对模型输出的引用仍需验证其是否真正支持回答。

## 当前进度

仓库已进入 Phase 0 工程底座建设：当前有锁定依赖的 FastAPI 健康检查与 OpenAPI、Vue 3/Vite/TypeScript 前端骨架，以及通过 Vercel AI Gateway 调用 Jev 的开发期判断脚本。数据库、迁移、Compose、worker、inference、登录授权、入库、检索和问答均尚未实现；文档中的资源预算与质量指标仍是待测目标。实际命令与已验证范围见 [开发约定](docs/development.md)。

## 文档入口

- [docs/AGENTS.md](docs/AGENTS.md)：技术文档索引与写作规则。
- [docs/architecture.md](docs/architecture.md)：计划中的系统组成和主要数据流。
- [docs/roadmap.md](docs/roadmap.md)：分阶段的技术范围与验收条件。
- [AGENTS.md](AGENTS.md)：仓库级开发规则。

演示和测试资料应使用自制或许可明确的无敏感内容。使用云模型处理真实资料前，需确认片段允许外发。
