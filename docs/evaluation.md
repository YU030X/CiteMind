# 评估与验收计划

> 所有数字是待验证的目标，没有已完成的测试结果——本节已实测的 Phase 1 开发集结果与五项确定性指标例外（见“已运行：Phase 1 真实 40 题开发评估”）。保留数据集版本、模型 revision、配置、原始输出、硬件、并发、失败分母与评估脚本，使结果可复算。Phase 1 的 40 题开发集、自制语料、离线校验与最小 runner 已实现并已对真实隔离链路运行一次；留出集与质量/性能目标仍是待验证目标，尚未测量。

## 固定题集

计划制作 100 道自制问题，按文档家族和问题模板拆为 40 道开发题、60 道留出题；同义改写不得分别落在两侧。每题保存角色、可访问的文档版本、预期行为、答案要点和最小充分 `gold_source_spans`。文档允许作为检索库，留出的是问题及模板。

| 类型 | 开发 | 留出 | 用途 |
| --- | ---: | ---: | --- |
| 单文档可回答 | 24 | 26 | 基础检索与引用 |
| 跨文档组合 | 8 | 12 | 多证据覆盖 |
| 无答案/证据不足 | 4 | 11 | 拒答 |
| 当前角色无权限 | 4 | 11 | 不泄露存在性 |

MVP 先有至少 30 道开发题；100 题与留出集用于完整评估。gold span 绑定固定原文版本、parser 版本与 headingPath + 1-based 行区间（Markdown）或 1-based page（PDF），不绑定 chunk ID。更换 parser 后重新映射并升级数据集版本。

## 已实现：Phase 1 开发评估集与离线校验

Phase 1 要交付的“至少 30 道开发题”已落地为**开发集**（`datasetKind=dev`，不是留出集或测试集）：`tests/evaluation/dev-questions.json` 含 40 道唯一题，分类计数与上表开发列一致（单文档 24、跨文档 8、无答案/证据不足 4、当前角色无权限 4），并覆盖版本更新、逻辑删除、多轮追问与 PDF 页定位四种场景标签。

**语料与定位。** 语料为自制、无敏感内容：`tests/evaluation/corpus/` 下的 Markdown 样本与由 `tests/evaluation/tools/build_corpus.py`（只用 dev 组已有的 `pypdf`，输出字节确定）生成的文本层 `cafeteria.pdf`；语料清单 `tests/evaluation/corpus/manifest.json` 记录每个逻辑文档的版本、样本文件、来源类型、解析器版本与 active/superseded/deleted 状态，另登记各角色可访问的 KB 集合。gold 引用只绑定（KB、文档、版本）与原文定位，外加解析器版本，**不绑定 chunk UUID**：Markdown 用 `locator.headingPath`（标题路径数组）加 1-based 闭区间 `startLine`/`endLine`；PDF 用 1-based `page`，`headingPath` 必须为空且不带行号（PDF 没有可靠行号，绝不伪造）。校验器用与入库同一实现重放样本文件：Markdown 重新解析后必须存在 `headingPath` 与行区间都相等的块且 `quote` 落在该块文本内，PDF 必须能在声明页抽到含 `quote` 的文本；清单 `parserVersion` 与实现常量不一致时报解析器漂移。

**每题字段。** 每题保存角色、请求 KB 集合（`scope.kbIds`）、问题（多轮题另存 `standaloneQuestion` 与 `history`）、预期行为、gold 答案要点与 `goldSourceSpans`。另有两类可选字段：

- `distractors`：答案题里**同文档的 superseded 版本**引文，用于“版本更新”场景，表示检索/生成不得把它当当前事实；校验要求它绑定 `status=superseded`，且其 KB 在本题 `scope.kbIds` 内并可由该角色访问。
- `unavailableDocumentIds` + `unavailableReason`：无权限或删除题里对当前角色不可用的文档；两者必须同时出现或同时为空。`no_permission` 要求该 KB 对当前角色不可访问、文档有有效版本；`deleted` 要求该 KB 对当前角色可访问、文档**没有有效版本**且至少有一个 `status=deleted` 的版本（校验的是最小删除状态事实，不证明真实权限或删除链路已生效）。

