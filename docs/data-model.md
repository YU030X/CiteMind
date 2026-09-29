# 数据模型与持久化约束

> 第一切片业务表已由迁移 `20260922_0002` 落地：`index_profile`、`knowledge_base`、`document`、`document_version`、`ingest_job` 与 `outbox_event` 六张表，均不含向量列。第二片（`index_generation`、`chunk`、`chunk_embedding` 与 `chunk_embedding VECTOR(512)`）已由 `20260922_0003` 落地并在真实 PostgreSQL 上验收；第三片 append-only 用量账本 `llm_usage` 已由 `20260923_0004` 落地并在隔离专用测试库上通过真实迁移与授权验收，且已接收一次真实 DeepSeek 成功调用写入的 `SUCCEEDED`/`PROVIDER_REPORTED` 行（见 [开发约定](development.md)）。第四片身份与会话基础表 `user_account`、`auth_session` 与 `kb_member` 已由 `20260923_0005` 落地；登录、限流、会话签发/撤销已实现并验收，KB 成员授权（`GET/POST /knowledge-bases`、成员读取与全量替换、服务端 `require_kb_role`）已在本切片实现并验收；第五片（迁移 `20260925_0006`）给 `ingest_job` 增加可空 `profile_id` 外键，且新 Markdown 上传写路径已在同一四表事务内登记默认全局 profile 并显式绑定该列（独立 tester 已在隔离 PostgreSQL 17 + Redis 上验收，详见下文）；问答四表 `conversation`/`message`/`query_run`/`citation` 已由迁移 `20260927_0009` 落地并由证据问答主流程实现与验收；文档 ACL（`document.acl_mode` 与 `document_acl`）已由迁移 `20260928_0012` 落地，读取收紧与原文下载已实现并在隔离 PostgreSQL 17 上聚焦验收（见下文）。增量 embedding 缓存改为复用既有 `chunk_embedding`（不新增缓存表，见“已实现：增量 embedding 缓存”），`retrieval_hit`/`feedback` 与价目快照仍是计划内容，尚未实现或验收。主键 UUID 由应用 `uuid4` 生成、数据库不设 UUID server default；时间为 UTC `timestamptz` 且 `server_default=now()`；外部 URL、文件名和模型名都不是可信主键。MVP 保留单组织字段，不实现组织开通或计费。

## 已实现：第一切片（迁移 20260922_0002）

迁移 `20260922_0002_core_business_tables` 紧接 `20260921_0001`，线性单 head，业务表由迁移账号创建，同一迁移内逐表 `REVOKE ALL ... FROM PUBLIC` 并显式 GRANT；不使用 PostgreSQL ENUM、serial/identity/sequence、`ALTER DEFAULT PRIVILEGES` 或 schema 级授权。SQLAlchemy 2 declarative 模型与迁移共享同一套约束命名，但迁移仍手写，不以 autogenerate 结果作为契约。

| 表 | 主要字段 | 关键约束与 ACL |
| --- | --- | --- |
| `index_profile` | id, embedding_model, model_revision, tokenizer_revision, chunker_version, keyword_analyzer_version, config_hash, dimension, normalize, created_at | `dimension = 512` 的具名 CHECK、`config_hash` 唯一、不可变（无 UPDATE 授权）；它是全局编码契约登记表，登记一个 profile 不代表任何 KB 可检索；api SELECT+INSERT，worker SELECT |
| `knowledge_base` | id, organization_id, name, active_index_profile_id, kb_revision, acl_revision, created_at, updated_at | `organization_id` 暂不建组织外键；两个 revision 默认 0 且 `>= 0`；`active_index_profile_id` 可空、外键 RESTRICT（语义见下文“index profile 契约与 KB active 可见性”）；api SELECT+INSERT+UPDATE，worker SELECT |
| `document` | id, kb_id, title, source_type, active_version_id, lifecycle_status, acl_mode, deleted_at, created_at, updated_at | `source_type IN (markdown, pdf, docx)`；`lifecycle_status IN (CREATED, INDEXING, READY, FAILED, DELETED)`；`acl_mode IN (INHERIT, RESTRICTED)`（文档 ACL 切片 `20260928_0012` 补加，`server_default='INHERIT'`；DOCX 切片 `20260929_0013` 扩 `source_type` 允许集合）；`(kb_id, lifecycle_status)` 索引；api SELECT+INSERT+UPDATE，worker SELECT+UPDATE |
| `document_version` | id, document_id, version_no, file_ref, file_hash, mime, parser_version, status, created_at, updated_at | `version_no > 0`；`status IN (PENDING, READY, FAILED, NEEDS_OCR)`；`(document_id, version_no)` 唯一；本切片不添加解析警告字段；`parser_version` 无 CHECK，既有行历史值为占位 `markdown-v1`，新上传按 `source_type` 写真实实现版本（Markdown `markdown-it-py-4.2.0-v1`、PDF `pypdf-6.19.0+pdfplumber-0.11.10-v1`；Markdown 新版行为已由独立 tester 在隔离 PostgreSQL 17 + Redis 上验收，`tests/integration/test_document_upload_flow.py` 11 passed/0 skipped），既有行不自动升级、需受控处理；api SELECT+INSERT+UPDATE，worker SELECT+UPDATE |
| `ingest_job` | id, document_id, version_id, status, attempt, lease_owner, lease_token, lease_until, heartbeat_at, next_run_at, dedupe_key, request_title, error_code, created_at, updated_at | `status IN (QUEUED, PARSING, CHUNKING, EMBEDDING, INDEXING, READY, FAILED, CANCELLED)`；`attempt >= 0`；租约 owner/token/until 三列全空或全非空；`dedupe_key` 唯一；`(status, next_run_at)` 索引；第一切片不含 `generation_id` 与独立 progress，第二切片补加可空 `generation_id`，第五切片（`20260925_0006`）再补加可空 `profile_id`，文档更新/删除切片（`20260926_0008`）再补加可空 `request_title`（受理时刻的标题快照，旧任务为 NULL，可空、无 server default/回填/索引，api/worker 表级授权已覆盖）；api SELECT+INSERT+UPDATE，worker SELECT+UPDATE |
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

