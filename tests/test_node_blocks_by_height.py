import json
from contextlib import asynccontextmanager

import grpc
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import DeployServiceV1_pb2 as service
import ServiceError_pb2 as errors
from src.grpc_node_client import (
    BLOCK_AVAILABLE,
    BLOCK_MISSING,
    BLOCK_PRUNED,
    BLOCK_UNAVAILABLE,
    GrpcNodeClient,
    common,
)

HASH_A = "aa" * 32
HASH_B = "bb" * 32
HASH_C = "cc" * 32


@asynccontextmanager
async def by_heights_node(responses):
    """A DeployService that answers getBlocksByHeights with the given items."""

    requests = []

    async def blocks(request, context):
        requests.append(request)
        for response in responses:
            yield response

    handlers = {
        "getBlocksByHeights": grpc.unary_stream_rpc_method_handler(
            blocks,
            request_deserializer=common.BlocksQueryByHeight.FromString,
            response_serializer=service.BlockInfoResponse.SerializeToString,
        )
    }
    server = grpc.aio.server()
    server.add_generic_rpc_handlers(
        (grpc.method_handlers_generic_handler("casper.v1.DeployService", handlers),)
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    client = GrpcNodeClient(node_host="127.0.0.1", grpc_port=port)
    try:
        yield client, requests
    finally:
        await client.close()
        await server.stop(None)


def served(height):
    return service.BlockInfoResponse(
        blockInfo=common.LightBlockInfo(blockHash=f"{height:064x}", blockNumber=height)
    )


@pytest.mark.asyncio
async def test_page_splits_served_pruned_and_unavailable_and_opts_in():
    async with by_heights_node(
        [
            served(10),
            service.BlockInfoResponse(
                pruned=service.BlockPrunedInfo(
                    hash=bytes.fromhex(HASH_B), block_height=9, prune_floor=9
                )
            ),
            service.BlockInfoResponse(
                unavailable=service.BlockUnavailableInfo(
                    hash=bytes.fromhex(HASH_C), block_height=11, prune_floor=9
                )
            ),
        ]
    ) as node:
        client, requests = node
        page = await client.get_blocks_by_height(9, 11)

    assert requests[0].startBlockNumber == 9
    assert requests[0].endBlockNumber == 11
    assert requests[0].include_missing is True
    assert [block["blockNumber"] for block in page.blocks] == [10]
    assert [
        (entry.block_hash, entry.block_height, entry.prune_floor)
        for entry in page.pruned
    ] == [(HASH_B, 9, 9)]
    assert [
        (entry.block_hash, entry.block_height, entry.prune_floor)
        for entry in page.unavailable
    ] == [(HASH_C, 11, 9)]
    assert page.fully_pruned_heights() == {9: [HASH_B]}


@pytest.mark.asyncio
async def test_observer_without_the_split_returns_served_blocks_only():
    async with by_heights_node([served(10), served(11)]) as node:
        client, requests = node
        page = await client.get_blocks_by_height(10, 11)

    # An observer that ignores the new field answers exactly as before.
    assert [block["blockNumber"] for block in page.blocks] == [10, 11]
    assert page.pruned == []
    assert page.unavailable == []
    assert page.prune_floor is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        service.BlockInfoResponse(error=errors.ServiceError(messages=["boom"])),
        service.BlockInfoResponse(),
    ],
    ids=["error-variant", "unset-variant"],
)
async def test_a_payload_that_is_not_a_block_fails_the_page(payload):
    async with by_heights_node([served(10), payload]) as node:
        client, _ = node
        page = await client.get_blocks_by_height(10, 10)

    assert page is None


@pytest.mark.asyncio
async def test_a_missing_entry_without_a_hash_fails_the_page():
    async with by_heights_node(
        [
            service.BlockInfoResponse(
                pruned=service.BlockPrunedInfo(hash=b"", block_height=9, prune_floor=9)
            )
        ]
    ) as node:
        client, _ = node
        page = await client.get_blocks_by_height(9, 9)

    assert page is None


@asynccontextmanager
async def http_node(status, body):
    app = web.Application()

    async def block(request):
        return web.Response(
            status=status, text=json.dumps(body), content_type="application/json"
        )

    app.router.add_get("/api/block/{block_hash}", block)
    server = TestServer(app)
    await server.start_server()
    try:
        yield server
    finally:
        await server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body,expected",
    [
        (200, {"blockInfo": {}}, BLOCK_AVAILABLE),
        (410, {"error": "block_pruned"}, BLOCK_PRUNED),
        (503, {"error": "block_unavailable"}, BLOCK_UNAVAILABLE),
        (404, {"error": "block_not_found"}, BLOCK_MISSING),
        # A busy observer is not a statement about this block.
        (503, {"error": "observer_busy"}, None),
        (410, {"error": "something_else"}, None),
        (502, {}, None),
    ],
)
async def test_http_availability_uses_only_documented_pairs(status, body, expected):
    async with http_node(status, body) as server:
        client = GrpcNodeClient(
            node_host="127.0.0.1", grpc_port=1, http_port=server.port
        )
        try:
            assert await client.get_block_availability(HASH_A) == expected
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_http_availability_returns_none_on_transport_failure():
    client = GrpcNodeClient(node_host="127.0.0.1", grpc_port=1, http_port=1)
    try:
        assert await client.get_block_availability(HASH_A) is None
    finally:
        await client.close()