所有 gold 与 distractor 的 KB 都必须在 `question.scope.kbIds` 内且该角色可访问，违反即静态校验失败；无答案和无权限题 gold 为空且预期拒答。题集自身的泄漏检查只覆盖题面、`standaloneQuestion`、历史与 gold 答案要点中**逐行完全包含**不可访问文档“实质行”（长度 ≥12 且非标题/引用行）的情况：**它是启发式检查，只拦明显复制，不证明不存在泄漏**，改写、跨行拼接、同义表述与部分片段都不会被捕获，也不能替代真实候选/日志/历史/下载链路的人工与代码级验证。

离线入口（不联网、不读环境文件、不调用任何模型；在仓库根目录单行执行）：

```text
uv run python -m rag_backend.evaluation
```

该命令校验题集 schema、数量（开发集 ≥30）、分类、来源存在与 gold 匹配，打印分类与标签计数后退出 0；失败时输出静态原因并退出 1。语料文件缺失、读取失败、Markdown 非法 UTF-8、PDF 解析具名异常（加密/结构损坏/超页）都统一收敛为静态 `DatasetValidationError` 并让 CLI 退出 1，不打印 traceback，也不用宽泛的 `except Exception` 吞掉程序自身 bug。可选的 `--results` 读取**真实运行产生**的结果文件（字段为 `questionId`、`behavior`、`citations[].{kbId,documentId,version}`、`answerText`），计算五个确定性指标；结果必须恰好覆盖题集全部 id，重复、未知或缺失都拒绝。结果文件里的引用来源是**手工按固定版本配对**的运行事实，不由校验器推断。

五个指标的分母与分子固定如下（`None` 表示该指标没有分母，例如没有应拒答题），其中 `citationSourceValidity` 按**引用条数**、`goldSourceCoverage` 按**整题回答数**，粒度不同：

| 指标 | 分子 | 分母 |
| --- | --- | --- |
| `refusalAccuracy` | 正确拒答的应拒答题数 | 全部应拒答题数 |
| `falseRefusalRate` | 被误拒的应作答题数 | 全部应作答题数 |
| `citationSourceValidity` | 已作答题返回的、命中本题 gold 的引用条数（按引用） | 已作答题返回的全部引用条数 |
| `goldSourceCoverage` | 引用覆盖了本题全部 gold (KB, 文档, 版本) 的应作答题数（按回答） | 全部应作答题数 |
| `permissionLeakCount` | 回答正文逐行包含不可访问文档实质行的应拒答题数 | 无（计数，目标 0） |

```text
uv run python -m rag_backend.evaluation --results path/to/results.json
```

聚焦单测（同样离线）：

```text
uv run pytest tests/unit/test_evaluation_dataset.py -q
```

本切片只交付开发集、离线校验与确定性指标计算，**不建评估平台、不建新数据库、不引入 LLM 裁判**；计算器不联网、不读环境文件、不调用任何模型，测试里用合成结果只验证计算分支。本轮实测：`uv run python -m rag_backend.evaluation` 退出 0、`total=40`；`uv run pytest tests/unit/test_evaluation_dataset.py -q` 为 28 passed。

以下验收仍未完成，需在真实模型与真实权限环境下另测，不能由本开发集或任何合成结果代替：留出集（上表留出列）尚不存在；句子级引用支持率（需人工审核 ≥100 事实句）、`Recall@10`/`nDCG@10`、rerank 与相似度阈值带来的拒答标定、真实 provider 失败/超时与思考模式端到端、费用与预算核算（价目 NULL）、三组消融与性能 p95 目标均未测量；无权限不泄漏本轮只在隔离合成数据上验收了单轮链路，重排、日志、历史与下载等完整链路尚未验收。开发集只用于开发期调参和结构自检，不得冒充留出集或作为最终质量结论。

## 已实现：开发集最小结果 producer（runner）

