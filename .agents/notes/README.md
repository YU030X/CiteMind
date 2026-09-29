# Agent Notes

长期有效的决策理由存放于此；当前技术契约仍留在 `docs/`。目录按状态分类：

- `proposed/`：尚未实施的方案取舍。
- `implemented/`：已实施的决策与理由。
- `rejected/`：已评估但未采用的方案。
- `archived/`：已被替代、仅保留历史。

## 已实施

| Note | 主题 |
| --- | --- |
| [bge-zh-query-v1](implemented/bge-zh-query-v1.md) | BGE 中文查询 instruction 前缀与 `bge-zh-query-v1` 命名契约 |
| [bge-reranker-base-degradable-rerank](implemented/bge-reranker-base-degradable-rerank.md) | bge-reranker-base 可降级重排、清单式离线身份与默认关闭 |
| [document-acl-read-narrowing](implemented/document-acl-read-narrowing.md) | 文档 ACL 只收紧读取、首次 DELETE 授权与下载鉴权时序 |
| [docx-minimal-subset](implemented/docx-minimal-subset.md) | DOCX 允许子集、`locator_version=3` 与 ZIP 安全限额 |
| [pdf-dual-engine-text-layer](implemented/pdf-dual-engine-text-layer.md) | PDF pypdf 预检 + pdfplumber 抽取的双引擎分工与诚实解析器版本 |
| [phase3-fixed-holdout-dataset-contract](implemented/phase3-fixed-holdout-dataset-contract.md) | Phase 3 固定 100 题数据契约、流程隔离留出与冲突/注入确定性指标 |
| [phase3-offline-ranking-calibration-ablation](implemented/phase3-offline-ranking-calibration-ablation.md) | Phase 3 第 2 片离线 Recall@10/nDCG@10、拒答阈值扫描与 A/B/C 消融产物契约 |
| [phase3-llm-usage-query-run-correlation](implemented/phase3-llm-usage-query-run-correlation.md) | Phase 3 成本片 `llm_usage.query_run_id` 调用前关联键、无外键与探针边界 |
| [phase3-runner-usage-artifact](implemented/phase3-runner-usage-artifact.md) | Phase 3 成本片 runner 逐题原始 usage 产物、只读归因与最终 ask 失败边界 |
| [phase3-runner-rewrite-artifact](implemented/phase3-runner-rewrite-artifact.md) | Phase 3 runner 追问改写观测产物、只读权威回读与不打语义质量分边界 |
| [phase3-rewrite-inspection-cli](implemented/phase3-rewrite-inspection-cli.md) | Phase 3 追问改写观测纯离线检查 CLI、final 覆盖与单一参考重合互斥计数边界 |
| [phase3-offline-cost-recompute](implemented/phase3-offline-cost-recompute.md) | Phase 3 成本片固定价目快照、显式 band 与纯离线 `Decimal` 成本复算边界 |
| [phase3-readonly-ablation-probe-core](implemented/phase3-readonly-ablation-probe-core.md) | Phase 3 第 3 片只读 A/B/C 探针核心、授权来源映射与降级边界 |
| [phase3-readonly-probe-cli-and-adapters](implemented/phase3-readonly-probe-cli-and-adapters.md) | Phase 3 第 4 片只读探针真实 adapter、DSN/身份/profile 护栏与 dry-run CLI、原子落盘 |
| [phase3-offline-calibration-consumption](implemented/phase3-offline-calibration-consumption.md) | Phase 3 离线拒答标定消费链：`analysis` 可选 `--calibration`、dev 选点与 holdout 固定点边界 |
| [phase3-offline-ablation-latency-degradation-summary](implemented/phase3-offline-ablation-latency-degradation-summary.md) | Phase 3 消融产物时延/降级默认汇总：nearest-rank p50/p95、`math.fsum` 均值与 stage 题数边界 |
| [phase3-per-question-failure-diagnosis](implemented/phase3-per-question-failure-diagnosis.md) | Phase 3 逐题质量失败诊断：固定原因顺序、复用聚合谓词与不改聚合口径边界 |
| [web-static-fetch](implemented/web-static-fetch.md) | 受限静态网页抓取、`locator_version=4` 与 DNS 竞态边界 |
