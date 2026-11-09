import grpc
import pytest
from pydantic import ValidationError

from src.config import Settings
from src.grpc_node_client import GrpcNodeClient, _block_query, common
from test_node_exact_api import BLOCK_HASH, DEPLOY_ID, node_api


@pytest.mark.asyncio
async def test_large_block_reads_respect_the_configured_receive_limit(monkeypatch):
    term = "x" * (5 * 1024 * 1024)
    async with node_api(1, term=term) as node:
        details = await node.client.get_block_details(BLOCK_HASH)
        assert details["deploys"][0]["term"] == term
        assert (await node.client.get_deploy_info(DEPLOY_ID))["term"] == term

        monkeypatch.setattr(
            "src.grpc_node_client.settings.grpc_max_receive_message_bytes", 1024 * 1024
        )
        limited = GrpcNodeClient(node_host="127.0.0.1", grpc_port=node.client.grpc_port)
        limited.timeout = 2
        try:
            with pytest.raises(grpc.aio.AioRpcError) as error:
                await limited.stub.getBlock(
                    _block_query(BLOCK_HASH),
                    timeout=limited.timeout,
                )
            assert error.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
            assert await limited.get_block_details(BLOCK_HASH) is None
        finally:
            await limited.close()


def test_receive_limit_can_be_set_through_the_environment(monkeypatch):
    monkeypatch.setenv("GRPC_MAX_RECEIVE_MESSAGE_BYTES", "8388608")
    assert Settings(_env_file=None).grpc_max_receive_message_bytes == 8388608


@pytest.mark.parametrize("limit", [-1, 0, 2**31])
def test_receive_limit_rejects_unlimited_or_invalid_values(limit):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, grpc_max_receive_message_bytes=limit)
