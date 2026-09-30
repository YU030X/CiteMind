# 文档入库、索引与版本

> 本文描述完整的 Markdown、可提取文本 PDF 与收窄子集 DOCX 入库契约。当前已实现的入库能力是 Markdown 与文本 PDF 上传受理（`POST /api/v1/knowledge-bases/{kb_id}/documents`：校验、KB 私有内容寻址保存，以及单事务登记默认全局 `index_profile` 并写入 `document`/`document_version`/`ingest_job`（含 `profile_id` 与受理快照 `request_title`）/`outbox_event` 并返回 `202`）与纯 Markdown/文本 PDF 解析/切分（解析与切分契约见“来源定位与切分”，落库与发布由下文默认关闭的真实入库管线负责）。outbox dispatcher 与 worker 接收壳代码已写：API 单进程 lifespan 可按配置运行 dispatcher，Compose 的 api 服务显式启用而宿主 Settings 默认关闭；真实 PostgreSQL/Redis 投递、worker 接收标记与重复投递、应用层故障注入为 pytest 自动集成，物理 Redis 停启后同一 publisher 退避再 SENT、worker 被 kill 后的补偿补投与幂等收敛由仓库外隔离手工探针实测（见 [开发约定](development.md)），但 Linux 容器 prefork 下的业务故障恢复、自然 3600 秒 visibility 重投、多 worker 并发、手工恢复 `DELIVERY_UNCONFIRMED` SQL 与 API 日志超过 12 轮仍未验收。接收壳只校验 job 状态、版本归属与文档 tombstone：合格 job（已绑定 profile 且 parser 为真实实现版本）只写 job 级接收标记（`lease_owner=event:<id>`、heartbeat、`error_code=HANDLER_NOT_READY`），job 状态仍为 `QUEUED`；无接收标记但 `error_code` 非 SQL NULL（含空串 `''`，如 `DELIVERY_UNCONFIRMED`）的旧 job 保持 `QUEUED` 与原诊断码/时间戳/租约/attempt/outbox 不动，接收壳只返回一个只读诊断状态、不写任何字段；只有无接收标记且 `error_code IS NULL` 的旧 job（`profile_id` 为 NULL 或 `parser_version` 为精确旧占位 `markdown-v1`）才走静态拒绝路径（仅置 `FAILED` + `LEGACY_JOB_UNSUPPORTED`，已由独立 tester 在隔离 PG17+Redis 与 Linux prefork worker 镜像上最终验收，见下文），该接收壳的所有路径都无解析/模型/索引发布，接收后仍不可检索（开启真实处理时由下文真实入库管线另行处理）；outbox `SENT` 只表示 broker 投递，不等于 worker 解析或入库。纯 Markdown 解析与切分已实现并独立验收（见下文）；配套的真实入库管线 `rag_backend.ingestion.indexing_worker` 已把 blob 读取、解析切分、编码、暂存与发布串成最小一致链路并接入 `rag_backend.ingest` 任务，但**默认关闭**（`INGEST_PROCESSING_ENABLED=0`），默认仍走安全接收壳；显式开启后才会读 blob、写 `generation`/`chunk`/`chunk_embedding` 并发布 READY（见下文“已实现：worker 真实入库管线”）；该管线已在隔离 PG17+Redis 上通过 15 passed/0 skip 的真实集成，并由独立 tester 以真离线模型在 Linux prefork concurrency 1 上端到端验收 PG `0007` READY（见 [开发约定](development.md)）。worker 侧本地真实 token 计数器与受限编码客户端已在**默认关闭**的真实入库管线中接线；文件回收仍未实现（授权检索首片已实现，见 [检索](retrieval.md)）；真实入库管线的隔离端到端 READY 已由独立 tester 验收（见 [开发约定](development.md)），但开关默认关闭、dev 未部署，dev 库没有 READY 索引，因此文档仍不可检索。第二切片的 `index_generation`、`chunk` 与 `chunk_embedding` 存储 schema（含 512 维向量列、`GIN(fts)` 与部分唯一索引）已由迁移 `20260922_0003` 落地；worker 写入路径（默认关闭）已实现，跨表来源一致性核对与发布事务见下文，并已由独立 tester 在隔离 PostgreSQL 17（pipeline 15 passed + 权限迁移 3 passed）与真实整链（Linux prefork concurrency 1、真离线模型、PG `0007` READY）上验收（见 [开发约定](development.md)）。MVP 目标格式为 Markdown、可提取文本的 PDF 与收窄子集的 DOCX；静态网页属于后续完整范围。DOCX 解析/切分与默认关闭的真实管线已实现：`python-docx` 只在 `dev` 与 `worker` 依赖组（api 镜像不安装 `python-docx`/`lxml`），按文档原顺序遍历段落与表格行、复用标题栈、不推测 Word 页码，以 `locator_version=3` 指向 1-based 段落索引或表格行/网格列；只索引 `word/document.xml` 正文（页眉、页脚、脚注、尾注、批注与文本框不索引）；可检测到的超集结构（嵌套表、`w:sdt`、`w:altChunk`/`w:customXml`、宏部件、实体声明）落 `PIPELINE_DOCX_UNSUPPORTED`，坏 ZIP/CRC/XML 与实际解压/资源超限落 `PIPELINE_DOCX_INVALID`，空文档不产出块。PDF 文本层解析/切分与默认关闭的真实管线已实现：`pypdf` 与 `pdfplumber` 都只在 `dev` 与 `worker` 依赖组（api 镜像不安装、也不导入 pdfplumber/pdfminer/Pillow），`pypdf` 只做加密标志/页数/结构预检，`pdfplumber` 负责逐页抽取、`heading_path` 为空且不伪造行号，chunk 不跨页并以 `locator_version=2` 指向原始 PDF 页，解析器版本诚实包含两个引擎的固定版本（`pypdf-6.19.0+pdfplumber-0.11.10-v1`）；零可提取文本落 `NEEDS_OCR`（部分空白页只被忽略），任何加密标志（含仅空口令即可解密的 PDF）、损坏与页数超限均静态失败；无新迁移（`document.source_type` 与 `document_version.status` 的 CHECK 已允许 `pdf`/`NEEDS_OCR`）。

## 入库状态与流程

