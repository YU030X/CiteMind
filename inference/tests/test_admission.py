"""有界准入回归。

覆盖三件事：tokenize 与 encode 共享同一个有界许可；队列满时立即返回可重试 503；许可
只在真实 CPU 线程结束后归还，HTTP 等待被取消不会放宽并发。
"""

import asyncio
import threading
import time

import pytest
from asgi_driver import call_embed, json_body, running_app, wait_until
from support import StubEmbedder, build_settings

from citemind_inference.admission import (
    CpuGate,
    EmbeddingBusyError,
    EmbeddingQueueTimeoutError,
    EmbeddingTaskRegistry,
)

# ---------------------------------------------------------------- CpuGate 单元行为


def test_gate_grants_immediately_while_capacity_remains() -> None:
    async def scenario() -> int:
        gate = CpuGate(concurrency=1, queue_depth=1)
        await gate.acquire(1.0)
        assert gate.active == 1
        gate.release()
        return gate.active

    assert asyncio.run(scenario()) == 0


def test_gate_rejects_immediately_when_queue_is_full() -> None:
    async def scenario() -> None:
        gate = CpuGate(concurrency=1, queue_depth=1)
        await gate.acquire(1.0)
        waiter = asyncio.create_task(gate.acquire(5.0))
        await wait_until(lambda: gate.waiting == 1)

        started = time.monotonic()
        with pytest.raises(EmbeddingBusyError):
            await gate.acquire(5.0)
        # 队列满必须立即失败，而不是等配置的等待时间。
        assert time.monotonic() - started < 0.5

        gate.release()  # 许可转交给排队者，active 保持不变
        await waiter
        assert gate.active == 1
        gate.release()
        assert gate.active == 0

    asyncio.run(scenario())


def test_gate_timeout_releases_the_queue_slot() -> None:
    async def scenario() -> None:
        gate = CpuGate(concurrency=1, queue_depth=1)
        await gate.acquire(1.0)

        with pytest.raises(EmbeddingQueueTimeoutError):
            await gate.acquire(0.02)
        assert gate.waiting == 0

        # 超时后超时者必须彻底离队，release 只归还一个许可。
        gate.release()
        assert gate.active == 0

    asyncio.run(scenario())


def test_gate_handles_cancelled_waiter_without_leaking() -> None:
    async def scenario() -> None:
        gate = CpuGate(concurrency=1, queue_depth=2)
        await gate.acquire(1.0)

        first = asyncio.create_task(gate.acquire(5.0))
        second = asyncio.create_task(gate.acquire(5.0))
        await wait_until(lambda: gate.waiting == 2)

        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert gate.waiting == 1

        gate.release()  # 只应转交给 second
        await second
        assert gate.active == 1
        assert gate.waiting == 0

        gate.release()
        assert gate.active == 0
        # 取消不会让 release 多归还许可。
        with pytest.raises(RuntimeError):
            gate.release()

    asyncio.run(scenario())


# ---------------------------------------------------------------- 任务登记表


def test_task_registry_keeps_a_reference_and_drains() -> None:
    async def scenario() -> None:
        registry = EmbeddingTaskRegistry()
        finished: list[str] = []

        async def slow() -> None:
            await asyncio.sleep(0.05)
            finished.append("done")

        registry.start(slow())
        assert registry.pending == 1
        await registry.drain(2.0)
        assert registry.pending == 0
        assert finished == ["done"]
        assert registry.failure_count == 0

    asyncio.run(scenario())


def test_task_registry_observes_unretrieved_failures() -> None:
    async def scenario() -> None:
        registry = EmbeddingTaskRegistry()

        async def boom() -> None:
            raise RuntimeError("cpu 阶段失败")

        registry.start(boom())
        await wait_until(lambda: registry.pending == 0)
        await registry.drain(1.0)
        # 失败被登记表取走并计数，不会变成“未观察异常”。
        assert registry.failure_count == 1
        assert "cpu 阶段失败" in str(registry.failures[-1])

    asyncio.run(scenario())


# ---------------------------------------------------------------- 应用级准入


