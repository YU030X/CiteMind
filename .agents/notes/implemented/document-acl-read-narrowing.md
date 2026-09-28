# Agent Note：文档 ACL 只收紧读取与首次 DELETE 授权

- 状态：已实现
- 范围：`document.acl_mode`、`document_acl`、文档读取/检索/下载与 `PUT /documents/{id}/acl`

## 背景

KB 成员授权是粗粒度的：KB 内所有有效成员都能读该 KB 的全部未删除文档，OWNER 也无法把
单篇文档对某些成员隐藏。Phase 2 需要文档级 ACL，但必须在**不锁死管理**、不扩大现有权限、
不引入新死锁环的前提下落地。同时 `/documents/{id}/content` 需要下载原文，而原文件此前只被
worker 只读读取，API 没有受权下载入口。

## 决策

1. **ACL 只收紧读取，不改变管理权。** `document.acl_mode` 为 `INHERIT`（默认，沿用 KB
   成员权限）或 `RESTRICTED`（只允许 `document_acl` 中登记 `USER`/`READ` 的用户）。ACL
   不参与更新、删除、成员管理：KB `EDITOR` 仍可上传新版本，`OWNER` 仍可删除与管理 ACL。
   `RESTRICTED` 空名单表示无人可读，连 OWNER 也不能读（管理权不等于读权），但 OWNER 仍可
   通过同一端点切回 `INHERIT` 恢复。这样 ACL 不会把文档变成无法管理。
2. **允许名单不是独立授权。** 读取仍要求用户是当前组织内未撤销的 `kb_member`，ACL 只在
   其上收紧。组织与成员关系走权威链 `document → knowledge_base → kb_member`，避免把
   `document_acl` 当第二条授权来源。
3. **所有读取路径统一叠加同一判定。** 文档列表/详情、向量与关键词两条候选 SQL、证据正文、
   来源状态复核都增加 `(acl_mode='INHERIT' OR EXISTS(allow-list))`。`load_chunk_source_states`
   刻意**不过滤**行，而是多返回一个 `acl_allowed` 布尔列，让 `is_authorized()` 把 ACL 纳入
   判定；否则用 `EXISTS` 过滤行会让「无成员行」与「ACL 拒绝」的 LEFT JOIN 语义混淆，历史
   引用可能被误判为仍授权。历史、引用、在途回答与追问改写的复核因此自动继承该布尔列。
4. **首次给运行角色 DELETE。** 此前 `citemind_api` 对任何业务表都没有 DELETE（软删优先）。
   全量替换 ACL 名单需要删除旧行，`document_acl` 是唯一例外：api 只有
   `SELECT, INSERT, DELETE`，没有 UPDATE（名单行不可变，只整行换）；worker 无权限。
   业务文档、blob、向量均不因此获得 DELETE。
5. **锁序与删除一致，避免新环。** 替换事务顺序为 `document` 行锁 → `knowledge_base` 行锁
   （`acl_revision` 条件递增）→ `document_acl` 删除/插入。删除已是 `document → knowledge_base
   → ingest_job`，成员替换是 `knowledge_base → kb_member`；ACL 只读 `kb_member` 不锁它，
   因此不与其成环。锁内复核调用者仍是 OWNER，避免授权校验与写入之间的 TOCTOU。
   `acl_revision` 只在 `acl_mode` 或名单实际变化时递增，重复替换幂等。
6. **下载在 IO 前后各鉴权一次。** `GET /documents/{id}/content` 默认交付 active，`versionId`
   可显式指定同文档版本；跨文档/跨组织/未授权/已删除统一 404。读取在 IO 线程内用
   `DocumentBlobStore.read_verified_blob`（严格 `file_ref`、摘要、20,000,000 字节），IO 前
   结束数据库事务、期间不持连接；交付前重核授权与版本：撤权/删除 404，默认 active 变化
   409（不用新 active 的授权交付旧字节），显式旧版本仍可下载；blob 损坏返回静态 500，不回显
   路径或摘要。响应 `no-store`/`nosniff`/`attachment`，MIME 只从受控来源映射。

## 后果与边界

- ACL 是读取收紧机制，不提供「按用户可见性」的写入隔离；同一 KB 的 EDITOR 仍能更新被
  `RESTRICTED` 的文档。
- 名单用户必须仍是同组织、启用的 KB 成员：成员被撤销或账号被禁用后，即使仍在
  `document_acl` 中也不可读，无需清理 ACL 行。数据库不强制组织一致，由写入事务核对。
- 撤权/删除在读取与交付之间生效时，服务端返回 404/409；但已经交付的字节无法追回。
- 只有 api 角色的 `document_acl` 获得 DELETE；worker 与 PUBLIC 均无权限，文档表本身仍无
  运行角色 DELETE。
- 本切片不提供版本的授权列表接口、`HEAD`/`Range`、HTML 预览或物理回收；下载允许
  `PENDING`/`FAILED`/`NEEDS_OCR` 版本，只要其条目存在且 blob 通过校验（原文件在受理时已落盘）。
- 迁移与权限由 `tests/integration/test_document_acl_migration.py` 验收；权限、revision、
  检索/证据拒绝、下载与锁序由 `tests/integration/test_document_acl_flow.py` 验收；单元层
  覆盖纯校验与路由契约（`tests/unit/test_document_acl.py`、`tests/unit/test_document_content.py`）。
