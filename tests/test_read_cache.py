import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer
from prometheus_client import REGISTRY
import pytest

from src.monitoring import MonitoringServer
from src.read_cache import ReadCache
from test_native_transfer_lifecycle import ledger  # noqa: F401 (fixture)

ADDRESS = "1111" + "a" * 40


def sample(name, labels=None):
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


def loader(*values):
    calls = AsyncMock(side_effect=list(values))
    return calls


@pytest.mark.asyncio
async def test_repeated_reads_load_once_and_count_hits_and_misses():
    cache = ReadCache(enabled=True, max_entries=8)
    load = loader({"n": 1})
    hits = sample("indexer_read_cache_hits_total", {"endpoint": "unit"})
    misses = sample("indexer_read_cache_misses_total", {"endpoint": "unit"})

    for _ in range(3):
        assert await cache.get_or_load("unit", ("key",), load) == {"n": 1}

    assert load.await_count == 1
    assert sample("indexer_read_cache_hits_total", {"endpoint": "unit"}) == hits + 2
    assert sample("indexer_read_cache_misses_total", {"endpoint": "unit"}) == misses + 1
    assert sample("indexer_read_cache_enabled") == 1


@pytest.mark.asyncio
async def test_invalidation_drops_every_entry():
    cache = ReadCache(enabled=True, max_entries=8)
    first, second = loader("a1", "a2"), loader("b1", "b2")
    evicted = sample("indexer_read_cache_evictions_total", {"reason": "block"})
    assert await cache.get_or_load("unit", "a", first) == "a1"
    assert await cache.get_or_load("unit", "b", second) == "b1"

    cache.invalidate("block")

    assert await cache.get_or_load("unit", "a", first) == "a2"
    assert await cache.get_or_load("unit", "b", second) == "b2"
    assert sample("indexer_read_cache_evictions_total", {"reason": "block"}) == evicted + 2


@pytest.mark.asyncio
async def test_a_load_that_overlaps_an_invalidation_is_not_stored():
    cache = ReadCache(enabled=True, max_entries=8)
    started, release = asyncio.Event(), asyncio.Event()
    values = iter(["read before the commit", "read after the commit"])

    async def slow_load():
        started.set()
        await release.wait()
        return next(values)

    pending = asyncio.ensure_future(cache.get_or_load("unit", "k", slow_load))
    await started.wait()
    cache.invalidate("block")
    release.set()
    assert await pending == "read before the commit"
    # The overlapping result was returned once but not kept.
    assert await cache.get_or_load("unit", "k", slow_load) == "read after the commit"


@pytest.mark.asyncio
async def test_least_recently_used_entry_is_evicted_at_capacity():
    cache = ReadCache(enabled=True, max_entries=2)
    evicted = sample("indexer_read_cache_evictions_total", {"reason": "capacity"})
    a, b, c = loader("a", "a-again"), loader("b"), loader("c")
    await cache.get_or_load("unit", "a", a)
    await cache.get_or_load("unit", "b", b)
    await cache.get_or_load("unit", "c", c)

    assert await cache.get_or_load("unit", "a", a) == "a-again"
    assert sample("indexer_read_cache_evictions_total", {"reason": "capacity"}) >= evicted + 1
    assert sample("indexer_read_cache_entries") == 2


@pytest.mark.asyncio
async def test_disabled_cache_queries_every_time():
    cache = ReadCache(enabled=False, max_entries=8)
    load = loader(1, 2, 3)
    assert [await cache.get_or_load("unit", "k", load) for _ in range(3)] == [1, 2, 3]
    assert sample("indexer_read_cache_enabled") == 0


class BrokenEntries(dict):
    def __contains__(self, key):
        raise RuntimeError("cache backend unavailable")

    def __setitem__(self, key, value):
        raise RuntimeError("cache backend unavailable")


@pytest.mark.asyncio
async def test_cache_failure_degrades_to_a_direct_query():
    cache = ReadCache(enabled=True, max_entries=8)
    cache._entries = BrokenEntries()
    errors = sample("indexer_read_cache_errors_total", {"endpoint": "unit"})
    load = loader("direct", "direct again")

    assert await cache.get_or_load("unit", "k", load) == "direct"
    assert await cache.get_or_load("unit", "k", load) == "direct again"
    assert sample("indexer_read_cache_errors_total", {"endpoint": "unit"}) == errors + 2


@pytest.fixture
def counted_db(monkeypatch):
    """A database stub that counts the queries behind each cached endpoint."""
    queries = []

    async def execute_raw(query, *args):
        queries.append(query)
        if "total_blocks" in query:
            return [{"total_blocks": 7, "total_deployments": 3, "total_transfers": 2}]
        if "total_validators" in query:
            return [{
                "total_validators": 1,
                "last_indexed_block": "6",
                "last_sync_time": None,
                "highest_stored_block": 6,
            }]
        if "COUNT(*)" in query:
            return [{"count": 0}]
        return []

    async def last_indexed():
        return 6

    monkeypatch.setattr("src.monitoring.db.execute_raw", execute_raw)
    monkeypatch.setattr("src.monitoring.db.get_last_indexed_block", last_indexed)
    return queries


