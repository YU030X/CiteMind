# Agent Note：DOCX 最小闭环的允许子集与定位契约

- 状态：已实现
- 范围：`rag_backend.ingestion.docx_parsing`、`locator_version=3`、ZIP 安全限额、迁移 `20260929_0013`

## 背景

Phase 2 要把 DOCX 纳入最小可检索闭环。DOCX 是 ZIP/OPC 包，内容结构比 Markdown/PDF 复杂，
存在嵌套表、结构化文档标签、宏部件、外部内容与实体声明等超出 MVP 的结构。若只挑自己能读的
部分收录并对外声称完整，会静默丢掉用户内容；若把整包无界交给第三方库解析，又无法说明资源边界。

## 决策

1. **只承诺收窄子集：仅索引 ``word/document.xml`` 正文。** 支持正文 `w:p`、表格 `w:tbl`、内置
   ``Heading N``/``标题 N`` 标题样式、横向/纵向合并与 `gridBefore`；页眉、页脚、脚注、尾注、批注
   与文本框不索引，也不做检测或拒绝。可检测到的超集结构具名静态失败：单元格内嵌套表、
   `w:sdt`、`w:altChunk`、`w:customXml`、宏部件或 `<!DOCTYPE`/`<!ENTITY` 声明落
   `PIPELINE_DOCX_UNSUPPORTED`；坏 ZIP/CRC/XML 与实际解压/资源超限落 `PIPELINE_DOCX_INVALID`。
   这样已检测到的失败是显式的，不会把残缺索引伪装成完整。
2. **不处理宏与外链。** 含宏部件的包直接拒绝；解析只读 `w:t` 文字，超链接只取其显示文字而
   从不读取目标关系，也绝不发起网络请求。
3. **来源定位用 `locator_version=3`，不伪造 Word 页码。** 段落以 1-based body 段落索引记录
   （空段也占索引但不产出块）；表格行记录 `table_index`/`row_index`、每个真实来源单元格的
   1-based 网格列与 `grid_span`，以及它在规范化行文字中的字符区间。横向合并只记 origin 一次；
   纵向合并的 continue 单元格不产生定位、不复制上一行文字。Markdown 的 `locator_version=1`
   与 PDF 的 `locator_version=2` 键集合、`text_hash`/`model_input_hash` 与 `config_hash` 均不变。
4. **ZIP 安全分两层且共用同一策略。** 受理期（不安装 `python-docx` 的 API 进程）用标准库
   `zipfile` 只读元数据快速拒绝非 PK、条目数 >512、声明累计解压 >64 MiB、单条 >1 MiB 且压缩比
   >100、加密、绝对/`..` 路径、反斜杠、NUL、重复名、缺必需部件与宏部件；worker 侧再用同一策略
   逐条有界流式实际读取并累计实际字节、校验 CRC、拒绝实体声明后，才把字节交给 `python-docx`。
   `python-docx==1.2.0` 的 XML 解析器已配置 `resolve_entities=False`，lxml 不联网。限额常量只
   定义一处，避免两份策略漂移。
5. **不声称内存硬隔离。** 只提供子进程 60 秒硬时限与返回体上限，没有 rlimit/cgroup 级内存硬限；
   文档与安全文档必须如实描述这一边界，不能把“有界流式读取”写成“内存隔离”。
6. **依赖只在 worker/dev。** `python-docx==1.2.0`（连带 `lxml`）只加入 `dev` 与 `worker` 依赖组，
   `docx_parsing` 顶层只导入标准库，`python-docx` 在 `parse_docx` 内延迟导入，因此 API 镜像
   不安装也不加载 `python-docx`/`lxml`。
7. **迁移只放宽 CHECK，降级不删数据。** `20260929_0013` 仅把 `ck_document_source_type` 扩到
   `docx`；降级时若已存在 `source_type='docx'` 的行直接失败并保留原行，绝不静默删除或改写文档。
