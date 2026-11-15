"""Pending deploys read from several validators (issue: observer buffers are always empty)."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from src.addr import convert_to_asi_address
from src.config import Settings, parse_nodes
from src.models import PendingDeploy
from src.pending_deploys import (
    HEARTBEAT_SECONDS,
    STATUS_KEY,
    TOTAL_KEY,
    PendingDeploysPoller,
    Snapshot,
    clear_pending_snapshot,
    merge,
)
from test_native_transfer_lifecycle import ledger  # noqa: F401

DEPLOYER = "04" + "11" * 64


def deploy(sig, rejected=False, **fields):
    return {"sig": sig, "deployer": DEPLOYER, "term": "Nil", "timestamp": 1,
            "phloPrice": 1, "isRejected": rejected, **fields}


# --- configuration -------------------------------------------------------------

def test_parse_nodes_accepts_names_ipv4_ipv6_and_drops_duplicates():
    assert parse_nodes(" v1:40412, 10.0.0.2:40422 ,[::1]:40432,v1:40412,") == [
        ("v1", 40412), ("10.0.0.2", 40422), ("::1", 40432)
    ]
    assert parse_nodes("") == []


@pytest.mark.parametrize("value", [
    "v1", "v1:", ":40412", "v1:0", "v1:65536", "v1:port", "::1:40412",
    "http://v1:40412", "v1 :40412", "[::1]40412",
])
def test_parse_nodes_rejects_invalid_entries(value):
    with pytest.raises(ValueError):
        parse_nodes(value)


def test_settings_validate_pending_nodes_and_default_to_disabled():
    assert Settings(_env_file=None).pending_deploy_nodes == []
    settings = Settings(_env_file=None, pending_deploys_nodes="v1:40412,v2:40422")
    assert settings.pending_deploy_nodes == [("v1", 40412), ("v2", 40422)]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, pending_deploys_nodes="v1")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, pending_deploys_interval=0.1)


# --- merge ---------------------------------------------------------------------

def test_merge_counts_a_deploy_once_and_keeps_a_fresh_copy():
    merged, total = merge([
        Snapshot((deploy("a", rejected=True), deploy("b")), 2),
        Snapshot((deploy("a"), deploy("c", rejected=True)), 2),
        Snapshot((deploy("c", rejected=True), {"sig": ""}), 2),
    ])
    assert sorted(merged) == ["a", "b", "c"]
    assert merged["a"]["isRejected"] is False
    assert merged["c"]["isRejected"] is True
    assert total == 3


def test_merge_adds_deploys_beyond_each_node_cap():
    merged, total = merge([Snapshot((deploy("a"),), 1001), Snapshot((deploy("b"),), 1)])
    assert len(merged) == 2
    assert total == 2 + 1000


# --- poller without a database ---------------------------------------------------

class FakeDb:
    """Records statements; a failing commit leaves the poller's state unchanged."""

    def __init__(self):
        self.statements = []
        self.fail = False

    @asynccontextmanager
    async def session(self):
        db = self

        class Session:
            async def execute(self, statement, params=None):
                db.statements.append((str(statement), params))

            async def commit(self):
                if db.fail:
                    raise ConnectionError("database down")

        yield Session()

    def take(self):
        statements, self.statements = self.statements, []
        return statements

    def states(self, statements):
        return {p["key"]: p["value"] for s, p in statements if p and "indexer_state" in s}


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def node(response=None, error=None):
    client = SimpleNamespace(close=AsyncMock())
    client.get_pending_deploys = AsyncMock(return_value=response, side_effect=error)
    return client


def poller_with(clients, monkeypatch, clock=None):
    fake = FakeDb()
    monkeypatch.setattr("src.pending_deploys.db", fake)
    nodes = [(f"v{i}", 40402 + 10 * i) for i in range(len(clients))]
    by_node = dict(zip(nodes, clients))
    poller = PendingDeploysPoller(
        nodes, interval=1, timeout=0.5, client_factory=by_node.__getitem__, clock=clock or Clock()
    )
    return poller, fake


