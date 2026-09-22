# API 契约草案

> 以下为计划接口，尚未实现或生成 OpenAPI。外部统一前缀 `/api/v1`。MVP 范围见 [交付计划](roadmap.md)；标为“后续”的接口不属于 MVP。

Session Cookie 使用 HttpOnly、SameSite，并在生产环境启用 HTTPS；状态变更接受 CSRF 防护。请求/响应由 Pydantic 定义，外部字段使用 camelCase。标准错误体含 `code`、`message`、`requestId`、`details`，无权资源使用不暴露存在性的统一策略。异步入库、重建和评估受理后返回 `202`，不代表任务完成。

| 方法与路径（省略 `/api/v1`） | 用途与关键约束 |
| --- | --- |
| `POST /auth/login`、`POST /auth/logout`、`GET /me` | 登录、注销、当前授权概览；不回显密码 |
| `GET /knowledge-bases`、`POST /knowledge-bases` | 列表和创建；创建限管理员 |
| `PATCH /knowledge-bases/{id}` | 更新元数据/策略，OWNER 与乐观锁 version |
| `GET /knowledge-bases/{id}/members`、`PUT /knowledge-bases/{id}/members` | 查看/替换成员，变更递增 `acl_revision` |
| `POST /knowledge-bases/{id}/documents` | multipart 上传、title、Idempotency-Key；检查配额，返回 documentId/versionId/jobId |
| `POST /knowledge-bases/{id}/web-imports`（后续） | 白名单内静态 URL；先通过 SSRF 和大小限制 |
| `GET /knowledge-bases/{id}/documents` | 按状态/格式/版本分页；无权文档不出现 |
| `GET /documents/{id}` | 文档详情、当前有效版本、任务和解析警告 |
| `POST /documents/{id}/versions` | 上传新版本，带 expectedVersion；过期版本返回 409 |
| `GET /documents/{id}/versions` | 读取授权历史版本；历史版不参与当前检索 |
| `GET /documents/{id}/content` | 当前或授权历史内容/解析预览；每次由 API 鉴权代理读取 |
| `DELETE /documents/{id}` | 同步 tombstone、异步清理，返回 cleanupJobId |
| `PUT /documents/{id}/acl`（后续） | OWNER 设置收紧文档 ACL，递增相关 revision |
| `GET /ingest-jobs/{id}`、`POST /ingest-jobs/{id}/retry` | 管理范围内查询任务或重试；重试不能覆盖更新版本 |
| `POST /documents/{id}/reindex`（后续） | 用已登记 profile 重建当前版本，旧索引保持服务 |
| `POST /conversations` | 限于已授权 kbIds 创建所有者会话 |
| `GET /conversations/{id}/messages` | 当前合法历史，撤权来源衍生内容需隐藏或替换 |
| `POST /conversations/{id}/messages` | 输入 question/requestId；完整校验后返回 answer、citations、queryRunId、insufficientEvidence、degradedStages |
| `GET /citations/{id}` | 每次复核权限，返回原版本页/段/行与短引文 |
| `POST /retrieval/debug` | 管理员在自己有权范围查看候选、排名和阶段状态 |
| `POST /messages/{id}/feedback` | 用户对本人可见回答反馈 |
| `POST /evaluation-runs`、`GET /evaluation-runs/{id}` | 后续受控评估与脱敏结果；不用管理员 bypass 验证权限 |
| `GET /usage`、`GET /audit-events` | 本人或可管理范围的用量与审计记录 |

当前已实现的 `GET /api/v1/health` 只是 HTTP liveness：返回 `status`、`service`、`environment`，不访问数据库也不代表依赖可用。postgres 自身的 healthcheck 与人工/集成 DSN 验收不能替代未来数据路由的 DB readiness；后续数据路由落地时应新增独立 readiness 探针，不修改现有 health 契约，也不引入 `SELECT 1` 之类探活查询。

内部 inference 接口：`POST /internal/embed` 返回 vectors、dimension、modelRevision、tokenCounts；`POST /internal/rerank`（后续）返回 candidateId 与 score；`GET /health` 检查进程存活，`GET /capabilities` 报告各能力是否就绪。内部接口有凭证、输入条数、字节、token、超时和并发限制，不开放公网，不接受任意模型路径，也不处理用户文档授权。当前实现会在启动时从本地目录加载构建期烘入的固定 revision 模型（缺失或不符即启动失败）：`/health` 返回 `modelLoaded=true`，`/ready` 与 `/capabilities` 报告 `embedding.ready=true`、dimension=512 与冻结 modelRevision，`/internal/embed` 缺少或错误 Bearer token 返回 401、`kind=document` 返回 512 维 L2 归一化向量与 tokenCounts、`kind=query` 因 Literal 校验返回 422；rerank 路由未实现，`/capabilities` 如实报告 rerank.ready=false。

问答 citation 至少含 `citationId`、`displayLabel`、`documentTitle`、`version`、`locator`、`quote`，全部由服务端映射。本次模型只可返回临时引用 ID。`get_current_user` 从数据库会话生成 AuthContext，`require_kb_role` 验证库角色，repository 继续施加文档 ACL；路由层登录校验不能代替资源级授权。
