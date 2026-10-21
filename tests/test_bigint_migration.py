"""Run with TEST_DATABASE_URL pointing to a PostgreSQL test server (CREATEDB)."""

import asyncio
import json
import os
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from src.amounts import MAX_AMOUNT, token_decimal
from src.block_indexer import BlockIndexer
from src.database import Database
from src.models import BalanceState, Block, Deployment, Transfer, Validator

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_migration_preserves_history_and_roundtrips_wide_amounts(monkeypatch):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "Set TEST_DATABASE_URL to run PostgreSQL migration integration tests"
        )
    admin = await asyncpg.connect(url)
    database = "bigint_test_" + uuid4().hex
    connection = engine = None
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        parsed = urlsplit(url)
        test_url = urlunsplit(parsed._replace(path="/" + database))
        connection = await asyncpg.connect(test_url)
        await connection.execute(
            (ROOT / "migrations/000_comprehensive_initial_schema.sql").read_text()
        )
        await connection.execute(
            """
            INSERT INTO blocks(block_hash, block_number, timestamp, proposer, finalization_status, bonds_map)
            VALUES ('legacy', 0, 1, 'validator', 'finalized', '[{"validator":"validator","stake":9007199254740993}]');
            INSERT INTO deployments(deploy_id, block_hash, block_number, deployer, deployer_address, term, timestamp, sig)
            VALUES ('legacy', 'legacy', 0, 'sender', 'sender', 'term', 1, 'legacy');
            INSERT INTO transfers(deploy_id, block_hash, block_number, from_address, to_address, amount_dust, amount_asi, timestamp)
            VALUES ('legacy', 'legacy', 0, 'sender', 'receiver', 123456789, 1.23456789, 1);
            INSERT INTO validators(public_key, total_stake) VALUES ('validator', 9007199254740993);
            INSERT INTO validator_bonds(block_hash, block_number, validator_public_key, stake)
            VALUES ('legacy', 0, 'validator', 9007199254740993);
            INSERT INTO balance_states(address, block_hash, block_number, unbonded_balance_dust, unbonded_balance_asi)
            VALUES ('receiver', 'legacy', 0, 123456789, 1.23456789);
        """
        )
        await connection.execute(
            (ROOT / "migrations/001_bigint_amounts.sql").read_text()
        )
        await connection.execute(
            (ROOT / "migrations/002_executed_transfers.sql").read_text()
        )
        for table in [
            "blocks",
            "transfers",
            "validators",
            "validator_bonds",
            "balance_states",
        ]:
            assert await connection.fetchval(f"SELECT token_decimals FROM {table}") == 8
        old = await connection.fetchrow(
            "SELECT * FROM transfers WHERE deploy_id='legacy'"
        )
        assert old["amount_dust"] == 123456789
        assert old["amount_asi"] == Decimal("1.23456789")
        assert (
            await connection.fetchval(
                "SELECT amount_asi FROM transaction_history_view WHERE deploy_id='legacy'"
            )
            == old["amount_asi"]
        )
        assert (
            json.loads(await connection.fetchval("SELECT bonds_map FROM blocks"))[0][
                "stake"
            ]
            == "9007199254740993"
        )
        assert (
            await connection.fetchval("SELECT total_balance_dust FROM balance_states")
            == 123456789
        )

        monkeypatch.setattr("src.models.settings.native_token_decimals", 18)
        engine = create_async_engine(
            test_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        )
        events = asyncio.Queue()
        await connection.add_listener(
            "new_transfer",
            lambda c, pid, channel, payload: events.put_nowait(json.loads(payload)),
        )
        async with AsyncSession(engine, expire_on_commit=False) as session:
            block = Block(
                block_hash="wide",
                block_number=1,
                timestamp=2,
                proposer="validator",
                finalization_status="finalized",
            )
            session.add(block)
            await session.flush()
            deploy = Deployment(
                deploy_id="wide",
                block_hash="wide",
                block_number=1,
                deployer="sender",
                deployer_address="sender",
                term="term",
                timestamp=2,
                sig="wide",
            )
            session.add(deploy)
            await session.flush()
            transfer = Transfer(
                deploy_id="wide",
                block_hash="wide",
                block_number=1,
                timestamp=2,
                from_address="sender",
                to_address="receiver",
                amount_dust=MAX_AMOUNT,
                amount_asi=token_decimal(MAX_AMOUNT, 18),
            )
            balance = BalanceState(
                address="receiver",
                block_hash="wide",
                block_number=1,
                unbonded_balance_dust=MAX_AMOUNT,
                unbonded_balance_asi=token_decimal(MAX_AMOUNT, 18),
                bonded_balance_dust=MAX_AMOUNT,
                bonded_balance_asi=token_decimal(MAX_AMOUNT, 18),
            )
            session.add_all([transfer, balance])
            indexer = BlockIndexer.__new__(BlockIndexer)
            await indexer._process_validators(
                session,
                {
                    "blockHash": "wide",
                    "blockNumber": 1,
                    "bonds": [{"validator": "validator", "stake": MAX_AMOUNT}],
                },
            )
            await session.commit()
            session.expunge_all()
            saved = await session.scalar(
                select(Transfer).where(Transfer.deploy_id == "wide")
            )
            assert isinstance(saved.amount_dust, int)
            assert saved.amount_dust == MAX_AMOUNT
            assert saved.amount_asi == token_decimal(MAX_AMOUNT, 18)
            assert saved.token_decimals == 18
            validator = await session.get(Validator, "validator")
            assert (validator.total_stake, validator.token_decimals) == (MAX_AMOUNT, 18)
            saved_balance = await session.scalar(
                select(BalanceState).where(BalanceState.block_hash == "wide")
            )
            assert saved_balance.total_balance_dust == MAX_AMOUNT * 2
            assert saved_balance.total_balance_asi == token_decimal(
                MAX_AMOUNT * 2, 18, aggregate=True
            )
        event = await asyncio.wait_for(events.get(), timeout=2)
        assert event["amount_dust"] == str(MAX_AMOUNT)
        assert Decimal(event["amount_asi"]) == token_decimal(MAX_AMOUNT, 18)
        assert event["token_decimals"] == 18
        assert (
            await connection.fetchval(
                "SELECT total_balance_dust FROM balance_states WHERE block_hash='wide'"
            )
            == MAX_AMOUNT * 2
        )
        await connection.execute((ROOT / "tests/bigint-storage.sql").read_text())
    finally:
        if engine is not None:
            await engine.dispose()
        if connection is not None:
            await connection.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
        await admin.close()


