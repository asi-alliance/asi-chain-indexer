"""Exercise real PostgreSQL transitions, adoption and rollback through the public CLI."""

import asyncio
import json
import os
import sys
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from src.migration_support import CATALOG_SQL, LOCK_KEY, ROOT

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
                INSERT INTO deployments(deploy_id,block_hash,block_number,deployer,deployer_address,term,timestamp,sig)
                VALUES ('legacy','legacy',1,'sender','sender','term',1,'legacy');
                INSERT INTO transfers(deploy_id,block_hash,block_number,from_address,to_address,
                                      amount_dust,amount_asi,timestamp)
                VALUES ('legacy','legacy',1,'sender','receiver',123456789,1.23456789,1);
            """)


@pytest.mark.asyncio
async def test_empty_upgrade_and_repeat_do_not_replay_sql(isolated):
    conn, url = isolated
    assert await cli(url, "status") == (0, "unversioned\n", "")
    for _ in range(2):
        code, _, error = await cli(url, "upgrade")
        assert code == 0, error
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "003"
    assert await conn.fetchval("SELECT count(*) FROM native_transfer_calls") == 0
    assert (
        await conn.fetchval(
            "SELECT value FROM indexer_state WHERE key='native_transfer_backfill_required'"
        )
        == "0"
    )
    assert await cli(url, "status") == (0, "003\n", "")


@pytest.mark.parametrize("revision", range(4))
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
    assert await conn.fetchrow("SELECT amount_dust,amount_asi FROM transfers") == before
    assert await conn.fetchval("SELECT token_decimals FROM transfers") == 8
    assert await conn.fetchval("SELECT count(*) FROM blocks") == 1
    assert (
        await conn.fetchval(
            "SELECT value FROM indexer_state WHERE key='native_transfer_backfill_required'"
        )
        == "1"
    )


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
            "SELECT count(*) FROM information_schema.columns WHERE table_name='blocks' AND column_name='token_decimals'"
        )
        == 0
    )
    assert await conn.fetchval("SELECT amount_dust FROM transfers") == -1
