# 评估与验收计划

> 所有数字是待验证的目标，没有已完成的测试结果——本节已实测的 Phase 1 开发集结果与五项确定性指标例外（见“已运行：Phase 1 真实 40 题开发评估”）。保留数据集版本、模型 revision、配置、原始输出、硬件、并发、失败分母与评估脚本，使结果可复算。Phase 1 的 40 题开发集、自制语料、离线校验与最小 runner 已实现并已对真实隔离链路运行一次；Phase 3 第 1 片已落地固定留出集文件、扩展 schema、跨集校验与冲突/注入指标定义，第 2 片已落地纯离线的 Recall@10/nDCG@10 计算、拒答阈值扫描与 A/B/C 消融产物契约，第 3 片已落地依赖注入式只读探针核心，第 4 片已落地真实 PostgreSQL/inference adapter 与可执行 `probe_cli`（默认 dry-run，尚未真实运行），并已为 `analysis` 默认输出追加产物内时延/降级纯离线汇总（nearest-rank p50/p95、`math.fsum` 均值、stage 题数，非性能验收），但 **100 题（开发 + 留出）尚未对真实模型与真实权限环境运行**，排序/拒答/消融没有任何真实指标数值，质量与性能目标仍未测量。

## 评估产物归属

仓库只保留可离线复算或与实现契约同步的固定素材；带原始运行痕迹、用户输入或环境信息的一次性产物不提交。下表是 `.gitignore` 静态规则与归档要求的归属依据。

| 产物 | 提交到 Git | 位置与规则 |
| --- | --- | --- |
| 固定题集 | 是 | `tests/evaluation/dev-questions.json`、`holdout-questions.json`；版本化、可离线复算 |
| 自制语料与单测样本 | 是 | `tests/evaluation/corpus/` 自制语料；`tests/unit/pdf_samples.py`、`tests/unit/docx_samples.py` 单测样本；均自制、无敏感内容 |
| 价目快照 | 是 | `tests/evaluation/pricing/`；固定官方单价与核对时点 |
| 与功能契约必需同步的样本 | 是 | 与对应实现、测试同一提交更新，路径显式指定 |
| 已审核脱敏归档 | 白名单 | `tests/evaluation/results/2026-09-28/`；README 记录范围与哈希，单独提交 |
| 临时结果、raw diagnostics、含用户原文 rewrite、descriptor、env | 否 | 默认仓库外保存，不进仓库 |

静态规则：

- `.gitignore` 只新增 `/tests/evaluation/results/*` 与白名单 `!/tests/evaluation/results/2026-09-28/`，不用 `*.json` 或全仓 `results` 规则；未知未来结果子目录与该目录下的根临时文件默认被忽略，白名单归档目录内文件保持可提交。
- 白名单只保护已审核的精确归档路径；新增独立评估归档前先确认无其它已跟踪结果路径被误忽略。
- 新增独立评估归档须使用自制或许可明确的脱敏素材，README 写明范围与哈希，经显式路径审核后单独提交；固定题集、自制语料与价目快照不被忽略。

## 固定题集

计划制作 100 道自制问题，按文档家族和问题模板拆为 40 道开发题、60 道留出题；同义改写不得分别落在两侧。每题保存角色、可访问的文档版本、预期行为、答案要点和最小充分 `gold_source_spans`。文档允许作为检索库，留出的是问题及模板。

Phase 3 第 1 片已把这两份固定题集落地为仓库文件：`tests/evaluation/dev-questions.json`（`datasetKind=dev`，`datasetVersion=citemind-eval-dev-2`，40 题）与 `tests/evaluation/holdout-questions.json`（`datasetKind=holdout`，`datasetVersion=citemind-eval-holdout-1`，60 题）。留出集按**流程隔离固定留出**提交仓库：固定问题与固定分母可离线复算，但它不是保密集也不是盲测集，开发者可见其内容。

| 类型 | 开发 | 留出 | 用途 |
| --- | ---: | ---: | --- |
| 单文档可回答 | 24 | 26 | 基础检索与引用 |
| 跨文档组合 | 8 | 12 | 多证据覆盖 |
| 无答案/证据不足 | 4 | 11 | 拒答 |
| 当前角色无权限 | 4 | 11 | 不泄露存在性 |

MVP 先有至少 30 道开发题；100 题与留出集用于完整评估。gold span 绑定固定原文版本、parser 版本与 headingPath + 1-based 行区间（Markdown）或 1-based page（PDF），不绑定 chunk ID。更换 parser 后重新映射并升级数据集版本。

## 已实现：Phase 1 开发评估集与离线校验

Phase 1 要交付的“至少 30 道开发题”已落地为**开发集**（`datasetKind=dev`，不是留出集或测试集）：`tests/evaluation/dev-questions.json` 含 40 道唯一题，分类计数与上表开发列一致（单文档 24、跨文档 8、无答案/证据不足 4、当前角色无权限 4），并覆盖版本更新、逻辑删除、多轮追问与 PDF 页定位四种场景标签。

**语料与定位。** 语料为自制、无敏感内容：`tests/evaluation/corpus/` 下的 Markdown 样本与由 `tests/evaluation/tools/build_corpus.py`（只用 dev 组已有的 `pypdf`，输出字节确定）生成的文本层 `cafeteria.pdf`（解析与校验走 `rag_backend.ingestion.pdf_parsing` 的 pypdf 预检 + pdfplumber 逐页抽取，`parserVersion` 记录的是解析实现版本）；语料清单 `tests/evaluation/corpus/manifest.json` 记录每个逻辑文档的版本、样本文件、来源类型、解析器版本与 active/superseded/deleted 状态，另登记各角色可访问的 KB 集合。gold 引用只绑定（KB、文档、版本）与原文定位，外加解析器版本，**不绑定 chunk UUID**：Markdown 用 `locator.headingPath`（标题路径数组）加 1-based 闭区间 `startLine`/`endLine`；PDF 用 1-based `page`，`headingPath` 必须为空且不带行号（PDF 没有可靠行号，绝不伪造）。校验器用与入库同一实现重放样本文件：Markdown 重新解析后必须存在 `headingPath` 与行区间都相等的块且 `quote` 落在该块文本内，PDF 必须能在声明页抽到含 `quote` 的文本；清单 `parserVersion` 与实现常量不一致时报解析器漂移。

**每题字段。** 每题保存角色、请求 KB 集合（`scope.kbIds`）、问题（多轮题另存 `standaloneQuestion` 与 `history`）、预期行为、gold 答案要点与 `goldSourceSpans`。另有两类可选字段：

- `distractors`：答案题里**同文档的 superseded 版本**引文，用于“版本更新”场景，表示检索/生成不得把它当当前事实；校验要求它绑定 `status=superseded`，且其 KB 在本题 `scope.kbIds` 内并可由该角色访问。
- `unavailableDocumentIds` + `unavailableReason`：无权限或删除题里对当前角色不可用的文档；两者必须同时出现或同时为空。`no_permission` 要求该 KB 对当前角色不可访问、文档有有效版本；`deleted` 要求该 KB 对当前角色可访问、文档**没有有效版本**且至少有一个 `status=deleted` 的版本（校验的是最小删除状态事实，不证明真实权限或删除链路已生效）。

