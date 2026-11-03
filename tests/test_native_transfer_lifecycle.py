import copy
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from src.amounts import MAX_AMOUNT
from src.block_indexer import BlockIndexer
from src.database import Database
from src.native_transfers import reconcile_block, reconcile_completed_duplicates
from src.transfer_reports import ReportUnavailable, block_events
from test_transfer_reports import expr, payment, report

ROOT = Path(__file__).resolve().parents[1]


@pytest_asyncio.fixture
async def ledger(monkeypatch, request):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL for PostgreSQL integration tests")
    admin = await asyncpg.connect(url)
    name = "native_lifecycle_" + uuid4().hex
    await admin.execute(f'CREATE DATABASE "{name}"')
    test_url = urlunsplit(urlsplit(url)._replace(path="/" + name))
    conn = await asyncpg.connect(test_url)
    database = Database(test_url)
    try:
        for migration in sorted((ROOT / "migrations").glob("[0-9][0-9][0-9]_*.sql")):
            if getattr(request, "param", None) == "002" and migration.name.startswith(
                "003"
            ):
                continue
            await conn.execute(migration.read_text())
        await database.connect()
        monkeypatch.setattr("src.block_indexer.db", database)
        monkeypatch.setattr("src.reindex_transfers.db", database)
        monkeypatch.setattr("src.block_indexer.settings.native_token_decimals", 18)
        monkeypatch.setattr(
            "src.block_indexer.settings.enable_asi_transfer_extraction", True
        )
        indexer = BlockIndexer()
        indexer.client = SimpleNamespace(get_block_report=AsyncMock())

        async def add(height, block_hash, entries, parents=()):
            details = {
                "blockInfo": {
                    "blockHash": block_hash,
                    "blockNumber": height,
                    "timestamp": height * 1000,
                    "sender": "validator",
                    "bonds": [],
                    "parentsHashList": list(parents),
                },
                "deploys": [],
            }
            reports = []
            for deploy_id, deploy_report in entries:
                deploy_report = copy.deepcopy(deploy_report)
                deploy_report["deployInfo"]["sig"] = deploy_id
                reports.append(deploy_report)
                details["deploys"].append(
                    {
                        **deploy_report["deployInfo"],
                        "timestamp": deploy_report["deployInfo"].get(
                            "timestamp", height * 1000
                        ),
                        "term": "Nil",
                        "deployer": "04" + "11" * 64,
                    }
                )
            replay = {"blockInfo": {"blockHash": block_hash}, "deploys": reports}
            indexer.client.get_block_report.return_value = replay
            await indexer._process_block(details)
            return details, replay

        yield SimpleNamespace(db=database, conn=conn, indexer=indexer, add=add)
    finally:
        await database.disconnect()
        await conn.close()
        await admin.execute(f'DROP DATABASE "{name}"')
        await admin.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [1, 2**53 + 1, 2**63, 2**64, 2**150, MAX_AMOUNT])
async def test_pending_restart_and_later_completion(ledger, raw):
    events = payment(raw)
    await ledger.add(1, "origin", [("start", report(events[:1]))])
    assert await ledger.conn.fetchval("SELECT count(*) FROM transfers") == 0
    initial = await ledger.conn.fetchrow("SELECT * FROM native_transfer_lifecycle")
    assert initial["status"] == "pending"
    assert initial["initiation_deploy_id"] == "start"
    assert initial["requested_amount"] == str(raw)
    assert initial["completion_deploy_id"] is None
    ledger.indexer.__dict__.update(BlockIndexer().__dict__)
    ledger.indexer.client = SimpleNamespace(get_block_report=AsyncMock())
    details, replay = await ledger.add(
        2, "completion", [("finish", report(events[1:]))], ["origin"]
    )
    row = await ledger.conn.fetchrow("SELECT * FROM transfers")
    assert row["amount_dust"] == raw
    assert row["token_decimals"] == 18
    assert (row["block_hash"], row["deploy_id"]) == ("completion", "finish")
    lifecycle = await ledger.conn.fetchrow("SELECT * FROM native_transfer_lifecycle")
    assert lifecycle["status"] == "completed"
    assert lifecycle["initiation_block_hash"] == "origin"
    assert lifecycle["completion_block_hash"] == "completion"
    assert lifecycle["initiation_timestamp"] == 1000
    assert lifecycle["completion_timestamp"] == 2000
    assert lifecycle["transfer_id"] == row["id"]
    await ledger.indexer._process_block(details)
    for _ in range(2):
        async with ledger.db.session() as session:
            moves = await reconcile_block(
                session,
                details["blockInfo"],
                details["deploys"],
                block_events(replay, "completion", ["finish"]),
            )
            await ledger.indexer._store_transfers(
                session,
                details["blockInfo"],
                {**details["deploys"][0], "executedTransfers": moves["finish"]},
            )
    assert await ledger.conn.fetchval("SELECT count(*) FROM transfers") == 1
    assert (
        await ledger.conn.fetchval("SELECT count(*) FROM native_transfer_results") == 1
    )


