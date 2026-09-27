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

# 补偿候选：QUEUED、已到期、未被标记未确认、无 worker 接收标记、无 PENDING 事件，且
# 最近一个 SENT 事件已超出接收宽限的 job。接收宽限以 SENT 的 ``sent_at`` 判断，不再借用
# ``next_run_at``；后者只用于处理中任务恢复后的退避与领取门槛。
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
      AND NOT EXISTS (
          SELECT 1 FROM outbox_event e
          WHERE e.job_id = j.id
            AND e.status = 'SENT'
            AND e.sent_at IS NOT NULL
            AND e.sent_at > now() - (:grace_seconds * interval '1 second')
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

# 处理中任务的过期活动租约恢复候选：只锁定活动阶段、租约非空且已过期、无诊断的 job；
# 已删除文档的 job 不参与。公平排序按最早过期的租约优先，批量上限由调用方限定。
LOCK_EXPIRED_PIPELINE_JOBS_SQL = text(
    """
    SELECT j.id, j.attempt, j.version_id, j.document_id
    FROM ingest_job AS j
    JOIN document AS d ON d.id = j.document_id
    WHERE j.status IN ('PARSING', 'CHUNKING', 'EMBEDDING', 'INDEXING')
      AND j.lease_until IS NOT NULL
      AND j.lease_until <= now()
      AND j.lease_token IS NOT NULL
      AND j.error_code IS NULL
      AND d.deleted_at IS NULL
    ORDER BY j.lease_until, j.id
    FOR UPDATE OF j SKIP LOCKED
    LIMIT :limit
    """
)

# 重排一条已过期的活动任务：清掉旧租约（owner/token/until/heartbeat）并重入 QUEUED，
# 把 backoff 写入 next_run_at；同一语句内原子新建 PENDING 事件，且事件的 next_send_at
# 取同一 next_run_at，保证投递门和领取门都不早于退避。attempt 不回写，由下一次 claim 递增。
REQUEUE_EXPIRED_PIPELINE_JOB_SQL = text(
    """
    WITH requeued AS (
        UPDATE ingest_job
        SET status = 'QUEUED',
            lease_owner = NULL,
            lease_token = NULL,
            lease_until = NULL,
            heartbeat_at = NULL,
            error_code = NULL,
            next_run_at = clock_timestamp() + (:delay_seconds * interval '1 second'),
            updated_at = clock_timestamp()
        WHERE id = :job_id
          AND status IN ('PARSING', 'CHUNKING', 'EMBEDDING', 'INDEXING')
          AND lease_until IS NOT NULL
          AND lease_until <= clock_timestamp()
          AND lease_token IS NOT NULL
          AND error_code IS NULL
        RETURNING id, next_run_at
    )
    INSERT INTO outbox_event (
        id, job_id, event_type, status, dispatch_attempt, next_send_at,
        lease_owner, lease_token, lease_until, sent_at, created_at, updated_at
    )
    SELECT gen_random_uuid(), r.id, :event_type, 'PENDING', 0, r.next_run_at,
           NULL, NULL, NULL, NULL, clock_timestamp(), clock_timestamp()
    FROM requeued AS r
    RETURNING id
    """
)

# 达到恢复上限：静态置 FAILED + PIPELINE_RETRY_EXHAUSTED 并清租约，不改 next_run_at、
# 不建事件；同事务内由调用方再按守卫同步 version/document。
EXHAUST_EXPIRED_PIPELINE_JOB_SQL = text(
    """
    UPDATE ingest_job
    SET status = 'FAILED',
        error_code = :error_code,
        lease_owner = NULL,
        lease_token = NULL,
        lease_until = NULL,
        heartbeat_at = NULL,
        updated_at = clock_timestamp()
    WHERE id = :job_id
      AND status IN ('PARSING', 'CHUNKING', 'EMBEDDING', 'INDEXING')
      AND lease_until IS NOT NULL
      AND lease_until <= clock_timestamp()
      AND lease_token IS NOT NULL
      AND error_code IS NULL
    RETURNING version_id, document_id
    """
)

# 恢复耗尽后同步依赖状态：只把 PENDING version 置 FAILED；文档只在没有 active version 时
# 才置 FAILED，使已有可检索文档继续服务。BUILDING generation 不做 GC（api 角色对其只有
# SELECT），任务下一次成功暂存会绑定到新 generation。
FAIL_EXHAUSTED_VERSION_SQL = text(
    """
    UPDATE document_version
    SET status = 'FAILED', updated_at = clock_timestamp()
    WHERE id = :version_id AND status = 'PENDING'
    """
)

FAIL_EXHAUSTED_DOCUMENT_SQL = text(
    """
    UPDATE document
    SET lifecycle_status = 'FAILED', updated_at = clock_timestamp()
    WHERE id = :document_id
      AND active_version_id IS NULL
      AND lifecycle_status IN ('CREATED', 'INDEXING')
    """
)


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


@dataclass(frozen=True)
class ExpiredPipelineJob:
    """一条处理中且租约已过期的 job 事实；供恢复服务做重排/耗尽决策。"""

    job_id: uuid.UUID
    attempt: int
    version_id: uuid.UUID
    document_id: uuid.UUID


@dataclass(frozen=True)
class PipelineRecoveryResult:
    """一次恢复扫描的结果：重排的 job id 与耗尽失败的 job id。"""

    requeued_job_ids: tuple[uuid.UUID, ...]
    exhausted_job_ids: tuple[uuid.UUID, ...]


class OutboxRepository(Protocol):
    """dispatcher 依赖的最小仓储接口；SQL 与内存实现共享同一语义。"""

    async def claim_due_event(self, *, owner: str) -> ClaimedOutboxEvent | None: ...

    async def mark_sent(self, claim: ClaimedOutboxEvent) -> bool: ...

    async def event_already_received(self, event_id: uuid.UUID) -> bool: ...

    async def defer_event(self, claim: ClaimedOutboxEvent, *, delay_seconds: int) -> bool: ...

    async def fail_event(self, claim: ClaimedOutboxEvent) -> bool: ...

    async def lock_compensation_candidates(
        self, *, limit: int, grace_seconds: int
    ) -> tuple[CompensationCandidate, ...]: ...

    async def create_followup_events(
        self, job_ids: Sequence[uuid.UUID]
    ) -> tuple[uuid.UUID, ...]: ...

    async def flag_delivery_unconfirmed(
        self, job_ids: Sequence[uuid.UUID]
    ) -> tuple[uuid.UUID, ...]: ...

    async def lock_expired_pipeline_jobs(
        self, *, limit: int
    ) -> tuple[ExpiredPipelineJob, ...]: ...

    async def requeue_expired_pipeline_job(
        self, job_id: uuid.UUID, *, delay_seconds: int
    ) -> uuid.UUID | None: ...

    async def exhaust_expired_pipeline_job(self, job_id: uuid.UUID) -> bool: ...

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
        self, *, limit: int, grace_seconds: int
    ) -> tuple[CompensationCandidate, ...]:
        result: Result[Any] = await self._session.execute(
            LOCK_COMPENSATION_CANDIDATES_SQL,
            {"limit": limit, "grace_seconds": grace_seconds},
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
        # 补投后的接收宽限由补偿候选按 SENT.sent_at 判断，不再回写 next_run_at。
        return tuple(row[0] for row in result)

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

    async def lock_expired_pipeline_jobs(
        self, *, limit: int
    ) -> tuple[ExpiredPipelineJob, ...]:
        result: Result[Any] = await self._session.execute(
            LOCK_EXPIRED_PIPELINE_JOBS_SQL, {"limit": limit}
        )
        return tuple(
            ExpiredPipelineJob(
                job_id=row[0],
                attempt=int(row[1]),
                version_id=row[2],
                document_id=row[3],
            )
            for row in result
        )

    async def requeue_expired_pipeline_job(
        self, job_id: uuid.UUID, *, delay_seconds: int
    ) -> uuid.UUID | None:
        result: Result[Any] = await self._session.execute(
            REQUEUE_EXPIRED_PIPELINE_JOB_SQL,
            {
                "job_id": job_id,
                "delay_seconds": delay_seconds,
                "event_type": protocol.INGEST_REQUESTED_EVENT_TYPE,
            },
        )
        row = result.first()
        return None if row is None else row[0]

    async def exhaust_expired_pipeline_job(self, job_id: uuid.UUID) -> bool:
        result: Result[Any] = await self._session.execute(
            EXHAUST_EXPIRED_PIPELINE_JOB_SQL,
            {"job_id": job_id, "error_code": protocol.PIPELINE_RETRY_EXHAUSTED},
        )
        row = result.first()
        if row is None:
            return False
        version_id, document_id = row[0], row[1]
        await self._session.execute(
            FAIL_EXHAUSTED_VERSION_SQL, {"version_id": version_id}
        )
        await self._session.execute(
            FAIL_EXHAUSTED_DOCUMENT_SQL, {"document_id": document_id}
        )
        return True

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()
