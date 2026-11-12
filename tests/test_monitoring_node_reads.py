from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer
from prometheus_client import REGISTRY
import pytest

from src.block_indexer import BlockIndexer
from src.monitoring import MonitoringServer

LAST_INDEXED = 100


@pytest.fixture
def stored(monkeypatch):
    """A database at LAST_INDEXED for both the sync loop and the HTTP server."""

    async def last_indexed():
        return LAST_INDEXED

    async def execute_raw(query, *args):
        if "COUNT(*)" in query:
            return [{
                "total_blocks": 3,
                "total_deployments": 2,
                "total_transfers": 1,
                "total_validators": 1,
                "last_indexed_block": str(LAST_INDEXED),
                "last_sync_time": None,
                "highest_stored_block": LAST_INDEXED,
            }]
        return [{"?column?": 1}]

    for module in ("src.monitoring.db", "src.block_indexer.db"):
        monkeypatch.setattr(f"{module}.get_last_indexed_block", last_indexed)
        monkeypatch.setattr(f"{module}.execute_raw", execute_raw)


def indexer_with_tip(tip):
    indexer = BlockIndexer()
    indexer.client = SimpleNamespace(
        get_last_finalized_block=AsyncMock(return_value={"blockNumber": tip})
    )
    return indexer


@pytest.mark.asyncio
async def test_metrics_and_status_use_the_tip_from_the_sync_loop(stored):
    indexer = indexer_with_tip(LAST_INDEXED + 20)
    rpc = indexer.client.get_last_finalized_block

    async with TestClient(TestServer(MonitoringServer(indexer).app)) as client:
        before = await (await client.get("/status")).json()
        assert before["node"]["connected"] is False
        assert before["node"]["latest_block"] is None
        assert before["indexer"]["sync_lag"] is None

        await indexer._sync_blocks()
        assert rpc.await_count == 1

        for _ in range(5):
            assert (await client.get("/metrics")).status == 200
            status = await (await client.get("/status")).json()

    # The sync loop made the only node call; ten HTTP reads added none.
    assert rpc.await_count == 1
    assert REGISTRY.get_sample_value("indexer_sync_lag_blocks") == 20
    assert REGISTRY.get_sample_value("indexer_last_block_height") == LAST_INDEXED
    assert status["node"]["connected"] is True
    assert status["node"]["latest_block"] == LAST_INDEXED + 20
    assert status["node"]["latest_block_observed_at"] == (
        indexer.chain_tip_observed_at.isoformat()
    )
    assert status["indexer"]["sync_lag"] == 20


@pytest.mark.asyncio
async def test_status_reports_the_node_as_unreachable_after_a_failed_sync_read(stored):
    indexer = indexer_with_tip(LAST_INDEXED)
    await indexer._sync_blocks()
    observed_at = indexer.chain_tip_observed_at
    indexer.client.get_last_finalized_block.return_value = None
    await indexer._sync_blocks()

    async with TestClient(TestServer(MonitoringServer(indexer).app)) as client:
        status = await (await client.get("/status")).json()

    assert status["node"]["connected"] is False
    assert status["node"]["latest_block"] is None
    assert status["node"]["latest_block_observed_at"] == observed_at.isoformat()
    assert status["indexer"]["sync_lag"] is None


@pytest.mark.asyncio
async def test_readiness_still_calls_the_node_on_every_probe(stored):
    indexer = indexer_with_tip(LAST_INDEXED)
    rpc = indexer.client.get_last_finalized_block

    async with TestClient(TestServer(MonitoringServer(indexer).app)) as client:
        for probe in ("/readiness", "/ready", "/readiness"):
            response = await client.get(probe)
            assert response.status == 200
            assert (await response.json())["checks"]["node"] is True

        rpc.return_value = None
        response = await client.get("/readiness")
        assert response.status == 503

    assert rpc.await_count == 4