1. 已实现：API 校验 KB `EDITOR` 角色、`Origin`、CSRF、必填 `Idempotency-Key`、`.md`/`.markdown`/`.pdf`/`.docx` 后缀、单文件 20,000,000 字节上限；Markdown 额外要求有效 UTF-8 文本，PDF 额外要求 `%PDF-` 魔数头，DOCX 额外要求标准库 ZIP 元数据满足收窄子集（PK 魔数、必需部件、条目数/声明解压量/压缩比/加密/路径/重复名/宏部件）；授权、CSRF、Origin 与接收阶段体积上限都在读取 multipart 正文之前完成。原文件由 API 写入 `api-documents` 命名卷（容器路径 `/var/lib/citemind/documents`，由 `DOCUMENT_STORAGE_DIRECTORY` 指定），相对路径只由服务端 `knowledge_base.id` 与内容 SHA-256 派生（`{kb_id}/{sha256}`），不拼接用户文件名；Compose 中 worker 以同一 `DOCUMENT_STORAGE_DIRECTORY` 只读（`:ro`，uid 10001）挂载同一命名卷，inference 不挂载。`DocumentBlobStore.read_verified_markdown` 提供严格 `file_ref` 匹配、父/叶符号链接与 Windows 联接点尽力校验、常规文件与 20,000,000 字节/摘要/UTF-8 控制字节校验的有界 fd 读取，读失败为 `BlobReadError`、非法 `file_ref` 为 `InvalidBlobReference`；`read_verified_blob`（返回二进制）、`read_verified_pdf`（额外校验 `%PDF-` 魔数）与 `read_verified_docx`（额外校验 `PK` 魔数）复同一套严格读取，`publish`/`content_hash` 本就二进制安全。真实 MIME 嗅探与 KB 配额未实现（KB 配额没有对应存储字段或实体）；PDF 页数/加密/结构损坏与 DOCX 嵌套表/实体/实际解压总量/CRC 不由 API 判定，而由 worker 解析子进程在写 blob 之后静态判定；任何 PDF 加密标志（含仅空口令即可解密的 PDF）都直接拒绝，不尝试空白口令解密。
2. 已实现：同一 KB、同一 `Idempotency-Key`、同内容且同标题的重复上传复用已有任务并返回同一组 id；内容或标题任一不同返回 409，跨 KB 的去重键互相独立且不通过冲突响应暴露其他 KB 的 key 使用情况。去重键是组织+KB+规范化 key 的 SHA-256，不保存也不回显原始 key。并发同 key 由唯一约束拒绝后回滚重读，再按复用/冲突规则处理；本切片起只有具名约束 `uq_ingest_job_dedupe_key` 的 SQLSTATE `23505` 冲突才被当作并发去重信号，其它完整性错误原样重抛为静态 500。本切片起，新上传路径先在 IO 线程按需构造 `KeywordAnalyzer` 以预热 jieba 身份（构造失败即 fail-closed，不发布 blob、不写库、不返回 `202`），随后在事务之前用一次只读 SELECT 预检同 `config_hash` 的既有 profile 行（`precheck_default_index_profile`，已由独立 tester 在隔离 PostgreSQL 17 + Redis 上验收；字段与默认契约不一致即 fail-closed，不发布 blob、不写库、不返回 `202`，并在 `finally` 里 rollback 立即释放这个独立只读短事务，避免随后 publish 的 fsync 期间空占连接），再在事务之前 publish blob，然后在同一个事务内先调用 `ensure_default_index_profile(session)` 登记/复用默认全局 profile，随后一并写入 `document`（`CREATED`、`active_version_id=NULL`、`source_type=markdown`；PDF 上传同一事务结构，写 `source_type=pdf` 与 `parser_version=pypdf-6.19.0+pdfplumber-0.11.10-v1`）、`document_version`（`version_no=1`、`PENDING`、`parser_version=markdown-it-py-4.2.0-v1`；登记真实解析器版本，该新版行为已由独立 tester 在隔离 PostgreSQL 17 + Redis 上验收，上传事务仍不调用解析器）、`ingest_job`（`QUEUED`、`attempt=0`、`profile_id`=本事务登记的默认全局 profile 行 id）与 `outbox_event`（`ingest.requested`、`PENDING`），提交后返回 `202`；这时文档仍不可检索（`knowledge_base.active_index_profile_id` 仍为 NULL）。该 profile 绑定已由独立 tester 在隔离 PostgreSQL 17 + Redis 上验收：新 job 的 `profile_id` 指向唯一默认全局 profile（`config_hash=4af4c33d…57fa`、七个 golden 契约字段），`knowledge_base.active_index_profile_id` 仍为 NULL，`parser_version` 为真实 `markdown-it-py-4.2.0-v1`，旧 `markdown-v1` 行幂等回放不补绑；并发同 key 只产生各 1 行 document/job/profile；同哈希字段篡改返回静态 500 且四表无新行、无最终 blob、无 `.tmp` 残留；worker 角色 INSERT `index_profile` 被拒。两个旧 reviewer P2 已收窄：同一 `config_hash` 既有行字段永久错配的**确定性**孤儿路径已由 publish 前只读预检缓解（该内容不再被 publish），但其它路径仍可能遗孤；`_insert_upload` 已把 `ensure_default_index_profile` 移出只针对四表去重键的 `try`（仍在同一事务内且仍是权威检查），只有 `uq_ingest_job_dedupe_key` 的 23505 才当并发去重，其它完整性错误原样重抛为静态 500。幂等回放**不改写也不会自动处理**已存在的 job：命中一个已被接收壳静态拒绝的旧 job（`FAILED` + `LEGACY_JOB_UNSUPPORTED`）时仍复用该 job 并返回 `202`，202 只表示受理、不代表进度或成功，也不会重新解析或复位该 job，需要的是独立授权的人工恢复；换一个全新 `Idempotency-Key` 会新建带默认 profile 与真实 parser 的 job，但这不是对旧 job 的自动恢复。同一去重键命中已有非 NULL 诊断码的旧 job 同样只返回 `202`，不会清诊断码或重投。详见 [开发约定](development.md)。
3. 已实现（默认关闭）：真实 handle 按 `QUEUED → PARSING → CHUNKING → EMBEDDING → INDEXING → READY` 执行，记录阶段、错误码与尝试次数；只有显式开启 `INGEST_PROCESSING_ENABLED` 时 `rag_backend.ingest` 才进入该路径，默认仍只写接收 marker（`HANDLER_NOT_READY`、`status` 仍 `QUEUED`）。真实路径的领取/发布/失败语义见下文“已实现：worker 真实入库管线”。解析硬时限已由受控 `subprocess` 入口实现（超时真实 `kill`+`wait` 并静态失败）；永久格式错误的通用重试仍未实现，处理中崩溃留下的过期活动租约由下文 dispatcher 的有界恢复扫描重排或耗尽失败（不设独立常驻 reaper）。
4. 已实现（纯内存解析与切分，未落库）：Markdown 解析器（`markdown-it-py==4.2.0`，实现版本 `markdown-it-py-4.2.0-v1`）把原始 UTF-8 字节解析成带来源位置的块：`token.map` 的 0-based 且结束不含边界统一转成 1-based 闭区间；标题按层级维护，进入引用块/列表容器时快照、退出时恢复；段落、列表与 fence/缩进代码块各自成块；原始 HTML 只识别、从不渲染且被排除在正文之外；图片只保留 alt，绝不抓取 URL。`source_sha256` 只按原始 bytes 计算（CRLF 与 Unicode 变化都会反映），解码用 `utf-8-sig` 只忽略文件开头 BOM、不改变行号。切分器把 `heading_path` 与正文拼成完整模型输入后交给注入的 token 计数器，按约 360 目标、约 60 重叠、512 硬上限打包；单块超硬上限时先在 `max_tokens` 处按字符确定性硬拆并保留重叠，短段落不强制重叠，chunk 内容不伪造。Markdown 的 `source_locator` 是 `locator_version=1` 的 JSON：含 `source_type`、`parser_version`、`source_sha256`、所跨块的 1-based `start_line`/`end_line`（块级粗粒度行范围，不是精确片段行）、`block_ordinals`，以及每个 piece 在其规范化块正文内的 `block_char_start`/`block_char_end` 字符区间（PDF 的 `locator_version=2` 见“来源定位与切分”表）。本片把上传事务登记的解析器版本从占位 `markdown-v1` 统一为真实实现版本 `markdown-it-py-4.2.0-v1`，新上传的 `document_version.parser_version` 写真实版本；既有版本行、既有 `QUEUED`/`HANDLER_NOT_READY` 任务仍保持占位 `markdown-v1`，不自动升级、不重投、不自动处理，需受控处理。该新版行为已由独立 tester 在隔离 PostgreSQL 17 + Redis 上验收（11 passed/0 skipped），并新增用例覆盖旧占位行的幂等回放仍保持 `markdown-v1` 不变。
5. 已实现（默认关闭，显式 `INGEST_PROCESSING_ENABLED=1` 才启用）：worker 读取 blob、在独立解析子进程中调用纯解析/切分，把 chunk 文本、token 数、hash、来源 locator、解析器与切分器版本写入 `chunk`，把 `build_model_input` 作为模型输入交给已校验的 `LocalTokenizerCounter` 与受限 `InternalEmbeddingClient` 批量编码，并把向量、FTS（参数绑定 `to_tsvector('simple', :jieba词流)`）与 locator 写入不可见的 `BUILDING` staging generation。解析阶段用受控 `subprocess` 入口（ `python -m rag_backend.ingestion.parse_subprocess`）携带硬时限运行，超时真实 `kill`+`wait` 回收并静态失败，子进程环境经白名单剥离 DSN/凭据、返回体有界；worker 侧另有显式工厂 `rag_backend.ingestion.worker_index_identity.initialize_worker_index_identity(model_directory)`，管线在领取任务后按需调用以组合只读索引身份（见下）。解析器版本对齐只覆盖新上传，既有占位行需受控处理。真实 PG 集成与真实模型整链已由独立 tester 验收（见 [开发约定](development.md)）。
6. 已实现（默认关闭，与第 5 步同一开关）：校验 chunk 数、非空文本、向量维度、映射和预期文档版本后，在锁定 job/document 的事务中发布 READY generation 并切换 `active_version_id`、版本/文档 `READY`，并用条件 UPDATE 首次置位 KB `active_index_profile_id` 且 `kb_revision+1`。首次导入失败不可检索；更新失败时旧版继续服务。处理中发生数据库错误时，管线在 rollback 后用独立短事务尝试落静态 `PIPELINE_DB_ERROR`；若 job 已是 `READY`（commit 结果未知）则保留成功，若数据库持续不可用或独立只读重判也读不到 job 事实（例如行不存在）则返回 `persist_unconfirmed`、保留人工发现与恢复，不谎报成功或失败。该返回值属于 Celery 任务的正常返回：消息会被 ACK，result 按 `task_ignore_result=True` 丢弃；dispatcher 的补偿只处理 `QUEUED` 投递，但同一后台周期会对处理中任务的过期租约做有界恢复扫描（见“outbox、租约与恢复”），因此仍处于活动阶段且租约过期的任务会被重排或耗尽失败；`persist_unconfirmed` 本身不重试、不自愈，需要人工发现与恢复，不设独立常驻 reaper。

已发布的最终 blob 在事务回滚或后续数据库异常时不删除，因为并发事务可能已引用它；profile 登记的 unique/`config_hash` 冲突、哈希或字段不一致，以及其它 PG 写入错误，同样可能在 blob 已 publish 后让事务失败，从而留下不被任何事务引用的最终 blob。同一 `config_hash` 既有行字段与默认契约永久不一致的**确定性**孤儿路径已缓解：新上传在 publish 之前先用一次只读 SELECT 预检该行并 fail closed，该内容不会被 publish，因此不再反复成为唯一内容孤儿（旧 reviewer P2 的确定性路径已缓解）。这只移除一条确定性路径，**不是零孤儿**：预检通过后到写事务提交之间仍可能发生 PG 故障、外键或其它完整性错误，以及预检与写事务之间的先查后写竞态，都会在 blob 已 publish 后让事务失败而留下孤儿；预检只缩小窗口、不消除窗口。旧孤儿 blob 未回收，且不能删除可能被并发事务共享的最终 blob，本切片不实现 GC、配额或告警，回收、配额与一致性告警留待后续作业（本切片独立 tester 的仓库外故障注入实测：撤掉 API 的 outbox INSERT 后 publish 后 PG 失败仍留 0 行四表/profile、`active_index_profile_id` NULL、无 `.tmp`，但孤儿最终 blob 保留 12/12，证明代码不自动 unlink）。`_insert_upload` 的去重误分类也已收窄：只有 `uq_ingest_job_dedupe_key` 的 SQLSTATE `23505` 冲突才回滚重读复用，其它唯一/外键或非 23505 的完整性错误原样重抛为静态 500，不再被误读为去重冲突（旧 reviewer P2 的去重误分类已收窄）。

## 来源定位与切分

