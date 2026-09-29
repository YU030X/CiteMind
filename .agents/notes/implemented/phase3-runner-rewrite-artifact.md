# Agent Note：Phase 3 runner 追问改写观测产物

- 状态：已实现（`evaluation/rewrite_artifact.py`、runner 可选 `--rewrite-out`、`SqlEvaluationDatabase.rewrite_rows_for` 只读查询、聚焦单测）；真实 PostgreSQL 只读回读与真实 `--rewrite-out` 运行本轮未执行
- 范围：`backend/src/rag_backend/evaluation/rewrite_artifact.py`、`runner.py`、`runner_adapters.py`、`tests/unit/test_evaluation_rewrite_artifact.py`、`tests/unit/test_evaluation_runner.py`

## 背景

runner 已经从成功 HTTP ask 拿到了 `queryRunId`，但追问改写是否真的发生、`standalone_question` 是什么，只存在于数据库 `query_run`。需要一份**只读观测**产物把已捕获 run 的权威改写文本固定下来，供后续分析消费，同时**不伪造**拿不到的事实，也不做语义质量评分。

## 决策

1. **只观测已捕获的 run。** 每个拿到 `queryRunId` 的 ask 都在产物中有一条 `RewriteRun`（`questionId`/`conversationId`/`queryRunId`/`turnIndex`/`isFinalQuestion`/`question`/`standaloneQuestion`），文本全部从 `query_run` 权威回读，不由 runner 编造。产物按 `(questionId, turnIndex)` 稳定排序。
2. **对齐必须无歧义。** captured `queryRunId` 与数据库返回的 `queryRunId` 都不允许重复；未知 row、缺失 row、同题重复 `turnIndex` 一律静态失败。**即使 `complete=false`，每个 captured run 也必须有权威行**：final ask 失败时 HTTP 错误响应不返回 `queryRunId`，本来就**没有** `AskRunRecord`，因此不会被猜测或按 `provider+model+stage+created_at` 时间窗口补进产物。
3. **成功拒答正常记录。** `REFUSED` 是成功响应，仍会写 `query_run` 行并返回 `queryRunId`，其 run 与其他成功 ask 一样进入产物。改写失败/回答失败而没有成功响应就没有 `AskRunRecord`，不按时间猜。
4. **首轮一致性与 strip。** `turnIndex=0` 时未发生改写，`standaloneQuestion` 必须等于 `question`；更晚轮次允许两者相等（模型判定问题已独立）。两个文本都必须非空，且 `standaloneQuestion` 必须已经 strip。
5. **只做结构一致性，不打质量分。** `question` 与 `standaloneQuestion` 字符串相等只被用作结构断言（首轮必须相等），**不等于**改写质量结论；本模块不判断改写是否更优，也不做 gold 字符串比对。
6. **只读、复用同一 engine。** `SqlEvaluationDatabase.rewrite_rows_for` 用参数化 expanding `SELECT id, question, standalone_question FROM query_run WHERE id IN :ids` 回读，空输入不查询；SQLAlchemy 错误收敛为静态 `RunnerError`，消息不含 SQL/UUID/文本。不写库、不建表、不迁移、不新增授权。
7. **原子落盘、不覆盖。** `--rewrite-out` 先写同目录唯一临时文件再 `os.replace`；拒绝与 `--results-out`/`--diagnostics-out`/`--usage-out` 同路径，拒绝覆盖已存在文件。查询或构建失败时静态退出，失败清理临时文件。
8. **完整或不完整都输出。** 在 `database.close()` 之前查询并写出；完整运行先写 usage 再写 rewrite 再写 results；不完整运行也写 `complete=false` 后返回 1，但绝不写 results。未提供 `--rewrite-out` 时旧行为逐字不变，`results.json`、`AskRunRecord` 与 API 均不改。
9. **文本敏感、隔离使用。** 产物包含用户生成的原始问题与改写文本，仅允许在隔离评估环境内使用；真实产物默认不提交仓库，与 `tests/evaluation/results/` 的脱敏归档口径一致。

## 后果与边界

- 产物不保证覆盖运行中全部改写：final ask 失败或其他未拿到 `queryRunId` 的 ask 缺失由 `complete=false` 体现。
- 本轮未运行真实 PostgreSQL 只读回读、真实 HTTP `queryRunId` 解析与真实 `--rewrite-out`；单测全部为合成 fake 与 fake engine 映射，不代表真实链路验收。
- 不做价格/费用、不做语义改写评分、不改 API/schema/migration/grant/results metrics。