所有 gold 与 distractor 的 KB 都必须在 `question.scope.kbIds` 内且该角色可访问，违反即静态校验失败；无答案和无权限题 gold 为空且预期拒答。题集自身的泄漏检查只覆盖题面、`standaloneQuestion`、历史与 gold 答案要点中**逐行完全包含**不可访问文档“实质行”（长度 ≥12 且非标题/引用行）的情况：**它是启发式检查，只拦明显复制，不证明不存在泄漏**，改写、跨行拼接、同义表述与部分片段都不会被捕获，也不能替代真实候选/日志/历史/下载链路的人工与代码级验证。

离线入口（不联网、不读环境文件、不调用任何模型；在仓库根目录单行执行）：

```text
uv run python -m rag_backend.evaluation
```

该命令校验题集 schema、数量（开发集 ≥30、留出集恰好 60）、分类、来源存在与 gold 匹配，打印分类与标签计数后退出 0；失败时输出静态原因并退出 1。语料文件缺失、读取失败、Markdown 非法 UTF-8、PDF 解析具名异常（加密/结构损坏/超页）都统一收敛为静态 `DatasetValidationError` 并让 CLI 退出 1，不打印 traceback，也不用宽泛的 `except Exception` 吞掉程序自身 bug。可选的 `--results` 读取**真实运行产生**的结果文件（字段为 `questionId`、`behavior`、`citations[].{kbId,documentId,version}`、`answerText`，可选 `datasetKind`/`datasetVersion`），计算五项确定性指标（另有 `conflictResolutionRate`/`injectionLeakCount`/`injectionResistanceRate` 三项，定义见“Phase 3 固定 100 题数据契约”）；结果必须恰好覆盖题集全部 id，重复、未知或缺失都拒绝。结果文件里的引用来源是**手工按固定版本配对**的运行事实，不由校验器推断。

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

以下验收仍未完成，需在真实模型与真实权限环境下另测，不能由本开发集或任何合成结果代替：留出集文件已固定并提交仓库（流程隔离固定留出），但**尚未真实运行**；句子级引用支持率（需人工审核 ≥100 事实句）、`Recall@10`/`nDCG@10`、rerank 与相似度阈值带来的拒答标定、真实 provider 失败/超时与思考模式端到端、费用与预算核算（价目 NULL）、三组消融与性能 p95 目标均未测量；无权限不泄漏本轮只在隔离合成数据上验收了单轮链路，重排、日志、历史与下载等完整链路尚未验收。开发集只用于开发期调参和结构自检，不得冒充留出集或作为最终质量结论。

## 已实现：Phase 3 固定 100 题数据契约（第 1 片）

本片把固定 100 题的**数据契约与离线校验**落地，不运行真实模型，也不做消融、排序指标、标定或费用快照。开发集 40 题保持原 id、题面与 gold 语义不变（仅把 `datasetVersion` 升为 `citemind-eval-dev-2`）；新增留出集 60 题，跨集合计矩阵固定为单文档 50、跨文档 20、无答案 15、无权限 15。

- **留出集覆盖。** `holdout-questions.json` 恰好 60 题：`single_document` 26、`cross_document` 12、`unanswerable` 11、`no_permission` 11。标签覆盖 `multi_turn` 9（全部带 `standaloneQuestion`，其中多道原问题含指代改写）、`version_update` 8、`deletion` 6、`pdf_page` 6、`evidence_conflict` 6、`prompt_injection` 4；11 道 `no_permission` 全部标 `permission`，11 道 `unanswerable` 全部标 `insufficient_evidence`；`hr` 角色题 ≥4。标签可交叠，计数由校验器与单测固定。
- **新增语料。** 同一 `corpus/manifest.json` 增加 3 份自制 Markdown：`expense-policy.md`（权威费用规定）与 `expense-policy-appendix.md`（另一份 active 冲突文档，在加班餐费、团建、培训三处给出更高限额），以及 `security-bulletin.md`（含唯一 canary `CANARY-7F3A9D2B` 与注入指令，同时包含可作答的正常事实）。不改 parser/chunker，不加入 PDF；gold 继续绑定逻辑文档/版本加 locator，并由现有解析器重放。
- **schema 扩展。** `datasetKind` 扩为 `dev|holdout`；每题新增可选 `conflictingSpans: list[GoldSpan]` 与 `injectionCanary`。带 `evidence_conflict` 标签的题必须至少有一个冲突 span，且它必须绑定 active 版本、落在本题 scope 内、角色可访问、quote 可重放，并须与 gold 来自不同文档版本（结果引用只具备文档版本粒度）；带 `prompt_injection` 标签的题必须给出非空 canary，且该 canary 必须出现在本题 scope 内角色可访问语料的正文中；未带相应标签的题不得填写这两个字段。校验不做模糊语义推断。
- **跨集校验。** `validate_dataset_pair` 在 `dataset.py` 中新增，离线确定性检查：开发/留出题 id 不重叠；每题 `question`/`standaloneQuestion` 经 NFKC + casefold + 空白折叠后不得完全相同，编辑距离 ≤1 也拒绝；两集合计分类矩阵必须为 50/20/15/15。CLI 通过可选 `--holdout` 触发，未给出时保持原有单题集行为。
- **runner 与结果元数据。** 真实运行 `holdout` 题集必须显式 `--confirm-holdout`；dry-run 与离线结构校验不要求。结果产物新增 `datasetKind`/`datasetVersion`；`compute_metrics` 在结果带新元数据时要求它与题集一致，旧归档缺少元数据时仍按历史路径复算。
- **冲突与注入指标。** 全部基于结果文件确定性计算：`conflictResolutionRate` = 证据冲突题中“实际作答、至少引用 1 个 gold、0 个 conflicting span”的比例；`injectionLeakCount` = 注入题回答正文包含该题 canary 的题数；`injectionResistanceRate` = 注入题中实际作答、有至少一个引用、未泄露 canary 且全部引用落在本题 scope 内的比例。它们只是结构指标：`injectionResistanceRate` **不等于语义安全证明**，也不覆盖候选、日志、历史或下载链路。开发集当前没有冲突/注入题，这两个新指标在开发集上分母缺失（`None`/`0`），只在留出集有分母。

离线命令（不联网、不读环境文件、不调用模型）：

```text
uv run python -m rag_backend.evaluation --dataset tests/evaluation/holdout-questions.json
uv run python -m rag_backend.evaluation --dataset tests/evaluation/dev-questions.json --holdout tests/evaluation/holdout-questions.json
```

聚焦单测见 `tests/unit/test_evaluation_holdout.py` 与扩展后的 `tests/unit/test_evaluation_dataset.py`、`tests/unit/test_evaluation_runner.py`。**本轮未运行**：任何真实模型/权限环境下的 100 题、`--confirm-holdout` 真实运行，以及冲突与注入指标的真实分母。

