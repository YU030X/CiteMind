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
| [web-static-fetch](implemented/web-static-fetch.md) | 受限静态网页抓取、`locator_version=4` 与 DNS 竞态边界 |
