# Agent Note：Phase 3 成本片固定价目快照与纯离线 Decimal 成本复算

- 状态：已实现（`evaluation/costs.py`、`tests/evaluation/pricing/deepseek-flash-usd-2026-09-29.json`、`UsageAttempt.provider`、聚焦单测）；真实 100 题成本、真实账本回读与 Docker 本轮未运行
- 范围：`backend/src/rag_backend/evaluation/costs.py`、`usage_artifact.py`、`runner_adapters.py`、`tests/evaluation/pricing/`、`tests/unit/test_evaluation_costs.py`、`tests/unit/test_evaluation_usage_artifact.py`、`tests/unit/test_evaluation_runner.py`

## 背景

成本片第 2 提交已能输出逐题原始 usage 产物，但账本行的 provider 事实此前没有进入 `UsageAttempt`/`UsageAttemptRow`/只读 `SELECT`，价目与费用也完全缺失。要按运行复算费用，需要一份固定来源的价目快照和一条不依赖网络/时间推断的纯离线计算路径。DeepSeek 的单价随 band（`peak`/`offPeak`）变化，band 又依赖 UTC 工作日窗口与中国法定节假日，后者无法离线确定性判定。

## 决策

1. **价目快照只记录项目核对时点，不伪造 effective date。** `snapshotVersion=citemind-price-1`、官方来源 URL、`observedAt` 只表示项目在 2026-09-29 核对页面的事实，`modelVersion=DeepSeek-V4.1-Flash`；页面未给出 effective date，快照就不写。`selectionRule` 以结构记录 UTC 工作日高峰窗口、中国法定节假日例外与 `defaultBand=offPeak`，但**不据此自动选 band**。
2. **band 由 operator 显式传入。** `build_cost_artifact(usage, snapshot, band)` 与 CLI `--band peak|offPeak` 都要求显式值；节假日与高峰窗口不能离线自动判定，任何自动推断都会撒谎。
3. **provider 是账本事实，先补齐再使用。** `UsageAttempt`/`UsageAttemptRow` 与只读 `SELECT` 增加 `provider`，`_to_attempt` 透传；不假设“来源一定是 DeepSeek”。成本计算只在 attempt 的 provider/model 与快照精确匹配时才进行，否则 `costAmount=None` 且 `reason=PROVIDER_MISMATCH`/`MODEL_MISMATCH`。
4. **失败与缺 token 记未知，绝不记 0。** 只有 `status=SUCCEEDED` 且 cacheHit/cacheMiss/completion 三者都非空才计算；失败/超时记 `NOT_SUCCEEDED`，任一必需 token 缺失记 `MISSING_TOKENS`，费用为 `None`。`promptTokens` 不进入公式，也不假设等于 hit+miss。
5. **公式与舍入固定。** `(hit*rateHit + miss*rateMiss + completion*rateOut) / perTokens`，单项统一 `quantize(0.00000001, ROUND_HALF_UP)`；汇总 `knownCostAmount` 先对可计算 attempt 的原始 `Decimal` 求和、再统一 quantize，避免逐项舍入误差。金额对外 JSON 用固定 8 位小数字符串，避免 `Decimal` 科学计数法与 float。
6. **产物保留完整身份、可复算。** `CostArtifact` 内含完整 `priceSnapshot`、`selectedBand`、`currency` 与逐 run/逐 attempt 的 token 事实、`costAmount`、`reason`，并给出 `totals`、`finalQuestionOnly` 与 `perQuestion` 三组同结构汇总。
7. **CLI 纯离线、原子、不可覆盖。** `python -m rag_backend.evaluation.costs --usage ... --price-snapshot ... --band ... --out ...` 不读环境/DB/网络；`--price-snapshot` **没有默认路径**，强制显式传入以避免误用旧价；`out` 已存在则拒绝覆盖，先写唯一临时文件再 `os.replace`；输入非法或快照来源/provider/model 不匹配时静态失败且不产生文件，错误消息不打印 traceback。

## 后果与边界

- 这是**估算快照复算，不是账单**：不覆盖真实 provider 的折扣、赠送额度、阶梯价或结算差异；provider 可能随时改价，快照只固定核对时点的事实。
- 只支持 USD 单币种，不做汇率换算；`llm_usage` 的价目/费用列与 results.json 契约不变，离线产物也不 `UPDATE` 账本。
- 未知费用的 attempt 在 `totals.knownCostAmount` 中不计入已知金额，`unknownCostAttemptCount` 单独计数；不能把 `knownCostAmount` 当作全部 attempt 的实际成本。
- 真实 100 题成本、真实 PostgreSQL 只读回读、真实 runner `--usage-out`、Docker 与全量 pytest/mypy 本轮未运行；单测全部为合成 fake，不代表真实账单。