## 已实现：第三切片（迁移 20260923_0004）

迁移 `20260923_0004_llm_usage` 紧接 `20260922_0003`，创建 append-only 的 `llm_usage` 用量账本。表由迁移账号创建，`REVOKE ALL ... FROM PUBLIC` 后显式 GRANT：api 只有 SELECT+INSERT，worker 无任何权限，不授权 UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER。不使用 PostgreSQL ENUM、serial/identity/sequence、触发器或 ANN 索引；本表也不建二级索引。

| 表 | 主要字段 | 关键约束与 ACL |
| --- | --- | --- |
| `llm_usage` | id, provider, model, stage, status, error_code, usage_source, attempt, prompt_tokens, completion_tokens, prompt_cache_hit_tokens, prompt_cache_miss_tokens, latency_ms, price_snapshot, price_source, price_currency, cost_amount, created_at | `status IN (SUCCEEDED, FAILED, TIMEOUT)`；`usage_source IN (PROVIDER_REPORTED, UNKNOWN)`；`attempt >= 1`；所有 token、`latency_ms`、`cost_amount` 非负；成功行必须 `usage_source = PROVIDER_REPORTED` 且 prompt/completion tokens 非空；非成功行必须带 `error_code`；`price_source`、`price_currency`、`cost_amount` 三者必须同时为空或同时非空。api SELECT+INSERT，worker 无权限 |

一次 provider attempt 恰好一行：失败、超时与凭据错误也必须追加事实，provider 未报告 usage 时不得伪造 token，该行只能是 `FAILED`/`TIMEOUT` 且 token 与费用为 NULL。表按应用语义不可变：api 没有 UPDATE/DELETE 权限，也没有对应触发器提供更新。`price_snapshot`、`price_source`、`price_currency`、`cost_amount` 为将来显式价目快照预留，一次性探针写入的这次真实成功行的四个价目/费用字段同样为 NULL，因此该表当前只承载 provider 报告的 token 事实，不承担费用核算。真实 PostgreSQL 迁移与授权验收由 `tests/integration/test_llm_usage_migration.py` 承担，未配置测试 DSN 时按守卫跳过。

## 已实现：第四切片（迁移 20260923_0005）

迁移 `20260923_0005_identity_and_kb_members` 紧接 `20260923_0004`，创建登录主体 `user_account`、服务端会话 `auth_session` 与 KB 成员授权 `kb_member`。三张表由迁移账号创建，逐表 `REVOKE ALL ... FROM PUBLIC` 后只给 api 角色 SELECT+INSERT+UPDATE；worker 在本切片没有身份写路径，不获任何权限；不授权 DELETE/TRUNCATE/REFERENCES/TRIGGER 或 sequence，也不使用 PostgreSQL ENUM、serial/identity 与 `ALTER DEFAULT PRIVILEGES`。迁移 `20260923_0005` 自身只建立持久化结构，不包含登录、会话签发/撤销、登录限流或权限判定逻辑，也不创建凭据、种子用户或默认管理员；这些 auth 与 KB 成员 API 已在其后的应用层实现并验收（见 [安全](security.md) 与 [API 契约](api.md)）。

