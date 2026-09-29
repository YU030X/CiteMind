# Agent Note：Phase 3 逐题质量失败诊断（不改聚合口径）

- 状态：已实现（`evaluation/metrics.py` 的 `QuestionAssessment`/`assess_questions`、`evaluation/__main__.py` 的输出追加、`tests/unit/test_evaluation_dataset.py`、`tests/unit/test_evaluation_holdout.py`）；真实 100 题（开发 + 留出）未运行
- 范围：`backend/src/rag_backend/evaluation/metrics.py`、`backend/src/rag_backend/evaluation/__main__.py`、`tests/unit/test_evaluation_dataset.py`、`tests/unit/test_evaluation_holdout.py`

## 背景

离线 `--results` 只给出聚合指标（`refusalAccuracy`/`falseRefusalRate`/`citationSourceValidity`/`goldSourceCoverage`/`permissionLeakCount`/冲突与注入三项）。聚合数字能说明“错了多少”，但不能说明“哪些题、因为哪种结构原因错了”。留出集验收要求“按固定分母报告…逐题失败”，而重新写一套判断逻辑会与聚合口径漂移，产生两套互相对不上的结论。

## 决策

1. **新增冻结结构，不加聚合字段。** 新增不可变 `QuestionAssessment(questionId→question_id, expectedBehavior, actualBehavior, failed, failureReasons)` 与 `assess_questions(...)`，按题集顺序返回全部题。`MetricsReport` 与其字段、`Results.json` schema、`EvaluationResults`/`QuestionResult` 均不变。`failed` 严格等于 `failure_reasons` 非空。
2. **单一输入对齐与单一判断路径。** 提取私有 `_validated_result_index`，让 `compute_metrics` 与 `assess_questions` 共用同一覆盖校验与元数据校验；提取 `_gold_keys`/`_citation_keys` 并让聚合的引用有效性与 gold 覆盖复用同一集合运算。未改动 `compute_metrics` 的任何输出。
3. **原因集与顺序固定为常量。** `FailureReason` Literal 与 `FAILURE_REASON_ORDER` 固定为：`false_refusal`、`missed_refusal`、`citation_outside_gold`、`incomplete_gold_coverage`、`permission_leak`、`conflict_unresolved`、`injection_leak`、`injection_unresisted`。输出按该顺序过滤，不随实现判断顺序变化；一道题可以同时命中多个原因。
4. **谓词严格复用聚合。** 引用越界/覆盖缺失复用聚合的 (KB、文档、版本) 集合口径，且只对 `expectedBehavior=answer` 且实际作答的题判断；`permission_leak` 复用 `_answer_leaks` 且只适用于 `expectedBehavior=refuse`（与聚合 `permissionLeakCount` 的遍历集合一致）；冲突与注入分别按 `evidence_conflict`/`prompt_injection` 标签复用 `_conflict_resolved`/`_injection_leaked`/`_injection_resisted`。因此诊断不扩大分母、不改变任何聚合值。
5. **CLI 追加确定性输出。** `python -m rag_backend.evaluation --results ...` 在既有聚合行之后追加 `failedQuestionIds=`（题集顺序；零失败为 `none`）与每个失败题一行 `questionId: reason1,reason2`。只输出题 id 与原因常量，不输出题面、答案或引用原文。未提供 `--results` 时行为与退出码不变；合法结果即使有质量失败仍退出 0。

## 后果与边界

- 这是**结构失败原因诊断，不是语义正确性判定**：`citationSourceValidity` 只验证引用落在本题固定版本 gold，`goldSourceCoverage` 只验证集合覆盖；它们都不判断答案文本是否真的被引用支持，也不替代句子级引用支持率与人工审核。
- 原因集边界：`permission_leak` 只覆盖回答正文里逐行出现不可访问文档实质行的情况，沿用既有启发式；`injection_leak` 只看 canary 是否出现在回答正文；`injection_unresisted` 不等于语义安全证明。
- 聚合值与 results.json schema 不变；runner diagnostics、analysis 排序失败与任何生产行为均不改。
- 测试用合成结果只证明分支；留出集的冲突/注入原因用合成结果构造，不代表真实分母。真实 100 题（开发 + 留出）未运行，因此没有任何真实逐题失败分布结论。