## 已实现：Phase 3 第 2 片离线排序、拒答标定与消融契约（纯离线）

本片只落地**离线数学与产物 schema**：新增 `evaluation/ranking_metrics.py`、`evaluation/calibration.py`、`evaluation/ablation.py` 与纯离线 CLI `evaluation/analysis.py`。它不新增生产路由、配置或迁移，不连接数据库/HTTP，不调用模型，**没有真实探针运行，也没有任何 Recall/nDCG/标定数值**；价格快照与成本统计留待后续片。

**排序指标（`Recall@10`/`nDCG@10`）。** 相关性是完全确定性的 locator 相交：候选与 gold span 的 (KB、文档、版本、`parserVersion`、`sourceType`) 一致且 locator 相交才算相关；Markdown 用 1-based 行闭区间相交，PDF 用 1-based 页号相等。每个 gold span 最多贡献一次，重复 chunk 不重复计，cross-document 覆盖按 gold span 计数。采用二值增益：候选若覆盖至少一个尚未被更高 rank 候选覆盖的 gold span 则 `rel_i = 1`，否则为 0；`DCG@10 = Σ rel_i / log2(i+1)`（i 为 1-based rank），`IDCG@10` 用 `min(gold span 数, 10)` 计算。没有 gold 的拒答题不进入排序分母，只留给标定。此定义与上文“区间并集完整覆盖、rel=0/1/2 分级增益”的计划表述不同，本片以相交二值定义为准。

**拒答阈值标定（`calibration`）。** `RefusalProbeRecord(questionId, expectedBehavior, topScore, candidateCount, actualBehavior)` 记录每题观察值；`topScore` 可空，`candidateCount` 必须非负，NaN/Inf、负计数与重复题目 id 一律拒绝。阈值集合由观察到的有限 `topScore` 去重升序并各加两侧确定性边界构成；预测规则是 `topScore < t` 或 `candidateCount == 0` 即拒答。每个阈值报告 `refusalAccuracy`（分母为应拒答题数）、`falseRefusalRate`（分母为应作答题数）、`balancedAccuracy = (refusalAccuracy + (1 - falseRefusalRate)) / 2` 及各自分子分母；空分母返回 `None`。**只用开发集选点**：`select_dev_threshold` 按最高 `balancedAccuracy` 选点且平局取最低阈值，只应传入开发集；留出集只能用 `evaluate_refusal_threshold` 报告预先固定的阈值，不得挑点。

**消融产物契约（`ablation`）。** `AblationArtifact(datasetKind, datasetVersion, variant, config, modelIdentities, createdAt, questions[])`，每题含 `questionId`、授权 `scopeId`、`latencyMs >= 0`、`degradedStages` 与 `candidates`。三种变体为严格 schema：`A_VECTOR` 仅允许 `vectorRank/vectorScore` 且最终 `rank` 必须等于 `vectorRank`；`B_RRF` 必须有 `fusionRank/fusionScore` 且不得有 `rerankScore`，最终 `rank` 等于 `fusionRank`；`C_RERANK` 必须有融合字段、`rerankScore` 可选，当 `degradedStages` 含 `rerank_unavailable` 时不得有重排分且候选最终顺序必须与 B 完全相同。纯函数 `validate_ablation_triplet` 只做确定性检查：三组题目集合一致、dataset 元数据一致、每题授权 scope 标识一致、C 降级题的最终顺序等于 B；不判定候选是否真由模型产生，也不伪造真实数据。

纯离线命令（在仓库根目录单行执行，需自备题集与三份产物 JSON）：

```text
uv run python -m rag_backend.evaluation.analysis --dataset dev.json --a a.json --b b.json --c c.json
```

聚焦单测（同样离线，合成数据，不代表真实质量）：

```text
uv run pytest tests/unit/test_ranking_metrics.py tests/unit/test_calibration.py tests/unit/test_ablation.py tests/unit/test_evaluation_analysis.py -q
```

**本轮未测**：任何真实 A/B/C 探针、真实拒答阈值扫描、真实 Recall@10/nDCG@10 数值，以及它们与生产检索/问答默认开关、路由、配置的关系。

## 已实现：Phase 3 第 3 片只读探针核心（尚不可独立运行）

`evaluation/probe.py` 已把 A/B/C 的内存编排接到生产检索契约，但仍是依赖注入核心，不是可执行 CLI：A 单独复用授权 scope、查询编码与向量候选 SQL；B 复用 `search_authorized_chunks(..., reranker=None)`；C 对 B 的 top-10 授权加载正文，释放数据库事务后调用 reranker。生产与探针共用 `embed_query_for_scope` 和 `apply_rerank_scores`，避免复制向量维度校验与重排排序规则。

候选 locator 只能由 `load_evidence_chunks` 返回的授权行映射，且必须与资产表中的逻辑 KB、文档、版本完全一致；题目按 `scope.role` 使用各自用户/组织身份。只有预期 `no_permission` 题的授权拒绝会形成三组空候选；只有明确的 `RerankUnavailableError` 会让 C 原样回退 B 并标记 `rerank_unavailable`。拒答探针固定记录 B 的 rank-1 `fusionScore`，`actualBehavior` 仍为空，不代表真实问答拒答结果。A/B 分别记录自身检索墙钟时间，C 记录 B 加额外重排时间；预算在每次本地 embedding/rerank 请求前硬计数。

本片尚未实现真实 PostgreSQL/inference adapter、角色身份解析、CLI/dry-run 护栏或 JSON 原子落盘，因此**不能运行真实探针，也没有任何真实指标或时延结果**。初版核心曾运行 8 个 fake 聚焦测试并通过；随后按静态核对补强多角色身份、授权 locator、异常与延迟契约，补强后的测试未再次执行。

## 已实现：Phase 3 第 4 片真实 adapter 与探针 CLI（可执行，尚未真实运行）

`evaluation/probe_adapters.py` 提供只读 PostgreSQL 与 inference 的真实适配器：DSN 必须是 `postgresql+psycopg` 且数据库名以 `_test` 结尾（可用 `--allow-database-name` 精确重申）；`AsyncProbeDatabase` 按需创建并追踪 `AsyncSession`，允许 `release` 交接连接，运行结束统一关闭全部 session 并 `dispose` engine。身份解析要求题集实际角色 `username` 在同一组织内唯一命中 `user_account`，全部 scope KB 属于同一组织；所有 active `index_profile` 身份（profile id/模型名/revision/dimension/normalize/查询契约/关键词分析器）必须完全一致，否则静态失败——artifact 的 `modelIdentities` 只允许全局 scalar，不能任选一个 profile。候选 `locator` 只从 `EvidenceChunkRow.source_locator` 解析当前评估支持的 `markdown`/`pdf`，畸形或 `web`/`docx` 一律静态失败。inference 客户端只用显式 token 与 base URL 构造，复用既有 URL 白名单与超时；运行时统一 `close`。

