# 文档入库、索引与版本

> 本文描述完整的 Markdown 与可提取文本 PDF 入库契约。当前已实现的入库能力是 Markdown 上传受理（`POST /api/v1/knowledge-bases/{kb_id}/documents`：校验、KB 私有内容寻址保存，以及单事务写入 `document`/`document_version`/`ingest_job`/`outbox_event` 并返回 `202`）与纯 Markdown 解析/切分（纯内存，未落库，见“来源定位与切分”）。outbox dispatcher 与 worker 接收壳代码已写：API 单进程 lifespan 可按配置运行 dispatcher，Compose 的 api 服务显式启用而宿主 Settings 默认关闭；真实 PostgreSQL/Redis 投递、worker 接收标记与重复投递、应用层故障注入为 pytest 自动集成，物理 Redis 停启后同一 publisher 退避再 SENT、worker 被 kill 后的补偿补投与幂等收敛由仓库外隔离手工探针实测（见 [开发约定](development.md)），但 Linux 容器 prefork 下的业务故障恢复、自然 3600 秒 visibility 重投、多 worker 并发、手工恢复 `DELIVERY_UNCONFIRMED` SQL 与 API 日志超过 12 轮仍未验收。接收壳只校验 job 状态、版本归属与文档 tombstone，并写 job 级接收标记（`lease_owner=event:<id>`、heartbeat、`error_code=HANDLER_NOT_READY`），job 状态仍为 `QUEUED`；outbox `SENT` 只表示 broker 投递，不等于 worker 解析或入库。纯 Markdown 解析与切分已实现并独立验收（见下文），但尚未接入 worker、未读 blob、未写入 `generation`/`chunk`/`chunk_embedding`，也未接入真实模型 tokenizer；编码、索引发布、检索与文件回收仍未实现，不能据此声称任何文档可被检索。第二切片的 `index_generation`、`chunk` 与 `chunk_embedding` 存储 schema（含 512 维向量列、`GIN(fts)` 与部分唯一索引）已由迁移 `20260922_0003` 落地，但 worker 写入路径、跨表来源一致性核对和发布事务仍未实现。MVP 目标格式为 Markdown 与可提取文本的 PDF；DOCX 与静态网页属于后续完整范围，PDF 与下文其余格式当前均未实现。

## 入库状态与流程

