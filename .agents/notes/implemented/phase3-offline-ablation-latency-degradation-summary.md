# Agent Note：Phase 3 消融时延与降级纯离线汇总

- 状态：已实现（默认输出扩展，纯离线）；真实探针未运行
- 范围：`rag_backend.evaluation.analysis` 的默认 stdout、`tests/unit/test_evaluation_analysis.py` 与 `docs/evaluation.md`

## 背景

`analysis.py` 已能读取 A/B/C 三份严格消融产物并输出 Recall@10/nDCG@10 与标定摘要，但产物里已记录的
`latencyMs` 与 `degradedStages` 从未被汇总，真实探针跑完后无法从同一入口直接看到时延/降级轮廓。
本片只补这一层确定性汇总，仍不联网、不调用模型、不产生任何真实数值。

约束：不新增 flag、不写新文件、不改 ablation schema/probe/生产检索问答/数据库/迁移/成本/`results.json`；
缺省行为之外的既有 ranking/calibration 逻辑与错误处理不变。

## 决策

1. **默认输出追加，不做可选开关。** 每个变体在既有 ranking 行后追加**一行**时延/降级观察汇总；不新增
   CLI 参数，也不写任何产物文件。这是默认输出的有意扩展，属于本片明确预期的行为变化。
2. **nearest-rank，不插值。** `p50`/`p95` 定义为升序样本的第 `ceil(p*n)-1` 项（0-based）；`mean` 使用
   `math.fsum` 求和后除以题数，避免朴素求和的抵消误差。汇总至少报告 `questions`/`mean`/`p50`/`p95`/
   `max`，单位毫秒，输出固定 3 位小数的确定性格式（如 `mean=20.000`）。
3. **降级按题计数，无降级显式。** `degradedStages` 按 stage 统计**包含该 stage 的题数**（同题重复出现
   只计一次），按 stage 名升序输出为 `stage/题数`；无任何降级时显式输出 `none/0`，不沉默。
4. **不筛选、不丢题。** 汇总覆盖产物内全部题目，不按成功与否或是否有 gold 过滤；`latencyMs` 的有限
   非负已由 `AblationQuestion` schema 保证，本层不再重复校验，非法值仍由 schema 静态拒绝。
5. **纯函数 + 不可变 dataclass。** `summarize_latency`/`summarize_degraded_stages`/
   `summarize_artifact_observations` 与 frozen `LatencySummary`/`VariantObservationSummary` 便于单测，
   不引入 IO 或全局状态。

## 后果与边界

- 这是 **artifact 内观察值的确定性汇总，不是 2 vCPU/4 GB 性能验收**；真实 A/B/C 探针未运行时没有可
  汇总的真实数值，当前仓库也未包含这些产物，因此文档不写任何真实指标。
- `latencyMs` 的记录口径（A/B/C 各自墙钟边界、C 是否含重排时间）由探针负责，本汇总不重新测量、
  不推断，也不定义延迟达标结论。
- 默认输出新增一行是唯一对外可见变化；解析 `key=value` 的调用方需容忍该附加行，但既有行内容与退出码
  不变。
