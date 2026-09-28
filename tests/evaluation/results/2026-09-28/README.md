# Phase 1 真实 40 题评估与权限验收归档（2026-09-28）

本目录是该轮评估的**精简可复算归档**，用于离线复算与出处核对，**不是生产就绪声明**。完整叙述、失败与缺口见原始证据 `FINDINGS.md` / `ACCOUNTING.md`（哈希见文末，临时路径不随仓库提交）。

## 出处与边界

- HEAD `480f3d8`，工作树干净；隔离栈 `myrag-p1eval` 已清理，本机 dev 六服务与开发库全程未动。
- 真实组件：真实 HTTP API、真实 PostgreSQL 17 + pgvector（隔离库 `20260927_0011`）、真实 Redis broker + 真实 Celery worker、真实本地 BGE 权重推理、真实 DeepSeek provider；无假编码器、无假 provider。
- 本轮为避开宿主网络占用，api 直接发布回环端口、未经网关；网关同源入口由 2026-09-28 隔离 Demo 单独实测，本轮未重复。
- 未收录：descriptor、env、凭据、冗余 log、截图、完整 diagnostics。归档仅含自制无敏感样本，不含 DSN、口令或令牌。

## 归档文件

| 文件 | sha256 | 说明 |
| --- | --- | --- |
| `results.json` | `bbf4ee8aa8d05e3e13e8e3bd3ecf84e070600bbcb96f6c00c7b30afc76b73d35` | runner 真实产出，恰好覆盖 40 题；最终回答引用 38 条 |
| `acl_evidence.json` | `a2579842f262d53fdc7810e6fe5a15899cf0257d041db0590c9101a887481f5a` | 同栈权限验收 19/19 脱敏证据（合成账号/UUID/自制标记） |
| `usage.json` | `1d0bcb5a0d4017bb0df7dc930d72d0e85323e87263ef9aa0058c401ac7649349` | 预算与 provider 用量快照 |

## 离线复算

```text
uv run python -m rag_backend.evaluation --results tests/evaluation/results/2026-09-28/results.json
```

该命令不联网、不读环境文件、不调用模型；预期输出 `total=40` 与下表五项确定性指标。

## 确定性指标（离线计算，无 LLM 裁判）

| 指标 | 实测值 | 分子/分母 |
| --- | --- | --- |
| `refusalAccuracy` | 1.0 | 8 / 8 应拒答题正确拒答 |
| `falseRefusalRate` | 0.03125 | 1 / 32 应作答题被误拒 |
| `citationSourceValidity` | 1.0 | 38 / 38 最终回答引用命中本题 gold |
| `goldSourceCoverage` | 0.9375 | 30 / 32 应作答题引用覆盖全部 gold |
| `permissionLeakCount` | 0 | 计数，目标 0 |

分母事实：`answered=31`、`refused=9`、`expectedAnswer=32`、`expectedRefuse=8`，漏题 0、错误 0。

`citationSourceValidity` 按**引用条数**统计，本轮分子与分母都是 `results.json` 的 38 条最终回答引用；数据库 `citation` 表另有 2 条多轮历史轮次引用（共 40 行），**不进该指标分母**。它是「引用命中固定版本 gold」的比例，**不是句子级引用支持率**（后者需人工审核，尚未测量）。

## 确证事实与原始报告修正

1. `citationSourceValidity` 是 **38/38**（不是原始 `FINDINGS.md` 写的 40/40）。
2. 无证据短路**已实现**（`conversation/service.py` 在 `not plan.evidence_ids` 时直接拒答、不调用模型）；本轮 9 道拒答题的检索候选**非空**，因此仍发生 provider 调用、由模型判定拒答。原始 `FINDINGS.md` 称「没有零候选即拒答的短路条件」有误。
3. 题集阶段实际请求 **44** 次（`qa_answer` 42 + `qa_rewrite` 2），权限验收再 +2，合计 **46**；全部 `SUCCEEDED`、`attempt=1`、`error_code` 为 NULL。
4. tokens：`prompt=15110`、`completion=1920`、cache hit/miss `640/14470`；`price_snapshot`/`price_source`/`price_currency`/`cost_amount` **四列全 NULL**，费用未知。
5. 预算公式为 **38×2 + 2×(2+3) = 86**（38 题无历史按 2；2 道多轮题各有 1 轮用户历史，按首问 2 + 后续（2 回答 + 1 改写））。原始 `ACCOUNTING.md` 写的 `38×2+2×3` 有误。86 是**成本上界/预留**，不是实际计费次数。
6. 推理镜像：复用既有的 query 编码冻结构建（本地 tag `citemind-inference:query-frozen-20260925T233937`，镜像 ID `sha256:7eb81f27817e0f9d06eab1767bb818e0b2f106175d8bb8f9cd9d2b454c5c9962`），`inference/src` 9/9 文件与 HEAD 一致；固定模型 revision `7999e1d3359715c523056ef9478215996d62a620`。旧 `citemind-inference:latest`（`sha256:02e801e8…`）只接受 `kind='document'`、对 `kind='query'` 返回 422，**不能当作 query 兼容镜像**；首次 run 因此在任何 provider 调用前失败（`llm_usage` 0 行）。

