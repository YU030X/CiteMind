# 个人 RAG 工作台演示流程

> 本文是一份手工演示脚本，用于在本地六服务环境里按固定顺序走通「两种格式上传 → 问答引用 → 追问 → 更新版本 → 删除 → 不同账号」。**本文撰写时未在真实环境执行过**，下文所有结果都是预期行为而不是实测记录，执行者需自行核对。
>
> 前端是 `frontend/` 里的正式工作台（Vue 3 + Tailwind CSS v4 + shadcn-vue/reka-ui 组件，视觉与组件源码来自已审核的 `ui-demos/` 视觉稿），只调用已实现的接口：`GET /me`、`POST /auth/login`、`POST /auth/logout`、`GET /knowledge-bases`、`POST /knowledge-bases`、`GET /knowledge-bases/{id}/documents`、`GET /documents/{id}`、`POST /knowledge-bases/{id}/documents`、`POST /documents/{id}/versions`、`DELETE /documents/{id}`、`GET /conversations`、`PATCH /conversations/{id}`、`DELETE /conversations/{id}`、`POST /conversations`、`GET|POST /conversations/{id}/messages`、`GET /citations/{id}`。目标后端缺少其中任一只读端点时，对应页面显示错误横幅，其余流程可继续。

## 需要你授权并自己执行的前置操作

1. 迁移目标库：`uv run alembic upgrade head`（需要 `MIGRATION_DATABASE_URL` 指向独占的 CiteMind 库；迁移会创建 `vector` 扩展，不能在共享库上执行）。
2. 启动六服务：`docker compose --env-file .env -f deploy/compose/compose.yml up -d --build --wait`；网关入口是 `http://127.0.0.1:58080`，只有网关发布回环宿主端口。
3. 建两个账号（密码只从环境变量、文件或标准输入读取，不进入命令行历史）：`uv run python -m rag_backend.auth.cli --username demo-hr --admin --password-env DEMO_HR_PASSWORD` 与 `uv run python -m rag_backend.auth.cli --username demo-staff --password-env DEMO_STAFF_PASSWORD`。
4. 在 `.env` 中开启真实入库与问答生成：`INGEST_PROCESSING_ENABLED=1`、`LLM_ENABLED=1`，并提供非占位 `LLM_API_KEY` 与 `INFERENCE_TOKEN`，然后重建并重启 `worker` 与 `api`。开启真实入库前按[部署](deployment.md)要求先备份并迁移目标库。不开启这些开关时，上传只停在受理状态、问答返回静态 503。
5. 只读核对：`docker compose --env-file .env -f deploy/compose/compose.yml ps`。

这些操作会修改本地数据卷、把选中的授权片段发给云 LLM 并产生费用，必须由你确认后执行；本文档不代为执行任何命令。

## 本地入口

工作台由 `frontend-gateway` 同源提供：Compose 用根 workspace 的 `pnpm --filter @citemind/frontend build` 构建静态产物，nginx 同时把 `/api/` 代理到内部 `api` 服务，因此登录、CSRF 与所有请求都是同源相对路径。改完前端后需要重新构建网关镜像才能看到新产物。

- 打开工作台：`http://127.0.0.1:58080`（默认进入「AI 问答」，`#knowledge-bases`、`#documents` 直接定位到另外两页）。
- 只做构建检查：`pnpm frontend:build`。
- `pnpm --dir frontend dev` 只提供静态资源，不带 `/api` 代理，界面里的请求会拿不到后端；要用它调试请自行把 `/api/v1` 指向网关。

## 语料

复用[开发评估语料](../tests/evaluation/corpus/)，均为自制、无敏感内容的样本，清单见该目录的 `manifest.json`。

| 样本 | 用途 |
| --- | --- |
| `handbook-v2.md`、`handbook-v3.md` | 同一文档的两个版本；v2 是 10 天年假，v3 是 12 天，用于更新版本 |
| `cafeteria.pdf` | 文本层 PDF，用于第二种格式与页定位 |
| `legacy-bonus-v1.md` | 用于删除流程 |
| `salary-bands.md` | 用于不同账号的权限对比 |

## 1. 两种格式上传

1. 用 `demo-hr` 登录，在「知识库」页点“新建知识库”创建 `demo-handbook`（该按钮只对管理员显示，角色由服务端判定）。卡片上的“打开文档管理”会选中该知识库并跳到「文档管理」页。
2. 在「文档管理」页点右上角“上传文档”，填标题、选文件、确认上传。
3. 预期：页面提示“上传已受理：202 只表示文件与任务已落库，解析与索引尚未完成”，文档出现在表格里并显示真实的生命周期与任务阶段（如“入库中 / 解析中 / 向量化中”）；只有存在非终态任务时列表每 5 秒自动刷新，任务到终态后停止轮询。
4. 再上传 `cafeteria.pdf`，预期与 Markdown 一致；该文档的引用定位应显示页号而不是行号。
5. 若某个 PDF 版本落入 `NEEDS_OCR`，状态列显示“需 OCR（无文本层）”，这表示没有可检索正文，不是成功。
6. 表格的“查看详情”按 `GET /documents/{id}` 实时读取，展示版本、最新任务与服务端返回的诊断码。

## 2. 问答与引用