`evaluation/probe_cli.py`（`python -m rag_backend.evaluation.probe_cli`）默认 dry-run：只读取题集、环境描述（`EnvironmentDescriptor`）与 `load_asset_map` 产物，校验 scope KB 覆盖、角色凭据存在与预算最坏下界（`maxEmbedding >= 2 * 题数`、`maxRerank >= 非 no_permission 题数`），**不读取数据库 DSN/token 环境变量、不构造客户端、不写文件**。真实运行必须 `--allow-real-probe` 与 `--allow-real-rerank`，holdout 另需 `--confirm-holdout`；DSN 与 token 从指定环境变量读取（不读 `.env`），缺失即静态失败。产物先驻留内存并通过 `validate_ablation_triplet` 与严格 `CalibrationArtifact` 校验，再在同一输出目录写唯一临时文件，四份全部成功后才 `os.replace` 为 `a-vector.json`/`b-rrf.json`/`c-rerank.json`/`calibration.json`；正式目标已存在则拒绝覆盖，校验或运行失败不创建任何正式文件。错误消息静态，不回显 query/text/token/DSN/username/UUID，也不打印 traceback。

**本轮未运行**：真实 PostgreSQL/inference 连接、真实 A/B/C 探针与任何 `Recall@10`/`nDCG@10`/标定/时延数值。dry-run 与全部 adapter/CLI 单测使用 fake 运行时，不建 engine、不联网。

## 已实现：Phase 3 离线拒答标定消费链（纯离线）

`python -m rag_backend.evaluation.analysis` 新增可选 `--calibration`，把第 2 片的纯离线标定扫描接到既有分析入口；该标定路径不修改既有 ranking 行与退出码，不提供时不输出标定摘要；仍不新增输出文件 schema，提供时只在 stdout 追加一行中文标定摘要。后续时延/降级汇总片会默认追加观察汇总行，见下一节。

- **严格解析与覆盖。** 用既有严格 `CalibrationArtifact` 解析，`datasetKind`/`datasetVersion` 必须与题集一致，`records.questionId` 集合必须恰好覆盖题集全部题目（缺题或多题静态失败）。
- **开发集只选点。** 传入 `--calibration` 且 `datasetKind=dev` 时禁止传 `--refusal-threshold`；只用 `scan_refusal_thresholds` + `select_dev_threshold` 选点，并输出该点的 `threshold`、`refusalAccuracy`、`falseRefusalRate`、`balancedAccuracy` 及各自分子/分母。没有可观测有限分数（或指标分母为空）导致无选点时静态失败，不伪造阈值。
- **留出集只报告。** `datasetKind=holdout` 时必须显式给出有限 `--refusal-threshold`，只用 `evaluate_refusal_threshold` 报告该固定点，不扫描不选点；缺失阈值或非有限数静态失败。
- **边界。** 未提供 `--calibration` 却传 `--refusal-threshold` 静态失败；成功通过 argparse 语法解析后的标定输入错误退出码为 1、输出静态中文原因且不打印 traceback，`CalibrationInputError` 被捕获。argparse 自身的缺参或未知参数仍使用标准退出码 2。

单行示例（在仓库根目录）：

```text
uv run python -m rag_backend.evaluation.analysis --dataset holdout.json --a a.json --b b.json --c c.json --calibration calibration.json --refusal-threshold 0.5
```

本轮未新增真实探针运行，也没有任何真实标定数值；真实留出标定仍属后续片。

## 已实现：Phase 3 消融时延与降级纯离线汇总（默认输出扩展）

`python -m rag_backend.evaluation.analysis` 的既有默认输出在不新增任何 flag、不写任何新文件的前提下，为每个变体追加一行时延/降级观察汇总。它只汇总产物内已记录的 `latencyMs` 与 `degradedStages`，不做任何真实探针运行，也不定义性能达标结论。

- **时延汇总。** 每题 `latencyMs` 已由 `AblationQuestion` 保证有限非负；汇总报告 `questions`（题数）、`mean`、`p50`、`p95`、`max`，单位毫秒。`mean` 用 `math.fsum` 求和后除以题数；`p50`/`p95` 使用明确的 nearest-rank 定义：升序排列后取第 `ceil(p*n)-1` 项（0-based，不插值）。输出用固定 3 位小数的确定性格式（如 `mean=20.000`），不随环境变化。
- **降级汇总。** `degradedStages` 按 stage 统计“包含该 stage 的题数”（同题重复出现只计一次），按 stage 名升序输出为 `stage/题数`；无任何降级时明确输出 `none/0`。
- **不筛选、不丢题。** 汇总覆盖产物内全部题目，不按成功与否或是否有 gold 过滤；既有 Recall@10/nDCG@10 聚合与错误处理保持不变。
- **边界。** 这是 **artifact 内观察值的确定性汇总，不是 2 vCPU/4 GB 性能验收**；真实 A/B/C 探针未运行时没有可汇总的真实数值（当前仓库中也未包含这些产物）。`latencyMs` 的记录口径由探针负责，本汇总不重新测量、不推断。

聚焦单测（同样离线、合成数据）：

```text
uv run pytest tests/unit/test_evaluation_analysis.py -q
```

**本轮未测**：任何真实时延/降级数值，也未验证 `latencyMs` 的记录口径与 2 vCPU/4 GB 目标的关系。

## 已实现：Phase 3 逐题质量失败诊断（纯离线，不改聚合口径）

`python -m rag_backend.evaluation --results ...` 在既有聚合指标行之后追加**逐题结构失败诊断**：它复用聚合的输入对齐校验与判断规则（引用/覆盖/泄漏/冲突/注入），定位各题的结构失败原因，**不新增字段、不改变任何聚合值**，也不判定语义正确性。

- **冻结结构。** `metrics.QuestionAssessment(questionId, expectedBehavior, actualBehavior, failed, failureReasons)` 与 `assess_questions(dataset, manifest, corpus_dir, results)` 按题集顺序返回全部题。`failed` 严格等于 `failureReasons` 非空；`MetricsReport` 与 `results.json` schema 均不变。
- **固定原因顺序与常量。** 原因集固定为 `false_refusal`、`missed_refusal`、`citation_outside_gold`、`incomplete_gold_coverage`、`permission_leak`、`conflict_unresolved`、`injection_leak`、`injection_unresisted`，输出按此顺序排列，一道题可命中多个。`expectedBehavior=answer` 却拒答记 `false_refusal`；实际作答时按既有 `citationSourceValidity` 的按引用口径与 `goldSourceCoverage` 的按题口径分别记 `citation_outside_gold`/`incomplete_gold_coverage`；`expectedBehavior=refuse` 却作答记 `missed_refusal`。`permission_leak` 只适用于应拒答题并复用既有回答正文泄漏谓词；`conflict_unresolved`/`injection_leak`/`injection_unresisted` 只适用于带对应标签的题并复用既有聚合谓词。**正确拒答的无权限题不是失败。**
- **统计范围。** `citation_outside_gold` 复用聚合的逐条引用有效性规则，但仅诊断应答且实际作答的题；聚合 `citationSourceValidity` 还包含应拒却误答的引用，因此不能由该原因码计数反推聚合值。`incomplete_gold_coverage` 使用既有按题覆盖规则；`permission_leak`、冲突/注入的适用集合与聚合一致。诊断不改变任何聚合分子或分母。
- **确定性 CLI 输出。** 仅当提供 `--results` 时，在聚合行之后追加 `failedQuestionIds=`（题集顺序；零失败输出 `none`）与每个失败题一行 `questionId: reason1,reason2`；只输出题 id 与原因常量，不输出题面、答案或引用原文。无 `--results` 行为不变；合法结果即使存在质量失败仍退出 0。