1. 已实现：API 校验 KB `EDITOR` 角色、`Origin`、CSRF、必填 `Idempotency-Key`、`.md`/`.markdown` 后缀、有效 UTF-8 文本与 20,000,000 字节单文件上限；授权、CSRF、Origin 与接收阶段体积上限都在读取 multipart 正文之前完成。原文件由 API 写入 `api-documents` 命名卷（容器路径 `/var/lib/citemind/documents`，由 `DOCUMENT_STORAGE_DIRECTORY` 指定），相对路径只由服务端 `knowledge_base.id` 与内容 SHA-256 派生（`{kb_id}/{sha256}`），不拼接用户文件名；Compose 中 worker 以同一 `DOCUMENT_STORAGE_DIRECTORY` 只读（`:ro`，uid 10001）挂载同一命名卷，inference 不挂载。`DocumentBlobStore.read_verified_markdown` 提供严格 `file_ref` 匹配、父/叶符号链接与 Windows 联接点尽力校验、常规文件与 20,000,000 字节/摘要/UTF-8 控制字节校验的有界 fd 读取，读失败为 `BlobReadError`、非法 `file_ref` 为 `InvalidBlobReference`；当前 `worker.py` 尚未调用该读取器。真实 MIME/PDF 页数校验、PDF 支持与 KB 配额未实现（KB 配额没有对应存储字段或实体）。
2. 已实现：同一 KB、同一 `Idempotency-Key`、同内容且同标题的重复上传复用已有任务并返回同一组 id；内容或标题任一不同返回 409，跨 KB 的去重键互相独立且不通过冲突响应暴露其他 KB 的 key 使用情况。去重键是组织+KB+规范化 key 的 SHA-256，不保存也不回显原始 key。并发同 key 由唯一约束拒绝后回滚重读，再按复用/冲突规则处理。上传事务一并写入 `document`（`CREATED`、`active_version_id=NULL`、`source_type=markdown`）、`document_version`（`version_no=1`、`PENDING`、`parser_version=markdown-v1`，该版本是占位登记值、上传事务不调用解析器）、`ingest_job`（`QUEUED`、`attempt=0`）与 `outbox_event`（`ingest.requested`、`PENDING`），提交后返回 `202`；这时文档仍不可检索。
3. 目标（未实现）：worker 按 `QUEUED → PARSING → CHUNKING → EMBEDDING → INDEXING → READY` 执行，记录阶段、进度、错误码与尝试次数。解析限制为单次 60 秒，超时要终止实际解析子进程。永久格式错误进入 FAILED；临时故障最多重试 3 次。dispatcher 与 worker 接收壳已在隔离 PostgreSQL/Redis/Celery 上验收（投递与应用层故障注入为 pytest 自动集成，物理 Redis 停启与 worker 被 kill 后补投由仓库外隔离手工探针实测）；接收壳只写接收标记，不进入 PARSING，`QUEUED` 任务尚未真正处理。
4. 已实现（纯内存解析与切分，未落库）：Markdown 解析器（`markdown-it-py==4.2.0`，实现版本 `markdown-it-py-4.2.0-v1`）把原始 UTF-8 字节解析成带来源位置的块：`token.map` 的 0-based 且结束不含边界统一转成 1-based 闭区间；标题按层级维护，进入引用块/列表容器时快照、退出时恢复；段落、列表与 fence/缩进代码块各自成块；原始 HTML 只识别、从不渲染且被排除在正文之外；图片只保留 alt，绝不抓取 URL。`source_sha256` 只按原始 bytes 计算（CRLF 与 Unicode 变化都会反映），解码用 `utf-8-sig` 只忽略文件开头 BOM、不改变行号。切分器把 `heading_path` 与正文拼成完整模型输入后交给注入的 token 计数器，按约 360 目标、约 60 重叠、512 硬上限打包；单块超硬上限时先在 `max_tokens` 处按字符确定性硬拆并保留重叠，短段落不强制重叠，chunk 内容不伪造。`source_locator` 是 `locator_version=1` 的 JSON：含 `source_type`、`parser_version`、`source_sha256`、所跨块的 1-based `start_line`/`end_line`（块级粗粒度行范围，不是精确片段行）、`block_ordinals`，以及每个 piece 在其规范化块正文内的 `block_char_start`/`block_char_end` 字符区间。上传事务目前仍写占位版本 `markdown-v1`（`validation.MARKDOWN_PARSER_VERSION`），与真实解析器版本不一致；本切片不改上传事务。
5. 目标（未实现）：worker 读取 blob、调用上述纯解析/切分，把 chunk 文本、token 数、hash、来源 locator、解析器与切分器版本写入 `chunk`，对缓存未命中的模型输入批量编码，并把向量、FTS 与 locator 写入不可见的 staging generation；接线前必须统一上传事务登记的解析器版本，并把假计数器换成实际模型 tokenizer 与服务预算。
6. 目标（未实现）：校验 chunk 数、非空文本、向量维度、映射和预期文档版本后，在锁定文档的事务中发布 READY generation 并切换 `active_version_id`。首次导入失败不可检索；更新失败时旧版继续服务。

已发布的最终 blob 在事务回滚或后续数据库异常时不删除，因为并发事务可能已引用它；这会产生无引用的孤儿文件窗口，本切片不实现 GC，回收与一致性核对留待后续作业。

## 来源定位与切分

| 格式 | 解析方式 | 引用位置 | 降级边界 |
| --- | --- | --- | --- |
| Markdown（纯解析/切分已实现，未落库） | markdown-it-py 4.2.0，保留标题、段落、列表与 fence 代码块；原始 HTML 不渲染且不进正文，图片只取 alt | 解析器版本、heading_path、所跨块的 1-based 块级起止行、`source_sha256`（原始 bytes）、块 ordinal 与块内规范化正文的字符区间 | token.map 起点 0-based、结束不含边界，统一转 1-based 闭区间；行号是块级粗粒度而非精确片段行；展示层必须转义，不能当 HTML |
| 文本 PDF | pypdf 逐页抽取 | 版本、页码、规范化文本偏移 | MVP 只保证页定位；扫描件标 `NEEDS_OCR`，不把空提取当成功；复杂版面标记质量警告 |
| DOCX（后续） | python-docx 按原顺序遍历段落和表格 | 版本、heading_path、段落或表格单元格索引 | 不推测 Word 页码；嵌套、合并表格单独验收 |
| 静态网页（后续） | 受限 HTTPX 抓取，BeautifulSoup4/lxml 清洗正文 | 原 URL、抓取时间、快照 hash、标题路径和 block ID | 不执行 JS、不登录、不递归抓取；保留结构快照与原 HTML |