| 表 | 主要字段 | 关键约束与 ACL |
| --- | --- | --- |
| `user_account` | id, organization_id, username, password_hash, enabled, is_admin, created_at, updated_at | `organization_id` 暂不建组织外键；`(organization_id, username)` 唯一，账号在组织内唯一；api SELECT+INSERT+UPDATE，worker 无权限 |
| `auth_session` | id, user_id, token_hash, csrf_token_hash, expires_at, revoked_at, created_at, updated_at | `user_id` 外键 RESTRICT；`token_hash` 唯一；`revoked_at` 可空，退出时可写入以立即失效；到期与用户禁用由查询按 `expires_at`/`user_account.enabled` 判断，不要求写 `revoked_at`；表只存 token 与 CSRF token 的 hash；api SELECT+INSERT+UPDATE，worker 无权限 |
| `kb_member` | id, kb_id, user_id, role, revoked_at, created_at, updated_at | `role IN (OWNER, EDITOR, READER)`；`(kb_id, user_id)` 唯一；`kb_id`/`user_id` 外键 RESTRICT；`revoked_at` 软撤销，因此不需要 DELETE；api SELECT+INSERT+UPDATE，worker 无权限 |

外键默认 `ON DELETE/UPDATE RESTRICT`。`kb_member` 的软撤销复用同一行（重新加入时清空 `revoked_at` 并更新 `role`），所以 `(kb_id, user_id)` 保持全表唯一。`kb_member` 的软撤销与 `user_account.enabled` 是两个维度：禁用账号不会撤销其成员行，成员列表可能包含已禁用用户，账号重新启用后原角色恢复。`auth_session` 只保存 token 与 CSRF token 的 hash，不保存原令牌；原令牌只存在于客户端 Cookie，服务端按 hash 校验并以服务端密钥与会话令牌派生的 CSRF 令牌配合，该会话/CSRF 逻辑已由 auth API 实现。运行角色没有 DELETE 权限，当前也没有会话清理作业，已撤销与已过期会话行会继续保留，清理期限与维护作业尚未实现。三张表都不建额外二级索引。

`kb_member` 的 `kb_id` 与 `user_id` 只是各自指向 `knowledge_base` 与 `user_account` 的两个独立外键，数据库**不保证**两者属于同一组织，也不保证成员所属组织与 `knowledge_base.organization_id` 一致；该不变量必须由业务写入事务在提交前核对（本切片 `create_knowledge_base` 与 `replace_knowledge_base_members` 在事务内按会话组织校验用户，读取路径也按 KB 组织过滤），当前没有触发器或复合外键在结构上强制它。同样，“替换后至少保留一名启用中的 OWNER”与“不得新加入或提升已禁用用户为 OWNER”都是替换事务内的应用校验，数据库没有对应约束；直接写库（例如禁用最后一个 OWNER）可以绕过，只能由运维在库层修正。

## 已实现：第五切片（迁移 20260925_0006）

迁移 `20260925_0006_ingest_job_profile` 紧接 `20260923_0005`，线性单 head，只给 `ingest_job` 增加一个可空 UUID 列 `profile_id`，并建立指向 `index_profile(id)` 的具名外键 `fk_ingest_job_profile_id_index_profile`（`ON DELETE/UPDATE RESTRICT`）。迁移不加 server default、不回填、不 seed、不新建索引、不改授权，也不改写既有 `QUEUED`/`HANDLER_NOT_READY` 行；api/worker 对 `ingest_job` 的既有表级 UPDATE 授权本就覆盖新列，因此不新增 `GRANT`/`REVOKE`。SQLAlchemy 模型（`backend/src/rag_backend/models/ingestion.py`）与迁移结构同步。

迁移只增加这一列及其外键；本切片起，**新 Markdown 上传写路径**在同一个四表事务内先 `ensure_default_index_profile(session)` 登记/复用默认全局 profile，再把其行 id 显式写入新 `ingest_job.profile_id`（独立 tester 已验收）；worker 接收壳仍只写 `HANDLER_NOT_READY` 接收标记、不读也不写 `profile_id`。幂等回放命中既有任务时不改写其 `profile_id`：既有 `QUEUED`/`HANDLER_NOT_READY` 任务升级后保持 NULL，**不得**按新 default profile 契约自动处理、补绑、重投或视为已绑定 profile。该绑定不代表任何文档可检索（`knowledge_base.active_index_profile_id` 仍为 NULL），也不代表任务已 READY。

因为 api/worker 对 `ingest_job` 拥有表级 UPDATE，数据库**不保证** `profile_id` 不可变，也不强制它与目标 generation 的 `profile_id` 一致；未来 worker 接线时写入事务必须自行限制对它的更改，并在提交前核对 `generation.profile_id` 一致。在实现与验收前不得声称该绑定已在结构上冻结。worker 侧另有纯身份预检模块 `rag_backend.ingestion.identity_preflight`（独立 reviewer APPROVED 与独立 tester 已验收）：只 import 标准库、不读写数据库，按调用方传入的 `ingest_job.profile_id`、`document.source_type`、`document_version.parser_version` 与 `index_profile` 行 DTO（id+七字段+`config_hash`）返回八类互斥静态判定，只有 row id、七字段、`config_hash`、parser 与来源全匹配才 `ALLOWED`；它当前未被 `worker.py` 调用，`ALLOWED` 也不代表 READY 或可检索，数据库层面 `profile_id` 的可变性与 generation 一致性仍无结构强制。

