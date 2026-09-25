# Agent Note：BGE 中文查询 instruction 契约 `bge-zh-query-v1`

- 状态：已实现
- 范围：inference `/internal/embed` 的 `kind=query` 与 API 侧查询编码客户端

## 背景

检索需要把用户问题编码成与文档向量同一模型空间中的向量。BGE 中文检索模型建议在查询侧
追加官方 instruction 前缀，而文档侧不加。原实现只接受 `kind=document`，`kind=query` 直接
422。

## 决策

1. **前缀由服务端追加，且只追加一次。** inference 在编码前对每条查询文本拼接
   `为这个句子生成表示以用于检索相关文章：`。客户端只发送原始查询文本；用户输入本身以该
   前缀开头时也不去重、不改写。
2. **查询 encoding 是独立的具名契约，不并入 `modelRevision`。** 常量
   `QUERY_ENCODING_CONTRACT = "bge-zh-query-v1"`；仅凭 `modelRevision` 无法识别 instruction
   漂移。`kind=query` 响应携带 wire 字段 `queryEncodingContract`，文档响应省略该字段，因此
   未来同 revision 换前缀可在 wire 上被检测；API 侧客户端以同名常量严格要求精确匹配，缺失/
   非法/未知一律失败。该契约与固定前缀另有独立 literal golden 测试钉死（改变实现而不同步
   golden 会失败）；golden 只做回归检测，不能自证前缀一定正确。
3. **计数与编码都基于追加前缀后的完整模型输入。** `tokenCounts` 含前缀与特殊 token；
   不截断；超过模型 512 个位置上限时返回显式 422。
4. **文档路径与既有 profile 不变。** `kind=document` 的行为、文档向量与模型输入不变，因此
   七个字段的 index profile `config_hash` 不变，不需要新 profile 或重索引。
5. **API 侧客户端不引入 tokenizer。** `rag_backend.retrieval.query_embedding_client` 不导入
   `tokenizers`/`torch`/`rag_backend.ingestion`，本地字符/字节上限按完整模型输入（instruction
   前缀 + 用户查询）折算到服务端预算，只做响应契约校验（512 维有限 L2 归一化、revision、
   `queryEncodingContract`、整数 1..512 的 `tokenCounts`）；API 镜像保持不安装 tokenizer。

## 后果与边界

- 改变前缀、追加位置、计数方式或查询编码策略必须提升 `QUERY_ENCODING_CONTRACT` 版本并重新
  评估，不能静默切换；若改动也影响文档向量，则必须另建 index profile。
- 本切片只实现查询编码与受限客户端，未接入检索路由/SQL/PDF；单次请求重试仍由未来上层检索
  策略决定，客户端自身零自动重试。
- 真实权重与真实 HTTP 已由隔离模型 tester 验收：`query(text)` 与 `document(prefix+text)` 向量逐元素差 0、
  token 相同；512 token 200 / 513 token 422 且不截断；完整 inference 146 passed、0 skipped（含 7 个真实
  golden）；backend 真实查询客户端 23/23（成功、revision 不匹配、timeout 与连接失败清理、close）。
- **未测**：513 拒绝前是否发生前向；真实 `EMBEDDING_BUSY`/`EMBEDDING_QUEUE_TIMEOUT` 触发；跨 CPU golden；
  完整检索（无路由、SQL 与授权）；app 超字符返回的是**完整模型输入**长度而非用户输入长度（已知，不扩修复）。
