"""dispatcher 核心的确定性单测：不连接 PostgreSQL 或 Redis。

真实的 ``FOR UPDATE SKIP LOCKED`` 竞争与 SQL CAS 由
``tests/integration/test_dispatcher_flow.py`` 在隔离测试库上验收；这里用与仓储同一语义的
内存实现覆盖投递编排、退避、迟到租约回写、回写失败与补偿计划。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from rag_backend.dispatch import protocol
from rag_backend.dispatch.repository import (
    ClaimedOutboxEvent,
    CompensationCandidate,
    CompensationResult,
)
from rag_backend.dispatch.service import (
    FAILURE_WARN_ATTEMPTS,
    FAILURE_WARN_INTERVAL,
    DispatchOutcome,
    OutboxDispatcher,
    _should_warn,
    compensate_unconfirmed_jobs,
    dispatch_one,
    plan_compensation,
)

BASE_TIME = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeClock:
    def __init__(self, start: datetime = BASE_TIME) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


@dataclass
class FakeEvent:
    id: uuid.UUID
    job_id: uuid.UUID
    event_type: str = protocol.INGEST_REQUESTED_EVENT_TYPE
    status: str = "PENDING"
    dispatch_attempt: int = 0
    next_send_at: datetime = BASE_TIME
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_until: datetime | None = None
    sent_at: datetime | None = None


@dataclass
class FakeJob:
    id: uuid.UUID
    status: str = "QUEUED"
    next_run_at: datetime = BASE_TIME
    error_code: str | None = None
    lease_owner: str | None = None
    heartbeat_at: datetime | None = None


class FakeOutboxRepository:
    """与 SQL 仓储同语义的内存实现：到期/无有效租约领取、token CAS、有界补偿。"""

    def __init__(self, *, clock: FakeClock) -> None:
        self._clock = clock
        self.events: list[FakeEvent] = []
        self.jobs: list[FakeJob] = []
        self.commits = 0
        self.rollbacks = 0

    def add_event(self, job_id: uuid.UUID, **overrides: Any) -> FakeEvent:
        values: dict[str, Any] = {
            "id": uuid.uuid4(),
            "job_id": job_id,
            "next_send_at": self._clock.now(),
        }
        values.update(overrides)
        event = FakeEvent(**values)
        self.events.append(event)
        return event

    def add_job(self, **overrides: Any) -> FakeJob:
        values: dict[str, Any] = {"id": uuid.uuid4(), "next_run_at": self._clock.now()}
        values.update(overrides)
        job = FakeJob(**values)
        self.jobs.append(job)
        return job

    def _find(self, event_id: uuid.UUID) -> FakeEvent:
        for event in self.events:
            if event.id == event_id:
                return event
        raise AssertionError(f"未找到事件 {event_id}")

    def _has_pending(self, job_id: uuid.UUID) -> bool:
        return any(
            event.job_id == job_id and event.status == "PENDING" for event in self.events
        )

    async def claim_due_event(self, *, owner: str) -> ClaimedOutboxEvent | None:
        now = self._clock.now()
        due = [
            event
            for event in self.events
            if event.status == "PENDING"
            and event.next_send_at <= now
            and (event.lease_until is None or event.lease_until <= now)
        ]
        due.sort(key=lambda event: (event.next_send_at, str(event.id)))
        if not due:
            return None
        event = due[0]
        event.dispatch_attempt += 1
        event.lease_owner = owner
        event.lease_token = protocol.lease_token_for(event.dispatch_attempt)
        event.lease_until = now + timedelta(seconds=protocol.LEASE_DURATION_SECONDS)
        return ClaimedOutboxEvent(
            event_id=event.id,
            job_id=event.job_id,
            event_type=event.event_type,
            dispatch_attempt=event.dispatch_attempt,
            lease_owner=owner,
            lease_token=event.lease_token,
            lease_until=event.lease_until,
        )

    def _holds(self, event: FakeEvent, claim: ClaimedOutboxEvent) -> bool:
        return (
            event.status == "PENDING"
            and event.lease_owner == claim.lease_owner
            and event.lease_token == claim.lease_token
        )

    async def mark_sent(self, claim: ClaimedOutboxEvent) -> bool:
        event = self._find(claim.event_id)
        assert event.lease_until is not None
        if not self._holds(event, claim) or event.lease_until <= self._clock.now():
            return False
        event.status = "SENT"
        event.sent_at = self._clock.now()
        event.lease_owner = None
        event.lease_token = None
        event.lease_until = None
        self._push_grace([event.job_id])
        return True

    async def event_already_received(self, event_id: uuid.UUID) -> bool:
        event = self._find(event_id)
        for job in self.jobs:
            if job.id == event.job_id:
                return (
                    job.lease_owner == protocol.receive_marker_owner(event_id)
                    and job.heartbeat_at is not None
                )
        return False

    def _push_grace(self, job_ids: Sequence[uuid.UUID]) -> None:
        floor = self._clock.now() + timedelta(seconds=protocol.RECEIVE_GRACE_SECONDS)
        for job in self.jobs:
            if job.id in job_ids and job.status == "QUEUED" and job.error_code is None:
                job.next_run_at = max(job.next_run_at, floor)

    async def defer_event(self, claim: ClaimedOutboxEvent, *, delay_seconds: int) -> bool:
        event = self._find(claim.event_id)
        if not self._holds(event, claim):
            return False
        event.next_send_at = self._clock.now() + timedelta(seconds=delay_seconds)
        event.lease_owner = None
        event.lease_token = None
        event.lease_until = None
        return True

    async def fail_event(self, claim: ClaimedOutboxEvent) -> bool:
        event = self._find(claim.event_id)
        if not self._holds(event, claim):
            return False
        event.status = "FAILED"
        event.lease_owner = None
        event.lease_token = None
        event.lease_until = None
        for job in self.jobs:
            if job.id == event.job_id and job.status == "QUEUED" and job.error_code is None:
                job.error_code = protocol.UNSUPPORTED_EVENT_TYPE
        return True

    async def lock_compensation_candidates(
        self, *, limit: int
    ) -> tuple[CompensationCandidate, ...]:
        now = self._clock.now()
        candidates: list[CompensationCandidate] = []
        for job in self.jobs:
            if job.status != "QUEUED" or job.error_code is not None:
                continue
            if job.next_run_at > now:
                continue
            if job.lease_owner is not None or job.heartbeat_at is not None:
                continue
            if self._has_pending(job.id):
                continue
            job_events = [event for event in self.events if event.job_id == job.id]
            candidates.append(
                CompensationCandidate(
                    job_id=job.id,
                    event_count=len(job_events),
                    has_pending=any(e.status == "PENDING" for e in job_events),
                    has_sent=any(e.status == "SENT" for e in job_events),
                    receive_marker=False,
                )
            )
            if len(candidates) >= limit:
                break
        return tuple(candidates)

    async def create_followup_events(
        self, job_ids: Sequence[uuid.UUID]
    ) -> tuple[uuid.UUID, ...]:
        created: list[uuid.UUID] = []
        for job_id in job_ids:
            event = self.add_event(job_id)
            created.append(event.id)
        self._push_grace(job_ids)
        return tuple(created)

    async def flag_delivery_unconfirmed(
        self, job_ids: Sequence[uuid.UUID]
    ) -> tuple[uuid.UUID, ...]:
        flagged: list[uuid.UUID] = []
        for job_id in job_ids:
            for job in self.jobs:
                if job.id == job_id and job.error_code is None:
                    job.error_code = protocol.DELIVERY_UNCONFIRMED
                    flagged.append(job.id)
        return tuple(flagged)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class FakePublisher:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object], str, str]] = []
        self._error = error

    async def publish(
        self,
        *,
        task_name: str,
        payload: dict[str, object],
        task_id: str,
        queue: str,
    ) -> None:
        if self._error is not None:
            raise self._error
        self.calls.append((task_name, payload, task_id, queue))


# --- 纯策略 -------------------------------------------------------------------


def test_build_dispatch_payload_only_carries_job_and_protocol_version() -> None:
    job_id = uuid.uuid4()
    payload = protocol.build_dispatch_payload(job_id)
    assert payload == {"protocolVersion": 1, "jobId": str(job_id)}
    assert set(payload) == {"protocolVersion", "jobId"}


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [(1, 5), (2, 10), (3, 20), (4, 40), (5, 80), (6, 160), (7, 300), (50, 300)],
)
def test_retry_delay_is_exponential_and_capped(attempt: int, expected: int) -> None:
    assert protocol.retry_delay_seconds(attempt) == expected


def test_retry_delay_rejects_non_positive_attempt() -> None:
    with pytest.raises(ValueError):
        protocol.retry_delay_seconds(0)


def test_lease_token_is_monotonic_string() -> None:
    assert protocol.lease_token_for(1) == "1"
    assert protocol.lease_token_for(2) == "2"


def test_receive_marker_detection() -> None:
    event_id = uuid.uuid4()
    assert protocol.receive_marker_owner(event_id) == f"event:{event_id}"
    assert protocol.has_receive_marker(
        lease_owner=protocol.receive_marker_owner(event_id), heartbeat_at=None
    )
    assert protocol.has_receive_marker(
        lease_owner=None, heartbeat_at=BASE_TIME
    )
    assert not protocol.has_receive_marker(lease_owner="dispatcher:abc", heartbeat_at=None)
    assert not protocol.has_receive_marker(lease_owner=None, heartbeat_at=None)


def test_default_lease_owner_is_prefixed() -> None:
    assert protocol.default_lease_owner().startswith("dispatcher:")


def test_unknown_event_type_is_not_supported() -> None:
    assert protocol.is_supported_event_type(protocol.INGEST_REQUESTED_EVENT_TYPE)
    assert not protocol.is_supported_event_type("unknown.event")


# --- 投递编排 -----------------------------------------------------------------


@pytest.mark.anyio
async def test_dispatch_once_sends_only_job_and_protocol_version() -> None:
    clock = FakeClock()
    repo = FakeOutboxRepository(clock=clock)
    job = repo.add_job()
    event = repo.add_event(job.id)
    publisher = FakePublisher()

    outcome = await dispatch_one(repo, publisher, owner="dispatcher:a")

    assert outcome is DispatchOutcome.SENT
    assert event.status == "SENT"
    assert event.lease_owner is None and event.lease_token is None and event.lease_until is None
    assert publisher.calls == [
        (
            protocol.INGEST_TASK_NAME,
            {"protocolVersion": 1, "jobId": str(job.id)},
            str(event.id),
            protocol.INGEST_QUEUE,
        )
    ]


@pytest.mark.anyio
async def test_dispatch_once_is_idle_when_nothing_due() -> None:
    clock = FakeClock()
    repo = FakeOutboxRepository(clock=clock)
    job = repo.add_job()
    repo.add_event(job.id, next_send_at=clock.now() + timedelta(seconds=30))
    publisher = FakePublisher()

    assert await dispatch_one(repo, publisher, owner="dispatcher:a") is DispatchOutcome.IDLE
    assert publisher.calls == []


@pytest.mark.anyio
async def test_claim_competition_only_dispatches_each_event_once() -> None:
    clock = FakeClock()
    repo = FakeOutboxRepository(clock=clock)
    job = repo.add_job()
    first = repo.add_event(job.id)
    second = repo.add_event(job.id)
    publisher = FakePublisher()

    assert await dispatch_one(repo, publisher, owner="dispatcher:a") is DispatchOutcome.SENT
    assert await dispatch_one(repo, publisher, owner="dispatcher:a") is DispatchOutcome.SENT
    # 第三个调用没有可领取事件，不能重复投递。
    assert await dispatch_one(repo, publisher, owner="dispatcher:a") is DispatchOutcome.IDLE

    sent_ids = {call[2] for call in publisher.calls}
    assert sent_ids == {str(first.id), str(second.id)}
    assert len(publisher.calls) == 2


@pytest.mark.anyio
async def test_claim_is_blocked_while_lease_is_valid() -> None:
    clock = FakeClock()
    repo = FakeOutboxRepository(clock=clock)
    job = repo.add_job()
    repo.add_event(job.id)
    publisher = FakePublisher()

    assert await dispatch_one(repo, publisher, owner="dispatcher:a") is DispatchOutcome.SENT
    assert await dispatch_one(repo, publisher, owner="dispatcher:b") is DispatchOutcome.IDLE
    assert len(publisher.calls) == 1


@pytest.mark.anyio
async def test_send_failure_defers_with_backoff_and_stays_pending() -> None:
    clock = FakeClock()
    repo = FakeOutboxRepository(clock=clock)
    job = repo.add_job()
    event = repo.add_event(job.id)
    publisher = FakePublisher(error=ConnectionError("redis down"))

    outcome = await dispatch_one(repo, publisher, owner="dispatcher:a")

    assert outcome is DispatchOutcome.DEFERRED
    assert event.status == "PENDING"
    assert event.dispatch_attempt == 1
    assert event.lease_owner is None and event.lease_token is None
    assert event.next_send_at == clock.now() + timedelta(
        seconds=protocol.retry_delay_seconds(1)
    )


@pytest.mark.anyio
async def test_redelivery_after_failure_uses_growing_backoff() -> None:
    clock = FakeClock()
    repo = FakeOutboxRepository(clock=clock)
    job = repo.add_job()
    event = repo.add_event(job.id)
    failing = FakePublisher(error=ConnectionError("redis down"))

    assert await dispatch_one(repo, failing, owner="dispatcher:a") is DispatchOutcome.DEFERRED
    assert event.next_send_at == clock.now() + timedelta(seconds=5)

    clock.advance(5)
    assert await dispatch_one(repo, failing, owner="dispatcher:a") is DispatchOutcome.DEFERRED
    assert event.dispatch_attempt == 2
    assert event.next_send_at == clock.now() + timedelta(seconds=10)


@pytest.mark.anyio
async def test_late_send_after_lease_expired_cannot_overwrite_new_claim() -> None:
    clock = FakeClock()
    repo = FakeOutboxRepository(clock=clock)
    job = repo.add_job()
    event = repo.add_event(job.id)

    stale = await repo.claim_due_event(owner="dispatcher:old")
    assert stale is not None

    # 旧租约过期后新 dispatcher 重新领取同一事件，token 单调递增。
    clock.advance(protocol.LEASE_DURATION_SECONDS + 1)
    fresh = await repo.claim_due_event(owner="dispatcher:new")
    assert fresh is not None
    assert fresh.dispatch_attempt == stale.dispatch_attempt + 1
    assert fresh.lease_token != stale.lease_token

    # 旧发送者的回写被 CAS 拒绝，事件仍由新租约持有。
    assert await repo.mark_sent(stale) is False
    assert event.status == "PENDING"
    assert event.lease_owner == "dispatcher:new"
    assert event.lease_token == fresh.lease_token

    # 新发送者回写成功。
    assert await repo.mark_sent(fresh) is True
    assert event.status == "SENT"


class _WriteBackFailingRepository(FakeOutboxRepository):
    """首次 SENT 回写抛错，用于覆盖“发送成功但回写失败”。"""

    def __init__(self, *, clock: FakeClock) -> None:
        super().__init__(clock=clock)
        self.fail_next_mark_sent = True

    async def mark_sent(self, claim: ClaimedOutboxEvent) -> bool:
        if self.fail_next_mark_sent:
            self.fail_next_mark_sent = False
            raise RuntimeError("db write-back failed")
        return await super().mark_sent(claim)


@pytest.mark.anyio
async def test_send_success_then_writeback_failure_stays_pending_for_redelivery() -> None:
    clock = FakeClock()
    repo = _WriteBackFailingRepository(clock=clock)
    job = repo.add_job()
    event = repo.add_event(job.id)
    publisher = FakePublisher()

    assert await dispatch_one(repo, publisher, owner="dispatcher:a") is DispatchOutcome.DEFERRED
    assert event.status == "PENDING"
    assert len(publisher.calls) == 1

    # 租约未过期时不会被第二个 dispatcher 重复领取。
    assert await dispatch_one(repo, publisher, owner="dispatcher:b") is DispatchOutcome.IDLE

    # 租约过期后补投，最终 SENT。
    clock.advance(protocol.LEASE_DURATION_SECONDS + 1)
    assert await dispatch_one(repo, publisher, owner="dispatcher:b") is DispatchOutcome.SENT
    assert event.status == "SENT"
    assert len(publisher.calls) == 2


@pytest.mark.anyio
async def test_unknown_event_type_is_failed_without_publishing() -> None:
    clock = FakeClock()
    repo = FakeOutboxRepository(clock=clock)
    job = repo.add_job()
    event = repo.add_event(job.id, event_type="mystery.event")
    publisher = FakePublisher()

    assert await dispatch_one(repo, publisher, owner="dispatcher:a") is DispatchOutcome.FAILED
    assert event.status == "FAILED"
    assert publisher.calls == []


# --- 补偿计划（纯函数） --------------------------------------------------------


def test_plan_compensation_creates_followup_for_unconfirmed_sent() -> None:
    job_id = uuid.uuid4()
    plan = plan_compensation([CompensationCandidate(job_id, 1, False, True, False)])
    assert plan.followup_job_ids == (job_id,)
    assert plan.unconfirmed_job_ids == ()


def test_plan_compensation_skips_pending_and_received() -> None:
    pending = CompensationCandidate(uuid.uuid4(), 1, True, False, False)
    received = CompensationCandidate(uuid.uuid4(), 1, False, True, True)
    plan = plan_compensation([pending, received])
    assert plan.followup_job_ids == ()
    assert plan.unconfirmed_job_ids == ()


def test_plan_compensation_flags_job_at_cap() -> None:
    job_id = uuid.uuid4()
    plan = plan_compensation(
        [CompensationCandidate(job_id, protocol.MAX_DELIVERY_ATTEMPTS, False, True, False)]
    )
    assert plan.followup_job_ids == ()
    assert plan.unconfirmed_job_ids == (job_id,)


# --- 补偿编排 -----------------------------------------------------------------


@pytest.mark.anyio
async def test_compensation_creates_single_followup_and_avoids_duplicate() -> None:
    clock = FakeClock()
    repo = FakeOutboxRepository(clock=clock)
    job = repo.add_job()
    repo.add_event(job.id, status="SENT")

    first = await compensate_unconfirmed_jobs(repo)
    assert len(first.created_event_ids) == 1
    assert len(repo.events) == 2
    assert repo.events[0].status == "SENT"  # 旧 SENT 保留

    second = await compensate_unconfirmed_jobs(repo)
    assert second == CompensationResult((), ())
    assert len(repo.events) == 2


@pytest.mark.anyio
async def test_compensation_flags_job_after_reaching_cap() -> None:
    clock = FakeClock()
    repo = FakeOutboxRepository(clock=clock)
    job = repo.add_job()
    for _ in range(protocol.MAX_DELIVERY_ATTEMPTS):
        repo.add_event(job.id, status="SENT")

    result = await compensate_unconfirmed_jobs(repo)
    assert result.created_event_ids == ()
    assert result.unconfirmed_job_ids == (job.id,)
    assert job.error_code == protocol.DELIVERY_UNCONFIRMED
    assert job.status == "QUEUED"  # 不置为 READY/PARSING/FAILED

    again = await compensate_unconfirmed_jobs(repo)
    assert again == CompensationResult((), ())


@pytest.mark.anyio
async def test_compensation_skips_job_with_receive_marker() -> None:
    clock = FakeClock()
    repo = FakeOutboxRepository(clock=clock)
    job = repo.add_job()
    sent = repo.add_event(job.id, status="SENT")
    job.lease_owner = protocol.receive_marker_owner(sent.id)

    result = await compensate_unconfirmed_jobs(repo)
    assert result.created_event_ids == ()
    assert len(repo.events) == 1


# --- P1 回归：接收宽限、上限、无限退避、对账、未知类型 -----------------------------


@pytest.mark.anyio
async def test_sent_event_pushes_receive_grace_before_compensation() -> None:
    clock = FakeClock()
    repo = FakeOutboxRepository(clock=clock)
    job = repo.add_job()
    repo.add_event(job.id)
    publisher = FakePublisher()

    assert await dispatch_one(repo, publisher, owner="dispatcher:a") is DispatchOutcome.SENT
    assert job.next_run_at >= clock.now() + timedelta(seconds=protocol.RECEIVE_GRACE_SECONDS)

    # 宽限期内 worker 尚未写 marker，补偿也不得新建事件。
    assert (await compensate_unconfirmed_jobs(repo)).created_event_ids == ()

    # 宽限期结束后才按计划补投一次。
    clock.advance(protocol.RECEIVE_GRACE_SECONDS)
    assert len((await compensate_unconfirmed_jobs(repo)).created_event_ids) == 1


@pytest.mark.anyio
async def test_compensation_respects_grace_then_stops_at_cap() -> None:
    clock = FakeClock()
    repo = FakeOutboxRepository(clock=clock)
    job = repo.add_job()
    repo.add_event(job.id, status="SENT")

    # 每个宽限周期只补投一次，累计到上限后标记未确认并停止。
    for _ in range(protocol.MAX_DELIVERY_ATTEMPTS - 1):
        created = (await compensate_unconfirmed_jobs(repo)).created_event_ids
        assert len(created) == 1
        for event in repo.events:
            if event.id == created[0]:
                event.status = "SENT"
        clock.advance(protocol.RECEIVE_GRACE_SECONDS)

    result = await compensate_unconfirmed_jobs(repo)
    assert result.created_event_ids == ()
    assert result.unconfirmed_job_ids == (job.id,)
    assert job.error_code == protocol.DELIVERY_UNCONFIRMED


@pytest.mark.anyio
async def test_redis_outage_keeps_pending_with_capped_backoff() -> None:
    clock = FakeClock()
    repo = FakeOutboxRepository(clock=clock)
    job = repo.add_job()
    event = repo.add_event(job.id)
    failing = FakePublisher(error=ConnectionError("redis down"))

    delays: list[float] = []
    for _ in range(12):
        assert await dispatch_one(repo, failing, owner="dispatcher:a") is DispatchOutcome.DEFERRED
        delays.append((event.next_send_at - clock.now()).total_seconds())
        assert event.status == "PENDING"
        clock.advance((event.next_send_at - clock.now()).total_seconds())

    assert delays[:6] == [5.0, 10.0, 20.0, 40.0, 80.0, 160.0]
    assert set(delays[6:]) == {300.0}
    assert event.status == "PENDING"  # 长时间故障不假成功、不落 FAILED
    assert event.lease_owner is None and event.lease_token is None


@pytest.mark.anyio
async def test_already_received_event_is_reconciled_without_publishing() -> None:
    clock = FakeClock()
    repo = FakeOutboxRepository(clock=clock)
    job = repo.add_job()
    event = repo.add_event(job.id)
    # 模拟 worker 已对该 eventId 写下 lease_owner 与 heartbeat。
    job.lease_owner = protocol.receive_marker_owner(event.id)
    job.heartbeat_at = clock.now()
    publisher = FakePublisher()

    assert await dispatch_one(repo, publisher, owner="dispatcher:a") is DispatchOutcome.SENT
    assert event.status == "SENT"
    assert publisher.calls == []


@pytest.mark.anyio
async def test_unknown_event_type_marks_job_error_code() -> None:
    clock = FakeClock()
    repo = FakeOutboxRepository(clock=clock)
    job = repo.add_job()
    event = repo.add_event(job.id, event_type="mystery.event")
    publisher = FakePublisher()

    assert await dispatch_one(repo, publisher, owner="dispatcher:a") is DispatchOutcome.FAILED
    assert event.status == "FAILED"
    assert job.error_code == protocol.UNSUPPORTED_EVENT_TYPE
    assert job.status == "QUEUED"
    assert publisher.calls == []


# --- 对账精确匹配与脱敏日志 -----------------------------------------------------


def test_failure_log_gating_limits_noise() -> None:
    assert _should_warn(1) and _should_warn(FAILURE_WARN_ATTEMPTS)
    assert not _should_warn(FAILURE_WARN_ATTEMPTS + 1)
    assert _should_warn(FAILURE_WARN_INTERVAL)
    assert not _should_warn(FAILURE_WARN_INTERVAL + 1)


@pytest.mark.anyio
async def test_other_event_heartbeat_does_not_reconcile_current_event() -> None:
    clock = FakeClock()
    repo = FakeOutboxRepository(clock=clock)
    job = repo.add_job()
    event = repo.add_event(job.id)
    # heartbeat 存在，但 lease_owner 指向别的事件：当前事件仍需正常投递。
    job.lease_owner = protocol.receive_marker_owner(uuid.uuid4())
    job.heartbeat_at = clock.now()
    publisher = FakePublisher()

    assert await dispatch_one(repo, publisher, owner="dispatcher:a") is DispatchOutcome.SENT
    assert len(publisher.calls) == 1
    assert event.status == "SENT"


@pytest.mark.anyio
async def test_send_failure_logs_sanitized_signal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock()
    repo = FakeOutboxRepository(clock=clock)
    job = repo.add_job()
    repo.add_event(job.id)
    publisher = FakePublisher(error=ConnectionError("redis://user:secret@host/0"))

    with caplog.at_level(logging.WARNING, logger="rag_backend.dispatch.service"):
        assert await dispatch_one(repo, publisher, owner="dispatcher:a") is DispatchOutcome.DEFERRED

    messages = [record.getMessage() for record in caplog.records]
    assert any("phase=send" in message and "ConnectionError" in message for message in messages)
    # 原始异常消息（含潜在 DSN/凭据）不得进入日志。
    assert all("secret" not in message for message in messages)


@pytest.mark.anyio
async def test_writeback_failure_logs_sanitized_signal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock()
    repo = _WriteBackFailingRepository(clock=clock)
    job = repo.add_job()
    repo.add_event(job.id)
    publisher = FakePublisher()

    with caplog.at_level(logging.WARNING, logger="rag_backend.dispatch.service"):
        assert await dispatch_one(repo, publisher, owner="dispatcher:a") is DispatchOutcome.DEFERRED

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "phase=writeback" in message and "RuntimeError" in message for message in messages
    )
    assert all("db write-back failed" not in message for message in messages)


@pytest.mark.anyio
async def test_delivery_unconfirmed_logs_job_id(caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock()
    repo = FakeOutboxRepository(clock=clock)
    job = repo.add_job()
    for _ in range(protocol.MAX_DELIVERY_ATTEMPTS):
        repo.add_event(job.id, status="SENT")

    with caplog.at_level(logging.WARNING, logger="rag_backend.dispatch.service"):
        result = await compensate_unconfirmed_jobs(repo)

    assert result.unconfirmed_job_ids == (job.id,)
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "delivery unconfirmed" in message and str(job.id) in message for message in messages
    )


class _RaisingDispatcher(OutboxDispatcher):
    """覆盖 run 的异常路径；不调用父类 __init__，避免需要真实 Session 工厂。"""

    def __init__(self) -> None:
        self.calls: int = 0

    async def dispatch_once(self) -> DispatchOutcome:
        self.calls += 1
        raise RuntimeError("boom-dsn")

    async def compensate(self) -> CompensationResult:
        raise RuntimeError("boom-dsn")


@pytest.mark.anyio
async def test_run_keeps_going_and_logs_sanitized_loop_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    dispatcher = _RaisingDispatcher()
    with caplog.at_level(logging.WARNING, logger="rag_backend.dispatch.service"):
        task = asyncio.create_task(dispatcher.run())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert dispatcher.calls >= 1
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "dispatcher loop failure" in message and "RuntimeError" in message
        for message in messages
    )
    assert all("boom-dsn" not in message for message in messages)


class _DeferredDispatcher(OutboxDispatcher):
    """每次 dispatch_once 返回 DEFERRED；不调用父类 __init__。"""

    def __init__(self) -> None:
        self.calls: int = 0

    async def dispatch_once(self) -> DispatchOutcome:
        self.calls += 1
        return DispatchOutcome.DEFERRED

    async def compensate(self) -> CompensationResult:
        return CompensationResult((), ())


class _ScriptedDispatcher(OutboxDispatcher):
    """前 N 次返回 SENT，之后 IDLE；用于确认正常路径不会过早停止。"""

    def __init__(self, sent_count: int) -> None:
        self.calls: int = 0
        self.sent_count = sent_count

    async def dispatch_once(self) -> DispatchOutcome:
        self.calls += 1
        return DispatchOutcome.SENT if self.calls <= self.sent_count else DispatchOutcome.IDLE

    async def compensate(self) -> CompensationResult:
        return CompensationResult((), ())


@pytest.mark.anyio
async def test_run_stops_sending_after_first_deferred() -> None:
    """broker 发布失败（DEFERRED）后本轮立即停止，不再对剩余 N 个事件依次尝试。"""

    dispatcher = _DeferredDispatcher()
    task = asyncio.create_task(dispatcher.run())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert dispatcher.calls == 1  # 不是按 DISPATCH_BATCH_PER_CYCLE 次热循环


@pytest.mark.anyio
async def test_run_keeps_sending_while_events_are_sent() -> None:
    """SENT 不被当作故障；会继续投递到 IDLE 才停止本轮。"""

    dispatcher = _ScriptedDispatcher(sent_count=3)
    task = asyncio.create_task(dispatcher.run())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert dispatcher.calls == 4  # 3 次 SENT + 1 次 IDLE
