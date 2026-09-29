# Agent Note：Phase 3 追问改写观测纯离线检查 CLI

- 状态：已实现（`evaluation/rewrite_inspect.py`、`python -m rag_backend.evaluation.rewrite_inspect`、聚焦单测）；真实 runner `--rewrite-out` 产物与真实留出集改写观测本轮未运行
- 范围：`backend/src/rag_backend/evaluation/rewrite_inspect.py`、`tests/unit/test_evaluation_rewrite_inspect.py`、`docs/evaluation.md`、`docs/development.md`、`docs/roadmap.md`

## 背景

runner 已能把已捕获 ask 的权威追问改写文本写成 `RunnerRewriteArtifact`（`question`/`standaloneQuestion` 只读回读 `query_run`），但产物本身不含任何消费侧检查：最终 run 是否恰好覆盖题集、partial 缺了哪些题、改写与题集参考的重合情况如何，都需要一个**只读、纯离线、不泄露题面**的入口。本片补上这一层，同时避免把它误当成质量评分。

## 决策

1. **只读题集与产物，不产生任何新事实。** `rewrite_inspect.py` 只用现有严格 `EvaluationDataset` 与 `RunnerRewriteArtifact`；不联网、不读环境文件、不连数据库、不调用模型、不写文件。`datasetKind`/`datasetVersion` 必须一致，漂移静态失败。
2. **final 覆盖按题集交叉校验，不只依赖 artifact schema。** `complete=true` 时 final run 的 `questionId` 集合必须恰好覆盖题集全部 id：缺任一 final、出现未知 id、或同一题多个 final 都静态失败。`complete=false` 时允许 final 缺失，但未知 `questionId` 仍然禁止。输出 `observedFinal`/`expectedFinal`/`missingFinal` 与 `partial` 标志，绝不把 partial 当完整。
3. **单一参考重合观察，三类互斥。** 只对题集中带 `standaloneQuestion` 且已观察到 final 的题，比较 final run 的 `standaloneQuestion` 与题集参考：`exactMatch`（逐字相等）、`normalizedMatch`（非逐字，但 NFKC + casefold + 所有连续 whitespace 折叠为单空格 + strip 后相等）、`different`。`normalizedMatch` 明确是“非 exact 但规范化后相等”，保证三类互斥。分母固定是“有参考且已观察到 final”的题数；无分母时输出 `None/0`。
4. **不做质量结论。** 题集只有一个 gold `standaloneQuestion`，它不是唯一正确表达；重合计数不衡量改写是否更优。不做 token 相似度、编辑距离、阈值、pass/fail 或质量等级。真实语义需要人工或另立授权成本的裁判。
5. **不泄露用户文本。** stdout/stderr 只打印题 id 与静态计数，绝不打印 `question`/`standaloneQuestion` 原文。`different` 题 id 单独列出供人工复核。artifact schema 校验失败时打印固定的静态中文消息，不转发可能带原文的 pydantic `ValidationError` 详情。
6. **错误处理与退出码。** 非法 schema/元数据/覆盖一律退出 1，输出静态中文错误、不打印 traceback；成功 stdout 确定性。只读、不写任何文件/环境/DB/网络。

## 后果与边界

- 本片未运行真实 runner `--rewrite-out` 产物，也未在真实留出集上产生任何重合计数；单测全部为合成题集与产物，不代表真实链路验收。
- `exactMatch`/`normalizedMatch`/`different` 只是字符串结构分类，不是语义改写质量度量，不能替代人工审核或另立授权成本的裁判。
- 不改 runner、API、数据库、迁移、`results.json`、`metrics.py` 或任何生产行为；不引入新的持久化产物。