`rag_backend.evaluation.runner` 把上述开发集接到**真实 API** 上，产出恰好覆盖 40 题的 results 文件供既有 `--results` 指标消费；它不建评估平台、不建新数据库、不引入 LLM 裁判，也不改业务 API 或公开 `Citation` 字段。

- **准备状态来自清单，与 gold 分离。** runner 只读 `corpus/manifest.json` 的版本与 active/superseded/deleted 状态来准备语料：开始任何上传前先通过真实 API 确认每个语料 KB 没有未删除文档（有则静态失败，不自动清理既有资料），然后同一文档按清单版本升序上传，每一版都等待成为 `active` 再上传下一版，最后删除 `currentVersion` 为空的文档；它不读题集 gold、不按 gold 注入答案或挑选检索证据。逻辑版本号与 API 的 `document_version.version_no` 不要求相等（例如 `handbook` 的逻辑版本 2/3 对应 API 的第 1/2 版），因此映射不使用版本号猜测。边界：文档列表只含未删除文档，仅含逻辑删除文档的 KB 会被判为空，因此可复现运行应使用**全新专用隔离 KB**。
- **逐题执行真实会话。** 每题按 `scope.role` 与 `scope.kbIds` 创建会话；多轮题先按真实顺序回放历史中的**用户**轮次（助手轮由真实模型生成），再提问，因此追问改写与失败都计入预算。无权限/无答案题由真实检索给出无证据拒答；创建会话被拒（KB 不可访问）按真实拒答记录，其它 API 错误、超时与未 READY 明确失败，不吞并。准备账号在上传模式下按 `GET /me` 的角色逐一核对：每个语料 KB 至少 EDITOR、含删除文档的 KB 必须 OWNER；题集实际使用的角色（当前仅 `staff`）的可访问 KB 方向也按清单核对。
- **引用 UUID 映射回逻辑标识。** 回答返回的引用 UUID 经只读 SQL `citation -> document_version` 得到版本 UUID，再由本次运行登记的 `version UUID -> (逻辑 KB, 逻辑文档, 逻辑版本)` 映射回逻辑标识；不按标题或版本号猜测，也不扩大响应字段。
- **默认 dry-run 与保守硬上限。** 默认在联网前先做静态配置校验（环境描述必须覆盖所有语料 KB、所有题目角色与准备角色），不通过就失败，不假成功；dry-run 只打印计划与保守预留，不联网、不写库、不调用模型、不写结果文件。真实运行必须显式 `--allow-real-llm` 并给出正的 `--max-model-requests`；服务端一次提问可因证据来源变化重试一次生成，故每次提问按最多两次回答请求预留，已有历史再加一次改写请求，在调用前扣除且失败不返还。该预留是**成本上界，不是实际计费次数**；当前 40 题开发集的最坏情况预留为 86 次。结果只有恰好覆盖题集全部 id 时才写出，任何缺失或错误（含预算不足、登录失败）都带题目 id 进入诊断并退出非零，不产出可被 `--results` 接受的半成品。
- **环境契约与同栈核对。** runner 需要一份显式环境描述（逻辑 KB -> 真实 UUID、各角色合成账号、上传模式的准备账号）或一份显式资产映射（无上传，不要求准备账号凭据）。API 目标默认只接受回环地址（非回环需显式 `--allow-non-loopback-api`）；只读数据库 DSN 由环境变量提供，数据库名不以 `_test` 结尾时必须显式重申；写库前必须取得直接证据：`GET /me` 返回的 KB UUID/角色必须与描述一致，且同一批语料 KB UUID 必须存在于只读数据库中（同 host 不作为证明），否则拒绝上传。它不自动部署用户环境。本地 Compose 的 http 回环入口需显式 `SESSION_COOKIE_SECURE=0`（仓库 Compose 已如此），否则登录 Cookie 不会被浏览器/客户端带回。这是题集之外必须由用户提供的信息：开发集只定义逻辑 KB/文档/版本与角色->KB 可读关系，不含真实 UUID、账号凭据或各 KB 的写权限。

代码入口（默认 dry-run，不联网、不调用模型；`--descriptor` 指向显式环境描述）：