@pytest.mark.asyncio
async def test_rejected_empty_target_and_unanswered_call_do_not_pin_cursor(
    ledger, monkeypatch
):
    bad = payment(1, False)
    bad[0]["Comm"]["produces"][0]["data"]["pars"][2] = expr("GString", "")
    await ledger.add(1, "rejected", [("bad", report(bad))])
    assert (
        await ledger.conn.fetchval("SELECT status FROM native_transfer_lifecycle")
        == "rejected"
    )
    await ledger.add(2, "pending", [("waiting", report(payment()[:1]))], ["rejected"])
    await ledger.add(3, "later", [("unrelated", report([]))], ["pending"])
    assert await ledger.conn.fetchval("SELECT count(*) FROM transfers") == 0
    monkeypatch.setattr("src.block_indexer.settings.delay_before_node", 0)
    ledger.indexer.client.get_last_finalized_block = AsyncMock(
        return_value={"blockNumber": 3}
    )
    ledger.indexer.client.get_blocks_by_height = AsyncMock(
        return_value=[
            {"blockNumber": n, "blockHash": h}
            for n, h in [(3, "later"), (1, "rejected"), (2, "pending")]
        ]
    )
    ledger.indexer.client.get_block_details = AsyncMock(
        side_effect=lambda h: {
            "blockInfo": {
                "blockHash": h,
                "blockNumber": {"rejected": 1, "pending": 2, "later": 3}[h],
            },
            "deploys": [],
        }
    )
    await ledger.db.set_last_indexed_block(0)
    await ledger.indexer._sync_blocks()
    assert await ledger.db.get_last_indexed_block() == 3


@pytest.mark.asyncio
async def test_same_block_late_rejection_and_identical_successes(ledger):
    pending = payment(5, False)
    await ledger.add(
        1,
        "block",
        [
            ("one", report(pending[:1])),
            (
                "two",
                report(
                    pending[1:] + payment(9, ret="34" * 32) + payment(9, ret="34" * 32)
                ),
            ),
        ],
    )
    rows = await ledger.conn.fetch(
        "SELECT event_index, amount_dust FROM transfers ORDER BY event_index"
    )
    assert [(row["event_index"], row["amount_dust"]) for row in rows] == [
        (0, 9),
        (1, 9),
    ]
    assert (
        await ledger.conn.fetchval(
            "SELECT count(*) FROM native_transfer_lifecycle WHERE status='rejected'"
        )
        == 1
    )


