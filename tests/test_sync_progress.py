from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from src.models import BalanceState, Block, Deployment, Transfer, ValidatorBond
from src.rust_indexer import RustBlockIndexer
from src.sync_progress import BlockBatchProgress


def test_block_hash_is_the_only_block_primary_key() -> None:
    primary_key_columns = Block.__table__.primary_key.columns
    primary_keys = {column.name for column in primary_key_columns}

    assert primary_keys == {"block_hash"}
    assert Block.__table__.c.block_number.unique is not True


def test_child_tables_use_block_hash_for_block_identity() -> None:
    for model in (Deployment, Transfer, ValidatorBond, BalanceState):
        assert "block_hash" in model.__table__.c
        block_hash_targets = {
            foreign_key.target_fullname
            for foreign_key in model.__table__.c.block_hash.foreign_keys
        }
        assert block_hash_targets == {"blocks.block_hash"}
        assert not model.__table__.c.block_number.foreign_keys


def test_progress_accepts_multiple_siblings_at_one_height() -> None:
    progress = BlockBatchProgress(10, 12)
    progress.mark_seen(10)
    progress.mark_seen(10)
    progress.mark_seen(11)
    progress.mark_seen(12)

    assert progress.last_completed_height == 12


def test_progress_stops_before_a_missing_height() -> None:
    progress = BlockBatchProgress(10, 12)
    progress.mark_seen(10)
    progress.mark_seen(12)

    assert progress.last_completed_height == 10


def test_failed_sibling_blocks_progress_at_its_height() -> None:
    progress = BlockBatchProgress(10, 12)
    progress.mark_seen(10)
    progress.mark_failed(10)
    progress.mark_seen(11)
    progress.mark_seen(12)

    assert progress.last_completed_height == 9


class FakeSession:
    async def scalar(self, _statement: object) -> int:
        return 1


class FakeDatabase:
    def __init__(self) -> None:
        self.last_indexed = 9
        self.set_calls: list[int] = []

    async def get_last_indexed_block(self) -> int:
        return self.last_indexed

    async def set_last_indexed_block(self, block_number: int) -> None:
        self.last_indexed = block_number
        self.set_calls.append(block_number)

    @asynccontextmanager
    async def session(self):
        yield FakeSession()


class FakeClient:
    def __init__(self) -> None:
        self.failed_hashes = {"block-10-b"}
        self.blocks = [
            {"blockNumber": 10, "blockHash": "block-10-a"},
            {"blockNumber": 10, "blockHash": "block-10-b"},
            {"blockNumber": 11, "blockHash": "block-11-a"},
            {"blockNumber": 12, "blockHash": "block-12-a"},
        ]

    async def get_last_finalized_block(self) -> dict[str, int]:
        return {"blockNumber": 12}

    async def get_blocks_by_height(
        self, start: int, end: int
    ) -> list[dict[str, object]]:
        assert (start, end) == (10, 12)
        return self.blocks

    async def get_block_details(self, block_hash: str):
        if block_hash in self.failed_hashes:
            return None
        summary = next(
            block for block in self.blocks if block["blockHash"] == block_hash
        )
        return {
            "blockInfo": {
                "blockNumber": summary["blockNumber"],
                "blockHash": block_hash,
            },
            "deploys": [],
        }


@pytest.mark.asyncio
async def test_sync_retries_a_failed_sibling_before_advancing(
    monkeypatch,
) -> None:
    fake_database = FakeDatabase()
    fake_client = FakeClient()
    indexer = RustBlockIndexer()
    indexer.client = fake_client
    indexer._process_block = AsyncMock()

    monkeypatch.setattr("src.rust_indexer.db", fake_database)
    monkeypatch.setattr("src.rust_indexer.settings.batch_size", 3)
    monkeypatch.setattr("src.rust_indexer.settings.start_from_block", 0)
    monkeypatch.setattr("src.rust_indexer.settings.delay_before_node", 0)

    await indexer._sync_blocks()

    assert fake_database.last_indexed == 9
    assert fake_database.set_calls == []

    fake_client.failed_hashes.clear()
    await indexer._sync_blocks()

    assert fake_database.last_indexed == 12
    assert fake_database.set_calls == [12]
    assert indexer._process_block.await_count == 7
