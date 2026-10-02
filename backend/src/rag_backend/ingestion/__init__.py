"""Phase 1 文档入库：上传受理、私有存储、解析切分、编码与默认关闭的真实入库管线。

本包覆盖 Markdown/文本 PDF/收窄子集 DOCX 的文件上传与静态网页 URL 导入的受理，以及私有内容
寻址存储、纯解析与切分、本地真实 token 计数、受限内部 embedding 客户端与增量缓存，并把它们
串成 ``rag_backend.ingestion.indexing_worker`` 的领取、暂存与发布链路。该链路接入
``rag_backend.ingest`` 任务但**默认关闭**（``INGEST_PROCESSING_ENABLED=0``），关闭时仍走只写
接收标记的安全接收壳；显式开启后才会读 blob、写 ``chunk``/``chunk_embedding`` 并发布 READY
generation。KB 配额当前仍没有对应的存储字段或实体，因此本包不实现配额判定
（见 ``create_markdown_document`` 的说明）。
"""
