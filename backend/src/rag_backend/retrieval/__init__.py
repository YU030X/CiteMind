"""Phase 1 检索切片：中文关键词分析器等检索前置能力。

本包目前只提供与数据库、worker、HTTP 无关的纯本地关键词分析：固定版本的 jieba 私有
``Tokenizer``、包内版本化领域词典，以及固定的 NFKC + casefold 规范化规则。它不连接
PostgreSQL、不注册 Celery 任务、不对外提供检索 API，也不发布索引；``to_tsvector`` 参数
绑定与 GIN/``ts_rank_cd`` 排序仍由后续检索实现负责。
"""