## 已实现：第六切片（迁移 20260925_0007）

迁移 `20260925_0007_worker_kb_publish_privileges` 紧接 `20260925_0006`，线性单 head，只给
`citemind_worker` 增加 `knowledge_base` 两列的列级 UPDATE 权限：

```sql
GRANT UPDATE (active_index_profile_id, kb_revision) ON TABLE knowledge_base TO citemind_worker;
```

它刻意不授予全表 UPDATE，也不授予 INSERT/DELETE/TRUNCATE/REFERENCES/TRIGGER；不改表结构、
不新增索引、不 seed、不改 api 角色或其它表授权。降级只 `REVOKE` 这两列权限。

列级权限服务于首次 READY 发布事务：worker 需要在同一事务内把
`knowledge_base.active_index_profile_id` 从 NULL 首次置为目标 generation 的 profile，并按
[入库与版本](ingestion.md) 的规则原子递增 `kb_revision`。数据库仍不强制该指针与 generation
profile 一致，发布事务必须用带谓词的条件 UPDATE 拒绝非 NULL 且与目标 profile 不同的 KB；
本迁移不代替这些应用层校验。worker 侧真实入库管线已实现但**默认关闭**，其发布行为已由独立 tester 在隔离 PostgreSQL 17（pipeline 15 passed + 权限迁移 3 passed）与 Linux prefork concurrency 1 真离线模型整链上端到端验收 PG `0007` READY（见 [开发约定](development.md)）。

## 已实现：文档更新/删除切片（迁移 20260926_0008）

文档更新/删除切片复用既有列与状态：`document.deleted_at`、`document.lifecycle_status='DELETED'`、`document_version.status`、`ingest_job.status='CANCELLED'` 与 `index_generation.status`；另新增迁移 `20260926_0008_ingest_job_request_title`，只给 `ingest_job` 加一个可空 Text 列 `request_title`（不加 server default/回填/索引/授权）。它固化受理那一刻的规范化请求标题，使幂等身份不再依赖会被新版本改写的 `document.title`；`request_title IS NULL` 的旧任务回退到 `document.title` 比较（旧数据边界，不回填、不静默改变旧 key 语义）。SQLAlchemy 模型与迁移结构同步。新版本去重键复用 `ingest_job.dedupe_key`（text，无结构变更），在更新命名空间 `ver1:<document_id>:<sha256>:<expected_active_version_id>` 下存储，与首次上传的裸 SHA-256 键互不匹配；该列因此不是数据库强制的结构化字段，一致性由应用写路径维护。`document_version(document_id, version_no)` 唯一约束是并发分配的最后防线（应用在 `document` 行锁内取 `max+1`）。删除只写 `document` 两列并递增 `knowledge_base.kb_revision`；删除事务锁序与 worker 相反导致的死锁由 API 侧完整事务重试收敛（见 [入库](ingestion.md)）。api 角色对 `document`/`document_version`/`ingest_job` 的既有 SELECT+INSERT+UPDATE 覆盖本次写入；worker 角色对 `document`/`document_version`/`ingest_job` 的 SELECT+UPDATE 覆盖更新发布与领取期静态拒绝，KB 两列权限仍由 `20260925_0007` 提供。物理回收与 `cleanupJobId` 属后续切片。

## 已实现：问答切片（迁移 20260927_0009）

迁移 `20260927_0009_conversation_tables` 紧接 `20260926_0008`，创建 `conversation`、`query_run`、`message`、`citation` 四张表。四张表由迁移账号创建，逐表 `REVOKE ALL ... FROM PUBLIC` 后只给 `citemind_api` 角色 SELECT+INSERT；worker 在本切片没有问答写路径，不获任何权限；不授权 UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER、sequence 或 PostgreSQL ENUM。会话归属与消息序号分配用会话级事务 advisory 锁（`pg_advisory_xact_lock`）序列化，因此不需要给 api 角色任何 UPDATE 权限。

