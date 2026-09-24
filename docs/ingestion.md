# 文档入库、索引与版本

> 本文描述完整的 Markdown 与可提取文本 PDF 入库契约。当前只实现了 Markdown 上传受理（`POST /api/v1/knowledge-bases/{kb_id}/documents`）：校验、KB 私有内容寻址保存，以及单事务写入 `document`/`document_version`/`ingest_job`/`outbox_event` 并返回 `202`。**没有 dispatcher，也没有 worker 业务任务**，因此 outbox 事件不会被投递、任务不会进入 worker；解析、切分、编码、索引发布、检索与文件回收均未实现，不能据此声称任何文档可被检索。第二切片的 `index_generation`、`chunk` 与 `chunk_embedding` 存储 schema（含 512 维向量列、`GIN(fts)` 与部分唯一索引）已由迁移 `20260922_0003` 落地，但 worker 写入路径、跨表来源一致性核对和发布事务仍未实现。MVP 目标格式为 Markdown 与可提取文本的 PDF；DOCX 与静态网页属于后续完整范围，PDF 与下文其余格式当前均未实现。

## 入库状态与流程

1. 已实现：API 校验 KB `EDITOR` 角色、`Origin`、CSRF、必填 `Idempotency-Key`、`.md`/`.markdown` 后缀、有效 UTF-8 文本与 20,000,000 字节单文件上限；授权、CSRF、Origin 与接收阶段体积上限都在读取 multipart 正文之前完成。原文件存入 api 进程私有的 `api-documents` 命名卷（容器路径 `/var/lib/citemind/documents`，由可选配置 `DOCUMENT_STORAGE_DIRECTORY` 指定），相对路径只由服务端 `knowledge_base.id` 与内容 SHA-256 派生（`{kb_id}/{sha256}`），不拼接用户文件名。真实 MIME/PDF 页数校验、PDF 支持与 KB 配额未实现（KB 配额没有对应存储字段或实体）。
2. 已实现：同一 KB、同一 `Idempotency-Key`、同内容且同标题的重复上传复用已有任务并返回同一组 id；内容或标题任一不同返回 409，跨 KB 的去重键互相独立且不通过冲突响应暴露其他 KB 的 key 使用情况。去重键是组织+KB+规范化 key 的 SHA-256，不保存也不回显原始 key。并发同 key 由唯一约束拒绝后回滚重读，再按复用/冲突规则处理。上传事务一并写入 `document`（`CREATED`、`active_version_id=NULL`、`source_type=markdown`）、`document_version`（`version_no=1`、`PENDING`、`parser_version=markdown-v1`，该版本仅登记契约、本切片不解析正文）、`ingest_job`（`QUEUED`、`attempt=0`）与 `outbox_event`（`ingest.requested`、`PENDING`），提交后返回 `202`；这时文档仍不可检索。
3. 目标（未实现）：worker 按 `QUEUED → PARSING → CHUNKING → EMBEDDING → INDEXING → READY` 执行，记录阶段、进度、错误码与尝试次数。解析限制为单次 60 秒，超时要终止实际解析子进程。永久格式错误进入 FAILED；临时故障最多重试 3 次。当前没有 dispatcher，`QUEUED` 任务不会进入 worker。
4. 目标（未实现）：解析器生成有位置映射的结构块；切分后保存 chunk 文本、token 数、hash、来源 locator、解析器与切分器版本。对缓存未命中的模型输入批量编码，将向量、FTS 与 locator 写入不可见的 staging generation。
5. 目标（未实现）：校验 chunk 数、非空文本、向量维度、映射和预期文档版本后，在锁定文档的事务中发布 READY generation 并切换 `active_version_id`。首次导入失败不可检索；更新失败时旧版继续服务。

已发布的最终 blob 在事务回滚或后续数据库异常时不删除，因为并发事务可能已引用它；这会产生无引用的孤儿文件窗口，本切片不实现 GC，回收与一致性核对留待后续作业。

## 来源定位与切分

