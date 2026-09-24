"""Phase 1 Markdown 上传事务片：输入校验、私有文件存储与入库事实写入。

本包只实现上传受理：保存原文件，并在一个 PostgreSQL 事务中写入 ``document``、
``document_version``、``ingest_job`` 与 ``outbox_event``。它不实现 dispatcher、worker
业务任务、解析、切分、embedding、发布与检索。KB 配额当前没有对应的存储字段或实体，
因此本切片不实现配额判定（见 ``create_markdown_document`` 的说明）。
"""