@pytest.mark.asyncio
async def test_first_refresh_writes_an_empty_snapshot_and_status(monkeypatch):
    poller, fake = poller_with([node({"deploys": [], "totalAvailable": 0})], monkeypatch)
    await poller.refresh()
    statements = fake.take()
    assert any(s.startswith("DELETE FROM pending_deploys") for s, _ in statements)
    assert not any("INSERT INTO pending_deploys" in s for s, _ in statements)
    states = fake.states(statements)
    assert json.loads(states[STATUS_KEY]) == {
        "nodes": 1, "reachable": 1, "count": 0, "total_available": 0, "interval_ms": 1000
    }
    assert states[TOTAL_KEY] == "0"


@pytest.mark.asyncio
async def test_unchanged_snapshot_writes_only_a_heartbeat(monkeypatch):
    clock = Clock()
    response = {"deploys": [deploy("a")], "totalAvailable": 1}
    poller, fake = poller_with([node(response)], monkeypatch, clock)
    await poller.refresh()
    assert any("INSERT INTO pending_deploys" in s for s, _ in fake.take())

    clock.now += 1
    await poller.refresh()
    assert fake.take() == []

    clock.now += HEARTBEAT_SECONDS
    await poller.refresh()
    statements = fake.take()
    assert [STATUS_KEY] == list(fake.states(statements))
    assert not any("pending_deploys " in s or "INTO pending_deploys" in s for s, _ in statements)


@pytest.mark.asyncio
async def test_only_new_or_flipped_deploys_are_upserted(monkeypatch):
    client = node({"deploys": [deploy("a"), deploy("b")], "totalAvailable": 2})
    poller, fake = poller_with([client], monkeypatch)
    await poller.refresh()
    fake.take()

    client.get_pending_deploys.return_value = {
        "deploys": [deploy("a", rejected=True), deploy("c")], "totalAvailable": 2
    }
    await poller.refresh()
    statements = fake.take()
    inserts = [s for s, _ in statements if "INSERT INTO pending_deploys" in s]
    assert len(inserts) == 1
    assert "ON CONFLICT (sig) DO UPDATE SET is_rejected" in inserts[0]
    assert poller._written == {"a": True, "c": False}
    assert json.loads(fake.states(statements)[STATUS_KEY])["count"] == 2


@pytest.mark.asyncio
async def test_unreachable_validator_keeps_its_last_snapshot(monkeypatch):
    first = node({"deploys": [deploy("a")], "totalAvailable": 1})
    second = node({"deploys": [deploy("b")], "totalAvailable": 1})
    poller, fake = poller_with([first, second], monkeypatch)
    await poller.refresh()
    fake.take()

    second.get_pending_deploys.return_value = None
    first.get_pending_deploys.side_effect = asyncio.TimeoutError()
    await poller.refresh()
    statements = fake.take()
    assert poller._written == {"a": False, "b": False}
    status = json.loads(fake.states(statements)[STATUS_KEY])
    assert status["reachable"] == 0 and status["nodes"] == 2
    assert first.get_pending_deploys.await_args.kwargs == {"timeout": 0.5}


@pytest.mark.asyncio
async def test_failed_write_is_retried_on_the_next_refresh(monkeypatch):
    poller, fake = poller_with([node({"deploys": [deploy("a")], "totalAvailable": 1})], monkeypatch)
    fake.fail = True
    with pytest.raises(ConnectionError):
        await poller.refresh()
    assert poller._written is None
    fake.fail = False
    fake.take()
    await poller.refresh()
    assert any("INSERT INTO pending_deploys" in s for s, _ in fake.take())
    assert poller._written == {"a": False}