@pytest.mark.asyncio
async def test_successful_noop_without_user_segment_advances_cursor(
    ledger, monkeypatch
):
    await ledger.add(0, "genesis", [])
    await ledger.db.set_last_indexed_block(0)
    noop = report([])
    noop["deployInfo"]["sig"] = "noop"
    del noop["report"][1]
    details = {
        "blockInfo": {
            "blockHash": "noop-block",
            "blockNumber": 1,
            "timestamp": 1000,
            "sender": "validator",
            "parentsHashList": ["genesis"],
        },
        "deploys": [
            {
                "sig": "noop",
                "term": "Nil",
                "timestamp": 1000,
                "deployer": "04" + "11" * 64,
            }
        ],
    }
    ledger.indexer.client.get_block_report.return_value = {
        "blockInfo": {"blockHash": "noop-block"},
        "deploys": [noop],
    }
    ledger.indexer.client.get_last_finalized_block = AsyncMock(
        return_value={"blockNumber": 1}
    )
    ledger.indexer.client.get_blocks_by_height = AsyncMock(
        return_value=[{"blockHash": "noop-block", "blockNumber": 1}]
    )
    ledger.indexer.client.get_block_details = AsyncMock(return_value=details)
    monkeypatch.setattr("src.block_indexer.settings.delay_before_node", 0)
    await ledger.indexer._sync_blocks()
    assert await ledger.db.get_last_indexed_block() == 1
    assert (
        await ledger.conn.fetchval(
            "SELECT count(*) FROM deployments WHERE deploy_id='noop'"
        )
        == 1
    )
    assert await ledger.conn.fetchval("SELECT count(*) FROM native_transfer_calls") == 0
    assert await ledger.conn.fetchval("SELECT count(*) FROM transfers") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [0, 1])
async def test_fee_phase_without_user_segment_completes_pending_call(ledger, phase):
    raw = 2**150
    events = payment(raw)
    await ledger.add(1, "origin", [("start", report(events[:1]))])
    completion = report([])
    del completion["report"][1]
    completion["report"][phase]["events"] += events[1:]
    await ledger.add(2, "completion", [("finish", completion)], ["origin"])
    row = await ledger.conn.fetchrow("SELECT * FROM transfers")
    assert (row["amount_dust"], row["block_hash"], row["deploy_id"]) == (
        raw,
        "completion",
        "finish",
    )
    assert (
        await ledger.conn.fetchval("SELECT status FROM native_transfer_lifecycle")
        == "completed"
    )
    assert await ledger.conn.fetchval("SELECT count(*) FROM transfers") == 1


@pytest.mark.asyncio
async def test_rolled_back_completion_preserves_pending_call(ledger):
    events = payment(12)
    await ledger.add(1, "origin", [("start", report(events[:1]))])
    failed = report(events[1:])
    failed["deployInfo"]["errored"] = True
    await ledger.add(2, "failed", [("failed", failed)], ["origin"])
    assert (
        await ledger.conn.fetchval("SELECT status FROM native_transfer_lifecycle")
        == "pending"
    )
    await ledger.add(3, "success", [("retry", report(events[1:]))], ["failed"])
    assert await ledger.conn.fetchval("SELECT count(*) FROM transfers") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [0, 2])
async def test_committed_fee_phase_can_complete_user_call_after_user_rollback(
    ledger, phase
):
    events = payment(12)
    await ledger.add(1, "origin", [("start", report(events[:1]))])
    failed = report([])
    failed["deployInfo"]["errored"] = True
    failed["report"][phase]["events"] += events[1:]
    await ledger.add(2, "completion", [("finish", failed)], ["origin"])
    assert await ledger.conn.fetchval("SELECT amount_dust FROM transfers") == 12


@pytest.mark.asyncio
async def test_sibling_response_cannot_complete_an_unrelated_call(ledger):
    events = payment(12)
    await ledger.add(1, "root", [("root", report([]))])
    await ledger.add(2, "left", [("start", report(events[:1]))], ["root"])
    await ledger.add(2, "right", [("unrelated", report(events[1:]))], ["root"])
    assert await ledger.conn.fetchval("SELECT count(*) FROM transfers") == 0
    await ledger.add(3, "child", [("finish", report(events[1:]))], ["left"])
    assert await ledger.conn.fetchval("SELECT count(*) FROM transfers") == 1


@pytest.mark.asyncio
async def test_invalid_success_rolls_back_block_and_observations(ledger):
    with pytest.raises(ValueError):
        await ledger.add(
            1, "bad", [("bad", report(payment(1) + payment(MAX_AMOUNT + 1)))]
        )
    for table in (
        "blocks",
        "deployments",
        "native_transfer_calls",
        "native_transfer_results",
        "transfers",
    ):
        assert await ledger.conn.fetchval(f"SELECT count(*) FROM {table}") == 0