| 格式 | 解析方式 | 引用位置 | 降级边界 |
| --- | --- | --- | --- |
| Markdown（纯解析/切分已实现，未落库） | markdown-it-py 4.2.0，保留标题、段落、列表与 fence 代码块；原始 HTML 不渲染且不进正文，图片只取 alt | 解析器版本、heading_path、所跨块的 1-based 块级起止行、`source_sha256`（原始 bytes）、块 ordinal 与块内规范化正文的字符区间 | token.map 起点 0-based、结束不含边界，统一转 1-based 闭区间；行号是块级粗粒度而非精确片段行；展示层必须转义，不能当 HTML |
| 文本 PDF（解析/切分与默认关闭管线已实现） | `pypdf` 做加密/页数/结构预检，`pdfplumber` 逐页抽取文本层；每个非空页一个块，`heading_path` 为空元组，`start_line`/`end_line` 为 `None`（不伪造行号），部分空白页只被忽略 | `locator_version=2`：`source_type="pdf"`、`parser_version`（含 pypdf 与 pdfplumber 两个固定版本）、`source_sha256`（原始 bytes）、页号列表与每段所在 `page`；chunk 按页强制切分，绝不跨页 | 只保证页定位；零可提取文本落 `document_version.status='NEEDS_OCR'` + job `FAILED`/`PIPELINE_NEEDS_OCR`，不把空提取当成功；任何加密标志（含空口令可解密）/损坏/超过 200 页分别静态失败；复杂版面不承诺准确表格推理 |
| DOCX（解析/切分与默认关闭管线已实现） | `python-docx==1.2.0` 按 XML 原顺序遍历 `w:p` 与 `w:tbl`；正文段落 1-based 编号（空段也计索引、不产出块），标题样式复用 Markdown 的标题栈；表格逐行一个块，横向合并只记真实 origin 一次，纵向合并 continue 不复制上一行文字，`gridBefore` 计入网格列 | `locator_version=3`：`source_type="docx"`、`parser_version`、`source_sha256`（原始 bytes）、`block_ordinals`，以及每段的 `block_char_start/end` 与所在 `paragraph_index` 或 `table_index`/`row_index`、`cells`（1-based 网格列、`grid_span`、规范化行文字内的字符区间）；不推测 Word 页码 | 只支持收窄子集且仅索引 `word/document.xml` 正文（页眉、页脚、脚注、尾注、批注与文本框不索引）：嵌套表、`w:sdt`、`w:altChunk`/`w:customXml`、宏部件、实体声明等收窄外结构落 `PIPELINE_DOCX_UNSUPPORTED`，坏 ZIP/CRC/XML 与实际解压/资源超限落 `PIPELINE_DOCX_INVALID`；不处理宏、不访问外链、不做 Word 分页/复杂版面推理 |
| 静态网页（网页切片已实现） | 受限 HTTPX 抓取原 HTML，worker 用 BeautifulSoup4/lxml 清洗静态正文：先整体移除 script/style/template/noscript/nav/aside/header/footer，再在 main/article/body 中按确定块（p/li/pre/blockquote）抽取，h1–h6 只维护 heading_path，不执行脚本、不抓外链 | `locator_version=4`：`source_type="web"`、`parser_version`（`beautifulsoup4-4.15.0+lxml-6.1.3-v1`）、`source_sha256`（原 HTML bytes）、`source_url`/`final_url`/`fetched_at`，以及 segments 的 block ordinal 与块内字符区间 | 只支持静态 HTML；不执行 JS、不登录、不递归、不下载资源、不读 Cookie/代理；允许主机为精确列表且默认空即禁用；每跳把已校验公网 IP 列表保序固定到实际 TCP 连接、Host/TLS SNI/证书校验保留原 hostname（应用层 DNS rebinding 窗口已关闭，完整 SSRF 属 Phase 4） |

网页切片已实现（迁移 `20260929_0015`）：`POST /api/v1/knowledge-bases/{id}/documents/web`（JSON `{url,title}`）与 `POST /api/v1/documents/{id}/versions/web`（JSON `{url,title,expectedVersionId}`）沿用 KB `EDITOR`、`Origin`、CSRF 与 `Idempotency-Key`，成功返回 `202` 与同一 `DocumentUploadResponse`。顺序冻结：先规范化 URL 与校验允许主机，再按去重键查已有请求；命中时只比对规范化 URL 与标题，**不联网**（同 URL+标题复用原 ids，不同则 409 `IDEMPOTENCY_KEY_REUSED`），只有新请求才在 API 返回 `202` 前抓取原 HTML 并写入既有内容寻址 blob；抓取成功后仍走既有 profile 预检、blob 发布与四表事务/CAS。允许主机由 `WEB_FETCH_ALLOWED_HOSTS` 配置（逗号分隔精确 host，不做后缀/通配符，默认空即禁用）；worker 只读 blob 离线解析，不需要抓取配置。worker 侧按 `source_type=web` 分派 `parse_web_in_subprocess`，解析后用 `dataclasses.replace` 注入 `document_version` 的 `source_url`/`final_url`/`fetched_at`，再切分为 `locator_version=4`；空正文落 `PIPELINE_CONTENT_EMPTY`，不新增状态。本片已由 `tests/unit/test_web_fetch.py`（MockTransport+假解析器）、`tests/unit/test_web_parsing.py`（5 份自制 HTML 正样本、子进程一致性与 locator v4）与 `tests/unit/test_web_import.py`（免联网幂等/路由）覆盖；真实抓取、真实 PostgreSQL 事务与端到端 READY 未在本片验收。

初始目标为每 chunk 约 360 个实际 tokenizer token、约 60 token 重叠，优先在标题和段落边界切分；短段落不强加重叠。标题、正文与特殊 token 合计不能超过 embedding 模型的 512 长度。已实现的切分器用可注入的 `TokenCounter` 验证这套预算（target 360 / overlap 60 / max 512），单元测试仍用假计数器。worker 侧新增 `LocalTokenizerCounter`，从镜像内固定 BGE tokenizer 四件按大小与 SHA-256 校验后离线加载（`tokenizers==0.23.2`，进程单例、不联网、不记录输入文本）；第一版镜像已由独立 tester 在 `--network none` 下与 inference `AutoTokenizer` 对 351 个样本逐项计数一致（空串、emoji、混合文本、golden 3 例逐 token id，及 511/512/513 与最大 1658），10 份自制 Markdown 中 9 份有正文的文档切成 27 个 chunk 且均 ≤512 并能用 locator 重放。这**只证明真实 tokenizer 计数一致，不代表文档已入库**；worker 侧默认关闭的真实入库管线已在领取任务后调用该计数器；本片把新上传的 `parser_version` 统一为真实 `markdown-it-py-4.2.0-v1`（已由独立 tester 在隔离 PostgreSQL 17 + Redis 上验收），但既有版本行仍为占位 `markdown-v1`，不自动升级、需受控处理，接线时仍须统一预算。worker 侧另有显式索引身份工厂 `rag_backend.ingestion.worker_index_identity.initialize_worker_index_identity` 可把该计数器、冻结契约常量与真实 `KeywordAnalyzer` 组合成只读索引身份（见下）；默认关闭的真实处理路径已在领取任务后按需调用它，默认接收壳仍不调用。PDF 可按页切以保留明确页码；实现上每个非空页是一个 `heading_path` 为空的块，切分器在页边界强制 flush 且不携带上一页重叠，因此 chunk 不会跨页、locator 的 `pages` 恒为单页；页内超预算块仍按字符确定性拆分并保留块内字符区间。表格序列化时继承表头并记录行范围（后续）。复杂跨页表格标低质量，不承诺准确表格推理。

每个 chunk 至少带 `organization_id`、`kb_id`、`document_id`、`version_id`、`generation_id`、`chunk_index`、`heading_path`、`source_locator`、`text_hash`、`token_count`、`parser_version`、`chunker_version`。来源在解析时固定，生成模型不能补猜。当前纯切分产出的内存 `Chunk` 只含 `chunk_index`、正文、`heading_path`、`token_count`、`text_hash`、`model_input_hash`、`source_locator`、`parser_version`、`chunker_version`；`organization_id`/`kb_id`/`document_id`/`version_id`/`generation_id` 等落库字段由默认关闭的真实入库管线在暂存事务中写入。定位可用同版本解析器重放加 `segments` 的块内字符区间还原规范化块正文，而不是对原始文件的字节偏移。

## outbox、租约与恢复

