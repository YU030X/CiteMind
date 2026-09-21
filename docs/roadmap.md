# 技术实施顺序

> 这是阶段计划，非已完成清单。验收以真实运行和结果文件为准；新增范围应先更新本页及相关契约。

当前处于 Phase 0：已建立锁文件、FastAPI 健康检查/OpenAPI、Vue/TypeScript 前端骨架与开发期 Jev 判断入口。数据库 Session、迁移、Compose、权限账号、独立 worker、inference 和云 LLM 用量账本均未完成，因此尚未达到 Phase 0 退出条件。

| 阶段 | 实现范围 | 退出条件 |
| --- | --- | --- |
| Phase 0：工程底座 | 固定 Python/依赖/模型 revision；uv 锁文件；FastAPI/Pydantic、SQLAlchemy/Alembic、数据库 Session、六服务 Compose、两个权限不同的账号；连通 embedding、真实 Celery worker 与云 LLM 用量账本 | OpenAPI 可用，512 维向量契约通过检查，独立 worker 收到任务，两账号授权不同；配置、镜像与运行命令可复现 |
| Phase 1：MVP | Markdown 与文本 PDF、受限异步入库、outbox、来源 locator、chunk 与索引版本；jieba/FTS + pgvector exact + RRF；证据回答、引用、拒答、多轮、KB 权限、更新删除、基础费用/耗时 | 两格式真实入库；Redis 断连后任务可补投；越权内容不进候选；来源能定位；旧版至新版发布前仍服务；至少 30 道开发题 |
| Phase 2：完整数据链 | DOCX、受限静态网页、pdfplumber 适配、文档 ACL、可降级 reranker、增量缓存、worker/dispatcher 租约恢复 | 各格式至少 5 份样本；撤权影响历史和下载；重复任务不重复发布；重排超时显示降级 |
| Phase 3：质量评估 | 固定 100 题，开发/留出分割；证据冲突、拒答标定、追问改写、提示注入；三组消融与成本/延迟分析 | 留出集按固定分母报告 Recall、nDCG、引用、拒答、泄露和逐题失败，不以单个总分代替分析 |
| Phase 4：工程验收 | 真实跨进程故障、权限并发、备份恢复、SSRF 与文件资源限制、CI、2c4GB 资源与性能测量 | worker/broker 中断可恢复；Alembic 和备份可恢复；模型超时明确；记录峰值内存、吞吐、p95 与实际限制 |

Phase 1 已包含基础 LLM 问答；Phase 3 深化评估。OCR、动态网页、SSO、跨组织 SaaS、独立搜索集群、GPU、Agent 或任意工具执行均不属于上述承诺。要引入 HNSW、OCR、内网模型、OIDC、对象存储或框架编排，先拿到对应的语料、性能、合规或连接器需求，分别评估质量、资源、迁移与授权一致性。一次只增加一个外部系统。

开发顺序内的停止扩张条件：核心格式有明确解析边界，答案可回到原文，撤权和版本更新真实生效，三组评估可复算，普通计算机可运行 MVP。目标达不到时记录实测与限制，不把设计数字写成成果。
