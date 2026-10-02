"""Phase 1 检索切片：授权混合检索、中文关键词分析、受限查询编码与可降级重排。

本包提供四部分能力：

- 纯本地关键词分析：固定版本的 jieba 私有 ``Tokenizer``、包内版本化领域词典与固定的
  NFKC + casefold 规范化规则；不连接 PostgreSQL、不注册 Celery 任务。
- 受限查询编码客户端：只把原始查询文本发给内部 inference（前缀由服务端追加），严格校验
  响应契约、向量与 revision，自身零自动重试。
- 授权混合检索：``POST /api/v1/retrieval/search`` 用同一授权 JOIN 跑 pgvector exact 与
  ``plainto_tsquery('simple', :query_terms)`` 关键词两路（全词项匹配），各取 top-20，再以
  RRF（k=60）按 ``chunkId`` 去重融合，最多 40 个候选。查询编码期间不持有数据库连接。
- 可降级重排：``rerank_client.RerankClient`` 只在调用方显式提供时对融合后前 10 个候选调用
  内部 inference 重排，失败整体保留原 RRF 顺序并标记 ``degraded_stages=('rerank_unavailable',)``；
  默认关闭，真实模型质量与延迟未验收。

本包仍不实现 LLM 生成、多轮、相似度阈值与更新/删除。
"""
