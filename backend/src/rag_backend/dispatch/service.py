"""dispatcher 编排：短事务领取、提交后投递、CAS 回写、有界补偿与后台循环。

网络发送永远发生在领取事务提交之后；任何发送/回写失败都不得让调用方抛 500，只按状态机
保留 PENDING 并退避。后台轮询由 API lifespan 显式启用（``dispatcher_enabled``）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from rag_backend.database import SessionFactory
from rag_backend.dispatch import protocol
from rag_backend.dispatch.repository import (
    ClaimedOutboxEvent,
    CompensationCandidate,
    CompensationResult,
    OutboxRepository,
    SqlOutboxRepository,
)

logger = logging.getLogger(__name__)

# 补偿扫描每轮最多处理的 job 数量，避免一次锁住过多行。
DEFAULT_COMPENSATION_LIMIT = 50
# 后台循环的轮询间隔与每轮最多投递事件数；低配单进程下不让一次 cron 卡住事件循环。
DISPATCH_POLL_SECONDS = 5.0
DISPATCH_BATCH_PER_CYCLE = 50
# 事件级失败的前几次与之后每隔若干次各记一条 warning，其余降为 debug；
# Redis 长时间故障时限制噪声，但首次与周期性信号仍可观察。
FAILURE_WARN_ATTEMPTS = 3
FAILURE_WARN_INTERVAL = 12
# 后台循环连续异常时，首条之后每 N 次记一条 warning。
LOOP_ERROR_LOG_INTERVAL = 12


def _should_warn(attempt: int) -> bool:
    """是否把一次事件级失败记到 warning（其余降为 debug）。"""

    return attempt <= FAILURE_WARN_ATTEMPTS or attempt % FAILURE_WARN_INTERVAL == 0


def _log_event_failure(phase: str, claim: ClaimedOutboxEvent, error: BaseException) -> None:
    """脱敏记录事件级失败。

    只记阶段、异常类型、owner 与 UUID；不记原始异常消息、traceback、DSN、token 或正文。
    """

    level = logging.WARNING if _should_warn(claim.dispatch_attempt) else logging.DEBUG
    logger.log(
        level,
        "dispatcher event failure phase=%s error=%s owner=%s event_id=%s job_id=%s attempt=%d",
        phase,
        type(error).__name__,
        claim.lease_owner,
        claim.event_id,
        claim.job_id,
        claim.dispatch_attempt,
    )


class Publisher(Protocol):
    """投递接口；生产实现为 ``CeleryPublisher``，测试可注入 fake。"""

    async def publish(
        self,
        *,
        task_name: str,
        payload: dict[str, object],
        task_id: str,
        queue: str,
    ) -> None: ...


class DispatchOutcome(Enum):
    """单次投递结果；供循环计数与测试断言，不进入数据库。"""

    IDLE = "IDLE"
    SENT = "SENT"
    DEFERRED = "DEFERRED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class CompensationPlan:
    """由候选事实推导出的补偿动作，纯函数输出。"""

    followup_job_ids: tuple[uuid.UUID, ...]
    unconfirmed_job_ids: tuple[uuid.UUID, ...]


def plan_compensation(
    candidates: Sequence[CompensationCandidate],
    *,
    max_attempts: int = protocol.MAX_DELIVERY_ATTEMPTS,
) -> CompensationPlan:
    """决定哪些 job 需要补投、哪些达到上限需标记未确认。

    已接收（有 worker marker）或已有 PENDING 事件的 job 既不补投也不标记；只有“有旧
    SENT、无 PENDING、无接收标记”的 job 才参与，达到上限则停止热循环并显式标记。
    """

    followup: list[uuid.UUID] = []
    unconfirmed: list[uuid.UUID] = []
    for candidate in candidates:
        if candidate.receive_marker or candidate.has_pending:
            continue
        if not candidate.has_sent:
            continue
        if candidate.event_count >= max_attempts:
            unconfirmed.append(candidate.job_id)
        else:
            followup.append(candidate.job_id)
    return CompensationPlan(tuple(followup), tuple(unconfirmed))


async def dispatch_one(
    repo: OutboxRepository,
    publisher: Publisher,
    *,
    owner: str,
) -> DispatchOutcome:
    """领取一条事件并在提交后投递，最后 CAS 回写 SENT。"""

    claim = await repo.claim_due_event(owner=owner)
    if claim is None:
        return DispatchOutcome.IDLE

    if not protocol.is_supported_event_type(claim.event_type):
        # 未知事件类型不投递，显式落 FAILED 并给 job 写错误码；回写失败仍留待重领。
        try:
            await repo.fail_event(claim)
        except Exception as error:
            _log_event_failure("fail", claim, error)
        return DispatchOutcome.FAILED

    # 租约过期后重新领到时，若 worker 已写下该事件的接收 marker，则对账标 SENT 而不再投递。
    try:
        already_received = await repo.event_already_received(claim.event_id)
    except Exception as error:
        _log_event_failure("receive_check", claim, error)
        already_received = False
    if already_received:
        try:
            updated = await repo.mark_sent(claim)
        except Exception as error:
            _log_event_failure("reconcile_writeback", claim, error)
            return DispatchOutcome.DEFERRED
        return DispatchOutcome.SENT if updated else DispatchOutcome.DEFERRED

    payload = protocol.build_dispatch_payload(claim.job_id)
    try:
        await publisher.publish(
            task_name=protocol.INGEST_TASK_NAME,
            payload=payload,
            task_id=str(claim.event_id),
            queue=protocol.INGEST_QUEUE,
        )
    except Exception as error:
        # 发送失败（含 Redis 不可达）：不抛 500，保留 PENDING 并按指数退避重排。
        _log_event_failure("send", claim, error)
        delay = protocol.retry_delay_seconds(claim.dispatch_attempt)
        try:
            await repo.defer_event(claim, delay_seconds=delay)
        except Exception as defer_error:
            # 退避回写也失败时保持租约，等 lease_until 过期后由下一轮重领。
            _log_event_failure("defer", claim, defer_error)
        return DispatchOutcome.DEFERRED

    # 发送已成功；回写失败时事件仍为 PENDING 且持有租约，租约过期后补投。
    try:
        updated = await repo.mark_sent(claim)
    except Exception as error:
        _log_event_failure("writeback", claim, error)
        return DispatchOutcome.DEFERRED
    return DispatchOutcome.SENT if updated else DispatchOutcome.DEFERRED


async def compensate_unconfirmed_jobs(
    repo: OutboxRepository,
    *,
    limit: int = DEFAULT_COMPENSATION_LIMIT,
    max_attempts: int = protocol.MAX_DELIVERY_ATTEMPTS,
) -> CompensationResult:
    """锁住到期未确认的 QUEUED job，按计划补投或标记未确认。

    候选行在同一个事务里被 ``FOR UPDATE SKIP LOCKED`` 锁定，插入新 PENDING 前后都不会有
    第二个 dispatcher 对同一 job 重复补偿。任何失败都回滚，不把 job 置为其他状态。
    """

    candidates = await repo.lock_compensation_candidates(limit=limit)
    plan = plan_compensation(candidates, max_attempts=max_attempts)
    if not plan.followup_job_ids and not plan.unconfirmed_job_ids:
        await repo.commit()
        return CompensationResult((), ())
    try:
        created = await repo.create_followup_events(plan.followup_job_ids)
        flagged = await repo.flag_delivery_unconfirmed(plan.unconfirmed_job_ids)
        await repo.commit()
    except Exception:
        await repo.rollback()
        raise
    if flagged:
        logger.warning(
            "dispatcher delivery unconfirmed job_ids=%s",
            ",".join(str(job_id) for job_id in flagged),
        )
    return CompensationResult(created, flagged)


class OutboxDispatcher:
    """按调用构造独立 Session 的 dispatcher；不持有长事务，也不持有网络连接。"""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        publisher: Publisher,
        owner: str | None = None,
        compensation_limit: int = DEFAULT_COMPENSATION_LIMIT,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher
        self._owner = owner if owner is not None else protocol.default_lease_owner()
        self._compensation_limit = compensation_limit

    async def dispatch_once(self) -> DispatchOutcome:
        async with self._session_factory() as session:
            repo = SqlOutboxRepository(session)
            return await dispatch_one(repo, self._publisher, owner=self._owner)

    async def compensate(self) -> CompensationResult:
        async with self._session_factory() as session:
            repo = SqlOutboxRepository(session)
            return await compensate_unconfirmed_jobs(repo, limit=self._compensation_limit)

    async def run(self) -> None:
        """后台循环：每轮投递一批到期事件后补偿一次；任何异常都不终止循环。

        数据库或 Redis 故障只记录在本轮失败上并等待下一轮，不向 API 请求路径抛错；
        取消（``asyncio.CancelledError``）直接传播，由调用方 await 释放。
        """

        consecutive_errors = 0
        while True:
            try:
                for _ in range(DISPATCH_BATCH_PER_CYCLE):
                    outcome = await self.dispatch_once()
                    # IDLE 无待发；DEFERRED（发送或 CAS 回写失败）说明 broker/DB 可能异常，
                    # 本轮立即停止后续发送，避免放大故障；PENDING + 指数退避负责下次重试。
                    if outcome is DispatchOutcome.IDLE or outcome is DispatchOutcome.DEFERRED:
                        break
                await self.compensate()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # DB/Redis 不可达：首条 warning 后按间隔记录，不影响 API 启动或已建立请求。
                consecutive_errors += 1
                if consecutive_errors == 1 or consecutive_errors % LOOP_ERROR_LOG_INTERVAL == 0:
                    logger.warning(
                        "dispatcher loop failure error=%s consecutive=%d",
                        type(error).__name__,
                        consecutive_errors,
                    )
            else:
                consecutive_errors = 0
            await asyncio.sleep(DISPATCH_POLL_SECONDS)
