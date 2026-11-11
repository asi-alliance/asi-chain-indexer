"""Exercise real PostgreSQL transitions, adoption and rollback through the public CLI."""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from src.migration_support import CATALOG_SQL, LOCK_KEY, ROOT
from src.amounts import MAX_AMOUNT

pytestmark = pytest.mark.integration
SQL_FILES = sorted((ROOT / "migrations").glob("[0-9][0-9][0-9]_*.sql"))


@pytest_asyncio.fixture
async def isolated():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "Set TEST_DATABASE_URL to a disposable PostgreSQL server with CREATEDB."
        )
    admin = await asyncpg.connect(url)
    name = "revision_test_" + uuid4().hex
    conn = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        test_url = urlunsplit(urlsplit(url)._replace(path="/" + name))
        conn = await asyncpg.connect(test_url)
        await conn.execute("SET search_path TO public, pg_catalog")
        yield conn, test_url
    finally:
        if conn:
            await conn.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        await admin.close()


async def cli(url, *args):
    env = dict(os.environ, DATABASE_URL=url)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "src.migrate",
        *args,
        cwd=ROOT,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
    return process.returncode, stdout.decode(), stderr.decode()


async def apply_through(conn, revision):
    for i, path in enumerate(SQL_FILES[: revision + 1]):
        await conn.execute(path.read_text())
        if i == 0:
            await conn.execute("""
                INSERT INTO blocks(block_hash,block_number,timestamp,proposer,finalization_status)
                VALUES ('legacy',1,1,'validator','finalized');
                INSERT INTO deployments(deploy_id,block_hash,block_number,deployer,
                                        deployer_address,term,timestamp,sig)
                VALUES ('legacy','legacy',1,'sender','sender','term',1,'legacy');
                INSERT INTO transfers(deploy_id,block_hash,block_number,from_address,to_address,
                                      amount_dust,amount_asi,timestamp)
                VALUES ('legacy','legacy',1,'sender','receiver',123456789,1.23456789,1);
                UPDATE deployments SET phlo_price=9223372036854775807;
                INSERT INTO pending_deploys(sig,deployer,deployer_address,term,timestamp,phlo_price)
                VALUES ('pending','sender','sender','Nil',1,9223372036854775807);
            """)


@pytest.mark.asyncio
async def test_empty_upgrade_and_repeat_do_not_replay_sql(isolated):
    conn, url = isolated
    assert await cli(url, "status") == (0, "unversioned\n", "")
    for _ in range(2):
        code, _, error = await cli(url, "upgrade")
        assert code == 0, error
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "005"
    assert (
        await conn.fetchval(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name='deployments' AND column_name='phlo_price'"
        )
        == "numeric"
    )
    assert await conn.fetchval("SELECT count(*) FROM native_transfer_calls") == 0
    assert (
        await conn.fetchval(
            "SELECT value FROM indexer_state WHERE key='native_transfer_backfill_required'"
        )
        == "0"
    )
    assert await cli(url, "status") == (0, "005\n", "")


@pytest.mark.parametrize("revision", range(6))
@pytest.mark.asyncio
async def test_adoption_and_upgrade_preserve_history_and_repair_flag(
    isolated, revision
):
    conn, url = isolated
    await apply_through(conn, revision)
    expected = json.loads(
        (ROOT / "migration_env/profiles" / f"{revision:03}.json").read_text()
    )
    assert dict(await conn.fetch(CATALOG_SQL)) == expected
    before = await conn.fetchrow("SELECT amount_dust,amount_asi FROM transfers")
    code, _, error = await cli(url, "adopt", "--revision", f"{revision:03}")
    assert code == 0, error
    assert (
        await conn.fetchval("SELECT version_num FROM alembic_version")
        == f"{revision:03}"
    )
    assert await conn.fetchrow("SELECT amount_dust,amount_asi FROM transfers") == before
    code, _, error = await cli(url, "upgrade")
    assert code == 0, error
    assert await conn.fetchval("SELECT version_num FROM alembic_version") == "005"
    assert await conn.fetchrow("SELECT amount_dust,amount_asi FROM transfers") == before
    assert await conn.fetchval("SELECT token_decimals FROM transfers") == 8
    assert await conn.fetchval("SELECT count(*) FROM blocks") == 1
    assert (
        await conn.fetchval(
            "SELECT value FROM indexer_state WHERE key='native_transfer_backfill_required'"
        )
        == "1"
    )
    for table in ("deployments", "pending_deploys"):
        assert await conn.fetchval(f"SELECT phlo_price FROM {table}") == 2**63 - 1
        for price in (2**63, 10**19 + 1, 2**128, MAX_AMOUNT):
            await conn.execute(f"UPDATE {table} SET phlo_price=$1", Decimal(price))
            assert await conn.fetchval(f"SELECT phlo_price FROM {table}") == price
        for price in (-1, MAX_AMOUNT + 1):
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(f"UPDATE {table} SET phlo_price=$1", Decimal(price))