def test_tokenize_and_encode_run_under_the_same_bounded_permit() -> None:
    count_gate = threading.Event()
    stub = StubEmbedder(count_gate=count_gate)
    settings = build_settings(
        embedding_max_concurrency=1,
        embedding_queue_depth=4,
        embedding_queue_wait_seconds=10.0,
    )

    async def scenario() -> None:
        async with running_app(settings, stub) as app:
            requests = [
                asyncio.create_task(call_embed(app, chunks=[json_body([f"text-{index}"])]))
                for index in range(4)
            ]
            # 第一个请求卡在 tokenize；其余三个必须留在队列里，不能并行 tokenize。
            await wait_until(lambda: len(stub.token_count_calls) >= 1)
            await asyncio.sleep(0.1)
            assert stub.max_in_flight == 1
            assert not any(request.done() for request in requests)

            count_gate.set()
            responses = await asyncio.gather(*requests)

            assert [response.status for response in responses] == [200, 200, 200, 200]

    asyncio.run(scenario())
    assert stub.max_in_flight == 1


def test_full_queue_returns_immediate_retryable_503() -> None:
    embed_gate = threading.Event()
    stub = StubEmbedder(embed_gate=embed_gate)
    settings = build_settings(
        embedding_max_concurrency=1,
        embedding_queue_depth=0,
        embedding_queue_wait_seconds=10.0,
    )

    async def scenario() -> None:
        async with running_app(settings, stub) as app:
            first = asyncio.create_task(call_embed(app, chunks=[json_body(["占住许可"])]))
            await wait_until(lambda: len(stub.embed_calls) == 1)

            started = time.monotonic()
            second = await call_embed(app, chunks=[json_body(["队列已满"])])
            elapsed = time.monotonic() - started

            assert second.status == 503
            assert second.body_json()["code"] == "EMBEDDING_BUSY"
            assert elapsed < 1.0  # 立即拒绝，不等 queue_wait_seconds
            assert second.body_json()["code"] != "EMBEDDING_QUEUE_TIMEOUT"

            embed_gate.set()
            assert (await first).status == 200

    asyncio.run(scenario())


def test_queue_wait_timeout_returns_distinct_503() -> None:
    embed_gate = threading.Event()
    stub = StubEmbedder(embed_gate=embed_gate)
    settings = build_settings(
        embedding_max_concurrency=1,
        embedding_queue_depth=1,
        embedding_queue_wait_seconds=0.05,
    )

    async def scenario() -> None:
        async with running_app(settings, stub) as app:
            first = asyncio.create_task(call_embed(app, chunks=[json_body(["占住许可"])]))
            await wait_until(lambda: len(stub.embed_calls) == 1)

            second = await call_embed(app, chunks=[json_body(["等到超时"])])

            assert second.status == 503
            assert second.body_json()["code"] == "EMBEDDING_QUEUE_TIMEOUT"
            assert second.body_json()["code"] != "EMBEDDING_BUSY"

            embed_gate.set()
            assert (await first).status == 200

    asyncio.run(scenario())


def test_cancelled_http_wait_does_not_release_the_cpu_permit() -> None:
    embed_gate = threading.Event()
    stub = StubEmbedder(embed_gate=embed_gate)
    settings = build_settings(
        embedding_max_concurrency=1,
        embedding_queue_depth=0,
        embedding_queue_wait_seconds=10.0,
    )

    async def scenario() -> None:
        async with running_app(settings, stub) as app:
            first = asyncio.create_task(call_embed(app, chunks=[json_body(["被取消的请求"])]))
            # 线程已进入 encode 并被闸门挡住。
            await wait_until(lambda: len(stub.embed_calls) == 1)

            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first

            # 取消只结束 HTTP 等待；CPU 线程仍在跑，许可不得提前归还。
            assert app.state.embedding_gate.active == 1
            busy = await call_embed(app, chunks=[json_body(["仍然在编码"])])
            assert busy.status == 503
            assert busy.body_json()["code"] == "EMBEDDING_BUSY"

            embed_gate.set()
            await wait_until(lambda: app.state.embedding_gate.active == 0)

            recovered = await call_embed(app, chunks=[json_body(["恢复后可以编码"])])
            assert recovered.status == 200

    asyncio.run(scenario())


