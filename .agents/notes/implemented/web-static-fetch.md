# Agent Note：受限静态网页抓取与 `locator_version=4`

- 状态：已实现
- 范围：`rag_backend.ingestion.web_fetch`、`web_parsing`、`locator_version=4`、迁移 `20260929_0015`

## 背景

Phase 2 要把受限静态网页纳入最小闭环。网页抓取天然带 SSRF 与资源耗尽风险：URL 任意、
DNS 可被重绑定、响应可无限大或伪装成文本。若只做字符串前缀白名单，无法说明真实连接目标；
若允许登录、执行 JS 或递归抓取，范围与责任都会失控。

## 决策

1. **抓取在 API、`202` 之前完成，worker 只读 blob。** 原 HTML 字节进入既有内容寻址存储，
   worker 与 Markdown/PDF/DOCX 一样只读 blob 离线解析；抓取失败在写库前返回静态具名错误，
   不产生任何入库行或 blob。
2. **幂等重放先于抓取。** 同一 `Idempotency-Key` 命中已有请求时，只比对服务端规范化 URL
   （`document_version.source_url`）与标题：一致返回原 ids、**不联网**，不一致静态 409。
   只有确定是新请求才调用抓取，随后仍走既有四表事务与唯一键 CAS 防并发。
3. **允许主机是精确列表，默认空即禁用。** `WEB_FETCH_ALLOWED_HOSTS` 只接受 IDNA 小写、去尾点
   的精确 host，不做后缀/通配符；未配置时任何网页导入都 `NOT_ALLOWED`（fail closed），
   worker 不需要也不读取该配置。
4. **只允许 http/https、默认 80/443、无 userinfo/fragment，最多 3 跳且每跳重校验。**
   每跳用标准库解析器解析全部 A/AAAA，任一地址非公网（回环/私有/link-local/多播/保留/未指定/
   CGNAT/IPv4-mapped）即拒绝，并拒绝 https→http 降级。0 重定向且只接受 200。
5. **连接与响应边界显式收紧。** httpx 同步客户端 `trust_env=False`、零自动重试、不发
   Cookie/Authorization、显式 `Accept-Encoding: identity`；只接受 `text/html`/
   `application/xhtml+xml`，拒绝任意 `Content-Encoding`，`Content-Length` 早拒且 `iter_raw`
   累计硬上限 2 MiB，并设显式 connect/read/write/pool 超时。错误全部静态脱敏。
6. **诚实披露 DNS 竞态。** 校验用标准库解析结果，实际连接仍用 hostname，未做 IP pin 或
   `getpeername` 复核；因此本切片**不声称**抗 DNS rebinding，完整 SSRF/网络策略归 Phase 4。
7. **解析只取静态正文，不执行任何东西。** BeautifulSoup4+lxml 只在 worker 子进程延迟导入；
   整体移除 script/style/template/noscript/nav/aside/header/footer，在 main/article/body 中按
   确定块（p/li/pre/blockquote）抽取，h1–h6 只维护 `heading_path`；不执行 JS、不访问任何
   外链目标。空正文落 `PIPELINE_CONTENT_EMPTY`，不新增状态。
8. **`locator_version=4` 与既有 locator 隔离。** locator 含 `source_url`/`final_url`/
   `fetched_at`、`source_sha256`、`parser_version` 与 segments 的 block ordinal/字符区间；
   抓取元数据由 worker 解析后用 `dataclasses.replace` 注入 `ParsedDocument`（新增字段默认
   `None`），因此 Markdown/PDF/DOCX 的 golden 结果不变。
9. **依赖只在 worker/dev。** `beautifulsoup4==4.15.0` 与 `lxml==6.1.3` 只加入 `dev`/`worker`
   依赖组，`web_parsing` 顶层只导入标准库并在 `parse_web` 内延迟导入 bs4，API 镜像不安装也
   不导入解析库。
10. **迁移只放宽 CHECK、加可空列，降级不删数据。** `20260929_0015` 把
    `ck_document_source_type` 扩到 `web` 并给 `document_version` 加可空
    `source_url`/`final_url`/`fetched_at`；降级时若已存在 `source_type='web'` 的行直接失败
    并保留原行，只有无 web 行时才删列并恢复旧约束。
