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
            VALUES ($1, 0, 1, 'validator', 'finalized', 18);
            """,
            key,
        )
        await connection.execute(
            """
            INSERT INTO deployments(deploy_id, block_hash, block_number, deployer,
                                    deployer_address, term, timestamp, sig)
            VALUES ($1, $1, 0, 'sender', 'sender', 'Nil', 1, $1);
            """,
            key,
        )
        await connection.execute(
            """
            INSERT INTO transfers(deploy_id, block_hash, block_number, from_address,
                                  to_address, amount_dust, amount_asi, timestamp,
                                  event_index, token_decimals)
            VALUES ($1, $1, 0, 'sender', 'receiver', $2, $3, 1, 0, 18);
            """,
            key,
            MAX_AMOUNT,
            display_amount,
        )
        selection = """
          blocks(where: {block_hash: {_eq: $hash}}) {
            deployments { transfers { amount_dust amount_asi token_decimals } }
          }
        """
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
