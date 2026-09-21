# 技术选型与引入条件

> 这是方案阶段的选型理由。依赖版本、模型接口和性能尚未在本仓库验证；Phase 0 实测后更新。外部产品的实现细节可能变化，不作为本项目的运行契约。

| 领域 | 计划选择 | 选择理由与改变条件 |
| --- | --- | --- |
| Web API | FastAPI、Pydantic v2、Uvicorn | 类型化输入输出与 OpenAPI 支持检索调试、严格模型结果校验及前端类型生成；CPU 密集工作仍独立进程 |
| 异步导入 | Celery + Redis，PostgreSQL job/outbox 保存事实 | 独立执行、有限重试与超时；broker 与数据库没有跨系统事务，必须有补投和幂等发布 |
| 业务和搜索存储 | PostgreSQL + pgvector + 预分词 FTS | 当前 5,000～10,000 chunk 目标下，可在同一 SQL 边界约束授权与版本；只有实测瓶颈才考虑 OpenSearch、Qdrant 或拆库 |
| RAG 编排 | 显式 Python service、Pydantic schema 与 HTTPX 模型适配 | 固定检索链保持可审查；连接器或编排复杂度确实增加时再评估一个框架，授权和发布事务仍由应用掌握 |
| 向量索引 | 512 维余弦、MVP exact search | 小规模和强权限过滤下优先验证正确性；HNSW 必须与 exact 比较召回、过滤和延迟 |
| Embedding | 固定 revision 的 BAAI/bge-small-zh-v1.5，CPU | 512 维中文模型作为起点；模型输入、tokenizer、pooling/normalize 与预处理一起版本化 |
| 重排 | 后续本地 bge-reranker-base | 只重排融合后 top-10，测质量增益与 CPU 延迟；vps-lite 可明确跳过 |
| 中文关键词 | jieba 搜索模式 + 版本化领域词典 + tsvector/GIN | 显式保留错误码、简称和代码标识；文档与查询共用管线；需求超过本方案时再评估独立搜索引擎 |
| 文件和解析 | 鉴权本地卷；按格式保留源位置的轻量解析器 | 小文档集无需自建对象存储集群；OCR 或复杂版面有样本与质量问题后再评估增强解析器 |
| 推理服务 | 独立单进程 sentence-transformers/PyTorch CPU | 避免 API 与 worker 复制模型内存；同一内部服务按需增加 rerank，先测峰值再扩并发 |
| 前端 | Vue 3、Vite、TypeScript、Element Plus | 登录后 SPA，无 SSR 需求；PDF.js 与图表只在对应页面需要时引入 |
| 部署 | Docker Compose v2 单机 profile | 六类服务已满足当前范围；Kubernetes 不作为问答正确性的前提 |

文档中的外部技术事实应优先按所锁定版本的官方文档核对：[FastAPI 后台任务](https://fastapi.tiangolo.com/tutorial/background-tasks/)、[SQLAlchemy Session](https://docs.sqlalchemy.org/en/20/orm/session_basics.html)、[Celery task](https://docs.celeryq.dev/en/latest/userguide/tasks.html)、[pgvector 过滤](https://github.com/pgvector/pgvector#filtering)、[PostgreSQL 全文检索](https://www.postgresql.org/docs/current/textsearch.html)、[BGE embedding 模型卡](https://huggingface.co/BAAI/bge-small-zh-v1.5)、[BGE reranker 模型卡](https://huggingface.co/BAAI/bge-reranker-base)。这些链接是复核入口，不表示当前仓库已完成兼容性或性能验证。