初始目标为每 chunk 约 360 个实际 tokenizer token、约 60 token 重叠，优先在标题和段落边界切分；短段落不强加重叠。标题、正文与特殊 token 合计不能超过 embedding 模型的 512 长度。已实现的切分器用可注入的假 `TokenCounter` 验证这套预算（target 360 / overlap 60 / max 512），**512 上限目前只对假计数器成立**；真实模型 tokenizer 与服务预算尚未接入，接线前必须统一解析器版本并换成实际推理 tokenizer。PDF 可按页切以保留明确页码；表格序列化时继承表头并记录行范围。复杂跨页表格标低质量，不承诺准确表格推理。

每个 chunk 至少带 `organization_id`、`kb_id`、`document_id`、`version_id`、`generation_id`、`chunk_index`、`heading_path`、`source_locator`、`text_hash`、`token_count`、`parser_version`、`chunker_version`。来源在解析时固定，生成模型不能补猜。当前纯切分产出的内存 `Chunk` 只含 `chunk_index`、正文、`heading_path`、`token_count`、`text_hash`、`model_input_hash`、`source_locator`、`parser_version`、`chunker_version`；`organization_id`/`kb_id`/`document_id`/`version_id`/`generation_id` 等落库字段尚未接线。定位可用同版本解析器重放加 `segments` 的块内字符区间还原规范化块正文，而不是对原始文件的字节偏移。

## outbox、租约与恢复

- `ingest_job` 和 outbox 由同一 PostgreSQL 事务保存。消息 JSON 正文只含 `jobId` 与 `protocolVersion=1`，不含正文、凭据或可执行函数路径；Celery `task_id` 承载 outbox 事件 id，任务投递到专用 `ingest` 队列。
- 已实现（隔离 PostgreSQL/Redis 已验收）：dispatcher 在短事务中用行锁/`SKIP LOCKED` 领取待发事件，保存 owner、单调增加的 `lease_token` 与期限，提交后才向 Redis 投递。回写要求事件仍待发、租约未过期且 token/owner 相同；迟到的旧发送者不能覆盖新领取者。简单短事务（领取、退避重排、失败标记、补投插入）仍用事务 `now()`；会等待 job 或 outbox 行锁、必须在解锁后反映真实时刻的语句改用 PostgreSQL `clock_timestamp()`：回写 SENT 的租约谓词与 `sent_at`/`updated_at`、推后接收宽限的 `next_run_at`、以及 worker 写接收标记的 `lease_until`/`heartbeat_at`。租约与接收宽限均为 60 秒。推后宽限先用 `MATERIALIZED` CTE 取得 job 行锁再计算 `clock_timestamp()+60s`，避免 PostgreSQL 在等锁扫描阶段就求值表达式；`MARK_SENT` 是 `UPDATE`，在 READ COMMITTED 下等锁后由 PostgreSQL EPQ 用新行版本重估谓词，因此 `clock_timestamp()` 能拒绝已过期租约——这只适用于该 `UPDATE` 加提交路径，不代表任意纯行锁等待都会重估过期条件。
- 已实现（隔离 Redis/Celery 接收已验收，业务入库未实现）：worker 接收壳在同一事务中先 `SELECT ... FOR UPDATE OF j` 锁住 job，只核对 job 是否仍 `QUEUED`、`document_version.document_id` 是否等于 job 的文档、文档是否已 tombstone。通过后写 job 级 `lease_owner=event:<eventId>`、随机 `lease_token`、`lease_until`、`heartbeat_at` 与 `error_code=HANDLER_NOT_READY`，其中期限与心跳用解锁后的 `clock_timestamp()`，`job.status` 保持不变；重复消息命中已有标记时不写任何字段。它不做解析、切分、编码或索引发布。
- 已实现（pytest 自动集成覆盖投递与应用层故障注入，物理 Redis 停启与 worker kill 由隔离手工探针实测）：`outbox SENT` 只表示 broker 已收到投递，不表示 worker 已解析或入库；job `QUEUED` 且无接收标记、无 PENDING 事件时，补偿扫描新建 PENDING 事件补投并保留旧 SENT，达到 `MAX_DELIVERY_ATTEMPTS=5` 后写 `error_code=DELIVERY_UNCONFIRMED` 停止热循环。发送失败（含 Redis 不可达）保持 PENDING 并按 5 秒起、300 秒封顶的指数退避重排。Redis 恢复后继续；不以 Celery result backend 代替业务事实。`SENT`、`HANDLER_NOT_READY` 与 `QUEUED` 都不等于入库；物理 Redis 停启、worker 被 kill 后补偿补投与重复消息幂等收敛由仓库外隔离手工探针实测（不在 pytest 自动用例内），Linux 容器 prefork 下的业务故障恢复与自然 3600 秒重投仍未验收。
- 已实现的 worker 传输配置：`worker_prefetch_multiplier=1`、`acks_late=True`、`task_acks_on_failure_or_timeout=True`、broker visibility timeout 3600 秒、不配置 result backend。业务阶段时限、`task_reject_on_worker_lost`、取消/删除检查点与发布事务核验均未实现。本轮未新增迁移，worker 对 outbox 的数据库权限也未变化。

