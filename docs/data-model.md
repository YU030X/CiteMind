# 数据模型与持久化约束

> 第一切片业务表已由迁移 `20260922_0002` 落地：`index_profile`、`knowledge_base`、`document`、`document_version`、`ingest_job` 与 `outbox_event` 六张表，均不含向量列。第二切片的 `index_generation`、`chunk`、`chunk_embedding` 与 `chunk_embedding VECTOR(512)` 列约束已由迁移 `20260922_0003` 落地并在真实 PostgreSQL 上验收；认证、会话、文档 ACL、缓存和问答表仍是计划内容，尚未实现或验收。主键 UUID 由应用 `uuid4` 生成、数据库不设 UUID server default；时间为 UTC `timestamptz` 且 `server_default=now()`；外部 URL、文件名和模型名都不是可信主键。MVP 保留单组织字段，不实现组织开通或计费。

## 已实现：第一切片（迁移 20260922_0002）

迁移 `20260922_0002_core_business_tables` 紧接 `20260921_0001`，线性单 head，业务表由迁移账号创建，同一迁移内逐表 `REVOKE ALL ... FROM PUBLIC` 并显式 GRANT；不使用 PostgreSQL ENUM、serial/identity/sequence、`ALTER DEFAULT PRIVILEGES` 或 schema 级授权。SQLAlchemy 2 declarative 模型与迁移共享同一套约束命名，但迁移仍手写，不以 autogenerate 结果作为契约。

| 表 | 主要字段 | 关键约束与 ACL |
| --- | --- | --- |
| `index_profile` | id, embedding_model, model_revision, tokenizer_revision, chunker_version, keyword_analyzer_version, config_hash, dimension, normalize, created_at | `dimension = 512` 的具名 CHECK、`config_hash` 唯一、不可变（无 UPDATE 授权）；api SELECT+INSERT，worker SELECT |
| `knowledge_base` | id, organization_id, name, active_index_profile_id, kb_revision, acl_revision, created_at, updated_at | `organization_id` 暂不建组织外键；两个 revision 默认 0 且 `>= 0`；`active_index_profile_id` 外键 RESTRICT；api SELECT+INSERT+UPDATE，worker SELECT |
| `document` | id, kb_id, title, source_type, active_version_id, lifecycle_status, deleted_at, created_at, updated_at | `source_type IN (markdown, pdf)`；`lifecycle_status IN (CREATED, INDEXING, READY, FAILED, DELETED)`；`(kb_id, lifecycle_status)` 索引；本切片不建 `acl_mode`；api SELECT+INSERT+UPDATE，worker SELECT+UPDATE |
| `document_version` | id, document_id, version_no, file_ref, file_hash, mime, parser_version, status, created_at, updated_at | `version_no > 0`；`status IN (PENDING, READY, FAILED, NEEDS_OCR)`；`(document_id, version_no)` 唯一；本切片不添加解析警告字段；api SELECT+INSERT+UPDATE，worker SELECT+UPDATE |
| `ingest_job` | id, document_id, version_id, status, attempt, lease_owner, lease_token, lease_until, heartbeat_at, next_run_at, dedupe_key, error_code, created_at, updated_at | `status IN (QUEUED, PARSING, CHUNKING, EMBEDDING, INDEXING, READY, FAILED, CANCELLED)`；`attempt >= 0`；租约 owner/token/until 三列全空或全非空；`dedupe_key` 唯一；`(status, next_run_at)` 索引；第一切片不含 `generation_id` 与独立 progress，第二切片补加可空 `generation_id`；api SELECT+INSERT+UPDATE，worker SELECT+UPDATE |
| `outbox_event` | id, job_id, event_type, status, dispatch_attempt, next_send_at, lease_owner, lease_token, lease_until, sent_at, created_at, updated_at | `status IN (PENDING, SENT, FAILED)`；`dispatch_attempt >= 0`；租约三列全空或全非空；`job_id` 不唯一；不建 payload；`(status, next_send_at)` 索引；api SELECT+INSERT+UPDATE，worker 无权限 |

