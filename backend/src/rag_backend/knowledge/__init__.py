"""知识库用例包：KB 成员授权与资源级判定。

本包只承载 KB 范围的用例与纯规则；HTTP 契约在 ``rag_backend.schemas``，路由在
``rag_backend.api.knowledge_bases``。成员授权的事实来源是数据库 ``kb_member``，
不在进程内缓存。
"""