@pytest.mark.asyncio
async def test_partial_history_can_index_new_calls_and_later_completion(
    ledger, monkeypatch
):
    monkeypatch.setattr("src.block_indexer.settings.start_from_block", 100)
    events = payment(23)
    await ledger.add(100, "start", [("start", report(events[:1]))], ["before-start"])
    await ledger.add(
        101, "finish", [("finish", report(events[1:]))], ["start", "before-start"]
    )
    assert await ledger.conn.fetchval("SELECT amount_dust FROM transfers") == 23


@pytest.mark.asyncio
@pytest.mark.parametrize("older_height", [98, 100])
async def test_partial_history_verifies_unseen_parent_height(
    ledger, monkeypatch, older_height
):
    monkeypatch.setattr("src.block_indexer.settings.start_from_block", 100)
    events = payment(23)
    await ledger.add(100, "start", [("start", report(events[:1]))], ["boundary"])
    ledger.indexer.client.get_block_details = AsyncMock(
        return_value={
            "blockInfo": {"blockHash": "lagging", "blockNumber": older_height},
        }
    )
    if older_height < 100:
        await ledger.add(
            101, "finish", [("finish", report(events[1:]))], ["start", "lagging"]
        )
        assert await ledger.conn.fetchval("SELECT amount_dust FROM transfers") == 23
    else:
        with pytest.raises(ReportUnavailable, match="Missing parent"):
            await ledger.add(
                101, "finish", [("finish", report(events[1:]))], ["start", "lagging"]
            )
        assert await ledger.conn.fetchval("SELECT count(*) FROM transfers") == 0
    ledger.indexer.client.get_block_details.assert_awaited_once_with("lagging")


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
async def test_missing_parent_rolls_back_and_retry_preserves_completion(ledger, empty):
    events = payment(23)
    entries = [] if empty else [("finish", report(events[1:]))]
    with pytest.raises(ReportUnavailable, match="Missing parent"):
        await ledger.add(2, "child", entries, ["origin"])
    assert await ledger.conn.fetchval("SELECT count(*) FROM blocks") == 0
    await ledger.add(1, "origin", [("start", report(events[:1]))])
    await ledger.add(2, "child", entries, ["origin"])
    if empty:
        await ledger.add(3, "finish", [("finish", report(events[1:]))], ["child"])
    assert await ledger.conn.fetchval("SELECT amount_dust FROM transfers") == 23


@pytest.mark.asyncio
async def test_repairing_origin_does_not_erase_later_completion(ledger):
    events = payment(15)
    details, replay = await ledger.add(1, "origin", [("start", report(events[:1]))])
    await ledger.add(2, "completion", [("finish", report(events[1:]))], ["origin"])
    async with ledger.db.session() as session:
        moves = await reconcile_block(
            session,
            details["blockInfo"],
            details["deploys"],
            block_events(replay, "origin", ["start"]),
        )
        await ledger.indexer._store_transfers(
            session,
            details["blockInfo"],
            {**details["deploys"][0], "executedTransfers": moves["start"]},
        )
    assert await ledger.conn.fetchval("SELECT count(*) FROM transfers") == 1
    assert (
        await ledger.conn.fetchval("SELECT status FROM native_transfer_lifecycle")
        == "completed"
    )


