"""Independent opposite-profile wire fields, including a schema switch after startup."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import grpc
import pytest
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from src.grpc_node_client import PROFILE, GrpcNodeClient, _to_dict, common
import CasperMessage_pb2 as casper
import DeployServiceV1_pb2 as service
from test_native_transfer_lifecycle import ledger  # noqa: F401
from test_node_exact_api import BLOCK_HASH, DEPLOY_ID


def opposite_money(message, classic_number, uint256_number, *, binary=False):
    """Append an independently encoded incompatible field, retaining valid fields."""
    schema = descriptor_pb2.FileDescriptorProto(name="other.proto", syntax="proto3")
    money = schema.message_type.add(name="Money")
    money.field.add(
        name="value",
        label=1,
        number=classic_number if PROFILE == "uint256" else uint256_number,
        type=3 if PROFILE == "uint256" else (12 if binary else 9),
    )
    pool = descriptor_pool.DescriptorPool()
    pool.Add(schema)
    other = message_factory.GetMessageClass(pool.FindMessageTypeByName("Money"))
    value = 1 if PROFILE == "uint256" else (bytes(31) + b"\x01" if binary else "1")
    return type(message).FromString(
        message.SerializeToString() + other(value=value).SerializeToString()
    )


@pytest.mark.parametrize(
    "name,field,old,new,binary",
    [
        ("BondInfo", "stake", 2, 3, False),
        ("DeployInfo", "phloPrice", 7, 16, False),
        ("Status", "minPhloPrice", 7, 18, False),
        ("DeployDataProto", "phloPrice", 7, 15, True),
        ("BondProto", "stake", 2, 3, True),
    ],
)
def test_mixed_monetary_profiles_are_rejected_even_with_valid_current_fields(
    name, field, old, new, binary
):
    module = casper if binary else common
    value = (1).to_bytes(32, "big") if binary else "1"
    message = getattr(module, name)(**{field: value if PROFILE == "uint256" else 1})
    with pytest.raises(ValueError, match="schema"):
        _to_dict(opposite_money(message, old, new, binary=binary))
    # The selected field number with an incompatible wire type is equally unsafe.
    number = message.DESCRIPTOR.fields_by_name[field].number
    with pytest.raises(ValueError, match="schema"):
        _to_dict(opposite_money(message, number, number, binary=binary))


@asynccontextmanager
async def switching_node(damage):
    state = SimpleNamespace(wrong=False)
    price = "10" if PROFILE == "uint256" else 10
    stake = "100" if PROFILE == "uint256" else 100

    def light():
        bond = common.BondInfo(validator="validator", stake=stake)
        if state.wrong and damage == "bond":
            # Only the opposite field is sent, reproducing classic zero defaults.
            bond = opposite_money(common.BondInfo(validator="validator"), 2, 3)
        return common.LightBlockInfo(blockHash=BLOCK_HASH, blockNumber=1, bonds=[bond])

    def full():
        deploy = common.DeployInfo(sig=DEPLOY_ID, phloPrice=price)
        if state.wrong and damage == "price":
            deploy = opposite_money(common.DeployInfo(sig=DEPLOY_ID), 7, 16)
        return common.BlockInfo(blockInfo=light(), deploys=[deploy])

    async def status(request, context):
        status = common.Status(
            minPhloPrice=price, nativeTokenSymbol="ASI", nativeTokenDecimals=18
        )
        if state.wrong:
            status = opposite_money(status, 7, 18)
        return service.StatusResponse(status=status)

    async def block(request, context):
        return service.BlockResponse(blockInfo=full())

    async def finalized(request, context):
        return service.LastFinalizedBlockResponse(blockInfo=full())

    async def find(request, context):
        return service.FindDeployResponse(blockInfo=light())

    async def blocks(request, context):
        yield service.BlockInfoResponse(blockInfo=light())

    async def pending(request, context):
        deploy = casper.DeployDataProto(
            phloPrice=(10).to_bytes(32, "big") if PROFILE == "uint256" else 10
        )
        if state.wrong:
            deploy = opposite_money(deploy, 7, 15, binary=True)
        return service.PendingDeploysResponse(
            payload=common.PendingDeploysResponsePayload(
                deploys=[common.PendingDeployInfo(deploy=deploy)],
                totalAvailable=1,
            )
        )

    handlers = {}
    for name, response, handler in [
        ("status", service.StatusResponse, status),
        ("getBlock", service.BlockResponse, block),
        ("lastFinalizedBlock", service.LastFinalizedBlockResponse, finalized),
        ("findDeploy", service.FindDeployResponse, find),
        ("getPendingDeploys", service.PendingDeploysResponse, pending),
    ]:
        handlers[name] = grpc.unary_unary_rpc_method_handler(
            handler,
            response_serializer=response.SerializeToString,
        )
    for name in ("getBlocksByHeights", "showMainChain"):
        handlers[name] = grpc.unary_stream_rpc_method_handler(
            blocks,
            response_serializer=service.BlockInfoResponse.SerializeToString,
        )
    server = grpc.aio.server()
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "casper.v1.DeployService",
                handlers,
            ),
        )
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    client = GrpcNodeClient(node_host="127.0.0.1", grpc_port=port)
    try:
        yield client, state
    finally:
        await client.close()
        await server.stop(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["bond", "price"])
async def test_every_read_rejects_incompatible_money_after_successful_startup(damage):
    async with switching_node(damage) as (client, state):
        await client.require_native_precision(18)
        assert await client.health_check()
        assert (await client.get_block_details(BLOCK_HASH))["deploys"][0][
            "phloPrice"
        ] == 10
        assert (await client.get_pending_deploys())["deploys"][0]["phloPrice"] == 10
        state.wrong = True
        assert await client.get_block_details(BLOCK_HASH) is None
        assert await client.get_deploy_info(DEPLOY_ID) is None
        assert await client.get_pending_deploys() is None
        assert not await client.health_check()
        if damage == "bond":
            assert await client.get_last_finalized_block() is None
            assert await client.get_bonds() is None
            assert await client.get_blocks_by_height(1, 1) == []
            assert await client.show_main_chain() is None


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["bond", "price"])
async def test_schema_switch_cannot_persist_defaults_or_advance_cursor(
    ledger, monkeypatch, damage  # noqa: F811
):
    await ledger.db.set_last_indexed_block(0)
    monkeypatch.setattr("src.block_indexer.settings.start_from_block", 1)
    async with switching_node(damage) as (client, state):
        await client.require_native_precision(18)
        state.wrong = True
        ledger.indexer.client = client
        await ledger.indexer._sync_blocks()
    assert await ledger.db.get_last_indexed_block() == 0
    for table in ("blocks", "deployments", "validators", "validator_bonds"):
        assert await ledger.conn.fetchval(f"SELECT count(*) FROM {table}") == 0
