"""云 LLM 用量账本模型（第三切片）。

``llm_usage`` 是 append-only 的 provider 调用事实：一次 provider attempt 恰好一行，
失败与超时也必须追加，不更新、不删除。provider 报告的 token 缺失时不得伪造，
该行只能是 ``FAILED``/``TIMEOUT`` 且 token 与费用列为 NULL；成功行必须带
``PROVIDER_REPORTED`` 的 prompt/completion tokens 才有资格被核对为连通成功。
单价与成本依赖缓存命中/未命中和高低峰价目，正确快照缺失时保持 NULL，本切片不计算费用。
"""

import uuid
from decimal import Decimal

from sqlalchemy import CheckConstraint, Integer, Numeric, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from evidencehub.models.base import Base, CreatedAtMixin

# 成功行必须由 provider 明确报告了 prompt/completion tokens；缺失即视为失败事实。
SUCCEEDED_USAGE_SQL = (
    "status <> 'SUCCEEDED' OR (usage_source = 'PROVIDER_REPORTED' "
    "AND prompt_tokens IS NOT NULL AND completion_tokens IS NOT NULL)"
)

# 价格快照按来源、币种、成本三者同时存在或同时缺失，避免只写半个快照造成误导。
PRICE_CONSISTENCY_SQL = (
    "(price_source IS NULL) = (price_currency IS NULL) "
    "AND (price_source IS NULL) = (cost_amount IS NULL)"
)


class LlmUsage(CreatedAtMixin, Base):
    """一次 provider 调用的 token/状态/耗时事实，append-only。"""

    __tablename__ = "llm_usage"
    __table_args__ = (
        CheckConstraint("status IN ('SUCCEEDED', 'FAILED', 'TIMEOUT')", name="status"),
        CheckConstraint(
            "usage_source IN ('PROVIDER_REPORTED', 'UNKNOWN')", name="usage_source"
        ),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
        CheckConstraint("prompt_tokens >= 0", name="prompt_tokens_non_negative"),
        CheckConstraint("completion_tokens >= 0", name="completion_tokens_non_negative"),
        CheckConstraint(
            "prompt_cache_hit_tokens >= 0", name="cache_hit_tokens_non_negative"
        ),
        CheckConstraint(
            "prompt_cache_miss_tokens >= 0", name="cache_miss_tokens_non_negative"
        ),
        CheckConstraint("latency_ms >= 0", name="latency_ms_non_negative"),
        CheckConstraint("cost_amount >= 0", name="cost_amount_non_negative"),
        CheckConstraint(SUCCEEDED_USAGE_SQL, name="succeeded_requires_provider_usage"),
        CheckConstraint(
            "status = 'SUCCEEDED' OR error_code IS NOT NULL", name="failure_has_error_code"
        ),
        CheckConstraint(PRICE_CONSISTENCY_SQL, name="price_consistent"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage_source: Mapped[str] = mapped_column(Text, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_cache_hit_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_cache_miss_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_currency: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
