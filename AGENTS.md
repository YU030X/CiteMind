# AGENTS.md

本文件只放每次开发都需要遵守的仓库级规则。项目介绍见 [README.md](README.md)；文档归属与写作规则见 [docs/AGENTS.md](docs/AGENTS.md)。

## 阅读入口

- 改动进程职责、数据流、持久化或生命周期前读 [架构设计](docs/architecture.md)，再从 [文档索引](docs/AGENTS.md) 找到负责该主题的文档。
- 构建、测试或提交前读 [开发约定](docs/development.md)；涉及阶段范围时核对 [技术实施顺序](docs/roadmap.md)。
- 区分计划、已实现行为和实测结果；不要把计划接口、命令、性能目标或验收标准写成已实现事实。

## 实现规则

- 做解决问题所需的最小一致改动；保持实现、公开接口、测试和对应文档同步，不顺手改写无关代码。
- 授权、引用、版本发布与任务恢复的具体约束分别由 [安全](docs/security.md)、[检索](docs/retrieval.md)和[入库](docs/ingestion.md)文档负责；改动前先核对其所属规则。
- 新行为要有明确的责任方和资源生命周期；启动时校验可独立判断的无效配置，失败时返回明确状态，显式清理连接、进程和临时资源。
- 不提交凭据、令牌、生成密钥、本地环境文件、模型缓存或真实企业资料；样本使用自制或许可明确的无敏感数据。

## 验证与记录

- 先运行与改动对应的聚焦检查；跨进程任务、权限、数据库和模型行为按 [开发约定](docs/development.md)与[评估计划](docs/evaluation.md)进行真实环境验收。只报告实际运行的命令与结果。
- 修改技术契约时更新其归属文档。长期有效的决策理由单独记录为 Agent Note；局部机械修改不需要记录，不把历史推理写进当前行为文档。
- 给用户的 shell 命令保持单行。实际入口建成后才记录可执行命令，不保留占位命令。

## Jev Decision Layer

Use Jev through the available Jev MCP tools as a lightweight semantic decision layer inside the workflow.

Jev provides bounded semantic judgments only. It does not own execution, replace deterministic application logic, or replace the primary reasoning agent.

### When to use Jev

Use Jev when the workflow needs a small, structured semantic judgment, such as:

- routing a task to the appropriate handler or subagent;
- deciding whether a semantic condition holds;
- choosing one action from a bounded set;
- scoring, ranking, or comparing candidates along a defined dimension;
- checking whether supplied evidence supports a claim;
- reviewing whether an implementation satisfies stated requirements;
- deciding whether a task should continue, retry, escalate, or stop.

Prefer the Jev MCP tool that most closely matches the judgment:

- `jev_verify` for checking a claim against supplied evidence;
- `jev_decide` for choosing between a small set of explicit alternatives;
- `jev_compare` or `jev_rerank` for comparing or ordering candidates;
- `jev_classify` for assigning an item to a predefined category;
- `jev_review` for structured review of an implementation or result;
- `jev_gate` for completion, continuation, retry, or escalation decisions.

Do not use Jev for deterministic rules, exact calculations, known lookups, filesystem operations, shell execution, code generation, open-ended research, or tasks that ordinary code or the primary reasoning agent can handle reliably.

Treat Jev output as decision evidence rather than authority. If the result is low-confidence, ambiguous, or conflicts with deterministic evidence, gather more evidence or continue reasoning instead of blindly following it.
