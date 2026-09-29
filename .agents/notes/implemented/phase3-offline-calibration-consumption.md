# Agent Note：Phase 3 离线拒答标定消费链

- 状态：已实现（纯离线消费链）；真实探针未运行
- 范围：`rag_backend.evaluation.analysis` 的可选 `--calibration` / `--refusal-threshold` 与 `docs/evaluation.md`

## 背景

Phase 3 第 2 片已落地 `calibration.py` 的纯函数扫描与严格 `CalibrationArtifact`，第 4 片的探针 CLI
会把标定产物写到 `calibration.json`，但**没有任何入口消费它**：`analysis.py` 只读题集与 A/B/C 三份
消融产物。本片把标定产物接到 `analysis`，仍不联网、不调用模型、不产生真实数值。

约束：不改生产检索/问答、`metrics.py`、数据库、迁移、配置、`results.json` 或成本模块；不新增输出
文件 schema；缺省 `--calibration` 时旧行为与输出必须不变。

## 决策

1. **标定是 `analysis` 的可选增量。** 新增可选 `--calibration PATH`；缺省时即使没有标定文件也完全
   保持旧的 stdout 与退出码，只打印既有三元组与 Recall/nDCG 行。
2. **严格解析与恰好覆盖。** 直接复用第 4 片的严格 `CalibrationArtifact`；`datasetKind`/
   `datasetVersion` 必须与题集一致，`records.questionId` 集合必须恰好覆盖题集全部题目，缺题或未知
   题目 id 都静态失败。
3. **开发集只选点，禁止传阈值。** `datasetKind=dev` 时禁止 `--refusal-threshold`，只用
   `scan_refusal_thresholds` + `select_dev_threshold` 选点，并输出该点的阈值、`refusalAccuracy`、
   `falseRefusalRate`、`balancedAccuracy` 及各自分子/分母。没有可观测有限分数（或指标分母为空）
   导致 `select_dev_threshold` 返回 `None` 时静态失败，**不伪造一个阈值**。
4. **留出集只报告固定点。** `datasetKind=holdout` 时必须显式给出有限 `--refusal-threshold`，只用
   `evaluate_refusal_threshold` 报告该预先固定的点，不扫描、不选点，避免在留出集上偷偷调参；缺失阈值
   或非有限/非数值阈值一律静态失败。
5. **只加一行 stdout，不写文件。** 摘要保持既有 `key=value` 中文 CLI 风格；不新增任何产物文件
   schema，也不改变 `results.json`/成本产物。
6. **非法输入统一静态失败。** 未提供 `--calibration` 却传 `--refusal-threshold`、标定文件不可读/
   schema 非法、`CalibrationInputError` 等全部退出码 1、静态中文错误、不打印 traceback。

## 后果与边界

- 本片只交付**消费链**：没有真实探针运行，也没有任何真实标定数值；`dev` 选点与 `holdout` 固定点
  都只是在给定观察记录上的确定性计算，不代表真实检索/问答拒答质量。
- `dev` 选点依赖记录里存在可观测的有限 top score 且两类分母都非空；数据不足时明确失败而不是默认
  一个阈值，这是有意的保守边界。
- 留出集的“固定”语义由调用方负责：CLI 只要求显式给出阈值并只报告该点，无法证明该阈值确实在留出
  运行前已冻结，仍需流程纪律保证。
