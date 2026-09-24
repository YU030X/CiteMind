"""outbox 投递边界。

本包只负责把已持久化的 ``outbox_event`` 投递到 broker：投递协议与纯策略在
``protocol``，参数化 SQL 在 ``repository``，编排与后台循环在 ``service``，Celery
``publisher`` 在 ``publisher``。它不解析文档、不写业务任务状态；是否启用后台轮询由 API
``dispatcher_enabled`` 配置决定。
"""