## 六退出条件证据矩阵（docs/roadmap.md）

| 退出条件 | 本轮证据 | 范围与限制 |
| --- | --- | --- |
| 两格式真实入库 | 本轮真实解析→真实本地模型：9 个版本（Markdown 7 + PDF 1 + 逻辑删除 1）全部 `READY`，`chunk=33`、`chunk_embedding=33`、`index_generation READY=9` | 自制语料；PDF 定位误拒见开发待办 |
| 越权内容不进候选 | 本轮权限验收 19/19：staff 候选 restricted=0、撤权后 `/me`/检索/引用/历史全部失效、`permissionLeakCount=0` | 隔离合成数据；不代表文档级 ACL（未实现） |
| 来源能定位 | 本轮引用详情返回 Markdown `headingPath`+行区间；PDF 页定位由 2026-09-28 `f6a5cf0` 隔离 Demo 真浏览器验证（`D:/tmp/myrag-demo-final/EVIDENCE.md`，第 1 页） | PDF 页定位证据来自 Demo 轮次，非本轮 |
| 旧版至新版发布前仍服务 | `tests/integration/test_document_update_delete_flow.py::test_update_publish_switches_pointer_and_keeps_old_before_after`：v2 未发布时真实 SQL 断言 `still_v1` 只召回 v1，模块 11 passed | 自动化集成用例；本轮评估未重测 |
| Redis 断连后任务可补投 | 历史物理探针：2026-09-24 隔离真 Redis stop/start 后同一 publisher 退避再 SENT、worker kill 后 PG 补偿补投（`%LOCALAPPDATA%/Temp/myrag-dispatcher-physical-20260924/evidence.jsonl`，见 docs/development.md）；其后宽限修复 `de27eaf` 由当前 20 个 dispatcher + 4 个 broker 用例覆盖 | 物理探针与新自动化组合**不是同轮实跑**，临时路径不永续背书 |
| 至少 30 道开发题 | 本轮 40 题首次对真实模型运行，产出 40 题结果与五项确定性指标 | 开发集，非留出集 |

## 开发质量待办 / 未测边界

开发质量待办（不改 gold/答案，未修）：

- `dev-single-023`（`pdf_page`）：中文提问未召回/采纳 PDF 内英文行 `Each voucher is worth 30 CNY`，产生 1 次误拒。
- `dev-cross-001`（`cross_document`）：最终引用未覆盖 `handbook` 逻辑版本 3，产生覆盖缺口。

未测边界（**不作为 Phase 1 阻塞，也不得声称已验证**）：rerank 与相似度阈值（未实现）、思考模式/思考强度失败组合、真实 provider 超时/5xx/截断、`Recall@10`/`nDCG@10`（runner 只产引用级结果）、句子级引用支持率（需人工审核 ≥100 事实句）、留出集、费用与预算核算（价目 NULL）、p95 性能与 2vCPU/4GB 资源、Linux prefork 业务故障恢复、自然 3600 秒 visibility 重投、完整文档级 ACL。

## 来源证据哈希（临时目录，仅出处核对）

| 来源 | sha256 |
| --- | --- |
| `FINDINGS.md` | `f4edb897861c37679e68221da0114e663d64d1dee59d45bcc4e8f4dac9b71a59` |
| `ACCOUNTING.md` | `dc5e02885c5304b4ba201824f315928724a20c0a7a347e042788cc3d07f937f1` |
| `results.json` | `bbf4ee8aa8d05e3e13e8e3bd3ecf84e070600bbcb96f6c00c7b30afc76b73d35` |
| `acl_evidence.json` | `a2579842f262d53fdc7810e6fe5a15899cf0257d041db0590c9101a887481f5a` |
| `diagnostics.json` | `ae03cfdbe71d9c8ea62903e743d5a64e72d308c0bf544668987438d379356baf` |
| `tail_facts.json` | `6fccc026670033cc65e526388aff085cc62320d322a222fd157ca4568f346725` |
| `metrics.log` | `7731e069b74c110139154f9152f62abd0678443bccc540f62cdf19eddf7113a7` |