@pytest.mark.asyncio
async def test_interrupted_historical_repair_resumes_without_moving_cursor(
    ledger, monkeypatch
):
    from src.reindex_transfers import repair

    events = payment(2**150)
    origin, origin_report = await ledger.add(
        1, "origin", [("start", report(events[:1]))]
    )
    completion, completion_report = await ledger.add(
        2, "completion", [("finish", report(events[1:]))], ["origin"]
    )
    await ledger.db.set_last_indexed_block(2)
    await ledger.conn.execute(
        """
        DELETE FROM transfers;
        DELETE FROM native_transfer_results;
        DELETE FROM native_transfer_calls;
        UPDATE indexer_state SET value='1' WHERE key='native_transfer_backfill_required';
    """
    )
    client = SimpleNamespace(
        get_block_details=AsyncMock(
            side_effect=lambda h: {"origin": origin, "completion": completion}[h]
        ),
        get_block_report=AsyncMock(
            side_effect=[origin_report, ReportUnavailable("busy")]
        ),
        close=AsyncMock(),
    )
    with monkeypatch.context() as patch:
        patch.setattr("src.reindex_transfers.GrpcNodeClient", lambda: client)
        patch.setattr(ledger.db, "connect", AsyncMock())
        patch.setattr(ledger.db, "disconnect", AsyncMock())
        patch.setattr("src.block_indexer.settings.native_token_decimals", 8)
        with pytest.raises(ReportUnavailable, match="busy"):
            await repair()
        assert (
            await ledger.conn.fetchval(
                "SELECT value FROM indexer_state WHERE key='native_transfer_backfill_required'"
            )
            == "1"
        )
        assert (
            await ledger.conn.fetchval("SELECT status FROM native_transfer_lifecycle")
            == "pending"
        )
        for _ in range(2):
            client.get_block_report.side_effect = lambda h: {
                "origin": origin_report,
                "completion": completion_report,
            }[h]
            await repair()
    assert await ledger.db.get_last_indexed_block() == 2
    assert (
        await ledger.conn.fetchval(
            "SELECT value FROM indexer_state WHERE key='native_transfer_backfill_required'"
        )
        == "0"
    )
    row = await ledger.conn.fetchrow(
        "SELECT amount_dust, token_decimals FROM transfers"
    )
    assert (row["amount_dust"], row["token_decimals"]) == (2**150, 18)
    assert await ledger.conn.fetchval("SELECT count(*) FROM transfers") == 1


def test_migration_entrypoints_include_the_same_ordered_schema():
    migrations = [
        path.name
        for path in sorted((ROOT / "migrations").glob("[0-9][0-9][0-9]_*.sql"))
    ]
    recipe = (
        (ROOT / "Makefile")
        .read_text()
        .split("migrate:", 1)[1]
        .split("\ndocker-build:", 1)[0]
    )
    compose = (ROOT / "docker-compose.yml").read_text()
    positions = [recipe.index(name) for name in migrations]
    assert positions == sorted(positions)
    assert recipe.count("ON_ERROR_STOP=1") == len(migrations)
    assert all(f"/docker-entrypoint-initdb.d/{name}" in compose for name in migrations)


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["002"], indirect=True)
async def test_upgrade_from_002_preserves_ledger_and_requires_backfill(
    ledger, monkeypatch
):
    await ledger.conn.execute(
        """
        INSERT INTO blocks(block_hash,block_number,timestamp,proposer,finalization_status)
        VALUES ('old',1,1000,'validator','finalized');
        INSERT INTO deployments(deploy_id,block_hash,block_number,deployer,
                                deployer_address,term,timestamp,sig)
        VALUES ('old','old',1,'sender','sender','Nil',1000,'old');
        INSERT INTO transfers(deploy_id,block_hash,block_number,from_address,
                              to_address,amount_dust,amount_asi,timestamp,event_index)
        VALUES ('old','old',1,'sender','receiver',100,1,1000,0);
    """
    )
    before = dict(await ledger.conn.fetchrow("SELECT * FROM transfers"))
    await ledger.conn.execute(
        (ROOT / "migrations/003_native_transfer_calls.sql").read_text()
    )
    after = dict(await ledger.conn.fetchrow("SELECT * FROM transfers"))
    assert after.pop("native_result_id") is None
    assert after == before
    assert (
        await ledger.conn.fetchval(
            "SELECT value FROM indexer_state WHERE key='native_transfer_backfill_required'"
        )
        == "1"
    )
    with monkeypatch.context() as patch:
        patch.setattr(ledger.db, "connect", AsyncMock())
        patch.setattr(ledger.db, "create_tables", AsyncMock())
        with pytest.raises(ValueError, match="reindex_transfers"):
            await ledger.indexer.start()