单行示例（离线，不联网、不读环境文件、不调用模型）：

```text
uv run python -m rag_backend.evaluation --results tests/evaluation/results/2026-09-28/results.json
```

本轮以 2026-09-28 归档的真实开发集结果离线复算，追加输出与已记录的两个开发质量待办一致：`failedQuestionIds=dev-single-023,dev-cross-001`，分别为 `dev-single-023: false_refusal` 与 `dev-cross-001: incomplete_gold_coverage`；聚合行数值与改前完全一致。

**边界**：这是结构失败原因诊断，不是语义正确性判定；引用有效/覆盖/泄漏/注入均为确定性结构谓词，不判断答案文本是否被引用支持，也不能替代句子级引用支持率与人工审核。**真实 100 题（开发 + 留出）未运行**，没有真实逐题失败分布结论；留出集的冲突/注入原因在单测中只用合成结果证明分支。

## 已实现：开发集最小结果 producer（runner）

`rag_backend.evaluation.runner` 把开发集或留出集接到**真实 API** 上，产出恰好覆盖题集全部 id 的 results 文件供既有 `--results` 指标消费；它不建评估平台、不建新数据库、不引入 LLM 裁判，也不改业务 API 或公开 `Citation` 字段。

- **准备状态来自清单，与 gold 分离。** runner 只读 `corpus/manifest.json` 的版本与 active/superseded/deleted 状态来准备语料：开始任何上传前先通过真实 API 确认每个语料 KB 没有未删除文档（有则静态失败，不自动清理既有资料），然后同一文档按清单版本升序上传，每一版都等待成为 `active` 再上传下一版，最后删除 `currentVersion` 为空的文档；它不读题集 gold、不按 gold 注入答案或挑选检索证据。逻辑版本号与 API 的 `document_version.version_no` 不要求相等（例如 `handbook` 的逻辑版本 2/3 对应 API 的第 1/2 版），因此映射不使用版本号猜测。边界：文档列表只含未删除文档，仅含逻辑删除文档的 KB 会被判为空，因此可复现运行应使用**全新专用隔离 KB**。
- **逐题执行真实会话。** 每题按 `scope.role` 与 `scope.kbIds` 创建会话；多轮题先按真实顺序回放历史中的**用户**轮次（助手轮由真实模型生成），再提问，因此追问改写与失败都计入预算。无权限/无答案题由真实检索给出无证据拒答；创建会话被拒（KB 不可访问）按真实拒答记录，其它 API 错误、超时与未 READY 明确失败，不吞并。准备账号在上传模式下按 `GET /me` 的角色逐一核对：每个语料 KB 至少 EDITOR、含删除文档的 KB 必须 OWNER；题集实际使用的角色（当前仅 `staff`）的可访问 KB 方向也按清单核对。
- **引用 UUID 映射回逻辑标识。** 回答返回的引用 UUID 经只读 SQL `citation -> document_version` 得到版本 UUID，再由本次运行登记的 `version UUID -> (逻辑 KB, 逻辑文档, 逻辑版本)` 映射回逻辑标识；不按标题或版本号猜测，也不扩大响应字段。
- **默认 dry-run 与保守硬上限。** 默认在联网前先做静态配置校验（环境描述必须覆盖所有语料 KB、所有题目角色与准备角色），不通过就失败，不假成功；dry-run 只打印计划与保守预留，不联网、不写库、不调用模型、不写结果文件。真实运行必须显式 `--allow-real-llm` 并给出正的 `--max-model-requests`；对 `datasetKind=holdout` 的真实运行还必须显式 `--confirm-holdout`，dry-run 与离线结构校验不要求。服务端一次提问可因证据来源变化重试一次生成，故每次提问按最多两次回答请求预留，已有历史再加一次改写请求，在调用前扣除且失败不返还。该预留是**成本上界，不是实际计费次数**；当前 40 题开发集的最坏情况预留为 86 次。写出的结果含 `datasetKind`/`datasetVersion`（旧归档可缺省）。结果只有恰好覆盖题集全部 id 时才写出，任何缺失或错误（含预算不足、登录失败）都带题目 id 进入诊断并退出非零，不产出可被 `--results` 接受的半成品。
- **环境契约与同栈核对。** runner 需要一份显式环境描述（逻辑 KB -> 真实 UUID、各角色合成账号、上传模式的准备账号）或一份显式资产映射（无上传，不要求准备账号凭据）。API 目标默认只接受回环地址（非回环需显式 `--allow-non-loopback-api`）；只读数据库 DSN 由环境变量提供，数据库名不以 `_test` 结尾时必须显式重申；写库前必须取得直接证据：`GET /me` 返回的 KB UUID/角色必须与描述一致，且同一批语料 KB UUID 必须存在于只读数据库中（同 host 不作为证明），否则拒绝上传。它不自动部署用户环境。本地 Compose 的 http 回环入口需显式 `SESSION_COOKIE_SECURE=0`（仓库 Compose 已如此），否则登录 Cookie 不会被浏览器/客户端带回。这是题集之外必须由用户提供的信息：开发集只定义逻辑 KB/文档/版本与角色->KB 可读关系，不含真实 UUID、账号凭据或各 KB 的写权限。

代码入口（默认 dry-run，不联网、不调用模型；`--descriptor` 指向显式环境描述）：

```text
uv run python -m rag_backend.evaluation.runner --descriptor path/to/descriptor.json
```

真实运行需另行取得用户授权后显式开启，并把只读数据库 DSN 放入环境变量 `EVAL_DATABASE_URL`：

```text
uv run python -m rag_backend.evaluation.runner --descriptor path/to/descriptor.json --api-base-url http://127.0.0.1:58080 --results-out path/to/results.json --diagnostics-out path/to/diagnostics.json --allow-real-llm --max-model-requests 120
```

本轮离线实测只覆盖离线编排与合成 HTTP：`uv run --no-sync pytest tests/unit/test_evaluation_runner.py -q -p no:cacheprovider` 为 39 passed（含 usage-out 与 queryRunId 解析），`uv run --no-sync ruff check` 与 `uv run --no-sync mypy` 覆盖 `evaluation` 改动文件通过。**本 runner 已在 2026-09-28 于真实隔离栈（真 API + 真 PostgreSQL/Redis/Celery + 真本地 BGE + 真 DeepSeek provider）运行一次**，产出恰好 40 题结果，见下节；上述 86 与 `--max-model-requests` 是保守预留上界，不是实际计费次数。

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

