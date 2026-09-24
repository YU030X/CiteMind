"""outbox 与 ingest_job 的参数化 SQL 仓储。

语句以数据库时间为基准；单语句且无后续等待的路径使用事务 ``now()``，而会被外部行锁
阻塞、必须在解锁后反映真实时刻的语句使用 ``clock_timestamp()``。使用行锁 + ``SKIP LOCKED``
领取/补偿，并用租约 token 做回写 CAS。仓储不发起网络投递，也不决定业务状态语义。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Result
from sqlalchemy.ext.asyncio import AsyncSession

from rag_backend.dispatch import protocol

STATUS_PENDING = "PENDING"
STATUS_SENT = "SENT"
STATUS_FAILED = "FAILED"

# 领取一条到期且无有效租约的 PENDING 事件；同事务内递增 dispatch_attempt 并把 token
# 设为递增后的字符串，owner/lease_until 一并写入。RETURNING 给出本次领取完整事实。
CLAIM_SQL = text(
    """
    WITH candidate AS (
        SELECT id
        FROM outbox_event
        WHERE status = 'PENDING'
          AND next_send_at <= now()
          AND (lease_until IS NULL OR lease_until <= now())
        ORDER BY next_send_at, id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    UPDATE outbox_event AS e
    SET dispatch_attempt = e.dispatch_attempt + 1,
        lease_token = (e.dispatch_attempt + 1)::text,
        lease_owner = :owner,
        lease_until = now() + (:lease_seconds * interval '1 second'),
        updated_at = now()
    FROM candidate
    WHERE e.id = candidate.id
    RETURNING e.id, e.job_id, e.event_type, e.dispatch_attempt,
              e.lease_owner, e.lease_token, e.lease_until
    """
)

# 回写 SENT 前必须核对事件仍待发、租约未过期且 token/owner 与本次领取一致；迟到的旧
# 发送者因此无法覆盖新领取者。这是等锁前的单条语句，用 ``clock_timestamp()`` 评估租约与
# ``sent_at``/``updated_at``，避免更早开启的事务用陈旧 ``now()`` 误判。
MARK_SENT_SQL = text(
    """
    UPDATE outbox_event
    SET status = 'SENT',
        sent_at = clock_timestamp(),
        lease_owner = NULL,
        lease_token = NULL,
        lease_until = NULL,
        updated_at = clock_timestamp()
    WHERE id = :event_id
      AND status = 'PENDING'
      AND lease_owner = :owner
      AND lease_token = :token
      AND lease_until > clock_timestamp()
    RETURNING id
    """
)

# 发送失败：保留 PENDING、清除租约并按指数退避重排。token/owner 不符时不做任何改动，
# 让新领取者继续持有。
DEFER_SQL = text(
    """
    UPDATE outbox_event
    SET next_send_at = now() + (:delay_seconds * interval '1 second'),
        lease_owner = NULL,
        lease_token = NULL,
        lease_until = NULL,
        updated_at = now()
    WHERE id = :event_id
      AND status = 'PENDING'
      AND lease_owner = :owner
      AND lease_token = :token
    RETURNING id
    """
)

# 未知事件类型显式落 FAILED，不投递；同样按 token/owner 做 CAS。
FAIL_SQL = text(
    """
    UPDATE outbox_event
    SET status = 'FAILED',
        lease_owner = NULL,
        lease_token = NULL,
        lease_until = NULL,
        updated_at = now()
    WHERE id = :event_id
      AND status = 'PENDING'
      AND lease_owner = :owner
      AND lease_token = :token
    RETURNING id, job_id
    """
)

# 未知事件类型对应的 job 显式错误码；只写 error_code，不改 job.status。
SET_JOB_ERROR_SQL = text(
    """
    UPDATE ingest_job
    SET error_code = :error_code,
        updated_at = now()
    WHERE id = :job_id
      AND status = 'QUEUED'
      AND error_code IS NULL
    RETURNING id
    """
)

# 投递成功/补投后推后 job.next_run_at 至少到真实时钟+grace，作为 worker 写接收 marker
# 的宽限期；同一事务内调用，避免 SENT 后立刻被补偿扫描当作未确认而重复新建事件。本语句会
# 与 worker 的 job 行锁竞争：PostgreSQL 在等锁的扫描阶段就会求值 SET 表达式，只用
# ``clock_timestamp()`` 仍会停在解锁前的时刻。因此先在同一语句的 CTE 里 ``FOR UPDATE``
# 真正取得行锁，再执行 UPDATE，让时钟在解锁后求值；``MATERIALIZED`` 防止 CTE 被内联回
# 到等锁的 UPDATE。
PUSH_RECEIVE_GRACE_SQL = text(
    """
    WITH locked AS MATERIALIZED (
        SELECT id
        FROM ingest_job
        WHERE id IN :job_ids
          AND status = 'QUEUED'
          AND error_code IS NULL
        FOR UPDATE
    )
    UPDATE ingest_job
    SET next_run_at = GREATEST(
            next_run_at, clock_timestamp() + (:grace_seconds * interval '1 second')
        ),
        updated_at = clock_timestamp()
    WHERE id IN (SELECT id FROM locked)
    """
).bindparams(bindparam("job_ids", expanding=True))

# 对账：仅当该事件所属 job 的 `lease_owner` 精确指向本次 eventId 且存在 heartbeat 时才认定
# 该事件已被接收。不能因为别的事件留下的 heartbeat 就把当前 outbox 当已收。
CHECK_EVENT_RECEIVED_SQL = text(
    """
    SELECT 1
    FROM outbox_event AS e
    JOIN ingest_job AS j ON j.id = e.job_id
    WHERE e.id = :event_id
      AND j.lease_owner = 'event:' || e.id::text
      AND j.heartbeat_at IS NOT NULL
    """
)

# 补偿候选：QUEUED、已到期、未被标记未确认、无 worker 接收标记、无 PENDING 事件的 job。
# 行锁 + SKIP LOCKED 保证同一 job 不会被两个 dispatcher 同时补偿。
LOCK_COMPENSATION_CANDIDATES_SQL = text(
    """
    SELECT j.id,
           (SELECT count(*) FROM outbox_event e WHERE e.job_id = j.id) AS event_count,
           EXISTS (
               SELECT 1 FROM outbox_event e
               WHERE e.job_id = j.id AND e.status = 'PENDING'
           ) AS has_pending,
           EXISTS (
               SELECT 1 FROM outbox_event e
               WHERE e.job_id = j.id AND e.status = 'SENT'
           ) AS has_sent,
           (COALESCE(j.lease_owner LIKE 'event:%', false)
            OR j.heartbeat_at IS NOT NULL) AS receive_marker
    FROM ingest_job AS j
    WHERE j.status = 'QUEUED'
      AND j.next_run_at <= now()
      AND j.error_code IS NULL
      AND (j.lease_owner IS NULL OR j.lease_owner NOT LIKE 'event:%')
      AND j.heartbeat_at IS NULL
      AND NOT EXISTS (
          SELECT 1 FROM outbox_event e
          WHERE e.job_id = j.id AND e.status = 'PENDING'
      )
    ORDER BY j.next_run_at, j.id
    FOR UPDATE OF j SKIP LOCKED
    LIMIT :limit
    """
)

# 补投：为指定 job 新建 PENDING outbox，保留旧 SENT 记录。
CREATE_FOLLOWUP_SQL = text(
    """
    INSERT INTO outbox_event (
        id, job_id, event_type, status, dispatch_attempt, next_send_at,
        lease_owner, lease_token, lease_until, sent_at, created_at, updated_at
    )
    SELECT gen_random_uuid(), j.id, :event_type, 'PENDING', 0, now(),
           NULL, NULL, NULL, NULL, now(), now()
    FROM ingest_job AS j
    WHERE j.id IN :job_ids
    RETURNING id
    """
).bindparams(bindparam("job_ids", expanding=True))

# 达到补偿上限：显式标记 job 投递未确认，停止热循环；不改变 job.status。
FLAG_UNCONFIRMED_SQL = text(
    """
    UPDATE ingest_job
    SET error_code = :error_code,
        updated_at = now()
    WHERE id IN :job_ids
      AND error_code IS NULL
    RETURNING id
    """
).bindparams(bindparam("job_ids", expanding=True))


@dataclass(frozen=True)
class ClaimedOutboxEvent:
    """一次成功领取的事件事实，用于随后的投递与 CAS 回写。"""

    event_id: uuid.UUID
    job_id: uuid.UUID
    event_type: str
    dispatch_attempt: int
    lease_owner: str
    lease_token: str
    lease_until: datetime


@dataclass(frozen=True)
class CompensationCandidate:
    """补偿扫描时的单个 job 事实；决策由 ``service.plan_compensation`` 负责。"""

    job_id: uuid.UUID
    event_count: int
    has_pending: bool
    has_sent: bool
    receive_marker: bool


@dataclass(frozen=True)
class CompensationResult:
    """一次补偿的结果：新建的事件 id 与被标记未确认的 job id。"""

    created_event_ids: tuple[uuid.UUID, ...]
    unconfirmed_job_ids: tuple[uuid.UUID, ...]


class OutboxRepository(Protocol):
    """dispatcher 依赖的最小仓储接口；SQL 与内存实现共享同一语义。"""

    async def claim_due_event(self, *, owner: str) -> ClaimedOutboxEvent | None: ...

    async def mark_sent(self, claim: ClaimedOutboxEvent) -> bool: ...

    async def event_already_received(self, event_id: uuid.UUID) -> bool: ...

    async def defer_event(self, claim: ClaimedOutboxEvent, *, delay_seconds: int) -> bool: ...

    async def fail_event(self, claim: ClaimedOutboxEvent) -> bool: ...

    async def lock_compensation_candidates(
        self, *, limit: int
    ) -> tuple[CompensationCandidate, ...]: ...

    async def create_followup_events(
        self, job_ids: Sequence[uuid.UUID]
    ) -> tuple[uuid.UUID, ...]: ...

    async def flag_delivery_unconfirmed(
        self, job_ids: Sequence[uuid.UUID]
    ) -> tuple[uuid.UUID, ...]: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class SqlOutboxRepository:
    """基于 ``AsyncSession`` 的仓储实现；每个方法只执行 SQL，不做业务判断。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_due_event(self, *, owner: str) -> ClaimedOutboxEvent | None:
        result: Result[Any] = await self._session.execute(
            CLAIM_SQL,
            {"owner": owner, "lease_seconds": protocol.LEASE_DURATION_SECONDS},
        )
        row = result.first()
        if row is None:
            await self._session.commit()
            return None
        claim = ClaimedOutboxEvent(
            event_id=row[0],
            job_id=row[1],
            event_type=row[2],
            dispatch_attempt=row[3],
            lease_owner=row[4],
            lease_token=row[5],
            lease_until=row[6],
        )
        await self._session.commit()
        return claim

    async def mark_sent(self, claim: ClaimedOutboxEvent) -> bool:
        result: Result[Any] = await self._session.execute(
            MARK_SENT_SQL,
            {
                "event_id": claim.event_id,
                "owner": claim.lease_owner,
                "token": claim.lease_token,
            },
        )
        row = result.first()
        if row is None:
            await self._session.commit()
            return False
        # 同一事务内推后 job.next_run_at 作为接收宽限期；回写失败则整体不提交。
        await self._session.execute(
            PUSH_RECEIVE_GRACE_SQL,
            {
                "job_ids": [claim.job_id],
                "grace_seconds": protocol.RECEIVE_GRACE_SECONDS,
            },
        )
        await self._session.commit()
        return True

    async def event_already_received(self, event_id: uuid.UUID) -> bool:
        result: Result[Any] = await self._session.execute(
            CHECK_EVENT_RECEIVED_SQL, {"event_id": event_id}
        )
        received = result.first() is not None
        # 结束只读事务，确保后续网络发送不在任何事务中执行。
        await self._session.commit()
        return received

    async def defer_event(self, claim: ClaimedOutboxEvent, *, delay_seconds: int) -> bool:
        result: Result[Any] = await self._session.execute(
            DEFER_SQL,
            {
                "event_id": claim.event_id,
                "owner": claim.lease_owner,
                "token": claim.lease_token,
                "delay_seconds": delay_seconds,
            },
        )
        updated = result.first() is not None
        await self._session.commit()
        return updated

    async def fail_event(self, claim: ClaimedOutboxEvent) -> bool:
        result: Result[Any] = await self._session.execute(
            FAIL_SQL,
            {
                "event_id": claim.event_id,
                "owner": claim.lease_owner,
                "token": claim.lease_token,
            },
        )
        row = result.first()
        if row is None:
            await self._session.commit()
            return False
        # 未知事件类型对 job 写明确错误码，避免 QUEUED 静默卡死；同事务提交。
        await self._session.execute(
            SET_JOB_ERROR_SQL,
            {
                "job_id": row[1],
                "error_code": protocol.UNSUPPORTED_EVENT_TYPE,
            },
        )
        await self._session.commit()
        return True

    async def lock_compensation_candidates(
        self, *, limit: int
    ) -> tuple[CompensationCandidate, ...]:
        result: Result[Any] = await self._session.execute(
            LOCK_COMPENSATION_CANDIDATES_SQL, {"limit": limit}
        )
        return tuple(
            CompensationCandidate(
                job_id=row[0],
                event_count=row[1],
                has_pending=row[2],
                has_sent=row[3],
                receive_marker=row[4],
            )
            for row in result
        )

    async def create_followup_events(
        self, job_ids: Sequence[uuid.UUID]
    ) -> tuple[uuid.UUID, ...]:
        if not job_ids:
            return ()
        result: Result[Any] = await self._session.execute(
            CREATE_FOLLOWUP_SQL,
            {
                "job_ids": list(job_ids),
                "event_type": protocol.INGEST_REQUESTED_EVENT_TYPE,
            },
        )
        created = tuple(row[0] for row in result)
        # 同事务推后 next_run_at：补投后 60s 内不再把该 job 当作未确认而重复新建。
        await self._session.execute(
            PUSH_RECEIVE_GRACE_SQL,
            {
                "job_ids": list(job_ids),
                "grace_seconds": protocol.RECEIVE_GRACE_SECONDS,
            },
        )
        return created

    async def flag_delivery_unconfirmed(
        self, job_ids: Sequence[uuid.UUID]
    ) -> tuple[uuid.UUID, ...]:
        if not job_ids:
            return ()
        result: Result[Any] = await self._session.execute(
            FLAG_UNCONFIRMED_SQL,
            {"job_ids": list(job_ids), "error_code": protocol.DELIVERY_UNCONFIRMED},
        )
        return tuple(row[0] for row in result)

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()
