"""Opt-in checks against a disposable, migrated DB with initialized Hasura metadata.

Set TEST_DATABASE_URL, TEST_HASURA_URL and TEST_HASURA_ADMIN_SECRET. The database
must be the default source of this Hasura instance. Never use a live installation.
"""

import asyncio
import os
from decimal import Decimal
from uuid import uuid4

import aiohttp
import asyncpg
import pytest

from analyze_transfers import fetch_transfers, transfer_report
from src.amounts import MAX_AMOUNT, token_decimal

pytestmark = pytest.mark.integration


async def next_message(websocket):
    # Bound the whole wait, including any keep-alive frames.
    async with asyncio.timeout(30):
        while True:
            message = await websocket.receive_json()
            if message["type"] != "ka":
                return message


@pytest.mark.asyncio
async def test_hasura_exact_amounts_relationships_and_subscription():
    database_url = os.environ.get("TEST_DATABASE_URL")
    base = os.environ.get("TEST_HASURA_URL", "").rstrip("/")
    secret = os.environ.get("TEST_HASURA_ADMIN_SECRET")
    if not all((database_url, base, secret)):
        pytest.skip("Set TEST_DATABASE_URL and TEST_HASURA_* for stack checks")

    connection = await asyncpg.connect(database_url)
    key = "hasura_check_" + uuid4().hex
    display_amount = token_decimal(MAX_AMOUNT, 18)
    try:
        await connection.execute(
            """
            INSERT INTO blocks(block_hash, block_number, timestamp, proposer,
                               finalization_status, token_decimals)
            VALUES ($1, 1, 1, 'validator', 'finalized', 18);
            """,
            key,
        )
        await connection.execute(
            """
            INSERT INTO deployments(deploy_id, block_hash, block_number, deployer,
                                    deployer_address, term, timestamp, sig, phlo_price)
            VALUES ($1, $1, 1, 'sender', 'sender', 'Nil', 1, $1, $2);
            """,
            key,
            MAX_AMOUNT,
        )
        await connection.execute(
            """
            INSERT INTO transfers(deploy_id, block_hash, block_number, from_address,
                                  to_address, amount_dust, amount_asi, timestamp,
                                  event_index, token_decimals)
            VALUES ($1, $1, 1, 'sender', 'receiver', $2, $3, 1, 0, 18);
            """,
            key,
            MAX_AMOUNT,
            display_amount,
        )
        selection = """
          blocks(where: {block_hash: {_eq: $hash}}) {
            deployments { phlo_price transfers { amount_dust amount_asi token_decimals } }
          }
        """
        # Exercise the shipped reporting query and exact formatting through the
        # public role, with the same recorded units as the persisted movement.
        rows = await asyncio.to_thread(fetch_transfers, base + "/v1/graphql")
        row = next(row for row in rows if row["deploy_id"] == key)
        assert row["amount_dust"] == str(MAX_AMOUNT)
        assert row["token_decimals"] == 18
        assert f"{display_amount} ASI" in transfer_report([row])
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as client:
            async with client.get(base + "/healthz") as response:
                assert response.status == 200
            async with client.post(
                base + "/v1/metadata",
                headers={"x-hasura-admin-secret": secret},
                json={"type": "get_inconsistent_metadata", "args": {}},
            ) as response:
                response.raise_for_status()
                assert (await response.json())["is_consistent"] is True

            async with client.post(
                base + "/v1/graphql",
                json={
                    "query": "query($hash: String!) {" + selection + "}",
                    "variables": {"hash": key},
                },
            ) as response:
                response.raise_for_status()
                result = await response.json()
                assert "errors" not in result, result
                assert result["data"]["blocks"][0]["deployments"][0][
                    "phlo_price"
                ] == str(MAX_AMOUNT)
                transfer = result["data"]["blocks"][0]["deployments"][0]["transfers"][0]
                assert transfer["amount_dust"] == str(MAX_AMOUNT)
                assert isinstance(transfer["amount_asi"], str)
                assert Decimal(transfer["amount_asi"]) == display_amount
                assert transfer["token_decimals"] == 18

            async with client.ws_connect(
                base + "/v1/graphql", protocols=("graphql-ws",)
            ) as websocket:
                await websocket.send_json({"type": "connection_init", "payload": {}})
                message = await next_message(websocket)
                assert message["type"] == "connection_ack", message
                await websocket.send_json(
                    {
                        "id": "1",
                        "type": "start",
                        "payload": {
                            "query": "subscription($hash: String!) {" + selection + "}",
                            "variables": {"hash": key},
                        },
                    }
                )
                message = await next_message(websocket)
                assert message["type"] == "data", message
                assert message["payload"] == result
                # A later committed write must reach the public live query too.
                updated_raw = 2**53 + 1
                await connection.execute(
                    "UPDATE transfers SET amount_dust=$2, amount_asi=$3 WHERE deploy_id=$1",
                    key,
                    updated_raw,
                    token_decimal(updated_raw, 18),
                )
                message = await next_message(websocket)
                assert message["type"] == "data", message
                transfer = message["payload"]["data"]["blocks"][0]["deployments"][0][
                    "transfers"
                ][0]
                assert transfer["amount_dust"] == str(updated_raw)
                assert isinstance(transfer["amount_asi"], str)
                assert Decimal(transfer["amount_asi"]) == token_decimal(updated_raw, 18)
                await websocket.send_json({"id": "1", "type": "stop"})
    finally:
        await connection.execute("DELETE FROM blocks WHERE block_hash = $1", key)
        await connection.close()