```text
uv run python -m rag_backend.evaluation.runner --descriptor path/to/descriptor.json
```

真实运行需另行取得用户授权后显式开启，并把只读数据库 DSN 放入环境变量 `EVAL_DATABASE_URL`：

```text
uv run python -m rag_backend.evaluation.runner --descriptor path/to/descriptor.json --api-base-url http://127.0.0.1:58080 --results-out path/to/results.json --diagnostics-out path/to/diagnostics.json --allow-real-llm --max-model-requests 120
```

本轮离线实测只覆盖离线编排与合成 HTTP：`uv run pytest tests/unit/test_evaluation_runner.py -q` 为 25 passed，`uv run ruff check backend/src/rag_backend/evaluation tests/unit/test_evaluation_runner.py` 与 `uv run mypy` 通过。**本 runner 已在 2026-09-28 于真实隔离栈（真 API + 真 PostgreSQL/Redis/Celery + 真本地 BGE + 真 DeepSeek provider）运行一次**，产出恰好 40 题结果，见下节；上述 86 与 `--max-model-requests` 是保守预留上界，不是实际计费次数。

## 已运行：Phase 1 真实 40 题开发评估（2026-09-28）

本轮在隔离栈 `myrag-p1eval`（真 API + 真 PostgreSQL 17 + pgvector `20260927_0011` + 真 Redis/Celery + 真本地 BGE + 真 DeepSeek provider）对 40 题开发集运行一次 runner，退出码 0、耗时约 81s，写出恰好 40 题结果；精简归档见 `tests/evaluation/results/2026-09-28/`（含 `results.json`、脱敏 `acl_evidence.json`、`usage.json` 与可复算说明）。

| 指标 | 实测值（分子/分母） |
| --- | --- |
| `refusalAccuracy` | 1.0（8/8） |
| `falseRefusalRate` | 0.03125（1/32） |
| `citationSourceValidity` | 1.0（38/38 最终回答引用） |
| `goldSourceCoverage` | 0.9375（30/32） |
| `permissionLeakCount` | 0 |

复算命令（离线，不联网、不读环境文件、不调用模型）：

```text
uv run python -m rag_backend.evaluation --results tests/evaluation/results/2026-09-28/results.json
```

事实与边界：`citationSourceValidity` 按引用条数统计，分子与分母都是 `results.json` 的 38 条最终回答引用；数据库 `citation` 表另有 2 条多轮历史轮次引用（共 40 行），**不进该指标分母**，且该指标**不等于句子级引用支持率**。无证据短路已实现（`not plan.evidence_ids` 时直接拒答、不调用模型），本轮 9 道拒答题的检索候选非空，因此仍调用 provider、由模型判定拒答。题集阶段实际 provider 请求 44 次（`qa_answer` 42 + `qa_rewrite` 2），权限验收再 +2，合计 46；tokens `prompt=15110`/`completion=1920`，价目与费用列全 NULL。预算公式为 `38×2 + 2×(2+3) = 86`，是成本上界而非实际计费次数。开发质量待办：`dev-single-023` 跨语言 PDF 误拒、`dev-cross-001` 跨文档覆盖缺口；未改 gold/答案。

## 消融与计分

在同一语料、权限、模型 revision、Prompt、chunk、上下文预算和硬件下比较 A 向量、B 向量+关键词+RRF、C B+reranker。记录逐题候选、回答、时延、费用与失败原因；重排收益不足或延迟过高可关闭。开发集调参，留出集只做最终比较。