@pytest.mark.asyncio
async def test_lifecycle_uses_execution_times_not_signed_deploy_times(ledger):
    events = payment(17)
    initial = report(events[:1])
    initial["deployInfo"]["timestamp"] = 100
    completion = report(events[1:])
    completion["deployInfo"]["timestamp"] = 50
    await ledger.add(1, "origin", [("start", initial)])
    await ledger.add(2, "completed", [("finish", completion)], ["origin"])
    state = await ledger.conn.fetchrow("SELECT * FROM native_transfer_lifecycle")
    assert (state["initiation_timestamp"], state["completion_timestamp"]) == (
        1000,
        2000,
    )
    assert await ledger.conn.fetchval("SELECT timestamp FROM transfers") == 2000
    assert (
        await ledger.conn.fetchval(
            "SELECT timestamp FROM deployments WHERE deploy_id='finish'"
        )
        == 50
    )


@pytest.mark.asyncio
async def test_repeated_deploy_keeps_each_invocation_block_visible(ledger):
    events = payment(19)
    await ledger.add(1, "root", [("root", report([]))])
    await ledger.add(2, "left", [("same-deploy", report(events[:1]))], ["root"])
    await ledger.add(2, "right", [("same-deploy", report(events[:1]))], ["root"])
    await ledger.add(3, "child", [("finish", report(events[1:]))], ["left"])
    rows = await ledger.conn.fetch(
        """
        SELECT initiation_block_hash, completion_block_hash, status
        FROM native_transfer_lifecycle ORDER BY initiation_block_hash
    """
    )
    assert [tuple(row) for row in rows] == [
        ("left", "child", "completed"),
        ("right", None, "pending"),
    ]
    assert await ledger.conn.fetchval("SELECT count(*) FROM transfers") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("right_precision", [18, 8])
async def test_merge_coalesces_identical_invocations_and_repair_is_idempotent(
    ledger,
    monkeypatch,
    right_precision,
):
    events = payment(29)
    await ledger.add(1, "root", [("root", report([]))])
    for branch in ("left", "right"):
        monkeypatch.setattr(
            "src.block_indexer.settings.native_token_decimals",
            18 if branch == "left" else right_precision,
        )
        await ledger.add(2, branch, [("same", report(events[:1]))], ["root"])
    details, replay = await ledger.add(
        3, "merged", [("finish", report(events[1:]))], ["left", "right"]
    )
    for _ in range(2):
        async with ledger.db.session() as session:
            movements = await reconcile_block(
                session,
                details["blockInfo"],
                details["deploys"],
                block_events(replay, "merged", ["finish"]),
            )
            await ledger.indexer._store_transfers(
                session,
                details["blockInfo"],
                {**details["deploys"][0], "executedTransfers": movements["finish"]},
            )
    rows = await ledger.conn.fetch("SELECT amount_dust FROM transfers")
    assert [row["amount_dust"] for row in rows] == [29]
    lifecycle = await ledger.conn.fetch(
        "SELECT initiation_block_hash, completion_block_hash, status "
        "FROM native_transfer_lifecycle ORDER BY initiation_block_hash"
    )
    assert [tuple(row) for row in lifecycle] == [
        ("left", "merged", "completed"),
        ("right", "merged", "coalesced"),
    ]
    assert (
        await ledger.conn.fetchval("SELECT count(*) FROM native_transfer_results") == 1
    )


@pytest.mark.asyncio
async def test_merge_rejects_conflicting_duplicate_invocations(ledger):
    await ledger.add(1, "root", [("root", report([]))])
    await ledger.add(2, "left-state", [("left-state", report([]))], ["root"])
    await ledger.add(2, "right-state", [("right-state", report([]))], ["root"])
    await ledger.add(3, "left", [("same", report(payment(29)[:1]))], ["left-state"])
    await ledger.add(3, "right", [("same", report(payment(30)[:1]))], ["right-state"])
    with pytest.raises(ReportUnavailable, match="Conflicting duplicate"):
        await ledger.add(
            4, "merged", [("finish", report(payment(29)[1:]))], ["left", "right"]
        )
    assert await ledger.conn.fetchval("SELECT count(*) FROM transfers") == 0
    assert (
        await ledger.conn.fetchval(
            "SELECT count(*) FROM blocks WHERE block_hash='merged'"
        )
        == 0
    )