def test_cancelled_queue_waiter_frees_its_slot() -> None:
    embed_gate = threading.Event()
    stub = StubEmbedder(embed_gate=embed_gate)
    settings = build_settings(
        embedding_max_concurrency=1,
        embedding_queue_depth=1,
        embedding_queue_wait_seconds=10.0,
    )

    async def scenario() -> None:
        async with running_app(settings, stub) as app:
            holder = asyncio.create_task(call_embed(app, chunks=[json_body(["占住许可"])]))
            await wait_until(lambda: len(stub.embed_calls) == 1)

            queued = asyncio.create_task(
                call_embed(app, chunks=[json_body(["排队中被取消"])])
            )
            await wait_until(lambda: app.state.embedding_gate.waiting == 1)
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
            assert app.state.embedding_gate.waiting == 0

            embed_gate.set()
            assert (await holder).status == 200
            await wait_until(lambda: app.state.embedding_gate.active == 0)

            # 取消排队者不能让许可泄漏：后续请求仍能正常编码。
            later = await call_embed(app, chunks=[json_body(["后续请求"])])
            assert later.status == 200

    asyncio.run(scenario())


def test_padding_positions_count_towards_the_token_budget() -> None:
    # 未 padding 的合计恰好等于预算，但 padding 后是 16 × 467 = 7472 个位置。
    texts = ["a"] * 15 + ["a" * 465]
    stub = StubEmbedder()
    settings = build_settings(
        embedding_max_batch_size=16,
        embedding_max_tokens_per_text=512,
        embedding_max_total_tokens=512,
    )

    async def scenario() -> None:
        async with running_app(settings, stub) as app:
            response = await call_embed(app, chunks=[json_body(texts)])
            assert response.status == 413
            assert response.body_json()["code"] == "EMBEDDING_PAYLOAD_TOO_LARGE"

    asyncio.run(scenario())

    counts = [len(text) + 2 for text in texts]
    assert sum(counts) == 512  # 只看合计本可以通过预算
    assert len(counts) * max(counts) == 7472  # padding 后才是真实占用
    assert stub.embed_calls == []  # 超预算时绝不进入编码


def test_padded_budget_allows_a_batch_that_fits() -> None:
    texts = ["短", "稍长一点的文本"]
    stub = StubEmbedder()
    settings = build_settings(
        embedding_max_batch_size=2,
        embedding_max_tokens_per_text=512,
        embedding_max_total_tokens=2 * 400,
    )

    async def scenario() -> None:
        async with running_app(settings, stub) as app:
            response = await call_embed(app, chunks=[json_body(texts)])
            assert response.status == 200

    asyncio.run(scenario())


def test_queue_is_bounded_across_many_clients() -> None:
    """并发 1 + 队列 2 时，第 4 个并发请求立即拿到 503，而不是无限排队。"""

    embed_gate = threading.Event()
    stub = StubEmbedder(embed_gate=embed_gate)
    settings = build_settings(
        embedding_max_concurrency=1,
        embedding_queue_depth=2,
        embedding_queue_wait_seconds=10.0,
    )

    async def scenario() -> None:
        async with running_app(settings, stub) as app:
            requests = [
                asyncio.create_task(call_embed(app, chunks=[json_body([f"并发-{index}"])]))
                for index in range(4)
            ]
            # 1 个执行中 + 2 个在队列里，第 4 个必须被立即拒绝。
            await wait_until(lambda: len(stub.embed_calls) == 1)
            await wait_until(lambda: app.state.embedding_gate.waiting == 2)
            await wait_until(lambda: sum(1 for request in requests if request.done()) == 1)

            rejected = next(request for request in requests if request.done())
            assert rejected.result().status == 503
            assert rejected.result().body_json()["code"] == "EMBEDDING_BUSY"

            embed_gate.set()
            remaining = await asyncio.gather(
                *(request for request in requests if not request.done())
            )
            assert [response.status for response in remaining] == [200, 200, 200]

    asyncio.run(scenario())
