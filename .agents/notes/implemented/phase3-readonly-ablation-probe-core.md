# Phase 3 只读消融探针核心

## 决策

真实 A/B/C 探针先实现为依赖注入核心，不在生产 API 增加调试路由，也不让评估代码复制授权 SQL。

- A 单独执行授权 scope、查询编码和 vector candidate 路径，因此候选与时延都不是从 B 推算。
- B 直接复用 `search_authorized_chunks(..., reranker=None)` 的双路检索与 RRF。
- C 复用 B 候选，对 top-10 通过授权链加载正文，释放数据库事务后再调用 reranker。
- 生产检索与探针共用 `embed_query_for_scope` 和 `apply_rerank_scores`，固定向量维度校验、错误分类以及重排排序规则。

## 授权与来源

题目按 `scope.role` 解析独立 `ProbeIdentity(user_id, organization_id)`。A/B 候选都必须再次通过 `load_evidence_chunks` 的授权链取得 `EvidenceChunkRow`，并与资产登记表中的逻辑 KB、文档和版本完全一致后才能生成 locator；评估 mapper 不得凭候选 UUID 猜测来源。

只有 `category=no_permission` 且预期行为为 `refuse` 的题可把 `KnowledgeBaseNotAccessible` 记录为三组空候选。其他题的授权失败属于运行错误。只有明确的 `RerankUnavailableError` 可以让 C 原样回退 B 并记录 `rerank_unavailable`；正文缺失、重复、版本漂移或非法分数集合都整体失败。

## 计量口径

- A 延迟：A 自身 scope、编码、vector SQL 和授权来源映射的墙钟时间。
- B 延迟：生产双路检索、RRF 和授权来源映射的墙钟时间。
- C 延迟：B 延迟加授权正文加载与 reranker 的额外时间。
- 拒答标定：固定使用 B 最终 rank-1 的 `fusionScore`；无候选时为空。`actualBehavior` 不由本核心产生。
- embedding 与 rerank 在每次实际客户端调用前消耗显式硬预算。

## 当前边界

本片只有内存编排与 fake 测试，没有真实 PostgreSQL/inference adapter、角色身份解析 CLI、dry-run 护栏或 JSON 落盘，因此尚不可独立运行，也没有真实 Recall@10、nDCG@10、阈值或时延结果。初版核心的 8 个 fake 聚焦测试通过；静态核对后的多角色、授权 locator 与异常边界补强未再次执行测试。