@pytest.mark.asyncio
async def test_completion_after_more_than_32767_ancestor_blocks(ledger):
    events = payment(2**150)
    await ledger.add(1, "origin", [("start", report(events[:1]))])
    await ledger.conn.execute(
        """
        INSERT INTO blocks(block_hash,block_number,timestamp,proposer,finalization_status)
        SELECT 'depth-' || n, n, n * 1000, 'validator', 'finalized'
        FROM generate_series(2,33000) n;
        INSERT INTO block_parents(block_hash,parent_hash,parent_index)
        SELECT 'depth-' || n,
               CASE WHEN n=2 THEN 'origin' ELSE 'depth-' || (n-1) END, 0
        FROM generate_series(2,33000) n;
    """
    )
    await ledger.add(
        33001, "completion", [("finish", report(events[1:]))], ["depth-33000"]
    )
    assert await ledger.conn.fetchval("SELECT amount_dust FROM transfers") == 2**150
    assert (
        await ledger.conn.fetchval("SELECT count(*) FROM native_transfer_results") == 1
    )


@pytest.mark.asyncio
async def test_completed_before_merge_and_third_copy_link_existing_result(ledger):
    events = payment(31)
    await ledger.add(1, "root", [("root", report([]))])
    for branch in ("left", "right", "third"):
        await ledger.add(2, branch, [("same", report(events[:1]))], ["root"])
    await ledger.add(3, "finished", [("finish", report(events[1:]))], ["left"])
    await ledger.add(4, "merged", [], ["finished", "right"])
    info, _ = await ledger.add(5, "merged-again", [], ["merged", "third"])
    for _ in range(2):
        async with ledger.db.session() as session:
            await reconcile_completed_duplicates(session, info["blockInfo"])
    rows = await ledger.conn.fetch(
        "SELECT initiation_block_hash, status, completion_block_hash, result_success "
        "FROM native_transfer_lifecycle ORDER BY initiation_block_hash"
    )
    assert [tuple(row) for row in rows] == [
        ("left", "completed", "finished", True),
        ("right", "coalesced", "finished", True),
        ("third", "coalesced", "finished", True),
    ]
    assert await ledger.conn.fetchval("SELECT count(*) FROM transfers") == 1
    assert (
        await ledger.conn.fetchval("SELECT count(*) FROM native_transfer_results") == 1
    )


@pytest.mark.asyncio
async def test_repair_preserves_live_owner_of_immediate_sibling_transfers(
    ledger, monkeypatch
):
    from src.reindex_transfers import repair

    blocks, reports = {}, {}
    blocks["root"], reports["root"] = await ledger.add(
        1, "root", [("root", report([]))]
    )
    for branch in ("left", "right"):
        blocks[branch], reports[branch] = await ledger.add(
            2, branch, [("same", report(payment(37)))], ["root"]
        )
    before = await ledger.conn.fetchrow(
        "SELECT block_hash, amount_dust, native_result_id FROM transfers"
    )
    assert before["block_hash"] == "right"
    client = SimpleNamespace(
        get_block_details=AsyncMock(side_effect=lambda h: blocks[h]),
        get_block_report=AsyncMock(side_effect=lambda h: reports[h]),
        close=AsyncMock(),
    )
    with monkeypatch.context() as patch:
        patch.setattr("src.reindex_transfers.GrpcNodeClient", lambda: client)
        patch.setattr(ledger.db, "connect", AsyncMock())
        patch.setattr(ledger.db, "disconnect", AsyncMock())
        for _ in range(2):
            await repair()
    rows = await ledger.conn.fetch(
        "SELECT block_hash, amount_dust, native_result_id FROM transfers"
    )
    assert rows == [before]
    assert (
        await ledger.conn.fetchval("SELECT count(*) FROM native_transfer_lifecycle")
        == 2
    )