@pytest.mark.parametrize("decimals", [8, 18])
@pytest.mark.asyncio
async def test_periodic_validator_refresh_updates_precision_after_migration(
    monkeypatch, decimals
):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "Set TEST_DATABASE_URL to run PostgreSQL migration integration tests"
        )
    admin = await asyncpg.connect(url)
    database_name = "validator_refresh_test_" + uuid4().hex
    connection = database = None
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}"')
        parsed = urlsplit(url)
        test_url = urlunsplit(parsed._replace(path="/" + database_name))
        connection = await asyncpg.connect(test_url)
        await connection.execute(
            (ROOT / "migrations/000_comprehensive_initial_schema.sql").read_text()
        )
        await connection.execute(
            """
            INSERT INTO blocks(block_hash, block_number, timestamp, proposer, finalization_status)
            VALUES ('legacy', 0, 1, 'validator', 'finalized');
            INSERT INTO validators(public_key, total_stake)
            VALUES ('validator', 123456789);
            INSERT INTO validator_bonds(block_hash, block_number, validator_public_key, stake)
            VALUES ('legacy', 0, 'validator', 123456789);
            """
        )
        await connection.execute(
            (ROOT / "migrations/001_bigint_amounts.sql").read_text()
        )
        await connection.execute(
            (ROOT / "migrations/002_executed_transfers.sql").read_text()
        )
        assert (
            await connection.fetchval(
                "SELECT token_decimals FROM validators WHERE public_key='validator'"
            )
            == 8
        )

        monkeypatch.setattr(
            "src.block_indexer.settings.native_token_decimals", decimals
        )
        database = Database(test_url)
        await database.connect()
        await database.set_last_indexed_block(0)
        monkeypatch.setattr("src.block_indexer.db", database)
        indexer = BlockIndexer()
        indexer.client = SimpleNamespace(
            get_bonds=AsyncMock(),
            get_active_validators=AsyncMock(return_value=["validator"]),
        )

        # Refresh at the same indexed height, without processing another block.
        for increment in (0, 1):
            stake = 10**decimals + increment
            indexer.client.get_bonds.return_value = {
                "bonds": [
                    {"validator": key, "stake": stake}
                    for key in ("validator", "new-validator")
                ]
            }
            await indexer._update_validator_states()
            assert indexer._cycle_error is None
            rows = await connection.fetch(
                "SELECT total_stake, token_decimals FROM validators"
            )
            assert len(rows) == 2
            for row in rows:
                assert (row["total_stake"], row["token_decimals"]) == (stake, decimals)
                assert token_decimal(
                    int(row["total_stake"]), row["token_decimals"]
                ) == (Decimal(1) + Decimal(increment).scaleb(-decimals))
            assert await database.get_last_indexed_block() == 0
            assert await connection.fetchval("SELECT COUNT(*) FROM blocks") == 1

        historical = await connection.fetchrow(
            "SELECT stake, token_decimals FROM validator_bonds WHERE block_hash='legacy'"
        )
        assert (historical["stake"], historical["token_decimals"]) == (123456789, 8)
        assert (
            await connection.fetchval(
                "SELECT token_decimals FROM blocks WHERE block_hash='legacy'"
            )
            == 8
        )
    finally:
        if database is not None:
            await database.disconnect()
        if connection is not None:
            await connection.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()


