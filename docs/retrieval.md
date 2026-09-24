# 检索、问答与引用

> 本文大部分是待实现的技术契约；中文关键词分析器已实现为纯函数（尚未接数据库），MVP 使用精确向量检索与中文关键词检索，神经重排属于后续完整范围。

## 编码与关键词索引

计划使用固定 revision 的 `BAAI/bge-small-zh-v1.5`，512 维，余弦距离，CPU 推理。查询与文档须使用同一已登记的编码契约；换模型、维度或规范化设置时另建 index profile。MVP 在 PostgreSQL/pgvector 内对已授权小语料做 exact search；HNSW 只有与 exact 基线比较过滤后的召回和延迟后才引入。

中文关键词路已实现纯函数分析器 `rag_backend.retrieval.KeywordAnalyzer`（未接数据库）：固定 `jieba==0.42.1`，用私有 `jieba.Tokenizer` 的 `cut_for_search`，先做 NFKC + Unicode `casefold`，把英文标识、错误码、数字与下划线/连字符整段保留，过滤纯标点/空白/emoji，重复词项保留；原始输入与 NFKC 后长度都不得超过 `MAX_INPUT_CHARS=100000`，超限抛 `KeywordAnalyzerInputError`（与部署/资源类 `KeywordAnalyzerError` 区分）。分析器标识为精确形状 `jieba-0.42.1-search-v1:base-sha256=7197c321…12a8:v1-sha256=e3b0c442…7852b855:norm=NFKC+casefold`，同时钉死 jieba 自带 `dict.txt`（5,071,852 字节 + SHA-256）与领域词典（当前 v1 为 0 字节空词典，因无可授权真实术语，不编造企业词）。构造先在私有临时目录内初始化分词器、结束后立即清理，绝不读写共享 `temp/jieba.cache`（jieba 默认缓存对基础词典不做 mtime 校验，可被其他进程投毒）；**运行时必须能创建可写私有临时目录**（POSIX 0700、Windows 使用 per-user TEMP ACL），只读容器必须提供安全 tmpfs，否则静态 `KeywordAnalyzerError` fail closed，不为可用性回退共享缓存。文档与查询必须用同一分析器产出的词项流，再以参数绑定交给 PostgreSQL `to_tsvector('simple', ...)`，以 GIN 过滤和 `ts_rank_cd` 排序；**不是**把未切分的中文原句直接送 `simple`，也不能把词项流直接当 `tsquery`（含 `gpt-4` 这类连字符会语法错误），查询侧必须用绑定参数构造合法 OR/`plainto` 查询。真实 PG 17 实测（隔离库 `public` 零表、仅用 psycopg 绑定参数验证词项链路）：中文经 jieba 切分后与 `simple` 索引可互相匹配，未切分原句不匹配；`ts_rank_cd` 在合法 OR 查询下约 0.2、`plainto_tsquery` 下约 0.1；但 `simple` 会再切分标识——`error_code` 丢失下划线、结果近似 `error code`（两者不可区分），`gpt-4` 被拆成 `gpt` 与 `-4`，因此不声称精确标识检索。改动词典必须同时提升词典版本、文件名、SHA-256 与 profile，并新建 index profile 重索引，绝不能改变旧 profile 的含义；未来用 freq=0 新增词条会改全局 `jieba.finalseg.Force_Split_Words`，新词典版本必须单独做安全评估。词典 hash 是索引契约的一部分，`ts_rank_cd` 不是 BM25，原文引用仍用未归一化文本。本切片只产出词项流，未写 `chunk.fts`、未 seed KB profile、无检索 API，因此文档仍不可检索。

## 授权候选与融合

1. 会话生成服务端 `AuthContext`：组织、用户、可访问 KB 和文档权限。请求的 `kbIds` 必须是其中的子集。
2. 对当前独立问题编码。向量路与关键词路在 SQL 中 JOIN 同一授权、未删除、有效版本、READY generation 集合，各取 top-20；可用一条双路 CTE 或一个短 `REPEATABLE READ` 事务顺序查询，不能并行共享 AsyncSession。
3. 用 RRF 合并：`RRF(d) = Σ 1 / (60 + rank_i(d))`，一路未命中时不贡献分数。按 `chunkId` 去重，保留两路原排名、分数与融合名次，最多 40 个候选。参数 60 和 top-k 是初值，需在开发集调优。
4. 完整范围可对融合后 top-10 调用 `bge-reranker-base`，根据模型 tokenizer 控制 query+chunk 长度，记录截断。超时或不可用时明确标记 `skipped`/`degraded`，按融合排名继续，不能伪造重排分数。
5. 从最终排名选 4～6 个片段，同一文档最多 3 个；相邻 chunk 也必须重新受权并服从上下文预算。数据库事务在候选 DTO 取出后结束，不持连接等待模型。

## 生成和结构校验

每次追问保存原问题与改写后的独立问题，并重新检索；改写不能扩大 KB 范围，也不能把上一轮模型回答当事实。生成最多带最近 3 轮合法历史；已撤权消息及其摘要不得进入模型上下文。LLM 单次初始预算为输入 4,000、输出 800 tokens，最多 6 个证据片段；预算和供应商用量由服务端计账。

证据构建器给本次授权片段分配 `E1`、`E2` 等临时 ID。模型返回句子或短段落列表、对应 `citationIds`、`insufficientEvidence` 与可选追问；Pydantic 严格拒绝额外字段、未知 ID 和非法类型。模型不填来源 URL、页码或数据库 ID。服务端从已保存的 locator 映射 citation，并验证 ID 在 allowlist、版本与权限仍有效。结构有效只证明来源存在且合法；来源是否真的支持该句需独立人工或评估验证。

MVP 完整生成并校验后返回，不以逐 token 流式输出绕过回答前的授权复核。调用重排或 LLM 前复核 ACL revision；回答前如关联文档更新则最多重新检索一次，持续变化返回“资料更新中，请重试”。撤权发生在内容已发给外部模型或客户端之后，已交付字节不可撤回，应停止后续调用和交付并记录事件。

## 回答策略与历史

- 无可访问证据或证据不足时拒答，不透露被过滤文档的存在。相似度阈值由开发集标定，不设跨模型通用常数。
- 当前有效文档互相冲突时展示双方证据、版本或生效日期，提示由责任方确认；不要静默选其中之一。若建立优先级规则，需有可审查的来源。
- `conversation` 只由所有者访问；历史消息、引用链接、文档预览和下载每次重新鉴权。删除或撤权后，不能只隐藏引用按钮，还要处理来源衍生的消息内容。
- 管理员检索调试页可查看自己有权范围内的候选、排名和阶段降级；调试权限不授予跨 KB 阅读权。

指标、三组消融与验收方法见 [评估与验收](evaluation.md)。