| 表 | 主要字段 | 关键约束与 ACL |
| --- | --- | --- |
| `conversation` | id, organization_id, owner_id, kb_scope JSONB, title, pinned_at, deleted_at, created_at, updated_at | `owner_id` 外键 RESTRICT；`kb_scope` 固化创建时可访问 KB 集合（改写成不了扩大范围的手段）；`title` 可空（首轮提问派生）、`pinned_at` 可空（非空即置顶）、`deleted_at` 可空（非空即软删）；api SELECT+INSERT，另加列级 UPDATE（`title, pinned_at, deleted_at, updated_at`），worker 无权限 |
| `query_run` | id, conversation_id, question, standalone_question, request_id, scope_snapshot JSONB, input_token_budget, output_token_budget, estimated_input_tokens, evidence_count, status, insufficient_evidence, degraded_stages JSONB, llm_usage_id, provider_prompt_tokens, provider_completion_tokens, created_at | `status IN (SUCCEEDED, REFUSED, FAILED)`；两个预算列 `> 0`；本地估算与 provider token 均为 `>= 0` 且分开存储；`question` 非空；api SELECT+INSERT |
| `message` | id, conversation_id, sequence, role, content, query_run_id, created_at | `role IN (user, assistant)`；`sequence > 0` 且 `(conversation_id, sequence)` 唯一；api SELECT+INSERT |
| `citation` | id, message_id, query_run_id, chunk_id, version_id, display_label, locator_snapshot JSONB, quote, quote_hash, created_at | `(message_id, display_label)` 唯一；`display_label` 非空；四个外键 RESTRICT；api SELECT+INSERT |

`citation` 的 `locator_snapshot` 与 `quote` 全部由服务端从已保存 `chunk.source_locator`/`chunk.text` 映射，`quote_hash` 是完整正文 SHA-256；模型只能返回临时 `E` 编号，不能提交 URL、页码或数据库 ID。`query_run.llm_usage_id` 指向对应 provider attempt 的 append-only 账本行（不在本表复制 provider 事实），`estimated_input_tokens` 是本地 tokenizer 估算，不冒充 provider 用量。`query_run.scope_snapshot` 存的是本次检索**实际解析出的可检索 KB 子集**（`knowledge_base.active_index_profile_id` 为 NULL 的 KB 被排除，无可检索 KB 时为空数组），不是会话名义 `kb_scope`，也不做二次范围查询；`query_run.degraded_stages` 只记录真实异常造成的静态阶段标识（当前为 `unsupported_text` 与 `source_retry`），正常的 top-k/同文档限量/预算裁剪不计入。问答四表的真实迁移与授权验收由 `tests/integration/test_conversation_migration.py` 承担（隔离 PG17 上 10 passed）；完整 HTTP/所有者与撤权/引用/预算/版本竞态/用量/失败，以及删除成员行/跨组织来源、实际 scope 快照、第二轮历史实际入参、旧版本历史展示与部分引用撤权验收由 `tests/integration/test_conversation_flow.py` 承担（15 passed）。

## 已实现：会话管理切片（迁移 20260927_0010）

迁移 `20260927_0010_conversation_title_pin_delete` 紧接 `20260927_0009`，只给 `conversation` 增加三个可空列：`title`（首轮提问派生的展示标题，来源是用户真实问题，不编造内容）、`pinned_at`（非空表示置顶，无布尔列与默认值）与 `deleted_at`（逻辑删除时间）。迁移不新增索引、不改表结构之外的其它对象、不 seed、不回填。

删除复用软删，因此 api 角色只追加 `conversation` 的列级 UPDATE，不给全表 UPDATE，也不授予 DELETE/TRUNCATE/REFERENCES/TRIGGER：

```sql
GRANT UPDATE (title, pinned_at, deleted_at, updated_at) ON TABLE conversation TO citemind_api;
```

三个列与应用行为对应：

- `title` 由首轮追问在写入消息的同一事务内按 `title IS NULL` 条件写入一次（`derive_conversation_title` 取首个非空行、折叠空白并截断到 200 字符），不覆盖用户后来的改名；为空仅表示尚无标题。
- `pinned_at` 由 `PATCH /conversations/{id}` 的 `pinned=true` 写 `now()`（重复置顶不刷新原时间）、`pinned=false` 清为 NULL；列表排序为置顶优先，其后按最近消息时间稳定倒序。
- `deleted_at` 由 `DELETE /conversations/{id}` 写入：列表、单会话读取、历史、引用与追加追问的 SQL 全部过滤 `deleted_at IS NULL`（引用通过 `message → conversation` 回表过滤），追加追问在写入事务内取会话级 advisory 锁后重查会话，因此删除与“查删除 + 插入”互相串行，模型调用期间被删不会把回答复活成新消息。删除只写本表，不删共享 `chunk`/`citation`/`llm_usage` 历史事实，也不做物理回收与级联清理。

