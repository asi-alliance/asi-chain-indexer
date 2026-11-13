import asyncio
import base64
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
import structlog

# generated stubs import each other by bare name, so their directory has to be on the path
sys.path.insert(0, str(Path(__file__).parent / "grpc_stubs"))
import grpc
import DeployServiceV1_pb2_grpc as svc
import DeployServiceCommon_pb2 as common
from google.protobuf import empty_pb2
from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.unknown_fields import UnknownFieldSet

from src.config import settings
from src.amounts import amount, canonical_signed_decimal
from src.off_loop import run_off_loop
from src.transfer_reports import trace_events

logger = structlog.get_logger(__name__)

PROFILE = settings.node_protocol_profile
STUB_PROFILE = (Path(__file__).parent / "grpc_stubs" / "protocol_profile.txt").read_text()
if STUB_PROFILE != PROFILE:
    raise RuntimeError("NODE_PROTOCOL_PROFILE differs from generated bindings; regenerate/rebuild")


def _block_query(block_hash):
    options = {"exactTransferAmounts": True} if PROFILE == "classic" else {}
    return common.BlockQuery(hash=block_hash, **options)


def _finalized_query():
    options = {"exactTransferAmounts": True} if PROFILE == "classic" else {}
    return common.LastFinalizedBlockQuery(**options)


def _unwrap(response, field_name: str):
    which = response.WhichOneof("message")
    if which is None:
        raise grpc.RpcError("Node returned no response payload")
    if which == "error":
        raise grpc.RpcError(str(response.error))
    return getattr(response, field_name)


def _transfer_to_dict(message) -> Dict[str, Any]:
    raw: Optional[str]
    if message.amountRaw:
        try:
            raw = canonical_signed_decimal(message.amountRaw)
        except ValueError:
            logger.warning(
                "Invalid transfer amountRaw; keeping unknown amount",
                amount_raw_length=len(message.amountRaw),
            )
            raw = None
    elif PROFILE == "uint256" or message.amount == -1:
        # Missing uint256 data, or an ambiguous legacy -1 sentinel, stays unknown.
        raw = None
    else:
        raw = str(message.amount)
    result = _to_dict(message)
    result["amountRaw"] = raw
    return result


_MESSAGE_DECODERS = {common.TransferInfo.DESCRIPTOR.full_name: _transfer_to_dict}
_MONETARY_FIELD_NUMBERS = {
    "casper.BondInfo": (2, 3),
    "casper.DeployInfo": (7, 16),
    "casper.Status": (7, 18),
    "casper.DeployDataProto": (7, 15),
    "casper.BondProto": (2, 3),
    "casper.TransferInfo": (3, 6),
}


def _require_monetary_profile(message):
    # Check every decoded monetary message, including nested data after reconnects.
    # Opposite-profile fields survive parsing as unknown fields; otherwise classic
    # scalar defaults would turn their missing values into plausible zeros.
    field_numbers = _MONETARY_FIELD_NUMBERS.get(message.DESCRIPTOR.full_name)
    if field_numbers is None:
        return
    # A monetary field of the wrong wire type is also unknown, even at a known
    # number. Reject it rather than replacing it with the proto3 default.
    if any(field.field_number in field_numbers for field in UnknownFieldSet(message)):
        raise ValueError("Node monetary schema does not match NODE_PROTOCOL_PROFILE")


def _field_value(field, value):
    if PROFILE == "uint256":
        name = (field.containing_type.name, field.name)
        if name in {("BondInfo", "stake"), ("DeployInfo", "phloPrice"),
                    ("Status", "minPhloPrice")}:
            return amount(value)  # Empty/new fields must never become zero.
        if name in {("DeployDataProto", "phloPrice"), ("BondProto", "stake")}:
            if len(value) != 32:
                raise ValueError("uint256 monetary wire field must contain exactly 32 bytes")
            return int.from_bytes(value, "big")
    if field.type == FieldDescriptor.TYPE_MESSAGE:
        decode = _MESSAGE_DECODERS.get(field.message_type.full_name, _to_dict)
        return decode(value)
    if field.type == FieldDescriptor.TYPE_BYTES:
        return base64.b64encode(value).decode()
    return value


def _to_dict(message) -> Dict[str, Any]:
    # Not MessageToDict: it renders int64 as strings and only unquotes below 2^53,
    # so genesis deploys (phloLimit=2^63-1) would break the BIGINT insert.
    _require_monetary_profile(message)
    result = {}
    for field in message.DESCRIPTOR.fields:
        value = getattr(message, field.name)
        if field.is_repeated:
            result[field.json_name] = [_field_value(field, item) for item in value]
        else:
            result[field.json_name] = _field_value(field, value)
    return result


