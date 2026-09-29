# 可降级 reranker：bge-reranker-base 与清单式离线身份

日期：2026-09-29
状态：已实现（未做真实模型验收）

## 决策

Phase 2 的神经重排采用固定 revision 的 `BAAI/bge-reranker-base`（`XLMRobertaForSequenceClassification`，MIT），通过现有 `transformers`/`torch` 直接用 `AutoTokenizer` + `AutoModelForSequenceClassification` 实现，**不引入** `sentence-transformers`。模型与 revision 冻结在 inference 配置中：`FROZEN_RERANK_MODEL` / `FROZEN_RERANK_REVISION=2cfc18c9415c912f9d8155881c133215df768a70`。

reranker 默认关闭（inference `RERANK_ENABLED=0`、backend `rerank_enabled=False`）：
- 关闭时 inference 不校验模型目录、不加载权重，`/capabilities` 如实报 `rerank.ready=false`；`POST /internal/rerank` 仍受 Bearer 与请求体上限保护，但静态返回 503 `RERANK_NOT_READY`，绝不返回任何分数。
- 关闭时检索完全不构造/调用重排客户端，也不标记降级。
- 显式开启时 inference 启动阶段要求模型目录与产物清单完整，否则 fail fast；backend 开启时要求已配置 `INFERENCE_TOKEN`。

## 为什么身份校验不针对 reranker 写死 SHA-256

embedding 的六个产物在本仓库内有真实下载核验过的 SHA-256，因此可以钉死在源码里做「自报身份」防线。reranker 权重在本轮**没有真实下载**（用户明确禁止联网/下载/模型加载/镜像构建），若在源码里写一组未经核验的摘要，等于伪造身份证据。因此采用「构建期生成、运行期离线核对」的清单契约：

- 构建期 `inference/scripts/prepare_model.py --model rerank` 从固定 HF revision 下载产物，逐个与 Hub 元数据的 `size`、`blobId`（非 LFS）或 `lfs.sha256`（LFS）交叉核验，再把实际字节大小与 SHA-256 写进 `rerank-model-manifest.json`（与模型目录同级）。
- 运行期 `rerank_identity.verify_rerank_artifacts` 只读该清单：要求声明的 model/revision 等于冻结值，要求目录内文件集合与清单逐一对应，且每个文件大小与 SHA-256 与清单一致。

这能拒绝身份漂移、文件集合不符与字节替换；它**不能**独立证明清单摘要来自官方 Hub。首次真实构建后必须人工确认清单来源，并把结论补进本 Note。

## 契约与顺序

- 请求：`POST /internal/rerank`，Bearer 鉴权，body 上限在 JSON 解析前生效；`{query, candidates:[{candidateId,text}]}`，`extra=forbid`，候选最多 10 条且 `candidateId` 唯一，query/单条文本/合计字节都有上限。
- 推理：pair tokenization，`max_length=512` 截断，`logits.squeeze(-1)` 原始分数，不归一化、不排序。CPU 并发 1 + 短排队。
- 响应：`{scores:[{candidateId,score}], modelRevision}`。
- API 侧 `RerankClient` 复用查询编码客户端的安全边界（`trust_env=False`、`retries=0`、不重定向、`Accept-Encoding: identity`、有界读取、静态脱敏错误）。任何超时/连接/非 2xx/非法响应/集合不完全/重复 id/非 finite/revision 不匹配/本地超限都收敛为统一的 `RerankUnavailableError`。
- 检索在 RRF 融合与数据库 release 之后，仅对 top-10 已授权候选加载正文并调用重排；成功后按 score 降序、同分 `chunkId` 升序重排这 10 个，其余 30 保持原融合顺序；**不改**每项的 `fusion_rank`/`fusion_score`。失败整体保持原 RRF 顺序并返回 `degraded_stages=('rerank_unavailable',)`。
- `RetrievalResult` 新增 `degraded_stages`；conversation 与 `unsupported_text`/`source_retry` 去重合并后进入既有 `query_run.degraded_stages` 与 `AnswerResponse.degradedStages`。不新增迁移、不改历史消息、不改 `/retrieval/search` wire 字段、不改引用/授权/RRF 常量。

## 未验收边界

- 未真实下载或运行 `bge-reranker-base`，未做真实模型质量、排序收益或延迟验收；所有 reranker 测试都注入 stub，**不代表真实重排**。
- 未做真实 inference HTTP 端到端（backend → inference）与镜像构建。
- Compose 只新增可选默认关闭变量；`local-full` 的开启步骤仅在文档说明，未重构 Compose。