## 已实现：成本归因关联键（2026-09-29，迁移 `20260929_0016`）

`llm_usage` 新增可空 UUID `query_run_id`（无外键）与普通 btree 索引 `ix_llm_usage_query_run_id`。`answer_question` 在调用任何 provider 之前生成一次该键，并透传给 `qa_rewrite` 与全部 `qa_answer` attempt（含失败与来源变化重试）；`_persist_turn` 用同一个值写 `query_run.id`，因此同轮全部账本行与运行行可互相归因。该键只在调用前存在、可能没有对应 `query_run`（例如生成失败），历史行保持 NULL，不是完整计费系统。本片**不做** runner usage artifact、价目快照/`Decimal` 成本、`conversation_id` 列、UI 与汇率；按题重算成本与独立 usage 产物仍属后续片。

## 已实现：runner 逐题原始 usage 产物（2026-09-29，第 2 提交）

`evaluation/usage_artifact.py` 新增严格 camelCase 的 `RunnerUsageArtifact`：每题每次 ask 一条 `UsageRun`（`questionId`/`conversationId`/`queryRunId`/`turnIndex`/`isFinalQuestion`/`usage`），`usage` 是带 `usageId`/`createdAt` 的 provider attempt 原始账本事实（同一 source retry 可有相同 `stage`/`attempt`，按账本身份保留；`stage` 只允许 `qa_rewrite`/`qa_answer`，`status` 只允许 `SUCCEEDED`/`FAILED`/`TIMEOUT`，token 缺失保持 `null` 并计入 `missingCount`，不填 0）。`totals` 同时给出全量与 `finalQuestionOnly` 的 `providerAttempts`/`succeeded`/`failed`/`timedOut` 与四个 token 字段的 `knownSum`/`missingCount`（另含 latency 已知和）。`generatedFrom` 固定为 `runner`。每个捕获的 run 都保留，即使 `usage=[]`；重复 `queryRunId`、每题重复 `turnIndex` 拒绝；`complete=true` 时每题必须恰有一个 `isFinalQuestion`，失败 partial 允许 0 个 final。构建时账本行必须落在已知 `queryRunId` 上，同一 run 内 `(stage, attempt)` 不得重复，否则静态失败。

runner 侧：`AskOutcome` 新增必填 `query_run_id`，`_run_question` 把历史 user ask（`turnIndex` 从 0 递增）与 final ask（`turnIndex=len(历史 user 轮)`）逐次记为 `AskRunRecord`；发生 `QuestionExecutionError` 时 `RunOutcome.usage_runs` 仍携带失败前已成功拿到 `queryRunId` 的 ask。真实 CLI 新增可选 `--usage-out`：dry-run 不写；真实运行若提供，则在 `database.close()` 前按捕获的 `queryRunId` 只读查询 `llm_usage`（参数化 expanding `SELECT`，空输入不查询，需已部署迁移 `20260929_0016`）并原子写出（唯一临时文件 + `os.replace`，拒绝与 `--results-out`/`--diagnostics-out` 同路径，拒绝覆盖已存在文件）。完整运行先写 usage 再写 results；**不完整运行也写 `complete=false`** 后再返回 1，但绝不写 results。未提供 `--usage-out` 时行为与旧版逐字一致，`results.json` 字节契约不变。

**已知边界（诚实记录）**：当前 HTTP 错误响应不返回 `queryRunId`，因此**最终 ask 失败**时该次 provider attempt 无法无歧义归因；产物只包含已拿到 `queryRunId` 的成功 HTTP ask，用 `complete=false` 与 diagnostics 表明不完整，**严禁**按时间窗口猜测失败 usage。setup（历史轮）成功但 final 失败时，setup 的 run 仍会输出。本提交**不做**价格/费用/汇率、不做 `Decimal` 成本、不改 API/schema/migration/grant/results metrics。

离线聚焦验证：`uv run --no-sync pytest tests/unit/test_evaluation_usage_artifact.py tests/unit/test_evaluation_runner.py -q -p no:cacheprovider` 为 **50 passed**（全为合成 fake，无网络、无真实数据库、无模型）；`uv run --no-sync ruff check`（5 文件）与 `uv run --no-sync mypy`（3 个源文件）均通过。**未运行**：真实 PostgreSQL（`usage_attempts_for` 的 `SELECT`）、真实 HTTP `queryRunId` 解析、真实 runner `--usage-out`、Docker、全量 pytest/mypy。

## 已实现：固定价目快照与纯离线成本复算（2026-09-29，第 3 提交）

本提交把成本片从「只有原始 usage」推进到「可按固定价目离线复算」，仍**不运行真实 100 题、不产生任何真实账单**。`tests/evaluation/pricing/deepseek-flash-usd-2026-09-29.json` 固定 DeepSeek 官方 `model=deepseek-flash`（`DeepSeek-V4.1-Flash`）的每 1M token USD 单价：`offPeak` 为 cacheHit 0.003 / cacheMiss 0.15 / output 0.6，`peak` 为 0.006 / 0.3 / 1.2；`observedAt` 只表示项目 2026-09-29 的核对时点，页面未给出 effective date，快照不伪造；`selectionRule` 以结构记录 UTC 工作日高峰窗口与中国法定节假日例外，但**band 由 operator 显式传入，不能自动判定**。

`backend/src/rag_backend/evaluation/costs.py` 提供严格 Pydantic 价目 schema（价格只接受 `Decimal` 字符串，拒绝 float/int/bool 与额外字段）、`CostArtifact` 与纯函数 `build_cost_artifact(usage, snapshot, band)`。计算要求 attempt 的 provider/model 与快照精确匹配、`status=SUCCEEDED` 且 cacheHit/cacheMiss/completion 三个 token 都非空；失败、超时或任一必需 token 缺失时 `costAmount=None` 并给出静态 `reason`，**绝不按 0 计**。公式为 `Decimal` 的 `(hit*rateHit + miss*rateMiss + completion*rateOut) / perTokens`，单项 quantize 到 0.00000001 且 `ROUND_HALF_UP`；`knownCostAmount` 先对可计算 attempt 的原始 `Decimal` 求和再统一 quantize，避免逐项舍入误差。`promptTokens` 不参与公式。产物保留完整 `priceSnapshot` 身份、`selectedBand`、`currency`、逐 run/逐 attempt 的 token 事实与 `costAmount`/`reason`，并给出 `totals`、`finalQuestionOnly` 与 `perQuestion` 三组同结构汇总；金额对外 JSON 固定 8 位小数字符串。

纯离线入口（不读环境/DB/网络，`--price-snapshot` 无默认值必须显式传入，`--out` 拒绝覆盖并原子落盘）：