## 未确认投递的查看与手工恢复

当前没有读取 job 状态或恢复投递的 HTTP API 或 CLI：`DELIVERY_UNCONFIRMED` 与 `UNSUPPORTED_EVENT_TYPE` 只落在 `ingest_job.error_code`，`outbox_event` 需要直接查询。以下只描述授权运维在目标库上的查看与有条件恢复步骤；该手工恢复 SQL 本轮仍未在真实库执行，因此是待验证操作，不是已验证流程（其余 dispatcher 隔离真库用例已通过）。执行前必须确认目标库已应用 `20260922_0002`，且连接角色拥有 `ingest_job` 的 SELECT/UPDATE 与 `outbox_event` 的 SELECT/INSERT（迁移把这三类权限授予 `citemind_api`；`citemind_worker` 没有 outbox 写权限，不能在 worker 角色下恢复）。凭据由运维从受管环境注入，本文不写 DSN、用户名或密码，也不自动操作开发库。

查看（只读）。`:'job_id'` 是 psql 变量写法（先 `\set job_id <uuid>`）；其他客户端改成对应绑定参数，不要字符串拼接。`jobId` 来自上传的 `202` 响应：

```sql
SELECT j.id, j.status, j.error_code, j.attempt, j.next_run_at,
       j.lease_owner, j.lease_until, j.heartbeat_at,
       e.id AS event_id, e.event_type, e.status AS event_status,
       e.dispatch_attempt, e.sent_at
FROM ingest_job AS j
LEFT JOIN outbox_event AS e ON e.job_id = j.id
WHERE j.id = :'job_id'::uuid
ORDER BY e.created_at, e.id;
```

`error_code='DELIVERY_UNCONFIRMED'` 表示有旧 `SENT` 事件在 60 秒接收宽限内未被 worker 写下接收标记，补偿扫描到 `MAX_DELIVERY_ATTEMPTS=5` 后停止热循环；`error_code='UNSUPPORTED_EVENT_TYPE'` 表示 dispatcher 拒绝的事件类型，其对应 outbox 事件为 `FAILED`。

有条件手工补投**仅适用于 `DELIVERY_UNCONFIRMED`，且仅在真实 worker 处理器上线并明确接收标记复位策略之后**。恢复前必须同时满足：`status='QUEUED'`、`error_code='DELIVERY_UNCONFIRMED'`、没有 `PENDING` 事件、没有接收标记（`lease_owner` 不以 `event:` 开头且 `heartbeat_at IS NULL`）。步骤在单个事务内先 `FOR UPDATE` 锁住 job，再用带全部守卫的 `UPDATE` 清 `error_code`、`INSERT` 一条新的 `PENDING` outbox（保留旧 `SENT`/`FAILED` 行）；第 2、3 步任一步影响行数不是 1 时必须 `ROLLBACK`：