- `ingest_job` 和 outbox 由同一 PostgreSQL 事务保存。消息 JSON 正文只含 `jobId` 与 `protocolVersion=1`，不含正文、凭据或可执行函数路径；Celery `task_id` 承载 outbox 事件 id，任务投递到专用 `ingest` 队列。
- 已实现（隔离 PostgreSQL/Redis 已验收）：dispatcher 在短事务中用行锁/`SKIP LOCKED` 领取待发事件，保存 owner、单调增加的 `lease_token` 与期限，提交后才向 Redis 投递。回写要求事件仍待发、租约未过期且 token/owner 相同；迟到的旧发送者不能覆盖新领取者。简单短事务（领取、退避重排、失败标记、补投插入、恢复候选扫描）仍用事务 `now()`；会等待 job 或 outbox 行锁、必须在解锁后反映真实时刻的语句改用 PostgreSQL `clock_timestamp()`：回写 SENT 的租约谓词与 `sent_at`/`updated_at`、处理中任务恢复重排的 `next_run_at`，以及 worker 写接收标记的 `lease_until`/`heartbeat_at`。租约与接收宽限均为 60 秒。接收宽限由补偿候选按最近 SENT 事件的 `sent_at` 判断（`sent_at > now() - 60s` 时不补投），不再借用 `ingest_job.next_run_at`；后者只用于处理中任务恢复后的退避与领取门槛。`MARK_SENT` 是 `UPDATE`，在 READ COMMITTED 下等锁后由 PostgreSQL EPQ 用新行版本重估谓词，因此 `clock_timestamp()` 能拒绝已过期租约——这只适用于该 `UPDATE` 加提交路径，不代表任意纯行锁等待都会重估过期条件。
- 已实现（隔离 Redis/Celery 接收已验收，业务入库未实现）：worker 接收壳在同一事务中先 `SELECT ... FOR UPDATE OF j` 锁住 job，只核对 job 是否仍 `QUEUED`、`document_version.document_id` 是否等于 job 的文档、文档是否已 tombstone。通过后（合格 job）写 job 级 `lease_owner=event:<eventId>`、随机 `lease_token`、`lease_until`、`heartbeat_at` 与 `error_code=HANDLER_NOT_READY`，其中期限与心跳用解锁后的 `clock_timestamp()`，`job.status` 保持不变；重复消息命中已有标记时不写任何字段。它不做解析、切分、编码或索引发布。
- 已实现（**本片最终独立验收快照**）：接收壳先做行锁 `SELECT ... FOR UPDATE` 检查 `status`/删除/版本归属，再按「已有接收标记 > 已有非 NULL 诊断码 > 无诊断的旧 job」固定优先级判定。已有 `HANDLER_NOT_READY` 标记的 job 返回 `already_received` 并保持原状、需独立授权的人工恢复；无接收标记但 `error_code` 非 SQL NULL 的旧 job 保持 `QUEUED`，其诊断码、时间戳、租约、`attempt` 与 outbox 全部不动，接收壳只返回一个只读诊断状态、不写任何字段（内部返回常量 `existing_diagnostic`/`RECEIVE_STATUS_EXISTING_DIAGNOSTIC`，已由单测固定；它不是对外 API 或 wire 契约，Celery result 按 `task_ignore_result=True` 丢弃）。判定以 SQL `NULL` 为界：任何非 NULL 值（包含空串 `''`）都保留，只有 SQL `NULL` 才可能落入下面的无诊断旧 job 拒绝；当前没有任何应用路径会写入空串 `error_code`，该保留分支也不代表自动修复；只有无接收标记且 `error_code IS NULL` 的旧 job（`profile_id` 为 NULL 或 `parser_version` 为精确旧占位 `markdown-v1`）才用带 `error_code IS NULL` 守卫的原子 CAS `UPDATE` **仅**把 `ingest_job.status` 置 `FAILED`、写静态 `error_code=LEGACY_JOB_UNSUPPORTED` 与 `updated_at`，提交后返回 `legacy_unsupported` 再 ACK；不改 `document_version.status`（仍 `PENDING`）、`document` 生命周期（仍 `CREATED`）、outbox、`lease_owner`/`lease_token`/`lease_until`/`heartbeat_at`、`attempt`、`next_run_at` 或 `profile_id`，不补绑 profile、不重投。重复投递读到 `FAILED` 落 `NOT_QUEUED`。合格 job（已绑定 profile 且 parser 为真实 `markdown-it-py-4.2.0-v1`）仍只写 `HANDLER_NOT_READY` 接收标记、`status` 仍 `QUEUED`。无新迁移、无新 `GRANT`；`worker.py` 仍未调用完整 `identity_preflight`/`worker_index_identity`，不要求 `/models`。**最终独立验收**：隔离 PG17+Redis（项目 `myrag-legacy-diagnostic-final`，合成凭据、`citemind_test`/PUBLIC 已收权、无真实 `.env`、无 revision override）三个独立 pytest 进程在 schema `0006` 为 legacy 14 + dispatcher_flow 14 + dispatcher_broker 4 = 32 passed/0 skip，逐次降回 base；最终源码 worker 镜像在 Linux prefork concurrency 1 独立网络上只固定 `model_assets` digest 实测 A–F 六场景与 FAILED 同/新 taskId 重投零写；独立代码 review 最终 APPROVED（worker SHA `fa9cac44…`，详见 [开发约定](development.md)）。**未测**物理 Redis 停启/kill/自然 3600 秒重投/多 worker/人工恢复 SQL/p95；该切片当时完整身份工厂未接线、无 READY/模型权重/GC；后续默认关闭的真实入库管线已接线这些前置能力，并由独立 tester 端到端验收 READY（见 [开发约定](development.md)）。
- 已实现（pytest 自动集成覆盖投递与应用层故障注入，物理 Redis 停启与 worker kill 由隔离手工探针实测）：`outbox SENT` 只表示 broker 已收到投递，不表示 worker 已解析或入库；job `QUEUED` 且无接收标记、无 PENDING 事件时，补偿扫描新建 PENDING 事件补投并保留旧 SENT，达到 `MAX_DELIVERY_ATTEMPTS=5` 后写 `error_code=DELIVERY_UNCONFIRMED` 停止热循环。发送失败（含 Redis 不可达）保持 PENDING 并按 5 秒起、300 秒封顶的指数退避重排。Redis 恢复后继续；不以 Celery result backend 代替业务事实。已成功投递但 SENT 回写失败时事件仍为 PENDING；若该 job 的 worker 随后中断，恢复重排会再建 PENDING，因此同一 job 可能短暂出现多个 PENDING；投递不承诺严格单次，正确性由 worker 领取门槛与租约 CAS 幂等保证。`SENT`、`HANDLER_NOT_READY` 与 `QUEUED` 都不等于入库；物理 Redis 停启、worker 被 kill 后补偿补投与重复消息幂等收敛由仓库外隔离手工探针实测（不在 pytest 自动用例内），Linux 容器 prefork 下的业务故障恢复与自然 3600 秒重投仍未验收。
- 已实现（隔离 PostgreSQL 17 聚焦验收）：处理中任务（`PARSING`/`CHUNKING`/`EMBEDDING`/`INDEXING`）的租约过期后，dispatcher 在每次补偿阶段用同一后台周期做有界恢复扫描（`FOR UPDATE OF j SKIP LOCKED`、按 `lease_until` 最早优先、每批上限 50）。候选只取活动阶段、`lease_until` 非空且已过期、`lease_token` 非空、`error_code IS NULL` 且文档未删除的 job。`attempt < MAX_PIPELINE_ATTEMPTS=3` 时在同一事务内清 `lease_owner`/`lease_token`/`lease_until`/`heartbeat_at`、重入 `QUEUED`、把退避写入 `next_run_at`（按已发生领取次数 5 秒起、300 秒封顶的指数退避）并原子新建 `PENDING` 事件（`next_send_at` 取同一 `next_run_at`）；`attempt` 不回写，由下一次 claim 递增。`claim_ingest_job` 只在 `next_run_at <= clock_timestamp()` 时领取，因此旧 Redis 重投不能绕过退避；worker 迟到的旧 token 心跳、阶段推进、失败标记与发布都被租约 CAS 拒绝。`attempt` 达到上限时静态 `FAILED` + `PIPELINE_RETRY_EXHAUSTED` 并清租约，同事务把该 version 置 `FAILED`、文档仅在 `active_version_id IS NULL` 时置 `FAILED`；已有 active 文档继续服务。API 角色对 `index_generation` 只有 SELECT，因此恢复不写 generation：遗留 `BUILDING` generation 不做 GC、不可检索，任务下一次成功暂存会绑定到新 generation；恢复耗尽后也不再建事件，需要受控人工处置。恢复与真实处理共用 `dispatcher_enabled` 与 `ingest_processing_enabled` 双门控（Compose 中 api 与 worker 都要显式开启）。
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

提交后仍需运行中的 dispatcher 才能领取并投递新事件；恢复后应重跑上面的只读查询确认 `error_code` 已清、恰有一条新 `PENDING` 事件，且旧 `SENT`/`FAILED` 行仍在。`HANDLER_NOT_READY` 不是可原地恢复的投递错误：它由当前接收壳写下接收标记时设置，job 仍有 `lease_owner=event:<oldEventId>` 与 `heartbeat_at`，而当前没有真实处理器也没有复位这些字段的策略；在二者具备前补投只会让接收壳把新消息识别为已接收并原样停在 `QUEUED`，因此**不要**对 `HANDLER_NOT_READY` 执行上面的补投，也不得把 `QUEUED` 加接收标记当作处理成功。静态拒绝路径只作用于无接收标记且 `error_code IS NULL` 的旧 job，把它们置 `FAILED` + `LEGACY_JOB_UNSUPPORTED`（已最终验收）；dispatcher 补偿不会自动重投 `FAILED` 事件。无接收标记但已有非 NULL 诊断码（如 `DELIVERY_UNCONFIRMED`）的旧 job 保持 `QUEUED` 与原诊断码/时间戳/租约/attempt/outbox 不动，接收壳只做只读返回，恢复仍按本节上面的守卫由授权运维手工执行。早期已写 `HANDLER_NOT_READY` 标记的旧 job 也不被改写，仍需独立授权的人工恢复；不得把 `FAILED`、`HANDLER_NOT_READY` 或已有诊断码当作可自动重投。`UNSUPPORTED_EVENT_TYPE` 的恢复要求先上线支持该事件类型的 dispatcher/协议版本；在此之前重复补投只会再次落 `FAILED`，因此也不得恢复。任何恢复都需要独立授权与操作记录，且不放宽 worker 对 `outbox_event` 的权限。

## 更新、删除和 profile 变更

原文件 checksum、index profile 与所有编码输入都不变时，可跳过解析和编码。已实现的增量 embedding 缓存**不新增表**，直接复用既有 `chunk_embedding`：编码前按 `chunk.model_input_hash` 批量查询同 `organization_id`、同 `profile_id`、`index_generation.status='READY'` 且 generation 与 embedding 双侧 profile 一致、文档未删除（`deleted_at IS NULL` 且 `lifecycle_status <> 'DELETED'`）的向量，并强制经权威链 `chunk→index_generation→document_version→document→knowledge_base` 校验，不信任 `chunk` 上冗余的 `organization_id`/`kb_id`/`document_id`/`version_id`。命中只复用向量、不复用来源位置，允许同组织跨文档与旧版本（发布切换前）复用，禁止跨组织。缓存键边界即不可变 `index_profile` 的 `profile_id` 与运行期身份预检：`model_revision`、维度、tokenizer/chunker 契约与 normalize 都随 profile 固定，编码请求固定 `kind='document'`；pooling/provider 不是当前可配置自由度，由冻结模型 revision 与 inference 契约约束，因此本切片不新增 profile 字段、不改 `config_hash`。每个 chunk 仍重建 FTS/locator 并写新 generation/embedding，发布 CAS、租约与重试语义不变。可编辑展示标题不参与当前编码；若以后加入编码，需把它纳入输入指纹。

普通文档更新构建新版本并只切换该文档的有效指针。已实现的切片在发布事务锁定 document，核对 `expected_active_version`，发布新 generation 并切换指针；**不退役旧 READY generation**（检索按 `active_version_id` 过滤，旧 generation 自然不入候选），并发失败者回滚重读。删除先 tombstone 并递增知识库 revision，新检索与引用立即失效，随后异步回收文件和索引。**退役/重建 generation 属于未来 `POST /documents/{id}/reindex`（见 [API](api.md)）与 KB 级 profile 切换契约，不在本切片范围内**：本切片不删除也不退役任何 generation，该规划为后续重索引保留。

已实现（文档更新/删除切片，迁移 `20260926_0008` 新增可空 `ingest_job.request_title`；`tests/integration/test_document_update_delete_flow.py` 在隔离 PostgreSQL 17 + Redis 上 11 passed）：`POST /api/v1/documents/{id}/versions`（multipart、`Idempotency-Key`、`expectedVersionId` 必填，KB `EDITOR`+Origin+CSRF）在 `document` 行锁内取 `version_no = max+1`；去重键以组织+KB+文档+key 为前缀（命名空间 `ver1:`）并拼接 expected active，命中时校验文档未删除、`expectedVersionId`、内容摘要与标题，同键同 expected 的有效回放复用同一 `document_version`/`ingest_job`，任一不同为 409 `IDEMPOTENCY_KEY_REUSED`，过期 expected 在写事务前只读预检与行锁内各判一次，返回 409 `DOCUMENT_VERSION_CONFLICT`。**请求身份不可变**：内容摘要取不可变 `document_version.file_hash`，标题取受理时写入的 `ingest_job.request_title`，不再直接依赖会随新版本切换的 `document.title`，因此改展示标题后原 key 仍能以原标题原样重放；旧数据边界：`request_title IS NULL` 的历史 job 仍回退到当前 `document.title` 比较，不静默改变旧 key 语义，也不回填。受理后 `document.active_version_id` 不变，v1 保持 `READY` 可检索；worker 侧 `rag_backend.ingestion.indexing_worker` 已支持 `version_no > 1` 的更新：领取要求去重键可解析出 expected active、文档已有 active version，且 **expected 等于文档当前 active**；expected 已被更早发布超越时（active 只前进不回退）在领取期静态拒绝为 `FAILED` + `PIPELINE_STALE_EXPECTED`，不解析/编码/暂存，发布事务仍保留同一 `active_version_id == expected_active` 的 CAS 作为并发变化的最后防线。发布事务在同一 `document` 行锁内 compare 且 `deleted_at IS NULL` 才把新 generation/版本置 `READY` 并切换 `document.active_version_id`；旧版本无需退役，检索只按 `active_version_id = dv.id` 过滤，因此不再入候选；发布失败只把新 generation/版本/job 置 `FAILED`，文档与 v1 不动，v1 继续服务。发布仍复用同一默认 index profile，不新增 profile、不改 KB `active_index_profile_id` 语义（同 profile 时递增 `kb_revision`）。

