# 评估与验收计划

> 所有数字是待验证的目标，没有已完成的测试结果。保留数据集版本、模型 revision、配置、原始输出、硬件、并发、失败分母与评估脚本，使结果可复算。Phase 1 的开发集、自制语料与离线校验已实现（见“已实现：Phase 1 开发评估集与离线校验”）；质量与性能数字仍是待验证目标，尚未测量。

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

以下验收仍未完成，需在真实模型与真实权限环境下另测，不能由本开发集或任何合成结果代替：**40 道开发题没有对任何真实检索/生成模型跑过**，因此没有真实检索/生成质量分数，也没有费用或延迟数字；留出集（上表留出列）尚不存在；真实检索/生成质量（Recall@10、nDCG@10、引用支持率、真实 provider 拒答与费用/延迟）尚未测量；无权限不泄漏尚未在真实候选、重排、日志、历史与下载链路上验收；三组消融与性能 p95 目标未测。开发集只用于开发期调参和结构自检，不得冒充留出集或作为最终质量结论。

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

在真实 PostgreSQL、Redis 和独立 Celery worker 上注入事务提交后断网、投递后未标记、worker 强制退出、broker 重启、重复投递及 dispatcher 旧租约迟到回写。验证数据库任务能补投、generation 只发布一次、解析超时真正终止计算进程、长模型调用期间轻量 API 仍响应，数据库连接没有一直被 LLM 等待占用。单测、mock、eager 模式和 Compose 静态校验均不能代替这些验收。dispatcher 相关用例共 18 个（`tests/integration/test_dispatcher_flow.py` 14 个真实数据库 + `tests/integration/test_dispatcher_broker.py` 4 个真 Redis + 独立 Celery worker），独立 tester 在全新隔离 PostgreSQL/Redis + Windows Celery solo 上实跑全仓 `-m integration` **104 passed、0 skipped**，并核对 worker 写下 HANDLER_NOT_READY 标记而 job 仍 QUEUED、重复同 taskId 无副作用、probe 默认队列可用；另由仓库外隔离手工故障探针（不属于上述 104 项 pytest）实测真 Redis 物理 stop/start 后同一 publisher 退避再 SENT、worker 被 kill 后 PG 补偿补投并被幂等收敛；job/outbox 行锁下的时序由真实数据库自动回归验证（旧 `now()` 吞 4 秒宽限，改用 `clock_timestamp()` 后正确拒绝迟到回写）。两条注入用例是**应用层故障**（真 PG SQL rollback + 真 Redis 发送，模拟应用故障），不是物理停库/磁盘故障。**仍未验收**：Linux 容器 prefork 下的业务故障恢复、自然 3600 秒 visibility 重投、多 worker 并发、`DELIVERY_UNCONFIRMED` 手工恢复 SQL、API 日志超过 12 轮；Phase 1 未退出、文档不可检索。