- **Recall@10**：对有合法答案的题，前 10 个授权候选在同一来源上的区间并集完整覆盖的 gold spans 数 / 本题全部 spans 数，然后宏平均。多个 chunk 合力覆盖算一次。
- **nDCG@10**：相关性标 0（未覆盖）、1（部分覆盖）、2（完整覆盖至少一个 gold span）；`DCG@10 = Σ(2^rel_i - 1)/log2(i+1)`。IDCG 由本题全部授权 chunk 的理想排序计算，不只对已召回项排序。
- **引用结构有效率**：引用 ID 属于本次 allowlist，来源版本、locator 和权限仍合法的回答比例。目标 100%；失败应降级为无法可靠作答。
- **引用支持率**：人工审核事实句中，引用确实支持对应句子的比例；目标 ≥95%，至少审核 100 个事实句，报告判定规则和样本量。合法 ID 不等于语义支持。
- **拒答**：无答案或无权限题正确拒答率目标 ≥90%，同时报告可回答题误拒率目标 ≤10%。权限泄露在所有设计用例中的目标为 0，检查候选、重排、LLM、日志、历史和下载，不只检查答案正文。
- **要点覆盖与成本**：报告命中的 gold 答案要点、实际重算 chunk/token、峰值资源、各阶段 tokens 与费用；不以单一总分掩盖保守但不回答的问题。

无答案和越权题单独统计拒答与泄露，不混入有答案题的 Recall/nDCG 均值。AI 评分只作辅助，不能替代人工引用支持判断或代码级权限测试。

## 性能与故障验收

目标条件为 2 vCPU/4 GB、5,000 chunks、无重排、并发 1 时检索 p95 ≤3 秒；完整 profile 的重排额外 p95 目标 ≤3 秒；完整问答 p95 ≤20 秒、超时 30 秒。至少 100 次有效请求，区分冷/热启动与并发 1/3；失败和超时计入成功率及耗时，供应商延迟单列。这些条件未测前不能宣布达标。

真实运行验收应覆盖：每格式至少 5 份样本核对文本与来源；上传至引用点击完整链路；10 次相同文件上传不产生重复有效索引；新版构建中强杀 worker 后旧版可查；切换后新问题不含旧 chunk；撤权后新请求、历史、引用和下载不泄露；错误向量维度拒绝写入；空/扫描/加密 PDF、损坏 DOCX 与模型超时有明确状态。

在真实 PostgreSQL、Redis 和独立 Celery worker 上注入事务提交后断网、投递后未标记、worker 强制退出、broker 重启、重复投递及 dispatcher 旧租约迟到回写。验证数据库任务能补投、generation 只发布一次、解析超时真正终止计算进程、长模型调用期间轻量 API 仍响应，数据库连接没有一直被 LLM 等待占用。单测、mock、eager 模式和 Compose 静态校验均不能代替这些验收。dispatcher 相关用例在该轮实跑时为 18 个（`tests/integration/test_dispatcher_flow.py` 14 个真实数据库 + `tests/integration/test_dispatcher_broker.py` 4 个真 Redis + 独立 Celery worker），独立 tester 在全新隔离 PostgreSQL/Redis + Windows Celery solo 上实跑全仓 `-m integration` **104 passed、0 skipped**，并核对 worker 写下 HANDLER_NOT_READY 标记而 job 仍 QUEUED、重复同 taskId 无副作用、probe 默认队列可用；另由仓库外隔离手工故障探针（不属于上述 104 项 pytest）实测真 Redis 物理 stop/start 后同一 publisher 退避再 SENT、worker 被 kill 后 PG 补偿补投并被幂等收敛；job/outbox 行锁下的时序由真实数据库自动回归验证（旧 `now()` 吞 4 秒宽限，改用 `clock_timestamp()` 后正确拒绝迟到回写）。两条注入用例是**应用层故障**（真 PG SQL rollback + 真 Redis 发送，模拟应用故障），不是物理停库/磁盘故障。**仍未验收**：Linux 容器 prefork 下的业务故障恢复、自然 3600 秒 visibility 重投、多 worker 并发、`DELIVERY_UNCONFIRMED` 手工恢复 SQL、API 日志超过 12 轮。上述 104 passed（18 项）是 `de27eaf` 宽限修复前的历史实跑；`de27eaf` 后的活动租约过期恢复使 `test_dispatcher_flow.py` 增至 20 项（broker 仍 4 项），该物理探针与新自动化组合**不是同轮实跑**，长期复验需重跑。Phase 1 六项退出条件已由包括上述故障证据与 2026-09-28 真实 40 题评估在内的组合达成，但文档级 ACL、重排、完整 provider 失败路径与性能仍未验收。