@pytest.mark.asyncio
async def test_existing_schema_requires_explicit_adoption(isolated):
    conn, url = isolated
    await apply_through(conn, 0)
    code, _, error = await cli(url, "upgrade")
    assert code != 0 and "adopt" in error
    assert await conn.fetchval("SELECT to_regclass('alembic_version')") is None
    assert await conn.fetchval("SELECT count(*) FROM transfers") == 1


@pytest.mark.parametrize(
    "change",
    [
        "ALTER TABLE transfers DROP CONSTRAINT uq_transfer_event",
        "DROP VIEW native_transfer_lifecycle",
        "DROP TRIGGER new_transfer_notify ON transfers",
        "ALTER TABLE transfers DISABLE TRIGGER new_transfer_notify",
        "ALTER TABLE blocks ADD COLUMN unexpected_column integer",
        "DROP INDEX ix_native_transfer_results_call_id",
    ],
)
@pytest.mark.asyncio
async def test_partial_schema_is_not_stamped(isolated, change):
    conn, url = isolated
    await apply_through(conn, 3)
    await conn.execute(change)
    code, _, error = await cli(url, "adopt", "--revision", "003")
    assert code != 0 and "does not match" in error
    assert await conn.fetchval("SELECT to_regclass('alembic_version')") is None


@pytest.mark.parametrize("revision", [3, 4, 5], ids=["003", "004", "005"])
@pytest.mark.parametrize(
    "table,mode",
    [
        ("native_transfer_calls", "DISABLE"),
        ("blocks", "DISABLE"),
        ("native_transfer_calls", "ENABLE REPLICA"),
    ],
    ids=["disabled-child", "disabled-parent", "replica-only"],
)
@pytest.mark.asyncio
async def test_adoption_rejects_changed_fk_enforcement(isolated, revision, table, mode):
    conn, url = isolated
    await apply_through(conn, revision)
    trigger = await conn.fetchval(
        "SELECT t.tgname FROM pg_trigger t "
        "JOIN pg_constraint c ON c.oid=t.tgconstraint "
        "WHERE t.tgrelid=$1::regclass AND t.tgisinternal AND c.contype='f' "
        "ORDER BY t.tgname LIMIT 1",
        table,
    )
    assert trigger is not None
    await conn.execute(f'ALTER TABLE {table} {mode} TRIGGER "{trigger}"')
    code, _, error = await cli(url, "adopt", "--revision", f"{revision:03}")
    assert code != 0 and "fk-trigger:" in error
    assert await conn.fetchval("SELECT to_regclass('alembic_version')") is None
    assert await conn.fetchval("SELECT count(*) FROM transfers") == 1


@pytest.mark.parametrize("revision", [0, 4, 5], ids=["000", "004", "005"])
@pytest.mark.parametrize(
    "change",
    [
        "ALTER SEQUENCE transfers_id_seq AS smallint MAXVALUE 2",
        "ALTER SEQUENCE transfers_id_seq INCREMENT BY 2",
        "ALTER SEQUENCE transfers_id_seq MINVALUE 0",
        "ALTER SEQUENCE transfers_id_seq MAXVALUE 2",
        "ALTER SEQUENCE transfers_id_seq START WITH 2",
        "ALTER SEQUENCE transfers_id_seq CACHE 2",
        "ALTER SEQUENCE transfers_id_seq CYCLE",
        "ALTER SEQUENCE transfers_id_seq OWNED BY NONE",
        "ALTER SEQUENCE transfers_id_seq OWNED BY blocks.block_number",
        "CREATE SEQUENCE unexpected_seq OWNED BY transfers.id",
    ],
    ids=[
        "type", "increment", "minimum", "maximum", "start", "cache", "cycle",
        "unowned", "wrong-column", "extra-owned",
    ],
)
@pytest.mark.asyncio
async def test_adoption_rejects_changed_owned_sequence(isolated, revision, change):
    conn, url = isolated
    await apply_through(conn, revision)
    before = await conn.fetchrow("SELECT * FROM transfers")
    await conn.execute(change)
    code, _, error = await cli(url, "adopt", "--revision", f"{revision:03}")
    assert code != 0 and "sequence:" in error
    assert await conn.fetchval("SELECT to_regclass('alembic_version')") is None
    assert await conn.fetchrow("SELECT * FROM transfers") == before


@pytest.mark.parametrize("is_called", [False, True])
@pytest.mark.asyncio
async def test_adoption_preserves_sequence_progress_and_unrelated_objects(
    isolated, is_called
):
    conn, url = isolated
    await apply_through(conn, 4)
    await conn.execute("SELECT setval('transfers_id_seq', 1000, $1)", is_called)
    await conn.execute(
        "CREATE TABLE unrelated (id SERIAL PRIMARY KEY); "
        "CREATE SEQUENCE unrelated_seq AS smallint INCREMENT BY 2 MAXVALUE 7"
    )
    code, _, error = await cli(url, "adopt", "--revision", "004")
    assert code == 0, error
    assert await cli(url, "status") == (0, "004\n", "")
    assert (await cli(url, "upgrade"))[0] == 0
    assert await conn.fetchval("SELECT nextval('transfers_id_seq')") == (
        1001 if is_called else 1000
    )
    assert await conn.fetchval("SELECT count(*) FROM transfers") == 1
    assert await conn.fetchval("SELECT to_regclass('unrelated')") is not None
    assert await conn.fetchval("SELECT to_regclass('unrelated_seq')") is not None