class GrpcNodeClient:
    def __init__(
        self,
        node_host: Optional[str] = None,
        grpc_port: Optional[int] = None,
        http_port: Optional[int] = None,
    ):
        self.node_host = node_host or settings.node_host or "localhost"
        self.grpc_port = grpc_port or settings.grpc_port or 40452
        self.http_port = http_port or settings.http_port or 40453
        self.timeout = settings.node_timeout

        self.channel = grpc.aio.insecure_channel(
            f"{self.node_host}:{self.grpc_port}",
            options=[("grpc.max_receive_message_length", settings.grpc_max_receive_message_bytes)],
        )
        self.stub = svc.DeployServiceStub(self.channel)

    async def require_native_precision(self, expected: int) -> None:
        response = await self.stub.status(empty_pb2.Empty(), timeout=self.timeout)
        status = _unwrap(response, "status")
        _to_dict(status)
        # Old nodes omit all metadata (proto3 scalar defaults). Their units are 8 decimals.
        has_metadata = bool(status.nativeTokenName or status.nativeTokenSymbol)
        actual = status.nativeTokenDecimals if has_metadata else 8
        if actual != expected:
            raise ValueError(
                f"Node native token decimals ({actual}) do not match "
                f"NATIVE_TOKEN_DECIMALS ({expected})"
            )

    async def close(self):
        await self.channel.close()

    async def get_blocks_by_height(self, start: int, end: int) -> List[Dict[str, Any]]:
        try:
            query = common.BlocksQueryByHeight(startBlockNumber=start, endBlockNumber=end)

            blocks = []
            async for response in self.stub.getBlocksByHeights(query, timeout=self.timeout):
                blocks.append(_to_dict(_unwrap(response, "blockInfo")))

            return blocks

        except (grpc.RpcError, ValueError) as e:
            logger.error(f"Failed to get blocks by height {start}-{end}: {e}")
            return []

    async def get_last_finalized_block(self) -> Optional[Dict[str, Any]]:
        try:
            response = await self.stub.lastFinalizedBlock(
                _finalized_query(), timeout=self.timeout
            )
            wrapper = _unwrap(response, "blockInfo")
            return _to_dict(wrapper.blockInfo)

        except (grpc.RpcError, ValueError) as e:
            logger.error(f"Failed to get last finalized block: {e}")
            return None

    async def get_bonds(self) -> Optional[Dict[str, Any]]:
        block = await self.get_last_finalized_block()
        return {"bonds": block.get("bonds", [])} if block is not None else None

    async def get_active_validators(self) -> Optional[List[Dict[str, Any]]]:
        # activeValidators is PoS contract state, no gRPC RPC exposes it
        url = f"http://{self.node_host}:{self.http_port}/api/validators"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as session:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

            return [
                {"validator": v["publicKey"], "stake": amount(v["stake"])}
                for v in data.get("validators", [])
            ]

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error(f"Failed to get active validators: {e}")
            return None

    async def get_block_details(self, block_hash: str) -> Optional[Dict[str, Any]]:
        try:
            query = _block_query(block_hash)
            response = await self.stub.getBlock(query, timeout=self.timeout)
            return _to_dict(_unwrap(response, "blockInfo"))

        except (grpc.RpcError, ValueError) as e:
            logger.error(f"Failed to get block details for {block_hash}: {e}")
            return None

    async def get_block_events(self, block_hash: str, deploy_ids: List[str]) -> List[Any]:
        """Read the block's BigInt-capable replay trace; failures must retry the block.

        A trace can be tens of megabytes, so a worker process parses it and extracts its events.
        """
        url = f"http://{self.node_host}:{self.http_port}/reporting/trace"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as session:
            async with session.get(url, params={"blockHash": block_hash}) as response:
                response.raise_for_status()
                body = await response.read()
        return await run_off_loop(trace_events, body, block_hash, list(deploy_ids))

    async def get_pending_deploys(self, deployer_pubkey: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Fetch the node's pending deploy snapshot via the getPendingDeploys RPC.

        Returns {"deploys": [...], "totalAvailable": N}. Each deploy dict holds
        the DeployDataProto fields plus an "isRejected" provenance flag; the raw
        bytes fields (deployer, sig) are hex-encoded to match the deployments
        table, where sig/deploy_id are hex strings.
        """
        try:
            deployer_bytes = (
                bytes.fromhex(deployer_pubkey.lower().removeprefix("0x"))
                if deployer_pubkey else b""
            )
        except ValueError:
            logger.error(f"Invalid deployer pubkey (not hex): {deployer_pubkey[:20]}...")
            return None

        try:
            query = common.PendingDeploysQuery(deployerPubkey=deployer_bytes)
            response = await self.stub.getPendingDeploys(query, timeout=self.timeout)
            payload = _unwrap(response, "payload")

            deploys = []
            for info in payload.deploys:
                d = _to_dict(info.deploy)
                # DeployDataProto.deployer/sig are raw bytes, which _to_dict
                # base64-encodes; re-encode as hex for consistency with the
                # deployments table (DeployInfo.sig is a hex string there).
                d["deployer"] = bytes(info.deploy.deployer).hex()
                d["sig"] = bytes(info.deploy.sig).hex()
                deploys.append({**d, "isRejected": info.isRejected})

            return {
                "deploys": deploys,
                "totalAvailable": payload.totalAvailable,
            }

        except (grpc.RpcError, ValueError) as e:
            logger.error(f"Failed to get pending deploys: {e}")
            return None

    async def get_deploy_info(self, deploy_id: str) -> Optional[Dict[str, Any]]:
        try:
            query = common.FindDeployQuery(deployId=bytes.fromhex(deploy_id))  # raises ValueError on a non-hex id
            find_response = await self.stub.findDeploy(query, timeout=self.timeout)
            light_block = _to_dict(_unwrap(find_response, "blockInfo"))

            block_response = await self.stub.getBlock(
                _block_query(light_block["blockHash"]),
                timeout=self.timeout,
            )
            full_block = _to_dict(_unwrap(block_response, "blockInfo"))

            for deploy in full_block["deploys"]:
                if deploy["sig"] == deploy_id:
                    return {
                        "deployId": deploy_id,
                        "blockHash": light_block["blockHash"],
                        "blockNumber": light_block["blockNumber"],
                        "status": "included",
                        **deploy,
                    }

            logger.warning(f"Deploy {deploy_id} not found in resolved block {light_block['blockHash']}")
            return None

        except (grpc.RpcError, ValueError) as e:
            logger.error(f"Failed to get deploy info for {deploy_id}: {e}")
            return None

    async def show_main_chain(self, depth: int = 10) -> Optional[List[Dict[str, Any]]]:
        try:
            query = common.BlocksQuery(depth=depth)
            main_chain_blocks = []
            async for response in self.stub.showMainChain(query, timeout=self.timeout):
                main_chain_blocks.append(_to_dict(_unwrap(response, "blockInfo")))

            return main_chain_blocks

        except (grpc.RpcError, ValueError) as e:
            logger.error(f"Failed to show main chain with depth {depth}: {e}")
            return None

    async def health_check(self) -> bool:
        try:
            response = await self.stub.status(empty_pb2.Empty(), timeout=self.timeout)
            _to_dict(_unwrap(response, "status"))
            return True

        except (grpc.RpcError, ValueError) as e:
            logger.error(f"Failed health check: {e}")
            return False

    async def get_epoch_info(self) -> Optional[Dict[str, Any]]:
        url = f"http://{self.node_host}:{self.http_port}/api/epoch"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as session:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

            return {
                "current_epoch": data.get("currentEpoch"),
                "epoch_length": data.get("epochLength"),
                "quarantine_length": data.get("quarantineLength"),
                "blocks_until_next_epoch": data.get("blocksUntilNextEpoch"),
                "last_finalized_block_number": data.get("lastFinalizedBlockNumber"),
                "block_hash": data.get("blockHash"),
            }

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error(f"Failed to get epoch info: {e}")
            return None

    async def get_network_consensus(self) -> Optional[Dict[str, Any]]:
        try:
            lfb = await self.get_last_finalized_block()
            if lfb is None:
                return None
            active = await self.get_active_validators()

            if active is None:
                return None

            bonds = lfb["bonds"]
            total_bonded = len(bonds)
            active_count = len(active)
            in_quarantine = max(0, total_bonded - active_count)

            # Height and bonds share one finalized snapshot; active membership is separate.
            active_keys = {v["validator"] for v in active}
            total_stake = sum(b["stake"] for b in bonds)
            active_stake = sum(
                b["stake"] for b in bonds if b["validator"] in active_keys
            )
            participation = (active_stake / total_stake) if total_stake else 0.0

            # mirrors the node's own casper.fault-tolerance-threshold (settings.fault_tolerance_threshold)
            safety_threshold = settings.fault_tolerance_threshold
            if participation > safety_threshold:
                status = "healthy"
            elif participation > (1 - safety_threshold):
                status = "degraded"
            else:
                status = "critical"

            return {
                "current_block": lfb["blockNumber"],
                "total_bonded_validators": total_bonded,
                "active_validators": active_count,
                "validators_in_quarantine": in_quarantine,
                "participation_rate": participation * 100,
                "status": status,
            }

        except Exception as e:
            logger.error(f"Failed to get network consensus: {e}")
            return None
