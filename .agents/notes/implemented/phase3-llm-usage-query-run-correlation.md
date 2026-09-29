# Agent Note：Phase 3 成本片 `llm_usage.query_run_id` 调用前关联键

- 状态：已实现（迁移 `20260929_0016`、`answer_question` 透传、单测与离线迁移 shape 检查）；真实 PostgreSQL 迁移用例本轮未运行
- 范围：`migrations/versions/20260929_0016_llm_usage_query_run_id.py`、`models/usage.py`、`conversation/repository.py`、`conversation/service.py`

## 背景

`llm_usage` 是 append-only 的 provider attempt 事实表，但此前没有任何列能把一次提问产生的账本行归到一起：`query_run.llm_usage_id` 只存**最终回答**那一行，`qa_rewrite` 行和来源变化重试产生的额外 `qa_answer` 行都不被引用；`query_run_id` 直到 `_persist_turn` 才生成，那时改写账本早已单独提交；answer 生成失败还会抛出领域错误、根本不写 `query_run`，失败 attempt 完全无 run 可指。仅靠 `provider+model+stage+created_at` 猜测不可靠。

## 决策

1. **在调用前 mint 关联键，而不是复用 `query_run.id`。** `answer_question` 在 `load_conversation` 确认会话存在后、任何历史读取/改写/检索/生成之前生成一次 `query_run_id = uuid.uuid4()`；改写、全部回答 attempt、以及最终 `query_run` 行共用同一值。失败提问没有 `query_run` 行，但失败 attempt 仍带该键。
2. **不建外键。** 账本按 attempt 分次 `commit()`，普通 FK 会在 `query_run` 行提交前校验失败；deferred FK 也因分次提交而失败，除非重排事务或预建 run 行（非最小、且与“网络调用期间不持有事务”冲突）。`query_run_id` 因此是 nullable 关联键，不代表一定存在对应 run。
3. **历史行保持 NULL，不回填、不伪造。** 迁移只加可空列与普通 btree 索引 `ix_llm_usage_query_run_id`；表级 `SELECT`/`INSERT` GRANT 已覆盖新列，worker 仍零权限，无新授权。降级先删索引再删列。
4. **`query_run.llm_usage_id` 保持只指最终 answer 行。** 它继续表示“本轮交付所用的回答账本行”，新列才承载整轮 attempt 集合；两者并存，不删除旧列。
5. **独立探针不归业务 run。** `rag_backend.llm_probe` 的账本写入省略 `query_run_id`（NULL），`LLM_USAGE_REQUIRED_COLUMNS` 是探针预检要求列的子集而非全表结构，因此不同步加列。

## 后果与边界

- `query_run_id` 只是归因键，不是计费事实：价目快照与 `cost_amount` 仍为 NULL，runner usage artifact、`Decimal` 成本、`conversation_id` 列、UI 与汇率都不在本提交。
- 当前 `answer_question` **没有**按 `request_id` 去重的幂等回放路径，重复请求会各自产生新 run 与新账本行；“幂等已有结果不落 usage”只在“没有 provider attempt 就不写账本”这一可实现意义上成立，并由单测覆盖。请求级幂等若将来实现，必须复用已有 run、不得再 mint。
- 真实 PostgreSQL 迁移与授权（列可空 UUID、无外键、具名 btree 索引、历史行 NULL、api 可读写、worker 拒绝、降级恢复）由 `tests/integration/test_llm_usage_query_run_id_migration.py` 承担；本轮受用户“禁止真实 DB/Docker”约束只做 `--collect-only`，未执行。
