from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import grpc
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from src.amounts import MAX_AMOUNT
from src.grpc_node_client import GrpcNodeClient, _to_dict, common
import DeployServiceV1_pb2 as service
import ServiceError_pb2 as errors


BLOCK_HASH = "ab" * 32
DEPLOY_ID = "12" * 32
MAX_INT64 = 2**63 - 1


def legacy_queries():
    schema = descriptor_pb2.FileDescriptorProto(
        name="legacy_queries.proto", package="legacy", syntax="proto3"
    )
    block = schema.message_type.add(name="BlockQuery")
    block.field.add(
        name="hash",
        number=1,
        type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
    )
    schema.message_type.add(name="LastFinalizedBlockQuery")
    pool = descriptor_pool.DescriptorPool()
    pool.Add(schema)
    return SimpleNamespace(
        **{
            name: message_factory.GetMessageClass(
                pool.FindMessageTypeByName("legacy." + name)
            )
            for name in ("BlockQuery", "LastFinalizedBlockQuery")
        }
    )


@asynccontextmanager
async def node_api(raw, *, legacy=False, available=True, error=None, amount_raw=None):
    queries = legacy_queries() if legacy else common
    representable = -(2**63) <= raw <= MAX_INT64
    transfer = common.TransferInfo(
        fromAddr="sender",
        toAddr="receiver",
        amount=raw if representable else -1,
        success=0 < raw <= MAX_AMOUNT,
        failReason="" if 0 < raw <= MAX_AMOUNT else "Rejected amount",
    )
    if not legacy:
        transfer.amountRaw = str(raw) if amount_raw is None else amount_raw
    block = common.BlockInfo(
        blockInfo=common.LightBlockInfo(
            blockHash=BLOCK_HASH,
            blockNumber=12,
            bonds=[common.BondInfo(validator="validator", stake=MAX_INT64)],
        ),
        deploys=[
            common.DeployInfo(
                sig=DEPLOY_ID,
                phloLimit=MAX_INT64,
                phloPrice=7,
                cost=2**53 + 1,
                transfersAvailable=available,
                transfers=[transfer] if available else [],
            )
        ],
    )
    requests = []

    def block_response(response_type, request):
        requests.append(request)
        reason = error
        if (
            not legacy
            and not request.exactTransferAmounts
            and not representable
            and available
        ):
            reason = "TRANSFER_AMOUNT_OUT_OF_RANGE: set exactTransferAmounts=true"
        if reason:
            return response_type(error=errors.ServiceError(messages=[reason]))
        return response_type(blockInfo=block)

    async def get_block(request, context):
        assert request.hash == BLOCK_HASH
        return block_response(service.BlockResponse, request)

    async def last_finalized(request, context):
        return block_response(service.LastFinalizedBlockResponse, request)

    async def find_deploy(request, context):
        assert request.deployId == bytes.fromhex(DEPLOY_ID)
        return service.FindDeployResponse(blockInfo=block.blockInfo)

    handlers = {
        "getBlock": grpc.unary_unary_rpc_method_handler(
            get_block,
            request_deserializer=queries.BlockQuery.FromString,
            response_serializer=service.BlockResponse.SerializeToString,
        ),
        "lastFinalizedBlock": grpc.unary_unary_rpc_method_handler(
            last_finalized,
            request_deserializer=queries.LastFinalizedBlockQuery.FromString,
            response_serializer=service.LastFinalizedBlockResponse.SerializeToString,
        ),
        "findDeploy": grpc.unary_unary_rpc_method_handler(
            find_deploy,
            request_deserializer=common.FindDeployQuery.FromString,
            response_serializer=service.FindDeployResponse.SerializeToString,
        ),
    }
    server = grpc.aio.server()
    server.add_generic_rpc_handlers(
        (grpc.method_handlers_generic_handler("casper.v1.DeployService", handlers),)
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    client = GrpcNodeClient(node_host="127.0.0.1", grpc_port=port)
    client.timeout = 2
    try:
        yield SimpleNamespace(client=client, requests=requests)
    finally:
        await client.close()
        await server.stop(None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        1,
        2**53 + 1,
        MAX_INT64,
        2**63,
        10**19,
        2**128,
        MAX_AMOUNT,
        0,
        -1,
        2**512,
        -(2**512),
    ],
)
async def test_exact_block_reads_and_deploy_lookup_keep_all_amounts(raw):
    async with node_api(raw) as node:
        if not -(2**63) <= raw <= MAX_INT64:
            legacy = await node.client.stub.getBlock(
                common.BlockQuery(hash=BLOCK_HASH), timeout=node.client.timeout
            )
            assert legacy.WhichOneof("message") == "error"
            assert legacy.error.messages[0].startswith("TRANSFER_AMOUNT_OUT_OF_RANGE:")
            node.requests.clear()

        details = await node.client.get_block_details(BLOCK_HASH)
        transfer = details["deploys"][0]["transfers"][0]
        legacy_amount = raw if -(2**63) <= raw <= MAX_INT64 else -1
        assert (transfer["amount"], transfer["amountRaw"]) == (legacy_amount, str(raw))
        assert transfer["success"] == (0 < raw <= MAX_AMOUNT)
        assert transfer["failReason"] == (
            "" if 0 < raw <= MAX_AMOUNT else "Rejected amount"
        )
        assert details["deploys"][0]["transfersAvailable"] is True
        assert details["deploys"][0]["phloLimit"] == MAX_INT64
        assert details["deploys"][0]["cost"] == 2**53 + 1

        latest = await node.client.get_last_finalized_block()
        assert latest["blockHash"] == BLOCK_HASH
        bonds = await node.client.get_bonds()
        assert bonds == {"bonds": [{"validator": "validator", "stake": MAX_INT64}]}
        deploy = await node.client.get_deploy_info(DEPLOY_ID)
        assert deploy["transfers"][0] == transfer
        assert len(node.requests) == 4
        assert all(request.exactTransferAmounts for request in node.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [0, 2**53 + 1, MAX_INT64, -2, -(2**63)])