会话管理切片的真实迁移与列级授权验收仍需在真实 PostgreSQL 上执行 `tests/integration/test_conversation_migration.py` 一类的迁移检查（本片未运行）；离线 SQL 与权限形态由 `tests/unit/test_migration_chain.py` 静态核对，所有者/组织隔离、软删后所有读取路径拒绝与追加追问提交前重查由 `tests/unit/test_conversation_manage.py` 与 `tests/unit/test_conversation_service.py` 的聚焦单测覆盖（不连真实数据库）。

## index profile 契约与 KB active 可见性

`index_profile` 契约已由纯标准库源码模块 `rag_backend.models.profile_contract` 实现（独立 review APPROVED、隔离 api+worker 镜像 tester 已验收：21 passed、非集成 828 passed/2 skipped；源码 SHA `0eabc8c6…`）；默认 `config_hash=4af4c33d4e8d5571cc513dc8c623b1fe66a5565683f95fc75a7b3f8a28dc57fa`、`tokenizer_revision` 摘要 `ca6e9808373afae7a8b131f50361c9b125ba5914eef0161b148b3ab6a105f9a8`，完整 golden 见 [入库与版本](ingestion.md)。KB 的独立 seed 与检索路径仍未实现；发布事务已由默认关闭的真实入库管线实现并端到端验收（首次 READY 置位指针、`kb_revision` 递增），但 dev 库仍为 `20260923_0005` 且未部署，所以当前 `knowledge_base.active_index_profile_id` 仍全部为 NULL。

- `index_profile` 是全局、不可变的编码契约登记表。七个契约字段为 `embedding_model`、`model_revision`、`dimension`、`normalize`、`tokenizer_revision`、`chunker_version`、`keyword_analyzer_version`；`config_hash` 是它们加 `schema_version='index-profile-v1'` 后规范化 JSON 的 SHA-256（算法、默认字段与完整 golden 见 [入库与版本](ingestion.md)）。`parser_version` 与来源相关字段刻意不属于 profile，也不参与 `config_hash`，保存在 `document_version`/`chunk`。
- 登记一个全局 profile 只表示该编码契约可用，不代表任何 KB 可检索；KB 是否可检索由 `knowledge_base.active_index_profile_id` 决定。
- `knowledge_base.active_index_profile_id` 是发布态指针，只表示该 KB 已发布索引当前使用的 profile。新 KB 尚无 READY 索引时保持 NULL；仅首次 READY 发布事务（以及后续 KB 级 profile 切换）可以置位或改写。上传事务与全局 profile 登记都不得把它从 NULL 回填为默认 profile。指针为 NULL 的 KB 不可检索。

默认 profile 的幂等登记入口 `rag_backend.ingestion.profile_repository.ensure_default_index_profile(session)` 已实现（工作树未提交，独立 review APPROVED 并修正 3 项 P2）：只依赖 api 角色对 `index_profile` 的 SELECT+INSERT，按 `config_hash` 执行 `INSERT ... ON CONFLICT (config_hash) DO NOTHING RETURNING id`；未插入时在同一事务内按 `config_hash` 重读既有行并逐项比对七个契约字段，字段不一致抛 `IndexProfileConflictError`，冲突后仍读不到行抛 `IndexProfileNotFoundError`。函数不提交/回滚事务（由调用方拥有事务）、不 UPDATE `index_profile` 或 `knowledge_base`，也不回填 `active_index_profile_id`，并用 `session.no_autoflush` 抑制自动 flush。该入口现已接入**新 Markdown 上传写路径**（同一四表事务内调用，已由独立 tester 验收），KB 创建路径仍不调用；默认关闭的真实入库管线在领取任务后改用身份预检与 `worker_index_identity` 工厂、不调用本入口。该 profile 登记切片没有新迁移、没有 seed 独立 profile，dev 库 `knowledge_base.active_index_profile_id` 仍全部为 NULL；跨源 tokenizer 常量的运行期一致性断言已前置到 `worker_index_identity` 工厂，登记成功不代表任何 KB 可检索。

## 已实现：DOCX 切片（迁移 20260929_0013）

迁移 `20260929_0013` 紧接 `20260928_0012`，线性单 head，只把既有具名 CHECK
`ck_document_source_type` 的允许集合从 `markdown`/`pdf` 扩到 `markdown`/`pdf`/`docx`；不新增
列、表、索引或授权，SQLAlchemy 模型 `Document` 的同一 CHECK 同步。降级**不删除数据**：若库中
已存在 `source_type='docx'` 的文档，降级直接失败并保留原行，只有无 DOCX 行时才恢复旧约束。
真实迁移升级、精确授权与含 DOCX 行的降级拒绝由 `tests/integration/test_document_source_docx_migration.py`
承担。

DOCX 解析器版本 `python-docx-1.2.0-v1` 随 `document_version.parser_version` 保存（该列无 CHECK，
不参与 `config_hash`）；`source_sha256`/locator 与 Markdown/PDF 同一约定，DOCX 使用
`locator_version=3`。

