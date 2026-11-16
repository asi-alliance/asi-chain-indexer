import asyncio
from contextlib import asynccontextmanager
import time

from prometheus_client import REGISTRY
import pytest

from src.block_indexer import BlockIndexer
from src.monitoring import sample_event_loop_lag


class UnindexedSession:
    async def scalar(self, _statement):
        return None


class FakeDatabase:
    def __init__(self, last_indexed=9):
        self.last_indexed = last_indexed
        self.set_calls = []

    async def get_last_indexed_block(self):
        return self.last_indexed

    async def set_last_indexed_block(self, block_number):
        self.last_indexed = block_number
        self.set_calls.append(block_number)

    @asynccontextmanager
    async def session(self):
        yield UnindexedSession()


class TimelineClient:
    """A node whose blocks each need a replay trace that takes `read` seconds."""

    def __init__(self, heights, timeline, read=0.05, missing=()):
        self.blocks = [{"blockNumber": h, "blockHash": f"block-{h}"} for h in heights]
        self.timeline = timeline
        self.read = read
        self.missing = set(missing)
        self.cancelled = []

    async def get_last_finalized_block(self):
        return {"blockNumber": self.blocks[-1]["blockNumber"]}

    async def get_blocks_by_height(self, start, end):
        return [b for b in self.blocks if start <= b["blockNumber"] <= end]

    async def get_block_details(self, block_hash):
        if block_hash in self.missing:
            await asyncio.sleep(0.1)
            return None
        number = int(block_hash.split("-")[1])
        return {
            "blockInfo": {"blockNumber": number, "blockHash": block_hash},
            "deploys": [{"sig": f"deploy-{number}"}],
        }

    async def get_block_events(self, block_hash, deploy_ids):
        self.timeline.append(("read", block_hash))
        try:
            await asyncio.sleep(self.read)
        except asyncio.CancelledError:
            self.cancelled.append(block_hash)
            raise
        return [(deploy_ids[0], [("events of", {"block": block_hash})])]


def indexer_with(monkeypatch, client, timeline, prefetch, write=0.05):
    monkeypatch.setattr("src.block_indexer.db", FakeDatabase())
    monkeypatch.setattr("src.block_indexer.settings.batch_size", 10)
    monkeypatch.setattr("src.block_indexer.settings.start_from_block", 0)
    monkeypatch.setattr("src.block_indexer.settings.delay_before_node", 0)
    monkeypatch.setattr("src.block_indexer.settings.enable_asi_transfer_extraction", True)
    monkeypatch.setattr("src.block_indexer.settings.block_prefetch", prefetch)
    indexer = BlockIndexer()
    indexer.client = client
    written = []

    async def index_block(block_data, traces=None):
        block_hash = block_data["blockInfo"]["blockHash"]
        timeline.append(("write", block_hash))
        await asyncio.sleep(write)
        written.append((block_hash, traces))
        timeline.append(("written", block_hash))

    indexer._index_block = index_block
    return indexer, written


@pytest.mark.asyncio
async def test_later_blocks_are_read_while_a_block_is_written(monkeypatch):
    timeline = []
    client = TimelineClient([10, 11, 12], timeline)
    indexer, written = indexer_with(monkeypatch, client, timeline, prefetch=2)

    await indexer._sync_blocks()

    assert [block_hash for block_hash, _ in written] == ["block-10", "block-11", "block-12"]
    for block_hash, traces in written:
        assert traces == [
            (f"deploy-{block_hash.split('-')[1]}", [("events of", {"block": block_hash})])
        ]
    assert timeline.index(("read", "block-11")) < timeline.index(("written", "block-10"))
    assert timeline.index(("read", "block-12")) < timeline.index(("written", "block-10"))


@pytest.mark.asyncio
async def test_without_prefetch_each_block_is_read_after_the_previous_write(monkeypatch):
    timeline = []
    client = TimelineClient([10, 11, 12], timeline)
    indexer, written = indexer_with(monkeypatch, client, timeline, prefetch=0)

    await indexer._sync_blocks()

    assert [block_hash for block_hash, _ in written] == ["block-10", "block-11", "block-12"]
    assert timeline == [
        event
        for h in (10, 11, 12)
        for event in (("read", f"block-{h}"), ("write", f"block-{h}"), ("written", f"block-{h}"))
    ]


@pytest.mark.asyncio
async def test_reads_past_a_failed_height_are_cancelled(monkeypatch):
    timeline = []
    client = TimelineClient([10, 11, 12, 13], timeline, read=0.5, missing={"block-10"})
    indexer, written = indexer_with(monkeypatch, client, timeline, prefetch=3)

    await indexer._sync_blocks()

    assert written == []
    assert sorted(client.cancelled) == ["block-11", "block-12", "block-13"]
    assert indexer_pending_reads() == []


def indexer_pending_reads():
    current = asyncio.current_task()
    return [
        task for task in asyncio.all_tasks()
        if task is not current and "_read_block" in repr(task.get_coro())
    ]


def lag_sample(name, **labels):
    return REGISTRY.get_sample_value(f"indexer_event_loop_lag_seconds{name}", labels) or 0.0


@pytest.mark.asyncio
async def test_event_loop_lag_records_a_blocked_loop():
    count, fast = lag_sample("_count"), lag_sample("_bucket", le="0.25")
    sampler = asyncio.create_task(sample_event_loop_lag(0.01))
    try:
        await asyncio.sleep(0.05)
        time.sleep(0.4)
        await asyncio.sleep(0.05)
    finally:
        sampler.cancel()
    slow = (lag_sample("_count") - count) - (lag_sample("_bucket", le="0.25") - fast)
    assert slow == 1