外键默认 `ON DELETE RESTRICT` 与 `ON UPDATE RESTRICT`；`document.active_version_id` 指向 `document_version` 且 `ON DELETE SET NULL`，在 `document_version` 建表后再补加该外键。运行角色只获得 SELECT/INSERT/UPDATE 的按表子集，从不获得 DELETE、TRUNCATE、REFERENCES、TRIGGER 或 sequence 权限；`index_profile` 的不可变性由“不授予 UPDATE”实现，而不是触发器。

## 已实现：第二切片（迁移 20260922_0003）

迁移 `20260922_0003_second_slice_tables` 紧接 `20260922_0002`，建立 `index_generation`、`chunk`、`chunk_embedding` 三张表并给 `ingest_job` 增加可空 `generation_id` 外键。三张表同样由迁移账号创建，逐表 `REVOKE ALL ... FROM PUBLIC` 并显式 GRANT；`chunk_embedding.embedding` 为 pgvector `VECTOR(512)`，本切片不建 ANN 索引，也不引入触发器。

| 表 | 主要字段 | 关键约束与 ACL |
| --- | --- | --- |
| `index_generation` | id, version_id, profile_id, status, expected_chunks, actual_chunks, ready_at, created_at, updated_at | `status IN (BUILDING, READY, RETIRED, FAILED)`；两个 chunk 计数默认 0 且 `>= 0`，并 `actual_chunks <= expected_chunks`；`(version_id,profile_id,status)` 二级索引；`(version_id,profile_id) WHERE status='READY'` 部分唯一索引；api SELECT，worker SELECT+INSERT+UPDATE |
| `chunk` | id, generation_id, organization_id, kb_id, document_id, version_id, chunk_index, text, text_hash, model_input_hash, parser_version, chunker_version, token_count, heading_path JSONB, source_locator JSONB, fts TSVECTOR, created_at | 全部业务字段非空；`chunk_index >= 0`、`btrim(text) <> ''`、`token_count >= 0`；`(generation_id,chunk_index)` 唯一；`generation_id` 二级索引；`GIN(fts)`；api SELECT，worker SELECT+INSERT（视为不可变，不授 UPDATE/DELETE） |
| `chunk_embedding` | chunk_id, profile_id, embedding VECTOR(512) | `chunk_id` 既是主键又是 `chunk` 外键；`profile_id` 外键 `index_profile`；MVP 每 chunk 一条 512 维向量；api SELECT，worker SELECT+INSERT（视为不可变，不授 UPDATE/DELETE） |

`ingest_job.generation_id` 可空并 `ON DELETE/UPDATE RESTRICT`；既有 API/worker 授权不扩大。`chunk` 的 `organization_id`、`kb_id`、`document_id`、`version_id` 是来源冗余字段（来自 [入库](ingestion.md) 要求），本切片没有 worker 写路径，因此**数据库尚未强制** `generation → version → document → KB/organization` 的跨表一致性，也没有在结构上强制 `chunk_embedding.profile_id` 与所属 generation 的 profile 一致；这两项不变量必须由未来的 worker 写入事务及其集成测试核对，检索不得信任客户端提交的来源字段。`chunk_embedding` 的 512 维列约束已用真实插入（512 维成功、非 512 维失败）验收，但不代表 ANN 检索或权限过滤下的召回已经实现。

## 计划中：后续切片

以下实体与字段仍未实现。

