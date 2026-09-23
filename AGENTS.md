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
- 准备提交前先加载仓库内的 Git 工作流 skill（[.agents/skills/git-workflow/SKILL.md](.agents/skills/git-workflow/SKILL.md)），按其提交信息格式、提交粒度与提交前检查执行。

## Agent orchestration

Use `pi-herdr-subagents` as the default execution layer for substantial repository work.

The primary agent is the coordinator, reviewer, and final integrator. It should not normally perform substantial repository exploration, implementation, debugging, testing, or open-ended research directly. Delegate that work to named subagents and use the primary context for decomposition, supervision, review, reconciliation, and the final user-facing result.

The primary agent may perform small read-only checks when needed to audit a result or resolve coordination state, but it should not take over work that can reasonably be delegated.

### Herdr execution model

use deepseek model

Create subagents through the `subagent` tool provided by `pi-herdr-subagents`.

- Each subagent must run in its own **new Herdr tab**. Do not create subagents by splitting the current pane.
- Use one primary responsibility per subagent and provide a bounded objective, relevant context, ownership boundaries, and expected output.
- Run independent tasks in parallel when their file ownership and dependencies do not conflict.
- Do not allow multiple implementation agents to edit the same files concurrently unless ownership is explicitly partitioned.
- Prefer named project-local agents from `.pi/agents/` so role defaults and safety constraints are applied consistently.

A delegated task remains part of the parent task until it reaches a terminal outcome and its result has been reviewed. The primary agent may become idle while subagents run; it should not busy-poll them. Completion, failure, stall/recovery, and `caller_ping` notifications from `pi-herdr-subagents` should wake the parent when action is required.

Do not send the final user-facing completion message while required delegated work is still active, waiting for review, or unresolved, unless the user explicitly cancels or narrows that work.

### Role routing

Use the narrowest role that matches the work:

- `scout`: fast, read-only repository reconnaissance and dependency/control-flow mapping.
- `researcher`: read-only technical investigation, evidence gathering, and alternative analysis.
- `worker`: bounded implementation or refactoring; may edit files and run focused verification.
- `tester`: reproduction, builds, tests, runtime checks, and validation; avoid unrelated implementation changes.
- `reviewer`: independent read-only review of changes, regressions, requirements, and evidence.
- `jev-decider`: bounded semantic decisions through Jev MCP; never implement repository changes.

The `plan-scout`, `plan-researcher`, and `plan-reviewer` roles are reserved for planning sessions and remain read-only.

### Parent responsibilities

For substantial work, the primary agent should:

1. Decompose the request into independent work units and identify dependencies.
2. Spawn the required named subagents in separate Herdr tabs.
3. Continue coordinating work that is not blocked on an outstanding result.
4. Review each returned result instead of accepting subagent claims automatically.
5. Re-dispatch missing implementation, verification, or investigation work to the appropriate subagent rather than silently completing it in the primary context.
6. Reconcile conflicting findings and confirm the repository is in a coherent final state.
7. Report only actions, commands, tests, and results that were actually executed or observed.

If a child uses `caller_ping`, resolve deterministic questions directly from evidence. For bounded semantic questions, consult the dedicated `jev-decider` session, then use `subagent_resume` to return the decision and relevant evidence to the blocked child.

## Jev decision subagent

Use a dedicated `jev-decider` subagent as the repository's semantic decision layer.

Maintain one logical Jev decision session for the current task or decision domain. Create it when semantic judgment is first needed, then prefer `subagent_resume` for follow-up decisions so the decision context remains coherent. Do not create competing Jev decision agents for the same decision stream unless the task intentionally requires independent judgments.

The `jev-decider` may use available Jev MCP tools for bounded judgments such as:

- routing a task to an appropriate handler or subagent;
- deciding whether a semantic condition holds;
- choosing among a small explicit set of alternatives;
- comparing or ordering candidates along a defined dimension;
- checking whether supplied evidence supports a claim;
- reviewing whether an implementation satisfies stated requirements;
- deciding whether work should continue, retry, escalate, or stop.

Prefer the Jev MCP operation that most closely matches the question, such as verify, decide, compare/rerank, classify, review, or gate when those operations are available.

Do not use Jev for deterministic rules, exact calculations, known lookups, filesystem operations, shell execution, code generation, or open-ended repository exploration. Supply Jev with the smallest sufficient evidence and an explicit bounded question.

Jev output is decision evidence, not authority. The primary agent must review the evidence and result before using it. If the result is low-confidence, ambiguous, or conflicts with deterministic evidence, gather more evidence or delegate further investigation rather than following it blindly.

The `jev-decider` must not edit repository files or perform implementation work. Implementation remains the responsibility of `worker`; verification remains the responsibility of `tester` and `reviewer`.
