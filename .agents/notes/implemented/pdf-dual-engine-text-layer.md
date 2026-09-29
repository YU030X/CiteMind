# Agent Note：PDF 双引擎预检 + pdfplumber 文本层抽取

- 状态：已实现
- 范围：`rag_backend.ingestion.pdf_parsing`、`PDF_PARSER_VERSION`、dev/worker 依赖组、PDF 样本与 locator

## 背景

Phase 2 要求把 PDF 正文抽取升级为 pdfplumber，以改善中文/复杂文本层的抽取质量。现有
`pypdf==6.19.0` 已承担加密标志、页数与结构预检，并映射出既有的 `PdfEncrypted`/
`PdfTooManyPages`/`PdfInvalid` 具名错误；直接换引擎会丢掉这些稳定契约。另一方面，如果
解析器版本只写 pdfplumber，会隐藏 pypdf 仍然参与解析的事实，来源定位与评估漂移校验就无法
解释同一实现版本下的行为差异。

## 决策

1. **双引擎分工，不做运行时开关。** `pypdf` 只负责 `parse_pdf` 开头的预检：读取结构、判定
   `is_encrypted`、取页数、碰 `PdfReadError`；`pdfplumber` 只负责逐页 `extract_text`。预检在
   抽取之前失败，加密/超页/损坏不再进入 pdfplumber。没有配置项能在两者之间切换。
2. **解析器版本诚实包含两个固定引擎。** `PYPDF_VERSION` 与 `PDFPLUMBER_VERSION` 是单一真源，
   `PDF_PARSER_VERSION = f"pypdf-{PYPDF_VERSION}+pdfplumber-{PDFPLUMBER_VERSION}-v1"`，当前为
   `pypdf-6.19.0+pdfplumber-0.11.10-v1`；测试同时断言字面量与两个已安装库的 `__version__`。
   升级任一引擎都必须同步更新常量、字面量测试、评估清单 `parserVersion` 与相关文档。
3. **依赖只进 worker/dev，API 不加载。** `pdfplumber==0.11.10` 与 `pypdf==6.19.0` 都只在
   `[dependency-groups]` 的 `dev` 与 `worker`；`pdfplumber` 的传递依赖为 `pdfminer.six`、`Pillow`、
   `pypdfium2`，`pdfminer.six` 再带 `charset-normalizer` 与 `cryptography`。其中 `Pillow`、
   `pypdfium2` 与 `cryptography` 含原生扩展/预编译二进制，会扩大 worker 镜像体积与依赖面；它们
   仍只在上述依赖组，API 镜像不安装。`pdf_parsing` 顶层只导入标准库，两个引擎都在 `parse_pdf`
   内延迟导入；因此 API 镜像不安装也不导入 `pdfplumber`/`pdfminer`/`PIL`/`pypdf`，
   `rag_backend.main` 的导入隔离有独立子进程测试证据（仅证明未导入，不对这些依赖的安全性做结论）。
4. **契约不变。** 20,000,000 字节受理上限、`MAX_PDF_PAGES=200`、60 秒子进程硬时限、返回体上限、
   `source_sha256`、1-based `page`、`heading_path=()`、不伪造行号、`locator_version=2`、chunk
   不跨页、既有具名错误码与 `document_version.status='NEEDS_OCR'` 终态都不变。语义上只有一处
   明确：部分空白页被忽略并保留有文本页，只有全部页都无文本才 `NEEDS_OCR`。
5. **不臆造中文样本。** 生成端不引入新生产依赖，也不嵌入字体：中文样本用标准 `STSong-Light`
   加 `UniGB-UCS2-H`/Adobe-GB1 映射构造文本层，`pdfminer` 通过其内置映射抽回中文；ASCII 样本用
   标准 Type1 Helvetica。5 份正样本字节确定且互不相同，负例覆盖全空白/加密/损坏/超页。
6. **不声称内存隔离。** 只保留子进程 60 秒硬时限与有界返回体，没有 rlimit/cgroup 级内存硬限；
   安全文档继续如实描述这一边界。