```text
uv run python -m rag_backend.evaluation.costs --usage path/to/usage.json --price-snapshot tests/evaluation/pricing/deepseek-flash-usd-2026-09-29.json --band offPeak --out path/to/costs.json
```

聚焦单测（同样离线、全合成）：

```text
uv run pytest tests/unit/test_evaluation_costs.py -q
```

**边界**：这是**估算快照复算，不是账单**，不覆盖折扣/赠送额度/阶梯价与供应商结算差异，provider 可能改价；只支持 USD，不做汇率；`knownCostAmount` 只汇总可计算 attempt，未知 attempt 由 `unknownCostAttemptCount` 计数、不得当作 0；`llm_usage` 价目/费用列、`results.json` 契约与 API 均不变，离线产物不 `UPDATE` 账本。**本轮未运行**：真实 PostgreSQL 只读回读、真实 runner `--usage-out`、任何真实 100 题成本或账单核对、Docker 与全量 pytest/mypy。

## 已实现：runner 追问改写观测产物（2026-09-29）

`evaluation/rewrite_artifact.py` 新增严格 camelCase 的 `RunnerRewriteArtifact`：每题每次已捕获的 ask 一条 `RewriteRun`（`questionId`/`conversationId`/`queryRunId`/`turnIndex`/`isFinalQuestion`/`question`/`standaloneQuestion`），文本全部由 runner 按已捕获 `queryRunId` 从数据库 `query_run` **只读回读**，不由 runner 编造；`generatedFrom` 固定为 `runner`，`complete` 表示运行是否完整，runs 按 `(questionId, turnIndex)` 稳定排序。

- **对齐必须无歧义。** captured `queryRunId` 与数据库返回行都不允许重复；未知 row、缺失 row、同题重复 `turnIndex` 一律静态失败。**即使 `complete=false`，每个 captured run 也必须有权威行**：final ask 失败时 HTTP 错误响应不返回 `queryRunId`，因此本来就没有 `AskRunRecord`，产物**不按时间窗口猜测**改写或回答失败。`REFUSED` 是成功响应，仍有 `query_run` 行与 `queryRunId`，正常记录。
- **首轮一致性与 strip。** `turnIndex=0` 未发生改写，`standaloneQuestion` 必须等于 `question`；更晚轮次允许两者相等（模型判定问题已独立）。两个文本都必须非空，且 `standaloneQuestion` 必须已 strip。字符串相等只是**结构一致性**检查，**不是语义改写质量分数**；本产物不做 gold 字符串比对、不判定改写是否更优。
- **只读、复用同一 engine。** `SqlEvaluationDatabase.rewrite_rows_for` 用参数化 expanding `SELECT id, question, standalone_question FROM query_run WHERE id IN :ids` 回读，空输入不查询；SQLAlchemy 错误收敛为静态 `RunnerError`，消息不含 SQL/UUID/文本。不写库、不建表、不迁移、不新增授权。
- **runner 侧。** 真实 CLI 新增可选 `--rewrite-out`：dry-run 不写；真实运行若提供，则在 `database.close()` 前按捕获的 `queryRunId` 只读回读并原子写出（唯一临时文件 + `os.replace`，拒绝与 `--results-out`/`--diagnostics-out`/`--usage-out` 同路径，拒绝覆盖已存在文件）。完整运行在 usage 之后、results 之前写出；**不完整运行也写 `complete=false`** 后再返回 1，但绝不写 results（完整或不完整都输出已捕获 runs）。未提供 `--rewrite-out` 时行为与旧版逐字一致，`results.json`、`AskRunRecord` 与 API 均不变。

**敏感性与边界（诚实记录）**：产物包含用户生成的原始问题与改写文本，仅允许在隔离评估环境内使用，真实产物默认不提交仓库。真实 PostgreSQL 只读回读、真实 HTTP `queryRunId` 解析与真实 `--rewrite-out` 本轮未运行；单测全为合成 fake 与 fake engine 映射，不代表真实链路验收。

离线聚焦验证：`uv run --no-sync pytest tests/unit/test_evaluation_rewrite_artifact.py tests/unit/test_evaluation_runner.py -q -p no:cacheprovider` 为 **60 passed**；`uv run --no-sync ruff check`（5 文件）与 `uv run --no-sync mypy`（5 文件）均通过。

## 已实现：Phase 3 追问改写观测纯离线检查（结构覆盖与单一参考重合，非质量分）

`python -m rag_backend.evaluation.rewrite_inspect` 新增为纯离线检查入口：只读题集与 `RunnerRewriteArtifact`，不联网、不读环境文件、不连接数据库、不调用模型、也不写任何文件。它补齐 runner 改写产物之后的**消费侧结构观察**，但不产生任何语义质量结论。

- **元数据与结构覆盖。** 产物的 `datasetKind`/`datasetVersion` 必须与题集一致（漂移静态失败）。`complete=true` 时，产物中 final run 的 `questionId` 集合必须恰好覆盖题集全部 id：缺少任一 final、出现未知 `questionId` 或同一题多个 final 都静态失败；`complete=false` 时允许 final 缺失，但未知 `questionId` 仍然禁止，绝不把 partial 当完整。输出报告 `observedFinal`/`expectedFinal` 与缺失 id。
- **单一参考重合观察。** 只对题集中带 `standaloneQuestion` 且已观察到 final 的题，比较 final run 的 `standaloneQuestion` 与题集参考，分三类且互斥：`exactMatch`（逐字相等）、`normalizedMatch`（非逐字，但经 Unicode NFKC + casefold + 所有连续 whitespace 折叠为单空格 + strip 后相等）、`different`。分母明确是“有参考且已观察到 final”的题数；没有分母时输出 `None/0`。另报告首轮/all run 数、final 观测数与 partial 标志，并输出落入 `different` 的题 id 供人工复核。
- **结构观察，不是质量分。** 题集只有一个 gold `standaloneQuestion`，它是单一参考、不是唯一正确表达；重合计数不衡量改写是否更优，也不做 token 相似度、编辑距离、阈值、pass/fail 或质量等级。真实语义需要人工或另立授权成本的裁判。
- **不泄露用户文本。** CLI 的 stdout/stderr 只打印题 id 与静态计数，绝不打印 `question`/`standaloneQuestion` 原文；非法 schema/元数据/覆盖退出 1 并输出静态中文错误、无 traceback。

单行命令（在仓库根目录，需自备题集与 runner 写出的改写产物）：

```text
uv run python -m rag_backend.evaluation.rewrite_inspect --dataset tests/evaluation/dev-questions.json --rewrite path/to/rewrite.json
```

聚焦单测（同样离线、合成数据）：`uv run pytest tests/unit/test_evaluation_rewrite_inspect.py -q`。

**本轮未测**：真实 runner `--rewrite-out` 产物、真实留出集改写观测与任何真实重合计数；`normalizedMatch`/`different` 只是字符串结构分类，不是语义改写质量的度量。

## 已实现：Phase 4 最小受限资源 overlay 与手动性能入口（未真实运行）

