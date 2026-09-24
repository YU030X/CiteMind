# 安全与数据边界

> 现状与计划并列：身份/会话、KB 成员授权与云 LLM 用量账本已实现并有实测；本页其余标记为计划的约束尚未实现。Phase 1 身份首片与 KB 成员授权切片的单元与真实数据库/Redis 集成测试已建立（`tests/unit/test_auth_*`、`tests/unit/test_kb_*`、`tests/unit/test_knowledge_*`、`tests/integration/test_auth_flow.py`、`tests/integration/test_kb_flow.py`），检索与文档 ACL 等其他安全测试尚不存在；云 LLM 用量账本 `llm_usage` 与一次性真实探针 `rag_backend.llm_probe` 已建立，且探针已执行一次真实成功调用并回读账本（见 [开发约定](development.md)），但供应商调用的失败/超时/越权场景与费用核算仍未验收。默认处理自制、无敏感样本文档；“私有知识库”表示应用访问控制，不意味着数据不离开主机。

## 身份和授权

已完成 Phase 1 身份首片：账号由运维 CLI `uv run python -m rag_backend.auth.cli` 显式创建（默认隐藏输入密码；自动化用 `--password-env`/`--password-file`/`--password-stdin`，明文与哈希都不打印），密码用 Argon2 哈希。CLI 拒绝 `--organization-id` 与 `ORGANIZATION_ID` 不一致的开户请求（返回非零且不建号），登录只按 `ORGANIZATION_ID` 查账号；建号前以只读 `SELECT current_user` 预检连接角色，非 `citemind_api` 时显式失败且不写入。单组织配置改变后，属于旧组织的既有会话在下次请求即返回 401（`build_auth_context` 校验 `user_account.organization_id` 与配置一致）。登录 `POST /api/v1/auth/login` 先校验 `Origin` 白名单，再在 Redis 中按客户端 IP 与用户名原子限流，通过后签发 256-bit 随机会话令牌。Cookie 为 HttpOnly + SameSite=Lax，生产默认 `Secure`，只在非生产的回环来源可用 `SESSION_COOKIE_SECURE=0` 显式关闭。数据库 `auth_session` 只保存 token hash、CSRF token hash、到期与撤销状态；CSRF 令牌由服务端 `CSRF_SECRET` 与会话令牌 HMAC 派生，因此 `GET /me` 可在进程重启后恢复同一 CSRF 令牌，而不保存原令牌。每次 `GET /me` 都从数据库重建授权上下文；注销、到期、禁用与撤销立即返回 401，权限结果不跨请求缓存。限流 Redis 不可用时登录返回 503（fail closed），未知用户、禁用与密码错误统一返回 401 并执行等价的 Argon2 计算。状态变更请求（注销、创建 KB、替换成员）要求 `X-CSRF-Token`；迁移 `20260923_0005` 的 api 角色只有 `user_account`/`auth_session`/`kb_member` 的 SELECT+INSERT+UPDATE，撤销走软删除，运行角色没有 DELETE 权限。`auth_session` 目前也没有清理作业，已撤销与已过期会话行会一直保留；清理期限与负责作业尚未实现，这是剩余保留风险和未来维护责任，本切片不声明任何已实施的清理策略。登录限流的客户端 IP 只信任显式配置的可信代理网段（`TRUSTED_PROXY_CIDRS`）：只有当直连对端落在该网段内时，才接受网关用 `$remote_addr` 覆盖的单值 `X-Real-IP`；否则退回对端地址，且从不读取可被追加伪造的 `X-Forwarded-For`；生产环境必须显式配置该边界，并拒绝 `0.0.0.0/0` 与 `::/0` 这类全网段（信任全网段等于取消对端校验），更窄的宽网段仍由运维按网络拓扑判断；生产环境 `ALLOWED_ORIGINS` 必须全部使用 https。注销只在服务端会话确实被撤销后才下发清除 Cookie，无有效会话的请求不下发 Set-Cookie。

KB 成员授权的服务端判定已实现：`require_kb_role` 每次请求都从数据库 `kb_member` join `knowledge_base` 重新判定当前用户在指定 KB 的最小角色（READER<EDITOR<OWNER），不跨请求缓存；KB 不存在、属于其他组织、成员已撤销或角色不足统一返回不暴露存在性的 404 `KNOWLEDGE_BASE_NOT_FOUND`。`GET /knowledge-bases` 只列会话组织内有效成员 KB；`POST /knowledge-bases` 只允许 `user_account.is_admin`，强制 Origin 与 CSRF，并在同一事务写入创建者 OWNER；`PUT /knowledge-bases/{id}/members` 只允许 OWNER，强制 Origin 与 CSRF，做全量替换：缺席者软撤销、重加入者清空 `revoked_at`，空、重复、缺少 OWNER 或引用外组织用户都拒绝且无部分事务，事务内锁定知识库行序列化并发并在锁内复核调用者仍是 OWNER，只有实际变化才递增 `acl_revision`。请求体中 `organizationId` 不生效（即使提交也不被采用），角色与用户组织都由服务端核对。替换结果必须至少保留一名启用中的 OWNER：新加入或提升一名已禁用用户为 OWNER 会被拒绝（422 `KB_MEMBER_OWNER_DISABLED`），只保留已禁用 OWNER 而不搭配任何启用 OWNER 则返回 409 `LAST_OWNER_REQUIRED`。该约束是替换事务时点的应用校验，数据库没有对应的永久约束，直接写库禁用最后一个 OWNER 或把唯一 OWNER 指向已禁用用户会绕过它。禁用 `user_account` 不会软撤销其 `kb_member` 行：成员记录保留，成员列表可能包含已禁用用户，账号重新启用后原角色恢复。正确的顺序是在禁用账号前先完成所有权移交；如果唯一 OWNER 已被直接写库禁用，现有 API 无法把所有权移交给一个无法登录的已禁用账号，本切片也没有管理员接管或自动恢复能力（当前没有 CLI 恢复/禁用入口），只能由运维在数据库层把该账号重新置为 `enabled` 后，再由本人登录完成移交。知识库角色为 OWNER、EDITOR、READER：READER 可问答和查看可见原文，EDITOR 可导入/更新，OWNER 可管理成员与删除。MVP 以 KB 成员授权；完整范围的文档 ACL 只能进一步收紧。读规则为当前组织、KB 成员、满足文档允许列表（若存在）、未删除、当前有效索引；组织和成员身份都来自后端会话。客户端 `kbIds` 只能取交集。