@pytest.mark.asyncio
async def test_public_report_fetches_genesis_statuses_with_recorded_precision():
    database_url = os.environ.get("TEST_DATABASE_URL")
    base = os.environ.get("TEST_HASURA_URL", "").rstrip("/")
    if not all((database_url, base)):
        pytest.skip("Set TEST_DATABASE_URL and TEST_HASURA_URL for stack checks")

    connection = await asyncpg.connect(database_url)
    key = "genesis_report_" + uuid4().hex
    block_hashes = [key + "_0", key + "_1"]
    # Actual ledger statuses emitted by BlockIndexer, with native8 historical
    # precision and native18 records. Failed attempts must stay out of totals.
    samples = [
        ("genesis_mint", 0, 123456789, 8),
        ("genesis_bond", 0, 10**19, 18),
        ("success", 1, 1, 18),
        ("failed", 1, 10**18, 18),
    ]
    try:
        async with connection.transaction():
            for height, block_hash in enumerate(block_hashes):
                await connection.execute(
                    """
                    INSERT INTO blocks(block_hash, block_number, timestamp, proposer,
                                       finalization_status, token_decimals)
                    VALUES ($1, $2, 1, 'validator', 'finalized', 18)
                    """,
                    block_hash,
                    height,
                )
            for status, height, raw, decimals in samples:
                deploy_id = key + "_" + status
                await connection.execute(
                    """
                    INSERT INTO deployments(deploy_id, block_hash, block_number,
                                            deployer, deployer_address, term, timestamp, sig)
                    VALUES ($1, $2, $3, 'sender', 'sender', 'Nil', 1, $1)
                    """,
                    deploy_id,
                    block_hashes[height],
                    height,
                )
                await connection.execute(
                    """
                    INSERT INTO transfers(deploy_id, block_hash, block_number,
                                          from_address, to_address, amount_dust,
                                          amount_asi, timestamp, event_index,
                                          token_decimals, status)
                    VALUES ($1, $2, $3, 'sender', 'receiver', $4, $5, 1, 0, $6, $7)
                    """,
                    deploy_id,
                    block_hashes[height],
                    height,
                    raw,
                    token_decimal(raw, decimals),
                    decimals,
                    status,
                )

        # No admin secret: use the public role and the actual shipped query.
        fetched = await asyncio.to_thread(fetch_transfers, base + "/v1/graphql")
        rows = [row for row in fetched if row["deploy_id"].startswith(key)]
        assert {row["deploy_id"] for row in rows} == {
            key + "_genesis_mint",
            key + "_genesis_bond",
            key + "_success",
        }
        result = transfer_report(rows)
        assert "Total transfers: 3" in result
        assert "Genesis transfers: 2" in result
        assert "Genesis total: 11.234567890000000000 ASI" in result
        assert "Total ASI moved: 11.234567890000000001 ASI" in result
        assert "User transfers: 1" in result
        assert "User total: 0.000000000000000001 ASI" in result
        assert "1.23456789 ASI (raw=123456789, token_decimals=8)" in result
    finally:
        await connection.execute(
            "DELETE FROM blocks WHERE block_hash = ANY($1::text[])",
            block_hashes,
        )
        await connection.close()


async def public_graphql(client, base, query, variables=None):
    async with client.post(
        base + "/v1/graphql", json={"query": query, "variables": variables or {}}
    ) as response:
        response.raise_for_status()
        return await response.json()