1. 在「AI 问答」页确认右上角选中的知识库，点左侧会话历史里的“新建会话”，会话范围固定为该知识库。
2. 提问“正式员工每个自然年有多少天带薪年假？”，预期答案指向 10 天。
3. 预期：生成期间不能重复提交；回答下方出现引用按钮（形如 `E1`）。
4. 点击引用按钮：前端重新请求 `GET /api/v1/citations/{id}`（服务端再次复核所有者与成员关系），右侧 Sheet 再显示服务端返回的文档标题、版本、定位与原文片段。Markdown 显示 1 起的块级行范围，PDF 显示页号；未知 locator 原样展示为只读文本。
5. 引用已撤权或来源已删除时应显示“无法读取引用”与 `CITATION_NOT_FOUND`，而不是旧内容。
6. 会话行的省略号菜单是真实操作：置顶/取消置顶与改名走 `PATCH /conversations/{id}`（标题去首尾空白、非空、最长 200 字符；置顶由服务端 `pinned_at` 决定），删除走 `DELETE /conversations/{id}` 并在确认弹窗里再次确认，软删后列表、历史、引用与追问都不再返回该会话。删除当前会话后问答区回到可“新建会话”的空欢迎态。菜单三项只对会话所有者可用（与 KB 角色无关），越权统一 404。

## 3. 追问

1. 在同一会话继续提问“那年假需要谁审批？”。
2. 预期：服务端先用受限改写把追问变成独立问题再检索，回答提示仍用原问题；历史消息按最新权限过滤，来源被撤权的助手消息会整体消失。
3. 服务端返回的 `followUp` 显示为“服务端建议的追问”按钮，点击只填入输入框，不会自动发送。

## 4. 更新版本

1. 在「文档管理」页对 `handbook` 那一行的省略号菜单点“上传新版本”，选择 `handbook-v3.md`。
2. 预期：提交的 `expectedVersionId` 是当时显示的可用版本；受理后当前可用版本不变，表格会同时显示“可用 v(n)”与“最新 v(n+1)（待处理）”，直到新版本发布才切换。
3. 若期间版本已被其他提交改变，服务端返回 409，前端刷新列表并提示确认最新版本后重新提交。
4. 新版本进入 READY 后再次提问同一个年假问题，预期答案变成 12 天，引用版本变为新版本号。
5. 同一次提交失败后再次点击，会复用同一个 `Idempotency-Key` 重试同一请求；一旦版本或幂等冲突，则必须用新的期望版本重新提交。

## 5. 删除

1. 对 `legacy-bonus` 那一行点“删除文档”，在确认弹窗里再次确认（该菜单项只对 OWNER 可用）。
2. 预期：删除后文档列表、当前会话消息与已打开的引用一起刷新；来自该文档的引用按钮消失，引用 Sheet 关闭。按[入库](ingestion.md)约束，已删除文档的检索与引用立即失效，但已经发出的字节无法追回。

## 6. 不同账号

1. 退出登录（侧边栏底部）并用 `demo-staff` 登录。
2. 预期：只能看到自己是成员的 KB；读者角色看到的“上传文档”按钮为禁用状态，行内菜单的“上传新版本”与“删除文档”也不可用（禁用项会说明所需角色）。
3. 把 `demo-staff` 加入某个 KB 并赋予 READER 后复测，再用 OWNER 复测删除；前端按钮只是提示，权限始终由服务端判定：读者直接构造上传或删除请求同样会被拒绝。
4. 若创建了限 HR 的知识库并只授予 `demo-hr`，则 `demo-staff` 登录后完全看不到该 KB，会话范围也不会被其他 KB 的会话污染。

## 已知未接通项

- **模型与思考控件受服务端白名单约束**：`GET /me`/登录响应的 `generation` 只读列出服务端已验证的模型与各自思考选项（当前仅 `deepseek-flash`，强度 `low`/`high`/`max`），并给出默认组合（默认模型 + 关闭思考）。`POST /conversations/{id}/messages` 请求体在 `question`/`requestId` 之外可选携带 `model`/`thinking`/`reasoningEffort`，前端选择器只能提交白名单内枚举，不提供任意模型名、endpoint 或强度别名；未验证模型返回 422 `GENERATION_OPTION_UNSUPPORTED`，关闭思考时提交强度返回 422。每轮选项记录在 `query_run.generation_options`，不随后续选择改写；思考模式的 CoT 不展示。
- **引用没有内联标记**：服务端保存的助手消息正文是纯文本，引用只作为 `citations` 列表返回（`displayLabel` 形如 `E1`）。前端以下方引用按钮为唯一入口；`markdown-it`（`html: false`）的 `[n]` 标记映射仍保留，但当前没有生产者会生成它。
- **拒答与降级未在界面上单独标注**：`AnswerResponse` 里有 `insufficientEvidence` 与 `degradedStages`，但 `GET /conversations/{id}/messages` 的消息对象没有这两个字段，历史刷新后无法还原，因此界面只展示服务端写入的正文。
- **文档列表缺少部分列**：后端不返回文件大小与上传者，表格因此没有这两列。
- **删除会话不可恢复**：`DELETE /conversations/{id}` 只做软删，不提供恢复、清空消息或物理回收入口。
- 前端不提供流式输出、PDF 阅读器、成员管理界面和任务管理后台。
- 文档列表与会话列表没有分页，按个人规模设计；没有分页参数或页码状态。
- 会话标题在首轮提问时由该问题派生（真实来源），之后可由用户改名；没有标题的空会话显示为“未命名会话”。