@pytest.mark.asyncio
async def test_run_survives_errors_and_stops_on_cancel(monkeypatch):
    poller, fake = poller_with([node({"deploys": [], "totalAvailable": 0})], monkeypatch)
    poller.interval = 0.01
    fake.fail = True
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)
    assert not task.done()
    fake.fail = False
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert poller._written == {}
    await poller.close()
    for client in poller._clients.values():
        client.close.assert_awaited_once()


def test_poller_needs_a_node():
    with pytest.raises(ValueError):
        PendingDeploysPoller([], interval=1, timeout=1, client_factory=lambda n: None)


@pytest.mark.asyncio
async def test_indexer_disabled_clears_the_pending_view(monkeypatch):
    from src.block_indexer import BlockIndexer

    fake = FakeDb()
    monkeypatch.setattr("src.pending_deploys.db", fake)
    monkeypatch.setattr("src.block_indexer.settings.pending_deploys_nodes", "")
    indexer = BlockIndexer()
    await indexer._start_pending_deploys()
    statements = [s for s, _ in fake.take()]
    assert indexer._pending_task is None
    assert statements[0].startswith("DELETE FROM pending_deploys")
    assert "DELETE FROM indexer_state" in statements[1]


@pytest.mark.asyncio
async def test_indexer_runs_the_poller_beside_the_sync_loop(monkeypatch):
    from src import block_indexer

    created = []

    class FakePoller:
        def __init__(self, nodes, interval, timeout):
            created.append((nodes, interval, timeout))
            self.close = AsyncMock()

        async def run(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(block_indexer, "PendingDeploysPoller", FakePoller)
    monkeypatch.setattr("src.block_indexer.settings.pending_deploys_nodes", "v1:40412,v2:40422")
    monkeypatch.setattr("src.block_indexer.db", SimpleNamespace(disconnect=AsyncMock()))
    indexer = block_indexer.BlockIndexer()
    await indexer._start_pending_deploys()
    poller = indexer.pending_deploys
    assert created == [([("v1", 40412), ("v2", 40422)], 1.0, 2.0)]
    assert not indexer._pending_task.done()
    await indexer.stop()
    assert indexer._pending_task is None
    poller.close.assert_awaited_once()


# --- PostgreSQL ------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.asyncio
async def test_snapshot_rows_keep_first_seen_time(ledger, monkeypatch):  # noqa: F811
    monkeypatch.setattr("src.pending_deploys.db", ledger.db)
    client = node({"deploys": [deploy("a"), deploy("b")], "totalAvailable": 2})
    poller = PendingDeploysPoller(
        [("v1", 40412)], interval=1, timeout=1, client_factory=lambda n: client
    )
    await poller.refresh()
    first_seen = await ledger.conn.fetchval("SELECT fetched_at FROM pending_deploys WHERE sig='a'")

    client.get_pending_deploys.return_value = {
        "deploys": [deploy("a", rejected=True), deploy("c")], "totalAvailable": 1001
    }
    await poller.refresh()
    async with ledger.db.session() as session:
        rows = {r.sig: r for r in (await session.execute(select(PendingDeploy))).scalars()}
    assert sorted(rows) == ["a", "c"]
    assert rows["a"].is_rejected is True
    assert rows["a"].fetched_at == first_seen
    assert rows["c"].deployer_address == convert_to_asi_address(DEPLOYER)
    state = dict(await ledger.conn.fetch(
        "SELECT key, value FROM indexer_state WHERE key = ANY($1)", [STATUS_KEY, TOTAL_KEY]
    ))
    assert state[TOTAL_KEY] == "1001"
    assert json.loads(state[STATUS_KEY])["count"] == 2

    monkeypatch.setattr("src.pending_deploys.db", ledger.db)
    await clear_pending_snapshot()
    assert await ledger.conn.fetchval("SELECT count(*) FROM pending_deploys") == 0
    assert await ledger.conn.fetchval(
        "SELECT count(*) FROM indexer_state WHERE key = ANY($1)", [STATUS_KEY, TOTAL_KEY]
    ) == 0