READS = (
    "/status",
    "/api/transfers?page=1&limit=10",
    f"/api/address/{ADDRESS}/transfers",
)


@pytest.mark.asyncio
async def test_endpoints_serve_repeated_reads_from_the_cache(counted_db, read_cache):
    async with TestClient(TestServer(MonitoringServer(None).app)) as client:
        for _ in range(3):
            for path in READS:
                assert (await client.get(path)).status == 200
        once = len(counted_db)
        # Different parameters are different entries.
        assert (await client.get("/api/transfers?page=2&limit=10")).status == 200
        assert len(counted_db) == once + 2
        read_cache.invalidate("block")
        for path in READS:
            assert (await client.get(path)).status == 200

    # status: counts once per generation, cursor/validators every time (3 + 1).
    # Each transfer endpoint: rows and count once per generation.
    assert once == 1 + 3 + 2 + 2
    assert len(counted_db) == once + 2 + (1 + 1 + 2 + 2)


@pytest.mark.asyncio
async def test_endpoints_query_every_time_when_the_cache_is_disabled(
    counted_db, monkeypatch
):
    monkeypatch.setattr("src.monitoring.read_cache", ReadCache(enabled=False, max_entries=8))
    async with TestClient(TestServer(MonitoringServer(None).app)) as client:
        for _ in range(3):
            for path in READS:
                assert (await client.get(path)).status == 200
    assert len(counted_db) == 3 * (2 + 2 + 2)


@pytest.mark.asyncio
async def test_endpoints_answer_from_the_database_when_the_cache_fails(
    counted_db, read_cache
):
    read_cache._entries = BrokenEntries()
    async with TestClient(TestServer(MonitoringServer(None).app)) as client:
        status = await client.get("/status")
        assert status.status == 200
        assert (await status.json())["database"]["total_blocks"] == 7
        for path in READS[1:]:
            response = await client.get(path)
            assert response.status == 200
            assert (await response.json())["pagination"]["total"] == 0


@pytest.mark.asyncio
async def test_health_and_readiness_are_never_cached(counted_db):
    indexer = SimpleNamespace(
        client=SimpleNamespace(
            get_last_finalized_block=AsyncMock(return_value={"blockNumber": 6})
        )
    )
    before = {
        name: sum(
            s.value for m in REGISTRY.collect() if m.name == name for s in m.samples
            if s.name == f"{name}_total"
        )
        for name in ("indexer_read_cache_hits", "indexer_read_cache_misses")
    }
    async with TestClient(TestServer(MonitoringServer(indexer).app)) as client:
        for _ in range(3):
            assert (await client.get("/health")).status == 200
            assert (await client.get("/readiness")).status == 200

    assert indexer.client.get_last_finalized_block.await_count == 3
    assert len(counted_db) == 3
    after = {
        name: sum(
            s.value for m in REGISTRY.collect() if m.name == name for s in m.samples
            if s.name == f"{name}_total"
        )
        for name in ("indexer_read_cache_hits", "indexer_read_cache_misses")
    }
    assert after == before


async def add_transfer(conn, block_hash, height, deploy_id):
    await conn.execute(
        """
        INSERT INTO deployments
            (deploy_id, block_hash, block_number, deployer, deployer_address,
             term, timestamp, sig)
        VALUES ($1, $2, $3, 'deployer', $4, 'Nil', $5, $1)
        """,
        deploy_id, block_hash, height, ADDRESS, height * 1000,
    )
    await conn.execute(
        """
        INSERT INTO transfers
            (deploy_id, block_hash, block_number, from_address, to_address,
             amount_dust, amount_asi, timestamp)
        VALUES ($1, $2, $3, $4, 'receiver', 1, 0.00000001, $5)
        """,
        deploy_id, block_hash, height, ADDRESS, height * 1000,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cached_reads_are_dropped_after_a_backfill_and_after_a_reorg(
    ledger, monkeypatch  # noqa: F811
):
    monkeypatch.setattr("src.monitoring.db", ledger.db)
    await ledger.add(5, "tip", [])
    await ledger.db.set_last_indexed_block(5)

    async with TestClient(TestServer(MonitoringServer(None).app)) as client:
        async def observed():
            status = await (await client.get("/status")).json()
            transfers = await (await client.get("/api/transfers")).json()
            address = await (await client.get(f"/api/address/{ADDRESS}/transfers")).json()
            return (
                status["database"]["total_blocks"],
                transfers["pagination"]["total"],
                address["pagination"]["total"],
            )

        assert await observed() == (1, 0, 0)

        # Rows written behind the indexer's back stay invisible while cached.
        await add_transfer(ledger.conn, "tip", 5, "outside-1")
        assert await observed() == (1, 0, 0)

        # A backfill of an older height commits through the indexer.
        await ledger.add(2, "older", [])
        assert await observed() == (2, 1, 1)

        await add_transfer(ledger.conn, "tip", 5, "outside-2")
        assert await observed() == (2, 1, 1)

        # The node's main chain has another block at a stored height: a reorg.
        ledger.indexer.client.show_main_chain = AsyncMock(
            return_value=[{"blockNumber": 5, "blockHash": "canonical"}]
        )
        await ledger.indexer._verify_main_chain()
        assert await observed() == (2, 2, 2)