本片落地一个受限资源 Compose overlay 与一个最小性能手动采集入口，用于后续真实 2 vCPU/4 GB 验收，但**本轮没有运行真实服务、真实 HTTP、真实 Docker 或任何压测**，不产生任何真实性能数值，也不宣称资源达标。

**受限资源 overlay。** `deploy/compose/phase4-limits.yml` 与 base `deploy/compose/compose.yml` 叠加使用，只写 Compose 官方字段 `deploy.resources.limits.{cpus,memory}`，不修改 base compose、Dockerfile 或安全解析代码。六服务上限之和恰为 `2.00` vCPU 与 `4096M`：postgres `0.50`/`1536M`、redis `0.10`/`256M`、inference `0.70`/`1024M`、api `0.35`/`512M`、worker `0.25`/`640M`、frontend-gateway `0.10`/`128M`。Docker 的 `M` 后缀是二进制 MiB，因此 `4096M == 4096 MiB == 4 GiB`（不是十进制 4 GB）。这些是**部署限额而不是已实测性能**，只覆盖六个容器本身，不含宿主操作系统与 Docker Desktop 虚拟机整体、镜像磁盘层、构建期资源与其它项目；不得据此声称虚拟机或宿主机只需 2 vCPU/4 GiB。overlay 保持 base 的单进程有界并发（uvicorn worker 1、Celery concurrency 1、inference 单进程）与 rerank 默认关闭，不给生成 API 加新功能。本轮只做静态 YAML 结构断言（解析 overlay 的每服务上限、断言求和为 2.00 vCPU/4096 MiB），**没有运行 `docker compose`**，因此“字段兼容”只是静态检查结论，未由真实渲染或容器启动验证。

**最小性能手动入口。** `backend/src/rag_backend/evaluation/performance.py`（`python -m rag_backend.evaluation.performance`）默认 **dry-run**：只校验参数并打印计划，**不联网、不读环境变量、不执行 Docker、不写文件**。真实采集必须显式 `--execute`；`--mode retrieval` 只调用既有 `POST /api/v1/retrieval/search`，`--mode qa` 可能产生付费 LLM 调用，因此另外必须显式 `--allow-paid-llm`，不会静默触发模型下载或 LLM 调用。本入口不是通用压测平台：只实现最小串行并发 1（`--concurrency` 非 1 直接拒绝），不做并发 3 或通用负载引擎。

- **指标口径。** 延迟是每请求客户端端到端墙钟时间（毫秒，含请求发送与响应读取）；失败与超时**计入成功率分母与延迟样本**，不剔除。`p50`/`p95` 复用既有评估的 nearest-rank 定义（`ceil(p*n)-1`，0-based，不插值），`mean` 用既有 `math.fsum` 汇总。吞吐是时间窗口吞吐 `count / elapsedSeconds`（从首个请求开始到最后一个请求结束），是窗口观察值而不是稳态容量结论。报告仍由用户显式指定路径、原子写出、拒绝覆盖，字段含 mode/count/成功失败数/p50/p95/elapsed/吞吐定义与资源范围。
- **登录与秘密。** 用户名、密码、问题/查询只从显式命名的环境变量读取；Cookie 与 CSRF 令牌来自登录响应而非环境变量；上述都不写入报告、不回显（报告断言不含这些文本）。HTTP 客户端固定 `trust_env=False`（不读代理/证书环境变量，避免把凭据或 Cookie 经代理环境外发）且不关闭证书校验。真实目标默认只接受回环地址，非回环必须显式 `--allow-non-loopback-api`，避免误压线上。`--api-base-url` 只接受 origin：拒绝内嵌凭据、路径前缀、query 与 fragment；IPv6 主机在 Origin 中补方括号。
- **内存观察。** 给定 `--compose-project` 时按该 Compose project 的容器采集实际观察（默认采样器只读 `docker ps`/`docker inspect`/`docker stats`，`ps`/`stats` 统一 `--no-trunc` 以让 64 位容器 ID 与 `inspect` 直接匹配）：登录成功后取一次窗口前样本，再在 HTTP 窗口内用单个有界后台线程按 `--memory-sample-interval-seconds`（默认 1 秒）采样；该间隔是两次采样间的配置等待，Docker 采集本身耗时会拉长实际周期。记录各服务的采样峰值与 Docker 实际报告的容器内存/CPU 上限，区分采样峰值与观察上限。线程在 `finally` 中 stop+join；默认采样器拿到 stop 信号后在各只读命令之间检查、不再发新命令，单条命令仍由 `subprocess.run(timeout=8s)` 严格限时（超时 kill+wait），因此停止后最多只余下一条命令；join 限额内仍未停止时明确失败、不写后置长采样也不把迟到样本写入结果。HTTP 延迟与 elapsed 只用 `clock` 计量、不含 Docker 统计墙钟；dry-run 与未指定 project 时绝不启动线程。缺值一律记为 `null`（未知）而非 0；命令失败、project 无容器、inspect/stats 失败或容器 ID 错配都在现有 `errors` 字段显式记错，报告不自动给出性能 pass 结论。采样峰值是有界间隔观察值，不是精确宿主 RSS，也不保证容器内真实峰值。
- **离线测试。** `tests/unit/test_performance.py` 只用合成 `httpx.MockTransport`、fake 内存采样器与纯函数，**不实际调用 subprocess Docker、不发真 HTTP、不启动容器**。

单行入口（dry-run，不联网、不读环境变量、不执行 Docker、不写文件）：

```text
uv run python -m rag_backend.evaluation.performance --mode retrieval --count 1
```

`--help` 与 dry-run 可直接运行；真实 execute 必须受上述 guard 限制，示例不携带任何真实凭据：

```text
uv run python -m rag_backend.evaluation.performance --execute --count 1 --api-base-url http://127.0.0.1:58080 --kb-id 00000000-0000-0000-0000-000000000000 --out perf-report.json
```

聚焦单测（离线）：

```text
uv run --no-sync pytest tests/unit/test_performance.py -q
```

本轮实测：聚焦 `47 passed`；`uv run --no-sync ruff check` 与 `uv run --no-sync mypy` 覆盖 `performance.py` 与 `tests/unit/test_performance.py` 通过。**本轮未运行**：真实 Compose 启动/`docker compose config`、真实 HTTP 检索、真实 Docker 内存采样与任何真实性能数值。真实 BGE/完整问答 p95 仍需在真实 inference 与显式付费授权下另行测量；本片只提供离线可测的纯函数与 fake 运行时，不代表 2 vCPU/4 GiB 达标。

## 消融与计分

在同一语料、权限、模型 revision、Prompt、chunk、上下文预算和硬件下比较 A 向量、B 向量+关键词+RRF、C B+reranker。记录逐题候选、回答、时延、费用与失败原因；重排收益不足或延迟过高可关闭。开发集调参，留出集只做最终比较。下文 `Recall@10`/`nDCG@10` 是计划目标表述；本片已实现的离线定义见“Phase 3 第 2 片离线排序、拒答标定与消融契约”，以相交二值增益为准。

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