已实现（逻辑删除，同一集成文件）：`DELETE /api/v1/documents/{id}`（KB `OWNER`+Origin+CSRF）在 `document` 行锁内置 `deleted_at` 与 `lifecycle_status=DELETED`、同事务递增 `kb_revision`，并把该文档所有非终态 `ingest_job`（`QUEUED/PARSING/CHUNKING/EMBEDDING/INDEXING`）置 `CANCELLED`、清租约并写静态诊断码 `DOCUMENT_DELETED`，使接收壳按 `NOT_QUEUED` 返回、dispatcher 补偿不再把它当候选（不会无限补投）；二次删除不重复写库并返回 204。删除事务锁序（`document→knowledge_base→ingest_job`）与 worker 失败/发布事务（`ingest_job→document`）相反，真实并发可能触发 PostgreSQL 死锁（SQLSTATE `40P01`）；API 删除路径在**完整事务回滚**后做有限次重试，每次重试都重新授权（不复用死锁事务中读到的角色快照）并重跑整个 tombstone，绝不只重试单条 SQL；成功只提交一次，因此 `kb_revision` 只递增一次、无重复副作用。只做逻辑 tombstone：保留共享 blob、版本与索引历史，**不**返回 `cleanupJobId`、不建清理实体、不做物理回收；已删除文档的旧幂等上传/新版本回放返回 409 `DOCUMENT_DELETED`，不得当有效新资源。删除与在途发布的竞争由 `document` 行锁与 `deleted_at` 守卫共同串行化：删除先提交时发布事务看到 `deleted_at`（`OUT_OF_SCOPE`）或失租约（`LEASE_LOST`）而不发布，发布先提交时文档已被 tombstone、检索仍零候选；即使 job 保留未过期租约，发布事务也必须先按 `deleted_at` 拒绝且不复活文档。文档 ACL 表与 `document_acl` 仍属完整范围计划，本切片不递增 `acl_revision`。

MVP 冻结 index profile。更换 embedding 模型、维度、切分器或分词契约时新建 profile/generation，不能混写旧列。未来 KB 级 profile 切换要记录开始时的 `kb_revision` 和有效文档清单；所有新增、更新、删除发布事务都锁 KB 行并递增 revision；切换时持同一锁核对 revision 未变且清单全 READY，否则中止并补建。

`index_profile` 是全局、不可变的编码契约登记表。七个契约字段（`embedding_model`、`model_revision`、`dimension`、`normalize`、`tokenizer_revision`、`chunker_version`、`keyword_analyzer_version`）加 `schema_version='index-profile-v1'` 后的规范化 JSON（`sort_keys=True`、`separators=(',',':')`、`ensure_ascii=False`、`allow_nan=False`，UTF-8 编码）的 SHA-256 小写 64 位十六进制即 `config_hash`；默认 profile 取 `embedding_model='BAAI/bge-small-zh-v1.5'`、`model_revision='7999e1d3359715c523056ef9478215996d62a620'`、`dimension=512`、`normalize=True`、`chunker_version='heading-pack-v1'`，`tokenizer_revision` 完整值为 `bge-small-zh-v1.5@7999e1d3359715c523056ef9478215996d62a620:tokenizer-artifacts-v1-sha256=ca6e9808373afae7a8b131f50361c9b125ba5914eef0161b148b3ab6a105f9a8`（四个 tokenizer 产物为 `tokenizer.json` 439125/`48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26`、`vocab.txt` 109540/`45bbac6b341c319adc98a532532882e91a9cefc0329aa57bac9ae761c27b291c`、`tokenizer_config.json` 367/`e6f3b96db926a37d4039995fbf5ad17de158dfb8f6343d607e4dbaad18d75f5a`、`special_tokens_map.json` 125/`b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3`，其 `{name:{size,sha256}}` 规范 JSON 的 SHA-256 即该摘要），`keyword_analyzer_version` 取 `KeywordAnalyzer` 的最终 analyzer id `jieba-0.42.1-search-v1:base-sha256=7197c3211ddd98962b036cdf40324d1ea2bfaa12bd028e68faa70111a88e12a8:v1-sha256=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855:norm=NFKC+casefold`。`parser_version` 与来源相关字段不属于 profile、也不参与 `config_hash`，保留在 `document_version`/`chunk`，使解析器升级不要求新建 profile；本片把新上传登记的解析器版本从占位 `markdown-v1` 统一为真实 `markdown-it-py-4.2.0-v1`（已由独立 tester 在隔离 PostgreSQL 17 + Redis 上验收），既有占位行不自动升级、需受控处理。上述字段连同 `schema_version='index-profile-v1'` 的规范 JSON 字节（可由字段与算法复算，golden 常量冻结在单测中）取 SHA-256 得 `config_hash=4af4c33d4e8d5571cc513dc8c623b1fe66a5565683f95fc75a7b3f8a28dc57fa`。该契约已由纯标准库源码模块 `rag_backend.models.profile_contract` 实现：规范 JSON 与 `config_hash` 计算本身无副作用，导入期不初始化 jieba，也不依赖 `tokenizers`/`torch`/`transformers`；`default_index_profile()` 仅在调用时按需构造 `KeywordAnalyzer` 以取 `keyword_analyzer_version`，因此该调用会做一次有限私有临时目录 IO，整体不是无副作用纯函数。最终版源码 SHA `0eabc8c6dd4d6c5458162823597488e8d1d868db18fd4cf9957b24e05fe2c3b4`（tests `f7f01bcf…`），聚焦 21 passed、非集成 828 passed/2 skipped，独立 review APPROVED 且两个隔离镜像已验收（module import 无 `tokenizers`/`torch`/`transformers`/`jieba`、默认 hash golden 一致、私有 jieba temp 退出清理且不写共享 cache）。这只证明源码级契约与镜像导入/互算一致；该模块切片当时无 DB seed、无发布事务、无数据库或真实模型运行证据；跨源常量一致性的运行期断言已前置到 `worker_index_identity` 工厂，发布与真实模型路径已由独立 tester 端到端验收（见下文与 [开发约定](development.md)）。登记该全局 profile 不代表任何 KB 可检索。

默认 profile 的登记入口 `rag_backend.ingestion.profile_repository.ensure_default_index_profile(session)` 已实现并接入**新 Markdown 上传写路径**（本切片，已由独立 tester 验收）：只使用 api 角色对 `index_profile` 的 SELECT+INSERT，按 `config_hash` 幂等（`INSERT ... ON CONFLICT (config_hash) DO NOTHING RETURNING id`）；未返回行时在同一事务内按 `config_hash` 重读既有行并逐项比对七个契约字段，字段不一致抛 `IndexProfileConflictError`（哈希碰撞或同哈希旧数据都不静默复用），冲突后仍读不到行抛 `IndexProfileNotFoundError`。函数不 `commit`、不 `rollback`（事务归调用方）、不 UPDATE `index_profile`、不 UPDATE `knowledge_base`，并用 `session.no_autoflush` 抑制自动 flush；READ COMMITTED 下并发插入同一 `config_hash` 时后来者等待先到者提交后复用同一行 id。同模块另提供只读预检 `precheck_default_index_profile(session)`（本切片，独立 tester 已验收）：在 publish 前只按默认契约 `config_hash` 做一次 SELECT，命中既有行时用同一份字段比对逻辑逐项核对七个契约字段，不一致抛 `IndexProfileConflictError`，无行时正常返回；它不 INSERT/UPDATE、不 `commit`/`rollback`，但会开启只读事务，调用方必须在 `finally` 中 rollback 释放。上传写路径在 publish 前先调用 `precheck_default_index_profile` 只读预检，再在同一个四表事务内调用 `ensure_default_index_profile` 取得默认 profile 行 id，并把它写入新 `ingest_job.profile_id`；预检不登记、不 seed、不修改 KB `active_index_profile_id`，登记与复用仍由写事务内的 `ensure_default_index_profile` 权威完成；登记成功仍不代表任何 KB 可检索，也不回填 `active_index_profile_id`。KB 创建与 worker 路径仍不调用它；本切片没有 seed profile、没有新迁移，dev 库 `active_index_profile_id` 仍全部为 NULL（真实处理默认关闭、dev 未部署）。

`knowledge_base.active_index_profile_id` 是发布态指针，只表示该 KB 已发布索引当前使用的 profile：新 KB 尚无 READY 索引时为 NULL，只有首次 READY 发布事务（以及后续 KB 级 profile 切换）才置位；上传受理本身既不置位也不把 NULL 回填为默认 profile。检索必须按该指针关联 READY generation 与 `chunk_embedding`，指针为 NULL 的 KB 不可检索。当前没有 seed 任何独立 profile；发布路径已由默认关闭的真实入库管线实现并端到端验收（首次 READY 置位指针），检索行为已由 [检索](retrieval.md) 的授权混合检索首片实现，仍只按该指针关联 READY generation 与 `chunk_embedding`。

`ingest_job.profile_id` 已由迁移 `20260925_0006` 增加（可空 UUID 外键 → `index_profile(id)`，`ON DELETE/UPDATE RESTRICT`，无默认/回填/seed/索引/新授权）。本切片起，**新 Markdown 上传写路径**在同一个四表事务内调用 `ensure_default_index_profile(session)` 并显式把默认 profile 的行 id 写入新 `ingest_job.profile_id`（独立 tester 已验收）；worker 接收壳读取 `ingest_job.profile_id`、`document_version.parser_version` 与 `error_code`，仅用于判定无接收标记且 `error_code IS NULL` 的旧 job（已最终验收），不改写这些字段。幂等回放命中既有任务时不改写其 `profile_id`：既有 `QUEUED`/`HANDLER_NOT_READY` 任务升级后保持 NULL、`parser_version` 保持占位 `markdown-v1`，**不得**按最新 default profile 契约自动处理、补绑、重投或视为已绑定 profile；旧 `HANDLER_NOT_READY` 接收标记也不得盲目清除。因为 api/worker 对 `ingest_job` 拥有表级 UPDATE，数据库不保证该列不可变，也不强制它与目标 generation 的 profile 一致；未来 worker 接线时写入事务必须在提交前限制对它的更改并核对 `generation.profile_id` 一致，否则不能声称 profile 绑定已冻结。