async def test_old_node_ignores_query_extensions_and_preserves_int64(raw):
    async with node_api(raw, legacy=True) as node:
        details = await node.client.get_block_details(BLOCK_HASH)
        transfer = details["deploys"][0]["transfers"][0]
        assert (transfer["amount"], transfer["amountRaw"]) == (raw, str(raw))
        assert (await node.client.get_last_finalized_block())["blockNumber"] == 12
        assert (await node.client.get_bonds())["bonds"][0]["stake"] == MAX_INT64
        assert (await node.client.get_deploy_info(DEPLOY_ID))["transfers"][
            0
        ] == transfer
        assert len(node.requests) == 4
        assert all(
            not hasattr(request, "exactTransferAmounts") for request in node.requests
        )


@pytest.mark.parametrize("legacy_amount", [-1, 7])
def test_exact_field_takes_precedence_over_legacy_amount(legacy_amount):
    raw = str(MAX_AMOUNT)
    deploy = common.DeployInfo(
        transfers=[common.TransferInfo(amount=legacy_amount, amountRaw=raw)]
    )
    transfer = _to_dict(deploy)["transfers"][0]
    assert transfer["amount"] == legacy_amount
    assert transfer["amountRaw"] == raw


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_missing_exact_amount_rejects_ambiguous_sentinel(legacy):
    async with node_api(-1, legacy=legacy, amount_raw="") as node:
        with pytest.raises(ValueError, match="ambiguous"):
            await node.client.get_block_details(BLOCK_HASH)
        # A deploy lookup fails closed through its existing ValueError handler.
        assert await node.client.get_deploy_info(DEPLOY_ID) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw", ["1e5", " 12", "12 ", "0x10", "+1", "01", "-01", "-0", "1.0", "١"]
)
async def test_malformed_exact_amount_does_not_escape_in_block_or_deploy(raw):
    async with node_api(1, amount_raw=raw) as node:
        with pytest.raises(ValueError, match="canonical signed integer"):
            await node.client.get_block_details(BLOCK_HASH)
        assert await node.client.get_deploy_info(DEPLOY_ID) is None


@pytest.mark.asyncio
async def test_network_consensus_reuses_one_finalized_block():
    async with node_api(MAX_AMOUNT) as node:
        node.client.get_active_validators = AsyncMock(
            return_value=[{"validator": "validator", "stake": 1}]
        )
        consensus = await node.client.get_network_consensus()
        assert consensus["current_block"] == 12
        assert consensus["total_bonded_validators"] == 1
        assert consensus["active_validators"] == 1
        assert consensus["participation_rate"] == 100
        assert len(node.requests) == 1
        assert node.requests[0].exactTransferAmounts is True


@pytest.mark.asyncio
async def test_network_consensus_does_not_invent_missing_finalized_state():
    async with node_api(1, error="Finalized block unavailable") as node:
        node.client.get_active_validators = AsyncMock(return_value=[])
        assert await node.client.get_network_consensus() is None
        assert len(node.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_unavailable_summary_is_not_a_zero_transfer(legacy):
    async with node_api(MAX_INT64, legacy=legacy, available=False) as node:
        details = await node.client.get_block_details(BLOCK_HASH)
        deploy = details["deploys"][0]
        assert deploy["transfersAvailable"] is False
        assert deploy["transfers"] == []
        assert (await node.client.get_last_finalized_block())["blockNumber"] == 12


@pytest.mark.asyncio
async def test_service_error_is_not_treated_as_an_empty_block():
    async with node_api(
        MAX_AMOUNT, error="TRANSFER_AMOUNT_OUT_OF_RANGE: unavailable"
    ) as node:
        assert await node.client.get_block_details(BLOCK_HASH) is None
        assert await node.client.get_last_finalized_block() is None
        assert await node.client.get_bonds() is None
        assert await node.client.get_deploy_info(DEPLOY_ID) is None
        assert len(node.requests) == 4
        assert all(request.exactTransferAmounts for request in node.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("stake", [MAX_INT64, str(MAX_INT64)])
async def test_validator_http_supports_legacy_numbers_and_exact_strings(stake):
    async def validators(request):
        return web.json_response(
            {"validators": [{"publicKey": "validator", "stake": stake}]}
        )

    app = web.Application()
    app.router.add_get("/api/validators", validators)
    async with TestServer(app, host="127.0.0.1") as server:
        client = GrpcNodeClient(node_host="127.0.0.1", http_port=server.port)
        try:
            assert await client.get_active_validators() == [
                {"validator": "validator", "stake": MAX_INT64}
            ]
        finally:
            await client.close()
