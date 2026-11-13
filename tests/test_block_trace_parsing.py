import asyncio
import json
import time

from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from src.grpc_node_client import GrpcNodeClient
from src.transfer_reports import ReportUnavailable, block_events, trace_events
from test_transfer_reports import payment, report


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
    with pytest.raises(ValueError, match="did not return a replay trace"):
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

    app = web.Application()
    app.router.add_get("/reporting/trace", trace)
    async with TestServer(app) as server:
        client = GrpcNodeClient(node_host=server.host, http_port=server.port)
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
            await client.close()
    assert ticks > 1
    assert worst < min(0.25, inline / 2)
