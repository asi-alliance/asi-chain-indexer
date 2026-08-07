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

from src.config import settings

logger = structlog.get_logger(__name__)


def _unwrap(response, field_name: str):
    which = response.WhichOneof("message")
    if which == "error":
        raise grpc.RpcError(str(response.error))
    return getattr(response, field_name)


def _field_value(field, value):
    if field.type == FieldDescriptor.TYPE_MESSAGE:
        return _to_dict(value)
    if field.type == FieldDescriptor.TYPE_BYTES:
        return base64.b64encode(value).decode()
    return value


def _to_dict(message) -> Dict[str, Any]:
    # Not MessageToDict: it renders int64 as strings and only unquotes below 2^53,
    # so genesis deploys (phloLimit=2^63-1) would break the BIGINT insert.
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

        self.channel = grpc.aio.insecure_channel(f"{self.node_host}:{self.grpc_port}")
        self.stub = svc.DeployServiceStub(self.channel)

    async def close(self):
        await self.channel.close()

    async def get_blocks_by_height(self, start: int, end: int) -> List[Dict[str, Any]]:
        try:
            query = common.BlocksQueryByHeight(startBlockNumber=start, endBlockNumber=end)

            blocks = []
            async for response in self.stub.getBlocksByHeights(query, timeout=self.timeout):
                blocks.append(_to_dict(_unwrap(response, "blockInfo")))

            return blocks

        except grpc.RpcError as e:
            logger.error(f"Failed to get blocks by height {start}-{end}: {e}")
            return []

    async def get_last_finalized_block(self) -> Optional[Dict[str, Any]]:
        try:
            response = await self.stub.lastFinalizedBlock(common.LastFinalizedBlockQuery(), timeout=self.timeout)
            wrapper = _unwrap(response, "blockInfo")
            return _to_dict(wrapper.blockInfo)

        except grpc.RpcError as e:
            logger.error(f"Failed to get last finalized block: {e}")
            return None

    async def get_bonds(self) -> Optional[Dict[str, Any]]:
        try:
            response = await self.stub.lastFinalizedBlock(common.LastFinalizedBlockQuery(), timeout=self.timeout)
            wrapper = _unwrap(response, "blockInfo")
            return {"bonds": _to_dict(wrapper.blockInfo).get("bonds", [])}

        except grpc.RpcError as e:
            logger.error(f"Failed to get bonds: {e}")
            return None

    async def get_active_validators(self) -> Optional[List[Dict[str, Any]]]:
        # activeValidators is PoS contract state, no gRPC RPC exposes it
        url = f"http://{self.node_host}:{self.http_port}/api/validators"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as session:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

            return [
                {"validator": v["publicKey"], "stake": v["stake"]}
                for v in data.get("validators", [])
            ]

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error(f"Failed to get active validators: {e}")
            return None

    async def get_block_details(self, block_hash: str) -> Optional[Dict[str, Any]]:
        try:
            query = common.BlockQuery(hash=block_hash)
            response = await self.stub.getBlock(query, timeout=self.timeout)
            return _to_dict(_unwrap(response, "blockInfo"))

        except grpc.RpcError as e:
            logger.error(f"Failed to get block details for {block_hash}: {e}")
            return None

    async def get_deploy_info(self, deploy_id: str) -> Optional[Dict[str, Any]]:
        try:
            query = common.FindDeployQuery(deployId=bytes.fromhex(deploy_id))  # raises ValueError on a non-hex id
            find_response = await self.stub.findDeploy(query, timeout=self.timeout)
            light_block = _unwrap(find_response, "blockInfo")

            block_response = await self.stub.getBlock(common.BlockQuery(hash=light_block.blockHash), timeout=self.timeout)
            full_block = _unwrap(block_response, "blockInfo")

            for deploy in full_block.deploys:
                if deploy.sig == deploy_id:
                    return {
                        "deployId": deploy_id,
                        "blockHash": light_block.blockHash,
                        "blockNumber": light_block.blockNumber,
                        "status": "included",
                        **_to_dict(deploy),
                    }

            logger.warning(f"Deploy {deploy_id} not found in resolved block {light_block.blockHash}")
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

        except grpc.RpcError as e:
            logger.error(f"Failed to show main chain with depth {depth}: {e}")
            return None

    async def health_check(self) -> bool:
        try:
            response = await self.stub.status(empty_pb2.Empty(), timeout=self.timeout)
            _unwrap(response, "status")
            return True

        except grpc.RpcError as e:
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
            bonds_data = await self.get_bonds()
            active = await self.get_active_validators()

            if lfb is None or bonds_data is None or active is None:
                return None

            total_bonded = len(bonds_data["bonds"])
            active_count = len(active)
            in_quarantine = max(0, total_bonded - active_count)

            # stake from bonds_data only: get_bonds/get_active_validators aren't one atomic call
            active_keys = {v["validator"] for v in active}
            total_stake = sum(b["stake"] for b in bonds_data["bonds"])
            active_stake = sum(b["stake"] for b in bonds_data["bonds"] if b["validator"] in active_keys)
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
