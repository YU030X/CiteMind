# 数据模型与持久化约束

> 计划 schema，尚无 Alembic 迁移。主键拟使用 UUID，时间使用 UTC `timestamptz`；外部 URL、文件名和模型名都不是可信主键。MVP 保留单组织字段，不实现组织开通或计费。

| 实体 | 主要字段 | 关键约束与用途 |
| --- | --- | --- |
| `user_account` | id, organization_id, username, password_hash, enabled | 登录主体；组织由服务端会话确定 |
| `auth_session` | id, user_id, token_hash, csrf_token_hash, expires_at, revoked_at | Cookie 保存随机原令牌，库中仅存 hash；退出、过期与禁用立即失效 |
| `knowledge_base` | id, organization_id, name, active_index_profile_id, kb_revision, acl_revision | 版本和权限变更的 revision 边界 |
| `kb_member` | kb_id, user_id, role | `(kb_id,user_id)` 唯一；角色 OWNER/EDITOR/READER |
| `document` | id, kb_id, title, source_type, active_version_id, lifecycle_status, acl_mode, deleted_at | 更新时旧 active version 继续有效；删除先 tombstone |
| `document_acl` | document_id, principal_type, principal_id, permission | 完整范围才启用；只能收紧 KB 成员权限 |
| `document_version` | id, document_id, version_no, file_ref, file_hash, mime, parser_version, status | 不可变原文件版本；`(document_id,version_no)` 唯一 |
| `ingest_job` | id, document_id, version_id, generation_id, status, attempt, lease_owner, lease_token, lease_until, heartbeat_at, next_run_at, dedupe_key, error_code | PostgreSQL 中的任务事实；`dedupe_key` 唯一；Celery task ID 仅供追踪 |
| `outbox_event` | id, job_id, event_type, status, dispatch_attempt, next_send_at, lease_owner, lease_token, lease_until, sent_at | 与 job 同事务创建；状态回写受租约 token 限制 |
| `index_profile` | id, embedding_model, model_revision, dimension, normalize, tokenizer_revision, chunker_version, keyword_analyzer_version, config_hash | 模型、预处理与切分的完整编码契约 |
| `index_generation` | id, version_id, profile_id, status, expected_chunks, actual_chunks, ready_at | 暂存不可见；同 version/profile 最多一个有效 READY generation |
| `chunk` | id, generation_id, chunk_index, text, text_hash, model_input_hash, token_count, heading_path, source_locator JSONB, fts TSVECTOR | `(generation_id,chunk_index)` 唯一；locator 绑定原文版本 |
| `chunk_embedding` | chunk_id, profile_id, embedding VECTOR(512) | MVP 每 chunk 一条固定维度向量；新维度用新表或 schema |
| `embedding_cache` | cache_key, model_revision, dimension, vector_payload, last_used_at | 只在本组织内复用，不复用来源位置 |
| `conversation` / `message` | owner_id, kb_scope；role, content, query_run_id, status | 会话属于用户；历史访问按当前 ACL 复核 |
| `query_run` / `retrieval_hit` | 问题、scope_snapshot、配置、版本、阶段耗时、tokens、cost；chunk_id、两路排名、RRF/rerank 分数 | 调试与复算；权限撤销后也需过滤 |
| `citation` / `feedback` | message_id, chunk_id, version_id, locator_snapshot, quote_hash；评分与预期证据 | 引用不接受 LLM 自造 URI；反馈不直接在线训练 |
| `eval_dataset` / `eval_case` / `eval_run` / `eval_result` | 数据集版本、角色、gold spans、split；配置、模型 revision、逐题结果 | 保留历史运行，不覆盖；gold 使用源区间而非 chunk ID |
| `audit_event` | actor_id, action, target_id, before_hash, after_hash, request_id, created_at | 记录授权、删除、索引切换等，默认不记正文 |

主关系为 `knowledge_base → document → document_version → index_generation → chunk → chunk_embedding`；任务为 `ingest_job → outbox_event → Celery 消息`；问答为 `conversation → message → query_run / citation`，检索命中归 `query_run`。授权沿 `auth_session → user_account → kb_member → document_acl` 应用于资源读取与两路检索。

## 数据库约束和索引

Alembic 至少建立 `auth_session(token_hash)` 唯一索引、`kb_member(user_id,kb_id)`、`document(kb_id,lifecycle_status)`、`document_version(document_id,version_no)`、`index_generation(version_id,profile_id,status)`、`chunk(generation_id)`、`GIN(chunk.fts)`、`ingest_job(status,next_run_at)`、`outbox_event(status,next_send_at)` 和 `query_run(user_id,created_at)`。`index_generation(version_id,profile_id) WHERE status='READY'` 的部分唯一索引是并发发布的最后约束。MVP 精确向量检索不建 ANN 索引；引入 HNSW 前测授权过滤下的召回。

文件、向量、聊天与审计按用途分开保留。演示环境计划保留原文及最近 3 版索引、查询明细 30 天、脱敏汇总 90 天；删除文档先禁止访问，再按保留策略清理文件、chunk、向量缓存和引用正文。保留期限和清理作业须在实现时由配置与测试固定，不能仅靠本页文字生效。