```sql
BEGIN;

-- 1) 锁 job 并复核；若任一守卫不成立，ROLLBACK，不要继续。
SELECT status, error_code, lease_owner, heartbeat_at
FROM ingest_job
WHERE id = :'job_id'::uuid
FOR UPDATE;

-- 2) 清 error_code（仅全部守卫成立时命中 1 行）。
UPDATE ingest_job
SET error_code = NULL, updated_at = now()
WHERE id = :'job_id'::uuid
  AND status = 'QUEUED'
  AND error_code = 'DELIVERY_UNCONFIRMED'
  AND (lease_owner IS NULL OR lease_owner NOT LIKE 'event:%')
  AND heartbeat_at IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM outbox_event
      WHERE job_id = :'job_id'::uuid AND status = 'PENDING'
  );

-- 3) 新建 PENDING 事件（保留旧 SENT/FAILED 行；仅第 2 步成功后命中 1 行）。
INSERT INTO outbox_event (
    id, job_id, event_type, status, dispatch_attempt, next_send_at,
    lease_owner, lease_token, lease_until, sent_at, created_at, updated_at
)
SELECT gen_random_uuid(), j.id, 'ingest.requested', 'PENDING', 0, now(),
       NULL, NULL, NULL, NULL, now(), now()
FROM ingest_job AS j
WHERE j.id = :'job_id'::uuid
  AND j.status = 'QUEUED'
  AND j.error_code IS NULL
  AND (j.lease_owner IS NULL OR j.lease_owner NOT LIKE 'event:%')
  AND j.heartbeat_at IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM outbox_event
      WHERE job_id = j.id AND status = 'PENDING'
  );

-- 只有在第 2、3 步各恰为 1 行时才 COMMIT，否则 ROLLBACK。
COMMIT;
```

提交后仍需运行中的 dispatcher 才能领取并投递新事件；恢复后应重跑上面的只读查询确认 `error_code` 已清、恰有一条新 `PENDING` 事件，且旧 `SENT`/`FAILED` 行仍在。`HANDLER_NOT_READY` 不是可原地恢复的投递错误：它由当前接收壳写下接收标记时设置，job 仍有 `lease_owner=event:<oldEventId>` 与 `heartbeat_at`，而当前没有真实处理器也没有复位这些字段的策略；在二者具备前补投只会让接收壳把新消息识别为已接收并原样停在 `QUEUED`，因此**不要**对 `HANDLER_NOT_READY` 执行上面的补投，也不得把 `QUEUED` 加接收标记当作处理成功。`UNSUPPORTED_EVENT_TYPE` 的恢复要求先上线支持该事件类型的 dispatcher/协议版本；在此之前重复补投只会再次落 `FAILED`，因此也不得恢复。任何恢复都需要独立授权与操作记录，且不放宽 worker 对 `outbox_event` 的权限。

## 更新、删除和 profile 变更

原文件 checksum、index profile 与所有编码输入都不变时，可跳过解析和编码。embedding 缓存键包含规范化模型输入 hash、模型 revision、维度、pooling/normalize 设置、编码角色和预处理版本；缓存仅复用向量，不复用旧来源位置。可编辑展示标题不参与当前编码；若以后加入编码，需把它纳入输入指纹。

普通文档更新构建新版本并只切换该文档的有效指针。发布事务锁定 document，核对 `expected_active_version`，把同版本/profile 的旧 READY generation 退役，发布新 generation，再更新指针；并发失败者回滚重读。删除先 tombstone 并递增权限/知识库 revision，新检索与引用立即失效，随后异步回收文件和索引。

MVP 冻结 index profile。更换 embedding 模型、维度、切分器或分词契约时新建 profile/generation，不能混写旧列。未来 KB 级 profile 切换要记录开始时的 `kb_revision` 和有效文档清单；所有新增、更新、删除发布事务都锁 KB 行并递增 revision；切换时持同一锁核对 revision 未变且清单全 READY，否则中止并补建。

必测故障：事务提交后 Redis 断连、投递成功但 SENT 回写失败、worker 在构建中被杀、broker 重启、重复消息、解析超时和旧 dispatcher 租约过期后迟到回写。详见 [评估与验收](evaluation.md)。