## 已实现：文档 ACL 切片（迁移 20260928_0012）

迁移 `20260928_0012` 紧接 `20260927_0011`，线性单 head，新增：

1. 给 `document` 增加非空 `acl_mode TEXT DEFAULT 'INHERIT'`，具名 CHECK `ck_document_acl_mode` 限定 `INHERIT`/`RESTRICTED`；`server_default` 只服务既有行升级回填，应用或数据库默认均为 `INHERIT`。本迁移不给 `document` 新增授权（既有表级 SELECT+INSERT+UPDATE 已覆盖新列）。
2. 创建 `document_acl`：`id` 主键、`document_id` 外键 ``document``（RESTRICT）、`principal_type TEXT NOT NULL`（CHECK 限定 `USER`）、`principal_id` 外键 `user_account`（RESTRICT）、`permission TEXT NOT NULL`（CHECK 限定 `READ`）、`created_at`；`(document_id, principal_type, principal_id, permission)` 具名唯一约束 `uq_document_acl_document_principal_permission`。

授权：`document_acl` 逐表 `REVOKE ALL ... FROM PUBLIC` 后只给 `citemind_api` **SELECT + INSERT + DELETE**，这是运行角色首次获得 DELETE（全量替换需要删除旧名单行）；不给 UPDATE（名单行不可变）、也不给 TRUNCATE/REFERENCES/TRIGGER、sequence 或 PostgreSQL ENUM。`citemind_worker` 不获任何权限。降级只删除 `document_acl` 表、`acl_mode` 列与其 CHECK。

数据模型不强制“名单用户是同组织有效 KB 成员”与“`INHERIT` 时名单为空”：前者由写入事务核对，后者由应用校验并保证写入时清空；直接写库可以绕过，二者都由读取判定的权威链与服务端校验共同保障。真实迁移与授权验收由 `tests/integration/test_document_acl_migration.py` 承担（升级、精确授权、api 插入/删除与 UPDATE 拒绝、worker 拒绝、降级无残留）；权限、revision 与锁序由 `tests/integration/test_document_acl_flow.py` 承担。

## 已实现：增量 embedding 缓存（迁移 20260929_0014）

缓存**不新增表**，直接复用既有 `chunk_embedding`；原计划的 `embedding_cache` 表作废。worker 在编码前按 `chunk.model_input_hash` 批量查询可复用向量，命中必须经权威链 `chunk→index_generation→document_version→document→knowledge_base`，且同 `organization_id`、`g.status='READY'`、`g.profile_id=:profile_id`、`ce.profile_id=:profile_id`，并排除已删除文档（`deleted_at` 非空或 `DELETED`）；不信任 `chunk` 上冗余的 `organization_id`/`kb_id`/`document_id`/`version_id`。缓存只复用向量，不复用来源位置，允许同组织跨文档与旧版本复用，禁止跨组织。

迁移 `20260929_0014` 只给 `chunk(model_input_hash)` 新增具名 btree 索引 `ix_chunk_model_input_hash`，使批量查找有索引支撑；降级只删除该索引，不动数据与授权。SQLAlchemy 模型 `Chunk` 同步声明同名索引。真实迁移与精确索引由 `tests/integration/test_chunk_model_input_hash_index_migration.py` 承担（无 Docker 守护进程时未跑并标注）。

## 计划中：后续切片

以下实体与字段仍未实现。

| 实体 | 主要字段 | 关键约束与用途 |
| --- | --- | --- |
| `conversation` / `message` | owner_id, kb_scope；role, content, query_run_id, status | 会话属于用户；历史访问按当前 ACL 复核（已由 `20260927_0009` 落地，见上文） |
| `query_run` / `retrieval_hit` | 问题、scope_snapshot、配置、版本、阶段耗时、tokens、cost；chunk_id、两路排名、RRF/rerank 分数 | 调试与复算；权限撤销后也需过滤（`query_run` 已落地；`retrieval_hit` 仍为计划） |
| `citation` / `feedback` | message_id, chunk_id, version_id, locator_snapshot, quote_hash；评分与预期证据 | 引用不接受 LLM 自造 URI；反馈不直接在线训练（`citation` 已落地；`feedback` 仍为计划） |
| `eval_dataset` / `eval_case` / `eval_run` / `eval_result` | 数据集版本、角色、gold spans、split；配置、模型 revision、逐题结果 | 保留历史运行，不覆盖；gold 使用源区间而非 chunk ID |
| `audit_event` | actor_id, action, target_id, before_hash, after_hash, request_id, created_at | 记录授权、删除、索引切换等，默认不记正文 |

