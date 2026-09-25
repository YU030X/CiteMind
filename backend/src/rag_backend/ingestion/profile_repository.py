"""默认 index profile 的幂等登记（API 角色，只 SELECT + INSERT）。

本模块只提供一个函数 :func:`ensure_default_index_profile`：把冻结的默认编码契约登记进
全局 ``index_profile`` 表。它是可单独调用的入口，**不接线任何业务路径**：不改 KB 创建、
上传受理、``knowledge_base.active_index_profile_id``、worker 任务或迁移；登记成功不代表
任何 KB 可检索，也不把 KB 指针从 NULL 回填。

语义边界：

- 幂等：同一默认契约（同一 ``config_hash``）重复登记返回同一行 id，不重复插入。
- 不提交事务：函数只执行 INSERT / SELECT，**调用方（session owner）拥有事务**，必须自行
  ``commit`` 或 ``rollback``；函数内部既不提交也不回滚。
- 冲突处理：单条 ``INSERT ... ON CONFLICT (config_hash) DO NOTHING ... RETURNING id``。
  没有返回行时在同一事务内重新 SELECT 既有行，并逐项比对七个契约字段；字段不一致抛
  :class:`IndexProfileConflictError`（哈希碰撞或既有旧数据都不能被静默复用）。冲突后仍读不到
  行为异常，抛 :class:`IndexProfileNotFoundError`；函数不在新事务中自动重试，也不捕获
  PG 异常转成假成功。
- 权限：只依赖 ``index_profile`` 上的 SELECT 与 INSERT，绝不 UPDATE / DELETE 该表，也不
  UPDATE ``knowledge_base``。
- 并发：READ COMMITTED 下并发插入同一 ``config_hash`` 时，后来者的 INSERT 会等待先到者
  提交；先到者提交后，后来者 ``DO NOTHING`` 并在此后语句的 READ COMMITTED 快照中看到该行，
  因此不会读到未提交数据。

自动 flush：函数不 ``session.add`` 任何对象，且在执行语句期间用 ``session.no_autoflush``
抑制 SQLAlchemy 的自动 flush，避免把调用方尚未 flush 的待写对象一并刷入、污染调用方预期的
事务快照。调用方仍应在提交前确认自己待写对象的状态。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import fields

from sqlalchemy import Insert, Select, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from rag_backend.models.indexing import IndexProfile
from rag_backend.models.profile_contract import IndexProfileContract, default_index_profile

# 与 ``config_hash`` 一起构成登记行的七个契约字段。生产从 frozen dataclass 唯一真源派生，
# 不手写字段名，避免契约增删字段时逐项比对清单静默漂移。
CONTRACT_FIELDS: tuple[str, ...] = tuple(
    field.name for field in fields(IndexProfileContract)
)


class IndexProfileRegistrationError(RuntimeError):
    """登记默认 index profile 失败；调用方应视为运行期状态异常而非可忽略情形。"""


class IndexProfileConflictError(IndexProfileRegistrationError):
    """已有同一 ``config_hash`` 的行，但其七个契约字段与默认契约不一致。

    可能是哈希碰撞，或同哈希下被写入了不同字段的旧数据；两者都不能被静默复用。
    """


class IndexProfileNotFoundError(IndexProfileRegistrationError):
    """``ON CONFLICT DO NOTHING`` 未插入，且随后在同事务内未读到既有行。"""


def _insert_values(
    contract: IndexProfileContract, profile_id: uuid.UUID
) -> dict[str, object]:
    """把默认契约摊平成插入值：显式主键、七个契约字段与 ``config_hash``。"""

    values: dict[str, object] = {
        "id": profile_id,
        "config_hash": contract.config_hash(),
    }
    for name in CONTRACT_FIELDS:
        values[name] = getattr(contract, name)
    return values


def _insert_statement(values: Mapping[str, object]) -> Insert:
    """构造按 ``config_hash`` 幂等的插入语句，冲突时不写入并返回已登记行的 id。"""

    return (
        pg_insert(IndexProfile)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[IndexProfile.config_hash])
        .returning(IndexProfile.id)
    )


async def _insert_or_none(
    session: AsyncSession, values: Mapping[str, object]
) -> uuid.UUID | None:
    """执行幂等插入；插入成功返回新行 id，冲突返回 None。"""

    result = await session.execute(_insert_statement(values))
    return result.scalar_one_or_none()


def _select_by_config_hash(config_hash: str) -> Select[tuple[IndexProfile]]:
    """构造按 ``config_hash`` 读取既有行的 SELECT；同事务内可见即可，不做脏读。"""

    return select(IndexProfile).where(IndexProfile.config_hash == config_hash)


async def _load_by_config_hash(
    session: AsyncSession, config_hash: str
) -> IndexProfile | None:
    """执行按 ``config_hash`` 的 SELECT 并返回单行。"""

    result = await session.execute(_select_by_config_hash(config_hash))
    return result.scalar_one_or_none()


def _assert_same_contract(existing: IndexProfile, contract: IndexProfileContract) -> None:
    """逐项比对七个契约字段；任一不同即明确失败，不静默复用。"""

    mismatched = [
        name
        for name in CONTRACT_FIELDS
        if getattr(existing, name) != getattr(contract, name)
    ]
    if mismatched:
        raise IndexProfileConflictError(
            "同 config_hash 的 index_profile 行与默认契约字段不一致："
            + "、".join(mismatched)
            + "；不得复用，应新建 profile/revision"
        )


async def ensure_default_index_profile(session: AsyncSession) -> uuid.UUID:
    """登记默认 index profile 并返回其行 id；重复调用返回同一 id。

    **事务归属**：调用方拥有事务，本函数不 ``commit``、不 ``rollback``，调用结束后必须由
    session owner 显式提交或回滚。函数不 ``session.add``，并用 ``session.no_autoflush``
    抑制自动 flush，因此不会替调用方刷写其待写对象。

    插入使用 ``ON CONFLICT (config_hash) DO NOTHING RETURNING id``；没有返回行时在同一事务
    内重新 SELECT 既有行并逐项比对七个契约字段。字段不一致抛
    :class:`IndexProfileConflictError`，冲突后读不到行抛 :class:`IndexProfileNotFoundError`；
    函数不自动重试，也不把 PG 异常转成成功。只用到 ``index_profile`` 的 SELECT 与 INSERT。
    """

    contract = default_index_profile()
    values = _insert_values(contract, uuid.uuid4())
    with session.no_autoflush:
        inserted_id = await _insert_or_none(session, values)
        if inserted_id is not None:
            return inserted_id
        existing = await _load_by_config_hash(session, contract.config_hash())

    if existing is None:
        raise IndexProfileNotFoundError(
            "index_profile 插入因 config_hash 冲突被跳过，但同事务内未读到既有行；"
            "不自动重试，请在提交后重试登记"
        )
    _assert_same_contract(existing, contract)
    return existing.id