@pytest.mark.asyncio
async def test_public_native_transfer_lifecycle_hides_raw_observations():
    database_url = os.environ.get("TEST_DATABASE_URL")
    base = os.environ.get("TEST_HASURA_URL", "").rstrip("/")
    if not all((database_url, base)):
        pytest.skip("Set TEST_DATABASE_URL and TEST_HASURA_URL for stack checks")

    connection = await asyncpg.connect(database_url)
    key = "lifecycle_" + uuid4().hex
    blocks = [key + "_b1", key + "_b2"]
    initiating, completing = key + "_init", key + "_done"
    calls = [key + "_pending", key + "_completed", key + "_rejected"]
    results = [key + "_r_completed", key + "_r_rejected"]
    big = 10**30 + 7
    try:
        async with connection.transaction():
            for height, block_hash in enumerate(blocks, 1):
                await connection.execute(
                    """
                    INSERT INTO blocks(block_hash, block_number, timestamp, proposer,
                                       finalization_status, token_decimals)
                    VALUES ($1, $2, $2, 'validator', 'finalized', 18)
                    """,
                    block_hash,
                    height,
                )
            for deploy_id, block_hash, height in (
                (initiating, blocks[0], 1),
                (completing, blocks[1], 2),
            ):
                await connection.execute(
                    """
                    INSERT INTO deployments(deploy_id, block_hash, block_number, deployer,
                                            deployer_address, term, timestamp, sig)
                    VALUES ($1, $2, $3, 'sender', 'sender', 'Nil', $3, $1)
                    """,
                    deploy_id,
                    block_hash,
                    height,
                )
            for index, (call_id, amount) in enumerate(
                zip(calls, ["7", str(big), "-5"])
            ):
                await connection.execute(
                    """
                    INSERT INTO native_transfer_calls(id, block_hash, deploy_id, event_index,
                                                      return_channel, from_address, to_address,
                                                      requested_amount, token_decimals, timestamp)
                    VALUES ($1, $2, $3, $4, 'internal-channel', 'sender', 'receiver', $5, 18, 1)
                    """,
                    call_id,
                    blocks[0],
                    initiating,
                    index,
                    amount,
                )
            for result_id, call_id, block_hash, deploy_id, success in (
                (results[0], calls[1], blocks[1], completing, True),
                (results[1], calls[2], blocks[0], initiating, False),
            ):
                await connection.execute(
                    """
                    INSERT INTO native_transfer_results(id, call_id, block_hash, deploy_id,
                                                        response_index, phase, success, timestamp)
                    VALUES ($1, $2, $3, $4, 0, 'committed', $5, 2)
                    """,
                    result_id,
                    call_id,
                    block_hash,
                    deploy_id,
                    success,
                )
            await connection.execute(
                """
                INSERT INTO transfers(deploy_id, block_hash, block_number, from_address,
                                      to_address, amount_dust, amount_asi, timestamp,
                                      event_index, token_decimals, native_result_id)
                VALUES ($1, $2, 2, 'sender', 'receiver', $3, $4, 2, 0, 18, $5)
                """,
                completing,
                blocks[1],
                big,
                token_decimal(big, 18),
                results[0],
            )

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as client:
            # No admin secret: the public role used by the explorer.
            result = await public_graphql(
                client,
                base,
                """
                query($calls: [String!], $init: String!, $done: String!) {
                  native_transfer_lifecycle(where: {call_id: {_in: $calls}},
                                            order_by: {call_index: asc}) {
                    call_id status requested_amount token_decimals
                    initiation_block_number completion_block_number
                    initiation_deployment { deploy_id }
                    completion_deployment { deploy_id }
                    initiation_block { block_hash }
                    completion_block { block_hash }
                    transfer { amount_dust }
                  }
                  initiated: deployments(where: {deploy_id: {_eq: $init}}) {
                    native_transfer_calls(order_by: {call_index: asc}) { call_id status }
                  }
                  completed: deployments(where: {deploy_id: {_eq: $done}}) {
                    completed_native_transfers { call_id status }
                  }
                }
                """,
                {"calls": calls, "init": initiating, "done": completing},
            )
            assert "errors" not in result, result
            rows = result["data"]["native_transfer_lifecycle"]
            assert [row["status"] for row in rows] == [
                "pending",
                "completed",
                "rejected",
            ]
            assert [row["requested_amount"] for row in rows] == ["7", str(big), "-5"]
            assert all(row["token_decimals"] == 18 for row in rows)
            assert all(
                row["initiation_deployment"]["deploy_id"] == initiating for row in rows
            )
            assert all(
                row["initiation_block"]["block_hash"] == blocks[0] for row in rows
            )
            assert [row["completion_block_number"] for row in rows] == [None, "2", "1"]
            assert rows[0]["completion_deployment"] is None
            assert rows[1]["completion_deployment"]["deploy_id"] == completing
            assert rows[1]["completion_block"]["block_hash"] == blocks[1]
            assert rows[1]["transfer"] == {"amount_dust": str(big)}
            assert rows[2]["transfer"] is None
            assert result["data"]["initiated"][0]["native_transfer_calls"] == [
                {"call_id": call, "status": status}
                for call, status in zip(calls, ["pending", "completed", "rejected"])
            ]
            assert result["data"]["completed"][0]["completed_native_transfers"] == [
                {"call_id": calls[1], "status": "completed"}
            ]

            # Raw observations, internal channels and lifecycle aggregates stay hidden.
            for query in (
                "{ native_transfer_calls { id } }",
                "{ native_transfer_results { id } }",
                "{ native_transfer_result_aliases { call_id } }",
                "{ native_transfer_lifecycle { return_channel } }",
                "{ native_transfer_lifecycle_aggregate { aggregate { count } } }",
            ):
                hidden = await public_graphql(client, base, query)
                assert "errors" in hidden, (query, hidden)
    finally:
        await connection.execute(
            "DELETE FROM transfers WHERE native_result_id = ANY($1::text[])", results
        )
        await connection.execute(
            "DELETE FROM native_transfer_results WHERE id = ANY($1::text[])", results
        )
        await connection.execute(
            "DELETE FROM native_transfer_calls WHERE id = ANY($1::text[])", calls
        )
        await connection.execute(
            "DELETE FROM blocks WHERE block_hash = ANY($1::text[])", blocks
        )
        await connection.close()