`document_acl` 已于文档 ACL 切片（迁移 `20260928_0012`）落地，不再列入计划表。增量 embedding 缓存改为复用既有 `chunk_embedding`，不新增 `embedding_cache` 表，也不再列入计划表（见“已实现：增量 embedding 缓存”）。
第一切片已实现的 `ingest_job` 已在第二切片新增可空 `generation_id`；第一切片已实现的 `document` 已在文档 ACL 切片（`20260928_0012`）补加 `acl_mode`；`document_acl` 也已落地。完整主关系仍是 `knowledge_base → document → document_version → index_generation → chunk → chunk_embedding`；任务为 `ingest_job → outbox_event → Celery 消息`；问答为 `conversation → message → query_run / citation`，检索命中归 `query_run`。授权沿 `auth_session → user_account → kb_member → document_acl` 应用于资源读取与两路检索。

## 数据库约束和索引

第一切片已实现的索引与唯一约束：`document(kb_id,lifecycle_status)`、`ingest_job(status,next_run_at)`、`outbox_event(status,next_send_at)` 三个二级索引，以及 `index_profile(config_hash)`、`document_version(document_id,version_no)`、`ingest_job(dedupe_key)` 三个唯一约束。所有表的主键都是 `pk_<table>`，全部具名 CHECK、外键与索引遵循同一命名规则。

第二切片已实现的索引与唯一约束：`index_generation(version_id,profile_id,status)` 二级索引、`index_generation(version_id,profile_id) WHERE status='READY'` 部分唯一索引、`chunk(generation_id)` 二级索引、`chunk(generation_id,chunk_index)` 唯一约束与 `GIN(chunk.fts)`。迁移 `20260929_0014` 另给 `chunk(model_input_hash)` 建具名 btree 索引 `ix_chunk_model_input_hash`，服务增量 embedding 缓存。其中部分唯一索引是并发发布的最后约束。第四切片已实现的唯一约束：`auth_session(token_hash)`、`user_account(organization_id, username)` 与 `kb_member(kb_id, user_id)`；这三张表都不建额外二级索引。以下仍是计划，尚未实现：面向“按用户列出可访问 KB”的 `kb_member(user_id,kb_id)` 二级索引和 `query_run(user_id,created_at)`。第三切片的 `llm_usage` 不建二级索引与唯一约束，只有主键与具名 CHECK。第五切片只增列与具名外键 `fk_ingest_job_profile_id_index_profile`，不新增任何二级索引、唯一约束或 ACL。MVP 精确向量检索不建 ANN 索引；引入 HNSW 前测授权过滤下的召回。

文件、向量、聊天与审计按用途分开保留，保留策略尚未实现。演示环境计划保留原文及最近 3 版索引、查询明细 30 天、脱敏汇总 90 天；删除文档先禁止访问，再按保留策略清理文件、chunk、向量缓存和引用正文。保留期限和清理作业须在实现时由配置与测试固定，不能仅靠本页文字生效。

第一切片与第二切片的真实 PostgreSQL 迁移验收分别由 `tests/integration/test_core_migration.py` 与 `tests/integration/test_second_slice_migration.py` 承担：第二切片聚焦测试 9 passed、当时全部 integration 为 25 passed，覆盖 9 张表、具名约束、GIN 与部分唯一索引、无 sequence/ENUM/ANN、PUBLIC 收权、api/worker 差异、512 维可插入与非 512 维被拒、主键与部分唯一冲突，finally 降级回 `20260922_0002` 与 base 后第一切片 6 表仍完整、新对象与授权无残留。第三切片由 `tests/integration/test_llm_usage_migration.py` 承担，核对 append-only 表、具名约束、PUBLIC 收权、api 仅 SELECT+INSERT、worker 无权限、成功/失败/超时事实与非负约束，已在用 `.env.example` 开发占位密码启动的隔离 Compose 专用测试库上运行：聚焦 13 passed，全部 integration 38 passed、2 skipped（跳过的 2 个是未配置 Redis 的 broker 用例）；该测试在 finally 降级回 `20260922_0003` 与 base 后确认无残留。未配置 `TEST_DATABASE_URL` 与 `ALLOW_DESTRUCTIVE_TEST_DB=1` 时按守卫跳过，跳过不代表通过。第四切片的真实 PostgreSQL 迁移与授权验收由 `tests/integration/test_identity_migration.py` 承担，覆盖三张身份表、具名约束/外键、PUBLIC 收权、api 仅 SELECT+INSERT+UPDATE、worker 无权限、用户名/令牌/成员唯一约束、角色 CHECK 与软撤销，已在 PostgreSQL 17.11 + pgvector 0.8.6 的专用测试库上运行：聚焦 10 passed，随后全部 `-m integration` 为 52 passed、2 skipped（跳过的 2 个是未配置 `TEST_REDIS_URL` 的 broker 用例）；该测试在 finally 降级回 `20260923_0004` 与 base 后确认身份表与授权无残留。
