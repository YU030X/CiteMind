"""入库身份预检：纯本地判定 job 绑定的 index profile 与当前 worker 契约是否一致。

本模块只做纯计算：不接触数据库、Celery、文件或网络，不修改任务状态、不写 marker、不 ACK
消息。调用方（未来的 worker）自行读取 ``ingest_job.profile_id``、
``document.source_type``、``document_version.parser_version`` 与 ``index_profile`` 行，
把结果作为普通数据传入；本模块返回互斥的静态判定枚举。``expected`` 契约由 worker
启动时构造并校验一次后注入，``expected_parser_version`` 由调用方传入
``rag_backend.ingestion.parsing.MARKDOWN_PARSER_VERSION``，因此本模块不导入解析、模型或数据库
依赖，导入期只引入标准库。

判定优先级（前者命中即返回，保证互斥）：

1. ``job_profile_id is None`` → ``PROFILE_UNBOUND``：旧任务未绑定，不自动补绑。
2. ``stored_profile is None`` → ``PROFILE_MISSING``：已绑定但读不到对应行。
3. ``stored_profile.profile_id != job_profile_id`` → ``PROFILE_ID_MISMATCH``：行与绑定不符。
4. ``source_type`` 不在 :data:`SUPPORTED_SOURCE_TYPES` → ``SOURCE_UNSUPPORTED``（当前仅 markdown）。
5. ``parser_version != expected_parser_version`` → ``PARSER_UNSUPPORTED``：占位版本不自动升级。
6. 用行七字段构造契约失败（如 ``normalize=False`` 或非法 ``dimension``）→ ``CONTRACT_MISMATCH``。
7. 行七字段规范 hash 与行 ``config_hash`` 不一致 → ``HASH_MISMATCH``。
8. 行七字段与 ``expected`` 不一致，或行 ``config_hash`` 与其不一致 → ``CONTRACT_MISMATCH``。

其余返回 ``ALLOWED``。判定只看 profile 与 parser/来源契约，不判定 ``ingest_job.status`` 或 KB 的
``active_index_profile_id``（首次 READY 发布前该指针为 NULL），也不构造默认 profile。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, fields
from enum import Enum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from rag_backend.models.profile_contract import IndexProfileContract

# 当前唯一受支持的来源类型；PDF 属于后续切片，不在这里实现。字面量与
# ``rag_backend.ingestion.service.SOURCE_TYPE_MARKDOWN`` 由单测交叉约束。
SOURCE_TYPE_MARKDOWN: Final = "markdown"
SUPPORTED_SOURCE_TYPES: Final[frozenset[str]] = frozenset({SOURCE_TYPE_MARKDOWN})


class ProfileIdentityDecision(Enum):
    """身份预检结果；互斥、值稳定，且不拼接 UUID、hash、路径或 DSN。"""

    ALLOWED = "ALLOWED"
    PROFILE_UNBOUND = "PROFILE_UNBOUND"
    PROFILE_MISSING = "PROFILE_MISSING"
    PROFILE_ID_MISMATCH = "PROFILE_ID_MISMATCH"
    CONTRACT_MISMATCH = "CONTRACT_MISMATCH"
    HASH_MISMATCH = "HASH_MISMATCH"
    PARSER_UNSUPPORTED = "PARSER_UNSUPPORTED"
    SOURCE_UNSUPPORTED = "SOURCE_UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class StoredIndexProfile:
    """``index_profile`` 行的只读元数据 DTO；只承载契约元数据，不含正文/blob。

    未来 worker 在加载行后一次映射为本对象，预检内部不接触 ORM 或数据库。七个契约字段与
    ``IndexProfileContract`` 同名，由防漂移单测约束；预检按契约字段名动态取值，契约新增字段
    时会显式失败而不是静默忽略。
    """

    profile_id: uuid.UUID
    config_hash: str
    embedding_model: str
    model_revision: str
    dimension: int
    normalize: bool
    tokenizer_revision: str
    chunker_version: str
    keyword_analyzer_version: str


def decide_profile_identity(
    *,
    job_profile_id: uuid.UUID | None,
    source_type: str,
    parser_version: str,
    stored_profile: StoredIndexProfile | None,
    expected: IndexProfileContract,
    expected_parser_version: str,
) -> ProfileIdentityDecision:
    """按模块 docstring 的优先级返回互斥判定；不做 IO、不改状态、不构造默认 profile。"""

    if job_profile_id is None:
        return ProfileIdentityDecision.PROFILE_UNBOUND
    if stored_profile is None:
        return ProfileIdentityDecision.PROFILE_MISSING
    if stored_profile.profile_id != job_profile_id:
        return ProfileIdentityDecision.PROFILE_ID_MISMATCH
    if source_type not in SUPPORTED_SOURCE_TYPES:
        return ProfileIdentityDecision.SOURCE_UNSUPPORTED
    if parser_version != expected_parser_version:
        return ProfileIdentityDecision.PARSER_UNSUPPORTED

    contract_type = type(expected)
    try:
        row_contract = contract_type(
            **{
                field.name: getattr(stored_profile, field.name)
                for field in fields(contract_type)
            }
        )
    except ValueError:
        # ProfileContractError 是 ValueError 子类；不导入模型包即可把行字段校验失败收敛为判定。
        return ProfileIdentityDecision.CONTRACT_MISMATCH
    if row_contract.config_hash() != stored_profile.config_hash:
        return ProfileIdentityDecision.HASH_MISMATCH
    if row_contract != expected or stored_profile.config_hash != expected.config_hash():
        return ProfileIdentityDecision.CONTRACT_MISMATCH
    return ProfileIdentityDecision.ALLOWED


_DECISION_REASONS: Final[dict[ProfileIdentityDecision, str]] = {
    ProfileIdentityDecision.ALLOWED: "profile 与 parser 均匹配，可继续处理",
    ProfileIdentityDecision.PROFILE_UNBOUND: "任务未绑定 index profile，不自动补绑",
    ProfileIdentityDecision.PROFILE_MISSING: "任务已绑定 profile 但读不到对应行",
    ProfileIdentityDecision.PROFILE_ID_MISMATCH: "读到的 profile 行与任务绑定不一致",
    ProfileIdentityDecision.CONTRACT_MISMATCH: "profile 七字段不满足冻结契约或与预期不一致",
    ProfileIdentityDecision.HASH_MISMATCH: "profile 七字段的规范 hash 与登记值不一致",
    ProfileIdentityDecision.PARSER_UNSUPPORTED: "parser_version 与当前实现不一致",
    ProfileIdentityDecision.SOURCE_UNSUPPORTED: "source_type 当前不受支持",
}


def static_reason(decision: ProfileIdentityDecision) -> str:
    """返回与输入无关的静态原因文本；不拼接 UUID、hash、parser/source 值、路径或 DSN。"""

    return _DECISION_REASONS[decision]
