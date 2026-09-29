# Phase 3 只读探针 CLI 与真实 adapter

## 决策

探针的**真实资源装配**与**只读编排**分开：`probe.py` 只做依赖注入，新增 `probe_adapters.py` 负责 PostgreSQL/inference 与 locator 解析，`probe_cli.py` 负责护栏、环境读取与原子落盘。

- DSN 护栏在 adapter 层而不是 CLI：必须 `postgresql+psycopg`，数据库名以 `_test` 结尾，只有 `--allow-database-name` 精确重申才放行。错误消息不回显 DSN 或库名。
- 只读 engine 用 async SQLAlchemy，`load_*` 一次性查询用 `async with` 会话；探针路径的 `repository_factory` 创建并追踪 session，运行结束统一 `close` 全部 session 再 `dispose` engine。探针只读，不写库、不迁移。
- 身份/profile 解析结果先收敛为**纯函数**（组织一致、角色账号唯一、多 profile 身份全等），SQL 行类型与归一逻辑拆开，便于无数据库单测。
- artifact 的 `modelIdentities` 只允许全局 scalar，因此多个 active `index_profile` 身份不完全一致时静态失败，而不是任选一个或改成嵌套结构。
- 候选 locator 只从真实 `EvidenceChunkRow.source_locator` 解析当前评估支持的 `markdown`/`pdf`；`web`/`docx` 或畸形结构属于评估范围外，静态失败，不猜测、不降级。
- inference 客户端只用显式 token 与 base URL 构造，复用既有 URL 白名单与超时；`ProbeRuntime.aclose` 统一关闭本地客户端与数据库。
- CLI 默认 dry-run，**绝不读取** DSN/token 环境变量，也不建 engine/client、不写文件；真实运行必须 `--allow-real-probe`+`--allow-real-rerank`，holdout 另需 `--confirm-holdout`。预算是硬下界（`2*题数` embedding、非 no_permission 题数 rerank）。
- 产物先在内存通过 `validate_ablation_triplet` 与严格 `CalibrationArtifact` 校验，再在同一输出目录写唯一 temp，四份全部成功后 `os.replace`；正式目标已存在则拒绝覆盖。不做过度事务模拟：replace 中途失败只尽力清 temp 并静态失败。

## 当前边界

本轮只有可执行入口与 fake 测试，**没有连接真实 PostgreSQL/inference，也没有任何真实 Recall@10/nDCG@10、标定或时延数值**。adapter 的连接行为、真实身份/profile 解析与真实 A/B/C 运行仍未验收。
