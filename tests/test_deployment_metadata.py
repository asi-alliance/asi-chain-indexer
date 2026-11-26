import pytest

from src.block_indexer import BlockIndexer


class CapturingSession:
    def __init__(self):
        self.statement = None

    async def execute(self, statement):
        self.statement = statement

    async def flush(self):
        pass


@pytest.mark.asyncio
async def test_deployment_upsert_does_not_write_block_metadata():
    indexer = BlockIndexer()
    session = CapturingSession()
    block = {"blockHash": "block-hash", "blockNumber": 10, "timestamp": 1}
    deploy = {
        "sig": "deploy-sig",
        "deployer": "deployer",
        "term": "Nil",
        "timestamp": 1,
        "sigAlgorithm": "secp256k1",
        "phloPrice": "1",
        "phloLimit": 1000000,
        "cost": 1,
        "validAfterBlockNumber": 0,
        "errored": False,
        "seqNum": 7,
        "shardId": "root",
    }

    await indexer._process_deployment_enhanced(
        session, block, deploy, store_transfers=False
    )

    statement = str(session.statement)
    assert "INSERT INTO deployments" in statement
    assert "seq_num" not in statement
    assert "shard_id" not in statement