向量与关键词候选 SQL 复用同一授权 JOIN（尚未实现）：权限过滤不能拖到生成回答之后；reranker、LLM、日志和缓存也不得先收到越权内容。会话、检索调试、反馈、旧版本、引用和文件下载各自重新鉴权。撤权和删除先提交授权事实与 `acl_revision`/`kb_revision`，让后续访问立即失效，再做异步物理清理。权限查询 MVP 不跨请求缓存最终答案或授权结果。

## 输入与文件

Markdown 上传（`POST /api/v1/knowledge-bases/{id}/documents`）已实现输入校验与文件隔离：服务端在读取 multipart 正文之前先完成会话鉴权（对目标 KB 至少 `EDITOR`）、`Origin` 白名单与 `X-CSRF-Token` 校验，以及接收阶段的请求体字节上限；未授权请求不会把正文落盘。只按客户端字节判定，不信任声明的 MIME：后缀必须是 `.md`/`.markdown`，内容必须是有效 UTF-8 且不含二进制控制字符，单文件最多 20,000,000 字节，空内容与伪装成文本的二进制都拒绝。原文件存入 api 进程私有的 `api-documents` 命名卷，相对路径只由服务端 KB id 与内容 SHA-256 派生，不拼接用户文件名；写入先落同目录临时文件、`fsync` 后原子替换，失败只清理本次临时文件。事务冲突时已发布的最终 blob 不删除（并发事务可能已引用），因此数据库异常等路径可能留下孤儿文件，本切片没有 GC 作业。

计划仍未实现：PDF 解析与 ZIP/Office 解压限制、真实 MIME 与页数校验、KB 配额、文件读取代理与预览 HTML 消毒、异步物理清理。PDF 解析与 ZIP/Office 文件需限制 CPU、内存、解压总量、条目数、压缩比及实际子进程运行时长；空/加密/损坏文档给出明确状态，不能静默生成空索引。Markdown 或网页预览输出 HTML 前消毒。

静态网页导入只允许 http/https 和配置的演示域名；每次 DNS 解析及重定向都检查目标地址，限制出站网络、响应大小、超时和重定向次数。不能仅检查字符串前缀，也不允许登录、执行 JavaScript 或递归整站抓取。

## 模型与输出

外部文档只作为引用材料，内容中的命令、账号请求和工具调用指示不获得权限。MVP 不开放任意工具执行。证据和模型输出分别受 token/字节限制，回答按严格 schema 校验；未知引用 ID、非法类型和模型自造 URL/页码拒绝。没有证据或权限时尽量不调用生成模型。

云 LLM 会收到问题与选中的授权片段。真实敏感材料只有在允许外发的策略下使用云服务，否则换内网模型。调用重排/LLM 前复核 ACL revision，回答交付前再次复核；撤权后已发出的字节无法追回，服务端只能停止后续调用/交付并记录事件。结构化日志默认不记原文正文；审计和检索调试结果按权限过滤。

服务端记录实际 input/output tokens、模型、阶段和单价快照，分别核算在线、评估与重试。预算到 80% 提醒，到 100% 停止新生成，并为在途请求预留额度。限流和预算都在后端执行；HTTPX、Celery 与模型格式重试分别设有限次数、超时和总请求预算，防止重试相乘放大费用。

已实现的 `llm_usage` 是 append-only 事实表：一次 provider attempt 一行，失败、超时与凭据错误也追加，不更新、不删除；prompt/completion tokens 由 provider 报告，缺失时只能写 `FAILED`/`TIMEOUT` 且 token 与费用为 NULL，不允许篡改成功。写入用 api 运行角色（仅 SELECT+INSERT），worker 在本切片没有该表权限。一次性探针 `rag_backend.llm_probe` 默认关闭，只有显式设置 `ALLOW_LLM_PROBE=1` 并提供独立非占位 `LLM_API_KEY`（不复用开发期 Node Jev 的 `AI_GATEWAY_API_KEY`）时才向固定 `https://api.deepseek.com` 发一次请求；缺失任一项时快速失败、不联网不写账本。探针不把 prompt、响应正文或密钥写入数据库、日志或 marker，也不使用响应内容。单价与费用依赖缓存命中/未命中和高低峰价目，正确快照缺失时保持 NULL，因此 `llm_usage` 当前不能作为费用事实来源。探针在触网前只读预检目标库：连接角色必须是 `citemind_api`、`public.llm_usage` 存在且必要列可读、SELECT+INSERT 可用；未迁移、角色不符或权限不足都不触网。云调用与 PostgreSQL 提交无法原子化，预检后数据库故障仍可能使网络已发生而事实未落账，此时退出非零，不得视为连通验收成功。

生产网络只公开网关；PostgreSQL、Redis、inference 与文件卷不直接暴露公网。内部推理接口有凭证、批量限制和信号量。数据库账号按进程最小授权；若未来引入 PostgreSQL RLS，需要验证连接池身份设置和回收，不把启用 RLS 当作已验证的隔离。
