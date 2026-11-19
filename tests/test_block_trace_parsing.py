import asyncio
from concurrent.futures.process import BrokenProcessPool
import errno
import gc
import json
import os
import time

from aiohttp import web
from aiohttp.test_utils import TestServer
from prometheus_client import REGISTRY
import pytest
from structlog.testing import capture_logs

from src.grpc_node_client import GrpcNodeClient, trace_workers
from src import off_loop
from src.off_loop import WorkerPool
from src.transfer_reports import ReportUnavailable, block_events, trace_events
from test_transfer_reports import payment, report


STAGES = (
    ("indexer_block_stage_duration_seconds_count", "stage", "trace_fetch"),
    ("indexer_block_stage_duration_seconds_count", "stage", "trace_queue"),
    ("indexer_block_stage_duration_seconds_count", "stage", "trace_parse"),
    ("indexer_node_request_duration_seconds_count", "request", "trace"),
)


def sample(name):
    metric, label, value = name
    return REGISTRY.get_sample_value(metric, {label: value}) or 0.0


def replay(padding=0):
    return {
        "blockInfo": {"blockHash": "hash"},
        "deploys": [report(payment())],
        "padding": [{"n": i} for i in range(padding)],
    }


def body(data):
    return json.dumps({"type": "block-traces-report", "report": data}).encode()


def test_trace_events_match_block_events():
    data = replay()
    assert trace_events(body(data), "hash", ["test"]) == block_events(
        data, "hash", ["test"]
    )
    with pytest.raises(ReportUnavailable, match="block hash"):
        trace_events(body(data), "other", ["test"])


@pytest.mark.parametrize(
    "response",
    [[], {"type": "other", "report": replay()}, {"type": "block-traces-report"}],
    ids=["array", "other-type", "missing-report"],
)
def test_trace_events_reject_a_response_that_is_not_a_replay_trace(response):
    with pytest.raises(ReportUnavailable, match="did not return a replay trace"):
        trace_events(json.dumps(response).encode(), "hash", ["test"])


@pytest.mark.asyncio
async def test_large_trace_keeps_the_event_loop_responsive():
    data = replay(padding=3_000_000)
    payload = body(data)
    started = time.perf_counter()
    json.loads(payload)
    inline = time.perf_counter() - started
    assert inline > 0.25

    async def trace(request):
        assert request.query["blockHash"] == "hash"
        return web.Response(body=payload, content_type="application/json")

    stages = {name: sample(name) for name in STAGES}
    app = web.Application()
    app.router.add_get("/reporting/trace", trace)
    async with TestServer(app) as server:
        client = GrpcNodeClient(node_host=server.host, http_port=server.port)
        gc.disable()
        try:
            loop = asyncio.get_running_loop()
            work = asyncio.ensure_future(client.get_block_events("hash", ["test"]))
            ticks, worst, last = 0, 0.0, loop.time()
            while not work.done():
                await asyncio.sleep(0.01)
                now = loop.time()
                worst, last, ticks = max(worst, now - last), now, ticks + 1
            assert await work == block_events(data, "hash", ["test"])
        finally:
            gc.enable()
            await client.close()
    assert ticks > 1
    assert worst < min(0.25, inline / 2)
    assert {name: sample(name) - before for name, before in stages.items()} == {
        name: 1.0 for name in STAGES
    }


@pytest.mark.asyncio
async def test_a_stopped_worker_is_replaced_on_the_next_call():
    pool = WorkerPool(1)
    with pytest.raises(BrokenProcessPool):
        await pool.run(os._exit, 1)
    assert pool._executor is None
    assert await pool.run(abs, -5) == 5


@pytest.mark.asyncio
async def test_a_failed_pool_start_releases_the_worker(monkeypatch):
    started = off_loop.ProcessPoolExecutor

    def fails_once(*args, **kwargs):
        monkeypatch.setattr(off_loop, "ProcessPoolExecutor", started)
        raise OSError(errno.EMFILE, "Too many open files")

    monkeypatch.setattr(off_loop, "ProcessPoolExecutor", fails_once)
    pool = WorkerPool(1)
    with pytest.raises(OSError):
        await pool.run(abs, -5)
    assert pool._executor is None
    assert await asyncio.wait_for(pool.run(abs, -5), 30) == 5
    assert pool._busy == 0


@pytest.mark.asyncio
async def test_a_free_worker_goes_to_the_lowest_priority_first():
    pool = WorkerPool(1)
    await pool.run(abs, -1)
    finished = []

    async def call(priority):
        finished.append(await pool.run(abs, -priority, priority=priority))

    busy = asyncio.create_task(pool.run(time.sleep, 0.3))
    await asyncio.sleep(0.05)
    calls = [asyncio.create_task(call(priority)) for priority in (12, 11, 10)]
    await asyncio.sleep(0.05)
    calls[1].cancel()
    await asyncio.gather(busy, *calls, return_exceptions=True)
    assert finished == [10, 12]
    assert pool._busy == 0


@pytest.mark.asyncio
async def test_a_cancelled_call_keeps_its_worker_until_the_job_ends():
    pool = WorkerPool(1)
    await pool.run(abs, -1)
    abandoned = asyncio.create_task(pool.run(time.sleep, 0.5))
    await asyncio.sleep(0.1)
    abandoned.cancel()
    with pytest.raises(asyncio.CancelledError):
        await abandoned
    waited, elapsed, result = await pool.run_timed(time.sleep, 0.05)
    assert result is None
    assert waited > 0.2
    assert 0.05 <= elapsed < 0.2


@pytest.mark.asyncio
async def test_a_stopped_worker_is_logged_with_the_block_hash(monkeypatch):
    payload = body(replay())

    async def trace(request):
        return web.Response(body=payload, content_type="application/json")

    async def stopped(*args, **kwargs):
        raise BrokenProcessPool("worker stopped")

    monkeypatch.setattr(trace_workers, "run_timed", stopped)
    app = web.Application()
    app.router.add_get("/reporting/trace", trace)
    async with TestServer(app) as server:
        client = GrpcNodeClient(node_host=server.host, http_port=server.port)
        try:
            with capture_logs() as logs, pytest.raises(BrokenProcessPool):
                await client.get_block_events("hash", ["test"])
        finally:
            await client.close()
    assert [
        (entry["log_level"], entry["block_hash"], entry["trace_bytes"])
        for entry in logs
        if entry["event"].startswith("Replay trace worker stopped")
    ] == [("warning", "hash", len(payload))]