worker 侧身份预检入口 `rag_backend.ingestion.identity_preflight` 已作为纯函数模块实现并完成独立验收（源码 SHA-256 `390B1D3C…1AA2`，独立 reviewer APPROVED、独立 tester 聚焦 36 passed）：导入期只引入标准库，不做任何 IO、不连数据库/Celery、不写接收 marker、不 ACK 消息、不改任务状态。调用方（未来的 worker）自行读取 `ingest_job.profile_id`、`document.source_type`、`document_version.parser_version` 与 `index_profile` 行，并把行 id、七个契约字段与 `config_hash` 映射为只读 DTO，连同 worker 启动时另行构造并校验过的 `IndexProfileContract`（`expected`）与真实 `expected_parser_version` 一起传入；模块返回八个互斥的静态判定。判定优先级为：`profile_id` 为 NULL → `PROFILE_UNBOUND`（旧任务不自动补绑）；读不到行 → `PROFILE_MISSING`；行 id 与绑定不符 → `PROFILE_ID_MISMATCH`；`source_type` 非 `markdown` → `SOURCE_UNSUPPORTED`；`parser_version` 与当前实现不一致（如旧占位 `markdown-v1`）→ `PARSER_UNSUPPORTED`；行七字段构造契约失败或与 `expected` 不一致 → `CONTRACT_MISMATCH`；行七字段规范 hash 与行 `config_hash` 不一致 → `HASH_MISMATCH`；只有 profile id、七字段、`config_hash` 与 parser/来源全部匹配才返回 `ALLOWED`。`ALLOWED` 只表示身份预检通过，**不等于 READY、不等于文档可检索**：发布前必须完成解析/切分/编码与发布事务，`knowledge_base.active_index_profile_id` 才可能置位。本模块当前由真实入库管线 `rag_backend.ingestion.indexing_worker` 在领取任务后调用（`worker.py` 本身不导入它；该管线默认关闭）：显式开启真实处理时先读取 `index_profile` 行再调用 `decide_profile_identity`，判定非 `ALLOWED` 即静态 `FAILED`。默认关闭的接收壳不导入本模块，而是在接收路径内用自己的 `profile_id`/`parser_version` 判定：只对无接收标记且 `error_code IS NULL` 的旧 job 静态拒绝为 `FAILED` + `LEGACY_JOB_UNSUPPORTED`，已有非 NULL 诊断码的旧 job 保持原样（已最终验收），合格 job 仍只写 `HANDLER_NOT_READY` 接收标记、`job.status` 仍为 `QUEUED`；本模块的八类判定尚未接入接收器，因此不能把本模块的独立验收获当作接收器已用完整身份预检。真实 tokenizer 四件与关键词分析器的显式初始化工厂见下段，但尚未接入 worker 进程启动；本模块不导入 `tokenizers`/`jieba`，API 镜像也不应导入 `tokenizers`。

worker 侧真实 tokenizer 四件与关键词分析器的启动校验由**可供 worker 显式调用的工厂**实现，并由默认关闭的真实入库管线在领取任务后调用：`rag_backend.ingestion.worker_index_identity.initialize_worker_index_identity(model_directory)`（工厂源码 SHA-256 `fe742eceadcf39dd910e4b66d742df4d065d0137f6f06cfdf1a1005ee4db866d`；作者侧聚焦 25 passed、1 skipped，skip 为宿主无 `/models` 真实资产，不计为 pass；独立 tester 已在真 worker 镜像内验收，见下）：只有上游显式调用才做 I/O，默认 `model_directory` 为 `token_counting.DEFAULT_MODEL_DIRECTORY`。校验顺序固定：先构造 `LocalTokenizerCounter(model_directory)`，逐件核对四个 tokenizer 产物的大小与 SHA-256、拒绝多余或缺失文件并只调用一次 `Tokenizer.from_file`；再逐件比较 `token_counting.TOKENIZER_ARTIFACTS` 与 `profile_contract.TOKENIZER_ARTIFACTS`，因第 1 步已把真实字节绑定到前者，逐件相等即传递绑定、不再二次哈希磁盘产物；再核对冻结常量 `embedding_client.EXPECTED_MODEL_REVISION`、`embedding_client.EMBEDDING_DIMENSION` 与 `chunking.CHUNKER_VERSION` 一致，并按冻结 revision 与摘要标签复算 `tokenizer_revision`；然后只构造一次 `KeywordAnalyzer()`（其 `analyzer_id` 直接进入契约，不调用 `default_index_profile()` 额外构造分析器）；最后用七个冻结字段与真实 `MARKDOWN_PARSER_VERSION` 构造 `IndexProfileContract`，成功才返回不可变的 `WorkerIndexIdentity`（`profile`、`parser_version`、`token_counter`、`keyword_analyzer`），并以 `@lru_cache(maxsize=1)` 按 `model_directory` 只缓存成功结果，失败不缓存、修复后可重试（`cache_clear()` 仅供测试）。任一分支失败统一收敛为静态 `WorkerIndexIdentityError`，消息只含类别、不回显目录/DSN/原始异常链。本模块导入即需要 worker 组依赖（`tokenizers`），但 `rag_backend.ingestion` 包导入期不引用它，API 镜像不应导入。本工厂**不核验** embedding/inference 的实际权重或实际使用的模型 revision（跨源检查只比较代码常量），也**不做**运行中目录被篡改后的再校验；它当前由默认关闭的真实入库管线在领取任务后按需调用（`worker.py` 本身不导入本模块，也无 Celery 启动信令），导入期不写 marker、不读 DB、不 ACK；关闭该开关时宿主缺 `/models` 的安全接收壳不变、`job.status` 仍为 `QUEUED`。**独立 tester 已在真 worker 镜像内独立验收**：最终工厂源码 SHA-256 `fe742ece…b866d`，宿主与镜像四关键源码 SHA 逐字节相同；有网按锁定 deps `docker buildx --target worker --build-context model_assets=docker-image://citemind-inference@sha256:02e801e8…` 全新构建两次（第二次 `--no-cache`，所有 RUN 重跑但 uv cache mount 仍复用；`jieba==0.42.1` 现场由锁定 sdist 构建、`tokenizers==0.23.2` 联网下载），两镜像在 `docker run --rm --network none --read-only` 加安全 tmpfs `/tmp` 下**各 99 断言全 pass**（golden 46/46、空 tmpfs 遮蔽 `/models` 负例 8/8、只读无可写 `/tmp` 负例 9/9、四件各改 1 字节/截断/复原重试 36/36），覆盖七字段、默认 hash `4af4c33d…57fa`、jieba 字典 size/sha、真 parser、模型文件 size/sha、golden token 22/31/11、空串 2、单次构造+成功缓存、失败不缓存后复原再成功、静态无路径无 cause 与私有 tmp 清理；host 聚焦工厂+token_counting 47 passed、2 skipped（两 skip 需宿主 `/models`，不计为 pass，已由镜像覆盖）。该验收只核 tokenizer 四件与冻结常量，**不核 inference 实际权重或 revision**；测试标签/容器已清空，dev 六 ID/镜像/健康不变，未读 `.env`、未连 PG/Redis，api target 本轮未重建，测试为仓库外脚本注入、`tests/` 不在镜像。**启动接线**：不在进程启动期加载，仅由默认关闭的入库管线在领取任务后调用；无 Celery 启动信令。

必测故障：事务提交后 Redis 断连、投递成功但 SENT 回写失败、worker 在构建中被杀、broker 重启、重复消息、解析超时和旧 dispatcher 租约过期后迟到回写。详见 [评估与验收](evaluation.md)。

## 已实现：worker 真实入库管线（默认关闭）

`rag_backend.ingestion.indexing_worker` 把已独立验收的前置能力串成一条最小一致的首次入库链，
并由 `rag_backend.worker.receive_ingest_request` 在显式开启 `INGEST_PROCESSING_ENABLED=1`
时调用。默认 `INGEST_PROCESSING_ENABLED=0`，接收任务行为与既有安全接收壳完全一致
（只写 `HANDLER_NOT_READY` 接收标记、`status` 仍 `QUEUED`）；开启后不再写接收标记，而是进入
真实处理；开启时启动校验要求 `INFERENCE_TOKEN` 存在。

- **领取**：`claim_ingest_job` 在行锁下读取 job/document/version/KB 事实，按已验收优先级判定：
  非 `QUEUED` → `not_queued`；已 tombstone → `deleted`；版本归属不符 → `version_mismatch`；
  已有接收 marker → `already_received`；已有非 NULL 诊断码 → `existing_diagnostic`
  （只读、不改写）；`profile_id` 未绑定、来源不在受支持集合或 `parser_version` 与按
  `document.source_type` 选定的当前实现版本不一致 → 静态 `FAILED` + `LEGACY_JOB_UNSUPPORTED`
  （复用接收壳同款守卫语义；PDF job 不会因 Markdown 版本而被误判）；首次版本（`version_no == 1`）
  已有 `active_version_id` 或该 version 已有 READY generation，或文档新版本缺可解析 expected/
  文档无 active/该 version 已有 READY generation → 静态 `FAILED` + `PIPELINE_UNSUPPORTED_UPDATE`
  （拒绝超范围更新，不猜退役逻辑）；文档新版本 expected 可解析且文档已有 active，但 expected 已
  被更早发布超越（active 只前进不回退）→ 静态 `FAILED` + `PIPELINE_STALE_EXPECTED`，不解析/
  编码/暂存，发布 CAS 仍保留。只有合格 job 才用带全部守卫的
  原子 CAS 写入 `lease_owner=pipeline:<eventId>`、随机 `lease_token`、`lease_until`、
  `heartbeat_at`、`attempt=attempt+1`、`status=PARSING`。