@pytest.mark.asyncio
async def test_replayed_transfers_replace_false_rows_and_keep_identical_events(
    monkeypatch,
):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL for PostgreSQL integration tests")
    admin = await asyncpg.connect(url)
    database = "report_test_" + uuid4().hex
    connection = engine = None
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        parsed = urlsplit(url)
        test_url = urlunsplit(parsed._replace(path="/" + database))
        connection = await asyncpg.connect(test_url)
        for migration in [
            "000_comprehensive_initial_schema.sql",
            "001_bigint_amounts.sql",
            "002_executed_transfers.sql",
        ]:
            await connection.execute((ROOT / "migrations" / migration).read_text())
        await connection.execute(
            """
            INSERT INTO blocks(block_hash, block_number, timestamp, proposer, finalization_status) VALUES ('block', 1, 1, 'validator', 'finalized');
            INSERT INTO deployments(deploy_id, block_hash, block_number, deployer, deployer_address, term, timestamp, sig)
              VALUES ('deploy', 'block', 1, 'sender', 'sender', 'arbitrary term', 1, 'deploy');
            INSERT INTO transfers(deploy_id, block_hash, block_number, from_address, to_address, amount_dust, amount_asi, timestamp)
              VALUES ('deploy', 'block', 1, 'sender', 'receiver', 2000, 2000, 1);
        """
        )
        engine = create_async_engine(
            test_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        )
        indexer = BlockIndexer()
        deploy = {
            "sig": "deploy",
            "timestamp": 1,
            "executedTransfers": [
                {
                    "event_index": index,
                    "from_address": "sender",
                    "to_address": "receiver",
                    "amount": 2**64 + 1,
                }
                for index in [0, 2]
            ],
        }
        block = {"blockHash": "block", "blockNumber": 1, "token_decimals": 18}
        for _ in range(2):
            async with AsyncSession(engine) as session:
                await indexer._store_transfers(session, block, deploy)
                await session.commit()
        rows = await connection.fetch(
            "SELECT event_index, amount_dust, amount_asi, token_decimals FROM transfers ORDER BY event_index"
        )
        assert [r["event_index"] for r in rows] == [0, 2]
        assert all(
            r["amount_dust"] == 2**64 + 1
            and r["amount_asi"] == Decimal("18.446744073709551617")
            and r["token_decimals"] == 18
            for r in rows
        )
        # A missing report aborts before deleting already verified movements.
        async with AsyncSession(engine) as session:
            with pytest.raises(ValueError):
                await indexer._store_transfers(session, block, {"sig": "deploy"})
        assert await connection.fetchval("SELECT count(*) FROM transfers") == 2
        async with AsyncSession(engine) as session:
            await indexer._store_transfers(
                session, block, {**deploy, "executedTransfers": []}
            )
            await session.commit()
        assert await connection.fetchval("SELECT count(*) FROM transfers") == 0
    finally:
        if engine:
            await engine.dispose()
        if connection:
            await connection.close()
        await admin.execute(f'DROP DATABASE "{database}" WITH (FORCE)')
        await admin.close()