| 实体 | 主要字段 | 关键约束与用途 |
| --- | --- | --- |
| `user_account` | id, organization_id, username, password_hash, enabled | 登录主体；组织由服务端会话确定 |
| `auth_session` | id, user_id, token_hash, csrf_token_hash, expires_at, revoked_at | Cookie 保存随机原令牌，库中仅存 hash；退出、过期与禁用立即失效 |
| `kb_member` | kb_id, user_id, role | `(kb_id,user_id)` 唯一；角色 OWNER/EDITOR/READER |
| `document_acl` | document_id, principal_type, principal_id, permission | 完整范围才启用；只能收紧 KB 成员权限 |
| `embedding_cache` | cache_key, model_revision, dimension, vector_payload, last_used_at | 只在本组织内复用，不复用来源位置 |
| `conversation` / `message` | owner_id, kb_scope；role, content, query_run_id, status | 会话属于用户；历史访问按当前 ACL 复核 |
| `query_run` / `retrieval_hit` | 问题、scope_snapshot、配置、版本、阶段耗时、tokens、cost；chunk_id、两路排名、RRF/rerank 分数 | 调试与复算；权限撤销后也需过滤 |
| `citation` / `feedback` | message_id, chunk_id, version_id, locator_snapshot, quote_hash；评分与预期证据 | 引用不接受 LLM 自造 URI；反馈不直接在线训练 |
| `eval_dataset` / `eval_case` / `eval_run` / `eval_result` | 数据集版本、角色、gold spans、split；配置、模型 revision、逐题结果 | 保留历史运行，不覆盖；gold 使用源区间而非 chunk ID |
| `audit_event` | actor_id, action, target_id, before_hash, after_hash, request_id, created_at | 记录授权、删除、索引切换等，默认不记正文 |

第一切片已实现的 `ingest_job` 已在第二切片新增可空 `generation_id`；第一切片已实现的 `document` 尚无 `acl_mode`，它属于完整范围的字段。完整主关系仍是 `knowledge_base → document → document_version → index_generation → chunk → chunk_embedding`；任务为 `ingest_job → outbox_event → Celery 消息`；问答为 `conversation → message → query_run / citation`，检索命中归 `query_run`。授权沿 `auth_session → user_account → kb_member → document_acl` 应用于资源读取与两路检索。

## 数据库约束和索引

第一切片已实现的索引与唯一约束：`document(kb_id,lifecycle_status)`、`ingest_job(status,next_run_at)`、`outbox_event(status,next_send_at)` 三个二级索引，以及 `index_profile(config_hash)`、`document_version(document_id,version_no)`、`ingest_job(dedupe_key)` 三个唯一约束。所有表的主键都是 `pk_<table>`，全部具名 CHECK、外键与索引遵循同一命名规则。

第二切片已实现的索引与唯一约束：`index_generation(version_id,profile_id,status)` 二级索引、`index_generation(version_id,profile_id) WHERE status='READY'` 部分唯一索引、`chunk(generation_id)` 二级索引、`chunk(generation_id,chunk_index)` 唯一约束与 `GIN(chunk.fts)`。其中部分唯一索引是并发发布的最后约束。以下仍是计划，尚未实现：`auth_session(token_hash)` 唯一索引、`kb_member(user_id,kb_id)` 和 `query_run(user_id,created_at)`。MVP 精确向量检索不建 ANN 索引；引入 HNSW 前测授权过滤下的召回。

文件、向量、聊天与审计按用途分开保留，保留策略尚未实现。演示环境计划保留原文及最近 3 版索引、查询明细 30 天、脱敏汇总 90 天；删除文档先禁止访问，再按保留策略清理文件、chunk、向量缓存和引用正文。保留期限和清理作业须在实现时由配置与测试固定，不能仅靠本页文字生效。

第一切片与第二切片的真实 PostgreSQL 迁移验收分别由 `tests/integration/test_core_migration.py` 与 `tests/integration/test_second_slice_migration.py` 承担，并已在 PostgreSQL 17.11 + pgvector 0.8.6 的专用测试库（`127.0.0.1:55433`）上通过：第二切片聚焦测试 9 passed，全部 integration 为 25 passed。覆盖 9 张表、具名约束、GIN 与部分唯一索引、无 sequence/ENUM/ANN、PUBLIC 收权、api/worker 差异、512 维可插入与非 512 维被拒、主键与部分唯一冲突，finally 降级回 `20260922_0002` 与 base 后第一切片 6 表仍完整、新对象与授权无残留；未配置 `CITEMIND_TEST_DATABASE_URL` 与 `CITEMIND_ALLOW_DESTRUCTIVE_TEST_DB=1` 时按守卫跳过，跳过不代表通过。