- **身份预检**：复用 `load_stored_profile` + `decide_profile_identity`，只有 row id、七字段、
  `config_hash`、parser 与来源（`markdown`/`pdf`）全匹配才继续；其余按静态错误码进入 `FAILED`。
  期望 parser 版本由 `document.source_type` 选定（Markdown 用
  `markdown-it-py-4.2.0-v1`，PDF 用 `pypdf-6.19.0+pdfplumber-0.11.10-v1`）。
- **解析切分（独立子进程）**：按 `document.source_type` 分派：Markdown 走
  `read_verified_markdown` + `parse_markdown_in_subprocess`；PDF 走 `read_verified_pdf`（同一严格
  摘要/大小/魔数校验）+ `parse_pdf_in_subprocess`。父进程用 `communicate(input=.., timeout=60s)`
  有界等待，超时真实 `kill`+`wait` 并静态 `FAILED`（`PIPELINE_PARSE_TIMEOUT`），子进程环境经白
  名单剥离 DSN/凭据、返回体受 `MAX_PARSE_RESULT_BYTES` 限制；PDF 解析最多 200 页，加密/超页/结构
  损坏分别映射为 `PIPELINE_PDF_ENCRYPTED`/`PIPELINE_PDF_TOO_MANY_PAGES`/`PIPELINE_PDF_INVALID`。
  随后在父进程用 `chunk_markdown` 与注入的真实 `LocalTokenizerCounter` 切分；PDF 在页边界强制
  切分、不跨页且不带上一页重叠；`source_sha256` 必须等于登记 `file_hash`，空正文/超预算分别静态
  失败。读 blob、解析、摘要失败都映射为静态 `FAILED`，不 DELETE、不碰共享 blob，也不让心跳无限期
  掩盖挂起的解析。**Linux 子进程入口在读取输入前对自身设 `RLIMIT_AS` 虚拟地址空间上限（默认
  1 GiB）**，只作用于该子进程，父 worker 与其它线程不受影响；上限设置失败在解析前静态失败，设置
  成功后分配超过地址空间才 `MemoryError`。该 1 GiB 值未在真实 Linux 上测量合法输入峰值余量，不保证
  覆盖所有合法最大输入，也不是 RSS/父侧缓冲/cgroup 完整隔离；Phase 4 overlay 的 640 MiB 是整容器内存限额（不等同进程 RSS，包含父子进程
  及其它 cgroup 记账内存），可能先 cgroup OOM 杀 worker 而非让 child 受控失败。Windows 及其它非 Linux 平台本实现不应用
  上限，只有 60 秒硬时限与返回体上限。
- **数据库异常收敛**：领取任务后若 `load_stored_profile`/阶段推进/暂存/发布或失败标记抛出
  `SQLAlchemyError`，在连接回滚释放后用独立短事务重读 job：已是 `READY`（commit 结果未知）则
  保留成功不误改 `FAILED`；仍持未过期租约且处于活动阶段才用 CAS 落 `FAILED`；数据库持续不可用
  时返回 `persist_unconfirmed` 保留人工发现与恢复，不谎报成功或失败（该状态可能来自数据库持续不可用或独立只读重判无法确认 job 事实；Celery 正常返回会被 ACK，result 按 `task_ignore_result=True` 丢弃，dispatcher 补偿不处理该任务，但同一后台周期的恢复扫描会处理租约已过期的活动任务；`persist_unconfirmed` 本身不自动重试）；绝不动他人租约。
- **编码**：`build_model_input(heading_path, text)` 作为编码输入，交给受限
  `InternalEmbeddingClient.embed_document_texts`；只有暂时性编码失败（busy/transport/upstream，
  由客户端 `retryable` 标记）在总尝试预算内退避重试，永久输入错误、未就绪、未知 503、响应契约
  不符都立即静态失败。`EMBEDDING_*` 失败不会把 job 永久留在 `QUEUED`。
- **暂存**：短事务写入 `index_generation(status='BUILDING', expected_chunks=N)`，在同一事务内把
  `ingest_job.generation_id` 绑定到该 generation，再写入 `chunk`（`fts` 由已验证
  `KeywordAnalyzer` 的 `to_tsvector('simple', :bound)` 参数绑定产生）与 `chunk_embedding`
  （`profile_id` 显式绑定、512 维）；提交前核对 chunk/向量数量，租约 CAS 失败即回滚。
- **发布**：单事务锁定 job 与 document，核对仍持有效租约、`generation_id`/`profile_id` 一致、
  document `active_version_id IS NULL` 且未删除，再核对 chunk/向量数量，随后：
  `knowledge_base` 用条件 UPDATE（`active_index_profile_id IS NULL OR = :profile`）首次置位并
  `kb_revision = kb_revision + 1`（非 NULL 且不同 profile 即拒绝）；`index_generation` 置
  `READY` 并 `actual_chunks=expected_chunks`、写 `ready_at`；`document.active_version_id` 与
  `lifecycle_status='READY'`；`document_version.status='READY'`；`ingest_job.status='READY'` 并
  清租约。任一步失败都显式 `rollback`，绝不提交部分写入。
- **失败与租约**：发布前失败只把目标 generation 置 `FAILED`、job 置 `FAILED` 并清租约，再用
  `active_version_id IS NULL` 守卫把版本与 document 置 `FAILED`，绝不使既有有效文档下线；
  **零可提取文本 PDF** 例外：`document_version` 置 `NEEDS_OCR`、job 置 `FAILED` +
  `PIPELINE_NEEDS_OCR`，不建 generation、不置 `document.active_version_id`（document 仍按
  `active_version_id IS NULL` 守卫置 `FAILED`）。
  失租约时返回 `lease_lost` 且不覆盖他人。`LeaseHeartbeat` 用独立连接续租，失租约/心跳异常都
  fail closed，`finally` 中 `stop()`/`join`；处理中崩溃若租约过期，由 dispatcher 的有界恢复扫描重排
  （未达 `MAX_PIPELINE_ATTEMPTS=3`）或静态 `FAILED`+`PIPELINE_RETRY_EXHAUSTED`；不设独立常驻
  reaper，恢复不写 BUILDING generation，遗留 `BUILDING` generation 不可检索、下一次成功暂存重绑。
- **依赖装配**：`worker.py` 在任务内按需导入真实 identity/embedder；模块导入期不加载
  `tokenizers`、不注册启动信令。`initialize_worker_index_identity` 失败统一收敛为静态
  `PIPELINE_IDENTITY_UNAVAILABLE`（先在领取阶段取得 lease，再写失败终态），因此资产/契约永久
  失败也有明确静态终态，不会 ACK 后永久 `QUEUED`。

**最终验收状态**：`tests/integration/test_indexing_pipeline_flow.py` 15 passed/0 skipped 与
`tests/integration/test_worker_kb_publish_migration.py` 3 passed/0 skipped 已由独立 tester 在各自
独立空库、各自降回 base 的情况下运行（同库错端口守卫的 15/3 errors 属预期负测，不是 pass）；
独立 61 checks 在真 PG 上以假 encoder/tokenizer、真解析子进程覆盖并发发布与非恢复分支；另一
独立 tester 用当前锁全新构建镜像、真离线模型在 Linux prefork concurrency 1 上端到端验收
PG `0007` READY。宿主最终聚焦 191 passed、非集成 1002 passed/3 skipped/156 deselected；ruff、
mypy（136 files）、`uv lock --check`、`git diff --check` 绿（**不称全 integration 绿、不称
`ruff format --check` 绿**）。完整证据见 [开发约定](development.md)。仍**未测**父 worker 被 kill
后子进程回收与租约恢复、物理 Redis 停启、自然 3600 秒重投、多 worker、p95 与 20MB 吞吐；GC、
问答主流程已实现（默认关闭，未对真实 provider 验收；授权检索首片见 [检索](retrieval.md)）。迁移 `20260925_0007` 只给 worker 增加
`knowledge_base(active_index_profile_id, kb_revision)`
列级 UPDATE，不授予全表 UPDATE。

**本轮新增：增量 embedding 缓存（迁移 `20260929_0014`）**。缓存直接复用既有 `chunk_embedding`，不新增 `embedding_cache` 表、Redis 或进程内 LRU。worker 在解析/切分后、构造 embedder 与编码前，按 `chunk.model_input_hash` 去重并批量查询可复用向量；命中按 hash 填充，miss 只编码唯一的 `model_input_hash` 对应输入并 fan-out 到重复 chunk，输出顺序与 chunks 一致。所有 chunk 仍重建 FTS/locator、写新 generation/embedding，发布 CAS、租约与重试语义不变。

缓存命中必须经权威链 `chunk→index_generation→document_version→document→knowledge_base` 校验，命中条件为同 `organization_id`、`g.status='READY'`、`g.profile_id=:profile_id`、`ce.profile_id=:profile_id`、`chunk.model_input_hash` 命中，并排除已删除文档（`deleted_at` 非空或 `DELETED`）；不要求缓存来源仍是文档 active version，允许同组织跨文档与旧版本复用。缓存键边界由不可变 `index_profile` 身份与运行期身份预检确定，编码请求固定 `kind='document'`（`embedding_client.REQUEST_KIND`），pooling/provider 不是当前可配置自由度、由冻结模型 revision 与 inference 契约约束，不新增 profile 字段、不改 `config_hash`。

缓存查询使用独立短只读事务并在 `finally` 显式 `rollback` 结束，绝不影响主事务；查询/连接失败只记静态、不含正文与 DSN 的 warning 并回退全量 miss 编码，不吞 `KeyboardInterrupt`/`SystemExit`/`MemoryError`，只捕预期 `SQLAlchemyError`。缓存向量经严格 512 维与 finite 校验，畸形/维度错误按该 hash miss 处理，绝不写入无效向量，结果不泄露源 chunk id/text。

迁移 `20260929_0014` 只新增具名 btree 索引 `ix_chunk_model_input_hash`（`chunk(model_input_hash)`），降级只删除该索引；SQLAlchemy 模型 `Chunk` 同步声明。

**退出证据口径**：至少覆盖（1）全命中不调用编码器；（2）部分命中只编码 miss；（3）重复 hash 只编码一次并 fan-out；（4）缓存查询失败回退全量编码；（5）畸形/维度错误回退；（6）SQL 形状覆盖权威链、同组织、generation 与 embedding 双侧 profile、READY、排除删除；（7）整篇相同、局部更新、同组织跨文档命中、跨组织不命中、不同 profile/revision 不命中、旧版本复用与发布 CAS 不变，并量化编码器收到的文本数与最终向量数。真实 PostgreSQL 集成在无 Docker 守护进程时应明确标注未跑，不能以跳过冒充通过。

