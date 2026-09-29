# Agent Note：Phase 3 成本片 runner 逐题原始 usage 产物

- 状态：已实现（`evaluation/usage_artifact.py`、runner `--usage-out`、adapter 只读查询、聚焦单测）；真实 PostgreSQL 查询、真实 HTTP `queryRunId` 与真实 `--usage-out` 运行本轮未执行
- 范围：`backend/src/rag_backend/evaluation/usage_artifact.py`、`runner.py`、`runner_adapters.py`、`tests/unit/test_evaluation_usage_artifact.py`、`tests/unit/test_evaluation_runner.py`

## 背景

成本片第 1 提交（迁移 `20260929_0016`）让同轮 `qa_rewrite`/`qa_answer` attempt 共用调用前生成的 `query_run_id`，但还没有任何按运行归因的产物。runner 需要把一次评估里每题每次 ask 对应的 provider 账本事实固定下来，供后续成本片消费，同时**不伪造**拿不到的事实。

## 决策

1. **产物只描述 runner 已拿到 `queryRunId` 的 ask。** 每次成功 HTTP ask 都会写一条 `UsageRun`（`questionId`/`conversationId`/`queryRunId`/`turnIndex`/`isFinalQuestion`），即使账本里暂时没有 attempt（`usage=[]`）也保留该 run。
2. **token 缺失保持空值。** `UsageAttempt` 的四个 token 字段可空，`usage_source` 未报告时写 `null`；`totals` 同时给 `knownSum` 与 `missingCount`，不把缺失当 0。`latencyMs` 由客户端每次尝试必写，故在产物中必填。
3. **账本事实必须无歧义。** 构建时账本行的 `query_run_id` 必须落在已知 run 上；`usageId` 不得重复；同一 run 内允许多条 `(stage, attempt)` 相同的独立 HTTP 调用，并按 `createdAt`/`usageId` 稳定排序。否则静态失败，不静默丢弃。stage 只允许 `qa_rewrite`/`qa_answer`，status 只允许 `SUCCEEDED`/`FAILED`/`TIMEOUT`。
4. **完整性与 final 约束。** 重复 `queryRunId`、每题重复 `turnIndex` 一律拒绝；`complete=true` 时每题必须恰有一个 `isFinalQuestion`，失败 partial（`complete=false`）允许 0 个 final。
5. **最终 ask 失败不猜测归因。** 当前 HTTP 错误响应不返回 `queryRunId`，因此最终 ask 失败时该次 provider attempt 无法无歧义归属。产物只用 `complete=false` 与 diagnostics 表明不完整，**严禁**按 `provider+model+stage+created_at` 时间窗口猜测。setup（历史轮）成功但 final 失败时，setup 的 run 仍输出。
6. **原子落盘、不覆盖。** `--usage-out` 先写同目录唯一临时文件再 `os.replace`；拒绝与 `--results-out`/`--diagnostics-out` 同路径，拒绝覆盖已存在文件。构建或查询失败时静态退出，不写半文件。完整运行先写 usage 再写 results；不完整运行也写 `usage complete=false` 后再返回 1，但绝不写 results。
7. **只读、复用同一 engine。** `SqlEvaluationDatabase.usage_attempts_for` 用参数化 expanding `SELECT` 按 `query_run_id` 读账本，空输入不查询，不写库、不建表、不新增授权。**目标库需已部署迁移 `20260929_0016`**，否则查询会失败。
8. **results 契约不变。** `results.json` 的字段与字节契约保持不变；`AskOutcome` 新增必填 `query_run_id` 不影响 results schema。

## 后果与边界

- 本提交不做价格/费用/汇率、不做 `Decimal` 成本、不做 `conversation_id` 列、不改 API/schema/migration/grant/results metrics。
- 产物不保证覆盖运行中全部 provider attempt：final ask 失败的 attempt 缺失由 `complete=false` 体现，不能当作完整成本分母。
- 真实 PostgreSQL 只读查询、真实 HTTP `queryRunId` 解析、真实 runner `--usage-out`、Docker 与全量 pytest/mypy 本轮未运行；单测全部为合成 fake，不代表真实链路验收。
