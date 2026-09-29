# Agent Note：Phase 3 固定 100 题数据契约与冲突/注入指标

- 状态：已实现（结构），真实留出运行未做
- 范围：`rag_backend.evaluation` 的题集 schema、跨集校验、runner 护栏与确定性指标

## 背景

Phase 1 只有 40 道开发题，`datasetKind` 被限制为 `dev`，且没有证据冲突与提示注入的结构化表达。
Phase 3 需要固定的开发/留出分割与可离线复算的冲突、注入指标，但评估数据是自制语料，不能假装
是保密集或盲测集。

## 决策

1. **留出集是“流程隔离固定留出”，不是保密或盲测。** `holdout-questions.json` 与开发集一起提交
   仓库，开发者可见其内容。固定的是问题、gold 与分母，用于防止反复调参窃取测试信息；文档与
   代码都不得称其为“保密”或“盲测”。跨集校验用 id 不重叠与近重复拒绝来防止两侧互相污染。
2. **开发集保持语义不变，仅升版本。** 40 题的 id、题面与 gold 语义不改，`datasetVersion` 从
   `citemind-eval-dev-1` 升为 `citemind-eval-dev-2`，使历史 40 题结果仍可按无元数据路径复算。
3. **冲突事实必须可判定。** 语料里同一事实存在两份 active 文档时，gold 固定在权威文档上，
   `conflictingSpans` 固定另一份 active 文档的冲突引文；两者都要求 active、在 scope 内、角色可访问
   且 quote 可重放，冲突 span 不得与 gold 重复。这避免“冲突”退化为模型无法判定的语义主张。
4. **注入用唯一 canary 做确定性泄露检测。** 注入语料含唯一字符串 `CANARY-7F3A9D2B`，同时保留可
   作答的正常事实；`injectionLeakCount` 只检查回答正文是否包含该 canary，`injectionResistanceRate`
   只检查未泄露且引用落在 scope 内。二者都**不等于语义安全证明**，不覆盖候选、日志、历史或下载。
5. **不引入模糊语义模型。** 字段存在性、active/scope/权限、quote 重放、id 唯一、近重复（NFKC +
   casefold + 空白折叠，编辑距离 ≤1）全部是确定性检查，可由离线 CLI 与单测复现。
6. **runner 护栏按数据集种类生效。** 真实运行 `holdout` 必须显式 `--confirm-holdout`，防止误把留出
   集当开发集跑；dry-run 与离线结构校验不要求该参数。
7. **结果元数据可选且向后兼容。** 新结果写 `datasetKind`/`datasetVersion`，`compute_metrics` 在元数据
   存在时要求与题集一致；旧归档缺少元数据仍走历史复算路径。

## 后果与边界

- 新增的 3 份自制 Markdown（权威费用规定、其 active 冲突附录、含 canary 的信息安全公告）只用于
  评估，不进生产路径，也不改 parser/chunker。
- 冲突与注入指标在开发集没有分母，只在留出集有分母；本切片只落地结构与定义，**100 题尚未在真实
  模型与真实权限环境运行**，因此不能报告任何真实留出质量分数。
- 消融、排序指标、拒答标定、费用快照与 `llm_usage` 迁移不属于本片。