**本轮新增：文本 PDF 最小兼容切片（未提交）**。在 Markdown 链路上按来源分派，复用同一
`index_profile`、`config_hash=4af4c33d…57fa` 与 `CHUNKER_VERSION='heading-pack-v1'`，不新增迁移。
新增 `rag_backend.ingestion.pdf_parsing`（`PDF_PARSER_VERSION='pypdf-6.19.0-v1'`、`MAX_PDF_PAGES=200`、
按页抽取、加密/超页/损坏具名错误），`pypdf` 只加入 `dev` 与 `worker` 依赖组并更新 `uv.lock`（api 镜像仍不
安装）；`parse_subprocess` 按 argv 来源分派并新增 PDF 具名退出码；chunking 在页边界强制切分并输出
`locator_version=2` 页定位（Markdown `locator_version=1` 键集合与 `text_hash`/`model_input_hash`
保持不变）；`identity_preflight.SUPPORTED_SOURCE_TYPES` 与 worker 身份均支持 `pdf`；PDF 零可提取
文本落 `NEEDS_OCR`。**本轮实际运行**：宿主单元与纯逻辑测试 1078 passed / 3 skipped（含新增
`tests/unit/test_pdf_parsing.py`、`test_parse_subprocess.py` 的 PDF 子进程用例、
`test_indexing_persist.py` 的 PDF 终态用例）、`ruff check` 与 `mypy`（150 files）绿。隔离验收：
一次性 pgvector/pg17 容器（`127.0.0.1:55535`，项目 `myrag-pdf-test`，合成凭据与 `citemind_test`
库，使用仓库 initdb 脚本，未触碰 dev 六服务）与一次性带鉴权 Redis（`127.0.0.1:56380/1`）上：
`test_indexing_pipeline_flow.py` **17 passed**（含 PDF READY + 页 locator、PDF 零文本层
`NEEDS_OCR`）、`test_retrieval_flow.py` **18 passed**（含同 KB 混合 Markdown+PDF 双文档召回与
`locator_version=2` DB 值）、`test_document_upload_flow.py` **16 passed**（含 `.pdf` 上传登记
`source_type=pdf`/`application/pdf`/`pypdf-6.19.0-v1` 与无魔数 422 `DOCUMENT_NOT_PDF`），全仓
`uv run pytest -m integration -q` 为 **178 passed / 0 skipped / 0 failed**。**未运行**：镜像构建、
真离线模型端到端与 dev 部署，因此不声称已部署或已用真模型验收；检索响应不回传原文/locator，
页定位验收落在 `chunk.source_locator` 与单元测试。

**本轮新增：DOCX 最小闭环（未提交）**。在 Markdown/PDF 链路上按来源分派，复用同一
`index_profile`、`config_hash=4af4c33d…57fa` 与 `CHUNKER_VERSION='heading-pack-v1'`，不新增
解析契约字段，只新增迁移 `20260929_0013` 放宽 `document.source_type` CHECK 到 `docx`。新增
`rag_backend.ingestion.docx_parsing`：`DOCX_PARSER_VERSION='python-docx-1.2.0-v1'`，只有
`parse_docx` 被调用时才延迟导入 `python-docx`，顶层只依赖标准库，因此 API 镜像仍不安装
`python-docx`/`lxml`；`pyproject.toml` 的 `dev` 与 `worker` 依赖组各加 `python-docx==1.2.0`
并更新 `uv.lock`。受理期用标准库 `zipfile` 元数据快速拒绝非 PK/条目数 >512/声明累计解压
>64 MiB/单条 >1 MiB 且压缩比 >100/加密/绝对或 `..` 路径/反斜杠/NUL/重复名/缺必需部件/宏部件；
worker 侧再用同一策略逐条有界流式实际读取、累计实际字节、校验 CRC 并拒绝 `<!DOCTYPE`/
`<!ENTITY` 声明后才交给 `python-docx`（其 XML 解析器配置 `resolve_entities=False`，外链不访问）。
`storage.read_verified_docx` 额外校验 `PK` 魔数；`parse_subprocess` 按 argv 分派并新增 DOCX 具名
退出码；`chunking` 输出 `locator_version=3`（Markdown `locator_version=1`、PDF `locator_version=2`
键集合与 golden 不变）；`identity_preflight.SUPPORTED_SOURCE_TYPES`、`worker_index_identity` 与
`indexing_worker` 按来源选期望 parser 版本，PDF/DOCX 不会被误判 `LEGACY_UNSUPPORTED`。

允许子集与索引边界：只索引 `word/document.xml` 正文，页眉、页脚、脚注、尾注、批注与文本框
不索引，也不做检测或拒绝。正文按 `w:p`/`w:tbl` 原顺序遍历，正文段落 1-based 编号（空段也计索引、
不产出块），内置 `Heading N`/`标题 N` 更新标题栈；表格逐行一个块，横向合并只记真实 origin 一次，
纵向合并 continue 不复制上一行文字，`gridBefore` 计入 1-based 网格列；单元格内嵌套表、`w:sdt`、
`w:altChunk`/`w:customXml`、宏部件与实体声明等收窄外结构一律静态失败并落
`PIPELINE_DOCX_UNSUPPORTED`，坏 ZIP/CRC/XML 与实际解压/资源超限落 `PIPELINE_DOCX_INVALID`。
Linux 子进程入口用 `RLIMIT_AS` 设虚拟地址空间上限（默认 1 GiB，设置失败在解析前静态失败，设置成功后超限才 `MemoryError`），只约束该子进程且未在真实 Linux 上测量合法输入峰值余量；Windows 及其它非 Linux 平台本实现不应用上限，只有 60 秒子进程硬时限与返回体上限；不声称 RSS/cgroup 级内存隔离。

**本轮实际运行**：`tests/unit/test_docx_parsing.py` **18 passed**，聚焦受影响的 13 个 unit 文件
**512 passed / 1 skipped**；`uv run ruff check backend/src migrations tests`、`uv run mypy`（206 files）
与 `uv lock --check` 绿；前端 `pnpm run build`（vue-tsc + vite）与 `pnpm test`（15 passed，含新增
`frontend/tests/labels.test.ts` 的 `locator_version=3` 与 DOCX 标签断言）绿。隔离验收：一次性
`pgvector/pgvector:pg17`（`127.0.0.1:55990`，项目容器 `myrag-docx-pg`，仓库 initdb 脚本、合成凭据）
与一次性带鉴权 `redis:7.4.9`（`127.0.0.1:56990/15`，合成密码）上：`test_document_source_docx_migration.py`
**3 passed**（升级允许 docx/拒绝未知、有 docx 行降级被拒不删数据、无 docx 行降级恢复旧约束）、
`test_indexing_pipeline_flow.py` **19 passed**（含 5 份自制正样本真实解析子进程 + 假编码器发布 READY
且落 `locator_version=3`、嵌套表 `PIPELINE_DOCX_UNSUPPORTED`）、`test_document_upload_flow.py`
**18 passed**（含 `.docx` 上传登记 `source_type=docx`/DOCX MIME/`python-docx-1.2.0-v1` 与坏 ZIP 422
`DOCUMENT_NOT_DOCX`）、`test_document_update_delete_flow.py` **11 passed**。**未运行**：镜像构建、
真离线模型端到端、dev 部署与物理前端 UI；因此不声称已部署或用真模型验收。

**本轮新增：PDF pdfplumber 逐页抽取适配（未提交）**。保留 `pypdf` 做加密标志、结构/页数预检与既有
`PdfEncrypted`/`PdfTooManyPages`/`PdfInvalid` 映射，只把逐页正文抽取换成 `pdfplumber`；解析器版本改为
单一真源 `PYPDF_VERSION`/`PDFPLUMBER_VERSION` 拼成的 `PDF_PARSER_VERSION='pypdf-6.19.0+pdfplumber-0.11.10-v1'`，
诚实包含两个引擎的固定版本。`pdfplumber==0.11.10` 只加入 `dev` 与 `worker` 依赖组并更新 `uv.lock`，保留
`pypdf==6.19.0`；`pdf_parsing` 顶层仍只导入标准库、两个引擎都在 `parse_pdf` 内延迟导入，因此 API 进程
导入 `rag_backend.main` 后 `pdfplumber`/`pdfminer`/`PIL`/`pypdf` 均不在 `sys.modules`（已实测断言）。
受理期 20,000,000 字节上限、`MAX_PDF_PAGES=200`、60 秒子进程硬时限、返回体上限、`source_sha256`、
1-based `page`、`heading_path=()`、无伪造行号、`locator_version=2`、chunk 不跨页与既有具名错误码全部不变；
只有全部页都无文本才落 `NEEDS_OCR`，部分空白页只被忽略。未做 OCR、表格结构化、布局坐标、新 locator、
运行时双引擎开关、迁移或 profile/`config_hash`/chunker 变更。

新增 `tests/unit/pdf_samples.py`：用标准库按 PDF 语法拼装 5 份字节确定的自制正样本（ASCII 多页、
中文文本层（标准 `STSong-Light`+`UniGB-UCS2-H`，不嵌入字体）、左右分栏+缩进、单页长文本触发页内
切分、空白页+文本页），以及全空白 `NEEDS_OCR`、加密、损坏、超页负例；生成端不新增生产依赖。
**本轮实际运行**：`tests/unit`（排除基线即失败的 `tests/unit/test_document_upload_cleanup.py`，
该文件在本轮基线 HEAD `8603d0f` 上因 DOCX 切片给 `open_upload_schema` 加了 `engine.begin()` 而
未同步更新假 engine，属既有失败、与 PDF 改动无关）为 **1464 passed / 4 skipped**；全仓
`uv run pytest -m "not integration" -q` 为 **1557 passed / 4 skipped / 1 failed**（唯一失败即上述
基线失败）；`uv run ruff check backend/src migrations tests`、`uv run mypy`（207 files）、
`uv lock --check` 绿。**未运行**：隔离 PG17+Redis 的迁移/pipeline/upload/update/retrieval 集成测试
（本机 Docker 守护进程未运行，无法建临时容器）、镜像构建、真离线模型端到端与 dev 部署，因此不声称
已部署或用真模型验收；5 份正样本已在 `tests/unit/test_parse_subprocess.py` 走真实 PDF 解析子进程并断言
`locator_version=2` 与单页 `pages`，但未在真实 PostgreSQL 上发布过 READY。
