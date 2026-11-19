import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import base58
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.addr import is_valid_vault_address
from src.amounts import MAX_AMOUNT
from src.grpc_node_client import GrpcNodeClient
from src.monitoring import MonitoringServer


ADDRESS = "1111x5QCpTeTXakyVzda9zAVyo6vnSG2WTd9TJ3n93LmMYRhzZouR"
PATH = f"/api/address/{ADDRESS}"


@pytest_asyncio.fixture
async def lookup():
    payload = {"address": ADDRESS, "balance": str(MAX_AMOUNT), "blockNumber": 7132,
               "blockHash": "ab" * 32}
    upstream = SimpleNamespace(payload=payload, status=200, requests=[], delay=0)

    async def balance(request):
        upstream.requests.append(request.path_qs)
        if upstream.delay:
            await asyncio.sleep(upstream.delay)
        if isinstance(upstream.payload, bytes):
            return web.Response(body=upstream.payload, content_type="application/json")
        return web.json_response(upstream.payload, status=upstream.status)

    app = web.Application()
    app.router.add_get(f"/api/balance/{ADDRESS}", balance)
    async with TestServer(app) as observer:
        node = GrpcNodeClient(node_host=observer.host, http_port=observer.port)
        node.verified_token_decimals = 18
        try:
            server = MonitoringServer(SimpleNamespace(client=node))
            async with TestClient(TestServer(server.app)) as api:
                yield api, upstream, node
        finally:
            await node.close()


@pytest.mark.asyncio
async def test_balance_and_block_come_from_one_fresh_observer_read(lookup):
    api, upstream, _ = lookup
    for raw, height, block_hash in [(str(MAX_AMOUNT), 7132, "ab" * 32), ("0", 7133, "cd" * 32)]:
        upstream.payload.update(balance=raw, blockNumber=height, blockHash=block_hash)
        response = await api.get(PATH)
        assert response.status == 200
        assert await response.json() == {
            "address": ADDRESS, "balance": raw, "token_decimals": 18,
            "block_hash": block_hash, "block_number": height,
        }
    assert upstream.requests == [f"/api/balance/{ADDRESS}"] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["invalid", ADDRESS[:-1] + "S", ADDRESS + "%20"])
async def test_invalid_address_skips_observer(lookup, address):
    api, upstream, _ = lookup
    response = await api.get(f"/api/address/{address}")
    assert response.status == 400
    assert upstream.requests == []


@pytest.mark.asyncio
async def test_observer_timeout_returns_unavailable(lookup):
    api, upstream, node = lookup
    upstream.delay = 0.2
    node.timeout = 0.05
    response = await api.get(PATH)
    assert response.status == 503
    assert await response.json() == {"error": "Balance unavailable"}


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", [
    {"address": "another"}, {"balance": 0}, {"balance": None}, {"balance": "1.5"},
    {"balance": "-1"}, {"balance": str(MAX_AMOUNT + 1)}, {"blockHash": "ab"},
    {"blockNumber": True}, {"blockNumber": -1},
])
async def test_malformed_snapshot_is_unavailable(lookup, fields):
    api, upstream, _ = lookup
    upstream.payload.update(fields)
    response = await api.get(PATH)
    assert response.status == 503
    assert await response.json() == {"error": "Balance unavailable"}


@pytest.mark.asyncio
async def test_balance_unavailable_until_precision_verified_and_observer_responds(lookup):
    api, upstream, node = lookup
    node.verified_token_decimals = None
    assert (await api.get(PATH)).status == 503
    assert upstream.requests == []
    node.verified_token_decimals = 8
    upstream.status = 504
    assert (await api.get(PATH)).status == 503
    upstream.status = 200
    assert (await (await api.get(PATH)).json())["token_decimals"] == 8


@pytest.mark.parametrize("address", [
    "1" * 55, ADDRESS[:-1] + "S", " " + ADDRESS, ADDRESS + " ", ADDRESS + "\n",
    ADDRESS[:-1] + "é", ADDRESS[:-1] + "0",
])
def test_rejects_invalid_vault_address(address):
    assert not is_valid_vault_address(address)


def test_rejects_wrong_prefix_with_valid_checksum():
    payload = b"\x00\x00\x00\x01" + bytes(32)
    checksum = hashlib.blake2b(payload, digest_size=32).digest()[:4]
    assert not is_valid_vault_address(base58.b58encode(payload + checksum).decode())


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [[], b"invalid JSON"])
async def test_invalid_observer_body_returns_unavailable(lookup, payload):
    api, upstream, _ = lookup
    upstream.payload = payload
    assert (await api.get(PATH)).status == 503


@pytest.mark.asyncio
async def test_transfers_route_works_before_node_client_is_ready(monkeypatch):
    server = MonitoringServer(SimpleNamespace(client=None))
    load = AsyncMock(return_value={"address": ADDRESS, "transfers": []})
    monkeypatch.setattr(server, "_load_address_transfers", load)
    async with TestClient(TestServer(server.app)) as api:
        assert (await api.get(PATH)).status == 503
        response = await api.get(PATH + "/transfers?page=3&limit=7")
        assert response.status == 200
        assert (await response.json())["transfers"] == []
        load.assert_awaited_once_with(ADDRESS, 3, 7)


@pytest.mark.asyncio
async def test_concurrent_reads_share_the_slot_and_release_it_on_failure(monkeypatch):
    monkeypatch.setattr("src.monitoring.MAX_BALANCE_LOOKUPS", 1)
    started, release = asyncio.Event(), asyncio.Event()

    async def read(address):
        started.set()
        await release.wait()
        return None

    client = SimpleNamespace(get_address_balance=AsyncMock(side_effect=read))
    server = MonitoringServer(SimpleNamespace(client=client))
    request = SimpleNamespace(match_info={"address": ADDRESS})
    other = SimpleNamespace(match_info={
        "address": "1111Tk7mMo8CUCdjBYiQKAFtPiK3spwPWyYBG2o6cVSG8ahqxpFNR"
    })
    first = asyncio.create_task(server.get_address_balance(request))
    await asyncio.wait_for(started.wait(), timeout=2)
    second = asyncio.create_task(server.get_address_balance(request))
    await asyncio.sleep(0)  # Let the second handler attach to the same in-flight read.
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    busy = await server.get_address_balance(other)
    assert busy.status == 503
    assert busy.headers["Retry-After"] == "1"
    release.set()
    assert (await second).status == 503
    client.get_address_balance.assert_awaited_once_with(ADDRESS)
    client.get_address_balance.side_effect = None
    client.get_address_balance.return_value = {"balance": "1"}
    assert (await server.get_address_balance(other)).status == 200