@pytest.mark.asyncio
async def test_wrong_revision_and_repeated_adoption_are_rejected(isolated):
    conn, url = isolated
    await apply_through(conn, 3)
    assert (await cli(url, "adopt", "--revision", "000"))[0] != 0
    assert (await cli(url, "adopt", "--revision", "003"))[0] == 0
    code, _, error = await cli(url, "adopt", "--revision", "003")
    assert code != 0 and "already" in error


@pytest.mark.asyncio
async def test_concurrent_migration_is_rejected_without_version_write(isolated):
    conn, url = isolated
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", LOCK_KEY)
        code, _, error = await cli(url, "upgrade")
        assert code != 0 and "migration lock" in error
        assert await conn.fetchval("SELECT to_regclass('alembic_version')") is None
    assert (await cli(url, "upgrade"))[0] == 0


@pytest.mark.asyncio
async def test_sql_failure_rolls_back_schema_data_and_revision(isolated):
    conn, url = isolated
    await apply_through(conn, 0)
    assert (await cli(url, "adopt", "--revision", "000"))[0] == 0
    # A negative legacy amount cannot pass the new uint256 constraint. The view
    # drop/column additions before that failure must roll back with the revision.
    await conn.execute("UPDATE transfers SET amount_dust=-1")
    code, _, _ = await cli(url, "upgrade")
    assert code != 0
    assert await conn.fetchval("SELECT version_num FROM alembic_version") == "000"
    assert (
        await conn.fetchval("SELECT to_regclass('transaction_history_view')")
        is not None
    )
    assert (
        await conn.fetchval(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name='blocks' AND column_name='token_decimals'"
        )
        == 0
    )
    assert await conn.fetchval("SELECT amount_dust FROM transfers") == -1


@pytest.mark.asyncio
async def test_004_failure_before_stamp_retains_schema_revision_and_lock(
    isolated, monkeypatch
):
    from alembic import op
    from src import migrate, migration_support

    conn, url = isolated
    await apply_through(conn, 3)
    assert (await cli(url, "adopt", "--revision", "003"))[0] == 0
    monkeypatch.setenv("DATABASE_URL", url)
    apply_sql = migration_support.apply_sql

    def fail_after_sql(filename):
        apply_sql(filename)
        if filename.startswith("004_"):

            async def check_lock(driver):
                competitor = await asyncpg.connect(url)
                try:
                    assert not await competitor.fetchval(
                        "SELECT pg_try_advisory_xact_lock($1)", LOCK_KEY
                    )
                finally:
                    await competitor.close()

            op.get_bind().connection.dbapi_connection.run_async(check_lock)
            raise RuntimeError("Injected failure after SQL and before stamping")

    monkeypatch.setattr(migration_support, "apply_sql", fail_after_sql)
    with pytest.raises(RuntimeError, match="Injected failure"):
        await migrate.run("upgrade")
    assert await conn.fetchval("SELECT version_num FROM alembic_version") == "003"
    for table in ("deployments", "pending_deploys"):
        assert await conn.fetchval(f"SELECT phlo_price FROM {table}") == 2**63 - 1
        assert (
            await conn.fetchval(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name=$1 AND column_name='phlo_price'",
                table,
            )
            == "bigint"
        )
    assert await conn.fetchval("SELECT count(*) FROM transfers") == 1


async def network_metrics(conn):
    return await conn.fetch(
        "SELECT * FROM get_network_metrics(1, 6) ORDER BY bucket_start"
    )


@pytest.mark.asyncio
async def test_network_metrics_return_empty_buckets_without_data(isolated):
    conn, url = isolated
    code, _, error = await cli(url, "upgrade")
    assert code == 0, error
    before = await conn.fetchval("SELECT now()")
    rows = await network_metrics(conn)
    assert len(rows) == 6
    assert all(row["deployments_count"] == row["transfers_count"] == 0 for row in rows)
    assert rows[-1]["bucket_end"] >= before
    assert rows[-1]["bucket_end"] - rows[0]["bucket_start"] == timedelta(hours=1)

    await conn.execute("""
        INSERT INTO blocks(block_hash,block_number,timestamp,proposer,finalization_status)
        VALUES ('only',0,1762770000000,'validator','finalized')
    """)
    rows = await network_metrics(conn)
    assert len(rows) == 6
    block_time = datetime.fromtimestamp(1762770000, timezone.utc)
    assert rows[-1]["bucket_end"] == block_time + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_005_upgrade_replaces_failing_network_metrics_on_tracked_004(isolated):
    conn, url = isolated
    for path in SQL_FILES[:5]:
        await conn.execute(path.read_text())
    with pytest.raises(asyncpg.PostgresError, match="interval out of range"):
        await network_metrics(conn)
    assert (await cli(url, "adopt", "--revision", "004"))[0] == 0
    code, _, error = await cli(url, "upgrade")
    assert code == 0, error
    assert await cli(url, "status") == (0, "005\n", "")
    assert len(await network_metrics(conn)) == 6
