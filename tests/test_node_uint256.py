"""Pinned #881 wire fixtures; no live node or signing is involved."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import grpc
import pytest
import os
import subprocess
import sys
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from sqlalchemy import select

from src.amounts import MAX_AMOUNT
from src.config import Settings
from src.grpc_node_client import PROFILE, GrpcNodeClient, _to_dict, common
from src.models import Deployment, PendingDeploy, ValidatorBond
import CasperMessage_pb2 as casper
import DeployServiceV1_pb2 as service
from test_native_transfer_lifecycle import ledger  # noqa: F401
from test_node_exact_api import BLOCK_HASH, DEPLOY_ID
from test_transfer_reports import payment, report

uint256 = pytest.mark.skipif(PROFILE != "uint256", reason="uint256 wire profile")


def test_profile_rejects_unknown_names():
    with pytest.raises(ValueError):
        Settings(_env_file=None, node_protocol_profile="auto")


def test_bindings_cannot_be_reused_with_another_runtime_profile():
    env = {
        **os.environ,
        "NODE_PROTOCOL_PROFILE": "classic" if PROFILE == "uint256" else "uint256",
    }
    result = subprocess.run(
        [sys.executable, "-c", "import src.grpc_node_client"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert "differs from generated bindings" in result.stderr


@uint256
def test_schema_uses_final_881_fields_and_keeps_counters_bounded():
    for message, name, number, kind in [
        (common.BondInfo, "stake", 3, 9),
        (common.DeployInfo, "phloPrice", 16, 9),
        (common.Status, "minPhloPrice", 18, 9),
        (common.TransferInfo, "amountRaw", 6, 9),
        (casper.DeployDataProto, "phloPrice", 15, 12),
        (casper.BondProto, "stake", 3, 12),
        (common.LightBlockInfo, "blockNumber", 12, 3),
        (common.DeployInfo, "phloLimit", 8, 3),
    ]:
        field = message.DESCRIPTOR.fields_by_name[name]
        assert (field.number, field.type) == (number, kind)
    assert "amount" not in common.TransferInfo.DESCRIPTOR.fields_by_name
    assert "exactTransferAmounts" not in common.BlockQuery.DESCRIPTOR.fields_by_name
    assert (
        "exactTransferAmounts"
        not in common.LastFinalizedBlockQuery.DESCRIPTOR.fields_by_name
    )


@uint256
@pytest.mark.parametrize("raw", [0, 2**53 + 1, 2**63, 2**128, MAX_AMOUNT])
def test_wide_stakes_prices_and_pending_bytes_roundtrip(raw):
    bond = common.BondInfo.FromString(
        common.BondInfo(stake=str(raw)).SerializeToString()
    )
    assert _to_dict(bond)["stake"] == raw
    assert _to_dict(common.DeployInfo(phloPrice=str(raw)))["phloPrice"] == raw
    assert _to_dict(common.Status(minPhloPrice=str(raw)))["minPhloPrice"] == raw
    pending = casper.DeployDataProto(phloPrice=raw.to_bytes(32, "big"))
    assert _to_dict(pending)["phloPrice"] == raw
    assert _to_dict(casper.BondProto(stake=raw.to_bytes(32, "big")))["stake"] == raw


@uint256
@pytest.mark.parametrize(
    "raw", ["", "01", "+1", "-1", "1e6", "1.0", str(MAX_AMOUNT + 1)]
)
def test_invalid_monetary_strings_never_default_to_zero(raw):
    for message in [
        common.BondInfo(stake=raw),
        common.DeployInfo(phloPrice=raw),
        common.Status(minPhloPrice=raw),
    ]:
        with pytest.raises(ValueError):
            _to_dict(message)


@uint256
@pytest.mark.parametrize("size", [0, 1, 31, 33])
def test_pending_price_requires_exactly_32_bytes(size):
    with pytest.raises(ValueError, match="32 bytes"):
        _to_dict(casper.DeployDataProto(phloPrice=b"\x01" * size))


@uint256
@pytest.mark.parametrize("raw", ["0", "-1", str(MAX_AMOUNT + 1), "-" + "9" * 4301])
def test_failed_transfer_attempts_preserve_signed_unbounded_text(raw):
    deploy = _to_dict(
        common.DeployInfo(
            phloPrice="1",
            transfers=[
                common.TransferInfo(amountRaw=raw, success=False),
            ],
        )
    )
    assert deploy["transfers"][0]["amountRaw"] == raw


@pytest.mark.asyncio
async def test_wrong_node_profile_is_rejected_at_startup():
    # Independent schema emits the OTHER profile's field, which these bindings
    # would otherwise silently discard and replace with zero/empty defaults.
    schema = descriptor_pb2.FileDescriptorProto(name="opposite.proto", syntax="proto3")
    status = schema.message_type.add(name="Status")
    field = status.field.add(
        name="minPhloPrice",
        number=7 if PROFILE == "uint256" else 18,
        type=3 if PROFILE == "uint256" else 9,
        label=1,
    )
    pool = descriptor_pool.DescriptorPool()
    pool.Add(schema)
    other = message_factory.GetMessageClass(pool.FindMessageTypeByName("Status"))
    payload = other(minPhloPrice=1 if field.type == 3 else "1").SerializeToString()
    client = GrpcNodeClient.__new__(GrpcNodeClient)
    client.timeout = 1
    client.stub = SimpleNamespace(
        status=AsyncMock(
            return_value=service.StatusResponse(
                status=common.Status.FromString(payload)
            )
        )
    )
    with pytest.raises(ValueError, match="schema"):
        await client.require_native_precision(8)


@uint256
@pytest.mark.asyncio
async def test_all_consumed_rpc_paths_decode_wide_money():
    light = common.LightBlockInfo(
        blockHash=BLOCK_HASH,
        blockNumber=12,
        bonds=[common.BondInfo(validator="validator", stake=str(MAX_AMOUNT))],
    )
    full = common.BlockInfo(
        blockInfo=light,
        deploys=[
            common.DeployInfo(
                sig=DEPLOY_ID,
                phloPrice=str(MAX_AMOUNT),
                phloLimit=2**63 - 1,
                transfersAvailable=True,
                transfers=[
                    common.TransferInfo(amountRaw=str(MAX_AMOUNT), success=True)
                ],
            )
        ],
    )
    pending = common.PendingDeploysResponsePayload(
        deploys=[
            common.PendingDeployInfo(
                deploy=casper.DeployDataProto(
                    sig=bytes.fromhex(DEPLOY_ID),
                    deployer=b"\x11",
                    phloPrice=MAX_AMOUNT.to_bytes(32, "big"),
                ),
                isRejected=True,
            )
        ],
        totalAvailable=1,
    )

    async def full_block(request, context):
        return service.BlockResponse(blockInfo=full)

    async def finalized(request, context):
        return service.LastFinalizedBlockResponse(blockInfo=full)

    async def find(request, context):
        return service.FindDeployResponse(blockInfo=light)

    async def blocks(request, context):
        yield service.BlockInfoResponse(blockInfo=light)

    async def pending_deploys(request, context):
        return service.PendingDeploysResponse(payload=pending)

    async def status(request, context):
        return service.StatusResponse(
            status=common.Status(
                minPhloPrice=str(MAX_AMOUNT),
                nativeTokenSymbol="ASI",
                nativeTokenDecimals=18,
            )
        )

    handlers = {}
    for name, query, response, handler in [
        ("getBlock", common.BlockQuery, service.BlockResponse, full_block),
        (
            "lastFinalizedBlock",
            common.LastFinalizedBlockQuery,
            service.LastFinalizedBlockResponse,
            finalized,
        ),
        ("findDeploy", common.FindDeployQuery, service.FindDeployResponse, find),
        (
            "getPendingDeploys",
            common.PendingDeploysQuery,
            service.PendingDeploysResponse,
            pending_deploys,
        ),
    ]:
        handlers[name] = grpc.unary_unary_rpc_method_handler(
            handler,
            request_deserializer=query.FromString,
            response_serializer=response.SerializeToString,
        )
    from google.protobuf.empty_pb2 import Empty

    handlers["status"] = grpc.unary_unary_rpc_method_handler(
        status,
        request_deserializer=Empty.FromString,
        response_serializer=service.StatusResponse.SerializeToString,
    )
    for name, query in [
        ("getBlocksByHeights", common.BlocksQueryByHeight),
        ("showMainChain", common.BlocksQuery),
    ]:
        handlers[name] = grpc.unary_stream_rpc_method_handler(
            blocks,
            request_deserializer=query.FromString,
            response_serializer=service.BlockInfoResponse.SerializeToString,
        )
    server = grpc.aio.server()
    server.add_generic_rpc_handlers(
        (grpc.method_handlers_generic_handler("casper.v1.DeployService", handlers),)
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    client = GrpcNodeClient(node_host="127.0.0.1", grpc_port=port)
    try:
        await client.require_native_precision(18)
        assert await client.health_check()
        assert (await client.get_last_finalized_block())["bonds"][0][
            "stake"
        ] == MAX_AMOUNT
        assert (await client.get_bonds())["bonds"][0]["stake"] == MAX_AMOUNT
        assert (await client.get_blocks_by_height(12, 12))[0]["bonds"][0][
            "stake"
        ] == MAX_AMOUNT
        assert (await client.show_main_chain())[0]["bonds"][0]["stake"] == MAX_AMOUNT
        assert (await client.get_block_details(BLOCK_HASH))["deploys"][0][
            "phloPrice"
        ] == MAX_AMOUNT
        assert (await client.get_deploy_info(DEPLOY_ID))["phloPrice"] == MAX_AMOUNT
        result = await client.get_pending_deploys()
        assert result["deploys"][0]["phloPrice"] == MAX_AMOUNT
        assert result["deploys"][0]["sig"] == DEPLOY_ID
        assert result["deploys"][0]["isRejected"] is True
    finally:
        await client.close()
        await server.stop(None)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wide_prices_stakes_and_units_survive_restart(ledger):  # noqa: F811
    # Exercise real block/deploy/bond writes and the pending snapshot transaction.
    details, replay = await ledger.add(
        1,
        "wide-money",
        [("wide-deploy", report(payment()))],
        bonds=[{"validator": "validator", "stake": MAX_AMOUNT}],
        process=False,
    )
    details["deploys"][0]["phloPrice"] = MAX_AMOUNT
    await ledger.indexer._process_block(details)
    pending = {
        "sig": "pending-wide",
        "deployer": "04" + "11" * 64,
        "phloPrice": MAX_AMOUNT,
        "timestamp": 1,
        "term": "Nil",
    }
    ledger.indexer.client.get_pending_deploys = AsyncMock(
        return_value={"deploys": [pending]}
    )
    await ledger.indexer._sync_pending_deploys()
    await ledger.db.disconnect()
    await ledger.db.connect()
    async with ledger.db.session() as session:
        assert (await session.get(Deployment, "wide-deploy")).phlo_price == MAX_AMOUNT
        assert (
            await session.get(PendingDeploy, "pending-wide")
        ).phlo_price == MAX_AMOUNT
        bond = (await session.execute(select(ValidatorBond))).scalar_one()
        assert bond.stake == MAX_AMOUNT
        assert bond.token_decimals == 18
    for table in ("deployments", "pending_deploys"):
        assert await ledger.conn.fetchval(f"SELECT phlo_price FROM {table}") == Decimal(
            MAX_AMOUNT
        )
        for invalid in (-1, MAX_AMOUNT + 1):
            import asyncpg

            with pytest.raises(asyncpg.CheckViolationError):
                await ledger.conn.execute(
                    f"UPDATE {table} SET phlo_price=$1", Decimal(invalid)
                )