| 格式 | 解析方式 | 引用位置 | 降级边界 |
| --- | --- | --- | --- |
| Markdown | markdown-it-py，保留标题、段落、列表和代码块 | 版本、heading_path、1-based 起止行、原文 hash | token.map 起点是 0-based，结束不含边界；统一转换并测试；展示 HTML 要消毒 |
| 文本 PDF | pypdf 逐页抽取 | 版本、页码、规范化文本偏移 | MVP 只保证页定位；扫描件标 `NEEDS_OCR`，不把空提取当成功；复杂版面标记质量警告 |
| DOCX（后续） | python-docx 按原顺序遍历段落和表格 | 版本、heading_path、段落或表格单元格索引 | 不推测 Word 页码；嵌套、合并表格单独验收 |
| 静态网页（后续） | 受限 HTTPX 抓取，BeautifulSoup4/lxml 清洗正文 | 原 URL、抓取时间、快照 hash、标题路径和 block ID | 不执行 JS、不登录、不递归抓取；保留结构快照与原 HTML |

初始目标为每 chunk 约 360 个实际 tokenizer token、约 60 token 重叠，优先在标题和段落边界切分；短段落不强加重叠。标题、正文与特殊 token 合计不能超过 embedding 模型的 512 长度。PDF 可按页切以保留明确页码；表格序列化时继承表头并记录行范围。复杂跨页表格标低质量，不承诺准确表格推理。

每个 chunk 至少带 `organization_id`、`kb_id`、`document_id`、`version_id`、`generation_id`、`chunk_index`、`heading_path`、`source_locator`、`text_hash`、`token_count`、`parser_version`、`chunker_version`。来源在解析时固定，生成模型不能补猜。

## outbox、租约与恢复

- `ingest_job` 和 outbox 由同一 PostgreSQL 事务保存。消息只含 `jobId` 和协议版本，JSON 序列化，不含正文、凭据或可执行函数路径。
- dispatcher 在短事务中用行锁/`SKIP LOCKED` 领取待发事件，保存 owner、单调增加的 `lease_token` 与期限，提交后投递 Redis。结果回写要求事件仍待发、租约未过期且 token 相同；迟到的旧发送者不能覆盖新领取者。
- worker 以独立 Session 原子领取 job 租约。已 READY/CANCELLED 的任务直接退出；有效租约未过期时不重复处理。每阶段与发布时核对租约、文档 tombstone 和期望版本。重复执行最多生成暂存数据，不产生重复有效索引。
- dispatcher 扫描未投递或租约过期的任务，按数据库状态补偿；补投创建新 outbox 事件，保留旧 SENT 记录。Redis 恢复后继续；不能以 Celery result backend 代替业务事实。Redis 可启 AOF，但投递语义仍是“至少一次 + 幂等发布”。
- 初始 worker `concurrency=1`、`worker_prefetch_multiplier=1`、`acks_late=True`；评估 `task_reject_on_worker_lost`，软/硬任务时限可从 8/10 分钟起测，Redis visibility timeout 要大于硬时限。取消或删除时，worker 在检查点停下，发布事务再次核验。

## 更新、删除和 profile 变更

原文件 checksum、index profile 与所有编码输入都不变时，可跳过解析和编码。embedding 缓存键包含规范化模型输入 hash、模型 revision、维度、pooling/normalize 设置、编码角色和预处理版本；缓存仅复用向量，不复用旧来源位置。可编辑展示标题不参与当前编码；若以后加入编码，需把它纳入输入指纹。

普通文档更新构建新版本并只切换该文档的有效指针。发布事务锁定 document，核对 `expected_active_version`，把同版本/profile 的旧 READY generation 退役，发布新 generation，再更新指针；并发失败者回滚重读。删除先 tombstone 并递增权限/知识库 revision，新检索与引用立即失效，随后异步回收文件和索引。

MVP 冻结 index profile。更换 embedding 模型、维度、切分器或分词契约时新建 profile/generation，不能混写旧列。未来 KB 级 profile 切换要记录开始时的 `kb_revision` 和有效文档清单；所有新增、更新、删除发布事务都锁 KB 行并递增 revision；切换时持同一锁核对 revision 未变且清单全 READY，否则中止并补建。

必测故障：事务提交后 Redis 断连、投递成功但 SENT 回写失败、worker 在构建中被杀、broker 重启、重复消息、解析超时和旧 dispatcher 租约过期后迟到回写。详见 [评估与验收](evaluation.md)。
