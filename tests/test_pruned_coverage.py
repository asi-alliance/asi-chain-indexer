import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.block_coverage import BlocksByHeightPage, MissingBlock
from src.block_indexer import BlockIndexer
from src.native_transfers import MissingParent, require_indexed_parents

HASH_X = "11" * 32
HASH_Y = "22" * 32
HASH_PARENT = "33" * 32


class FakeSession:
    def __init__(self, block_count=3, missing=()):
        self.block_count = block_count
        self.missing = list(missing)

    async def scalar(self, statement):
        return self.block_count if "COUNT(*)" in str(statement) else None

    async def scalars(self, _statement):
        return list(self.missing)


class FakeDatabase:
    def __init__(self, last_indexed=9):
        self.last_indexed = last_indexed
        self.coverage = {}
        self.set_calls = []
        self.saved = []
        self.session_state = FakeSession()

    async def get_last_indexed_block(self):
        return self.last_indexed

    async def set_last_indexed_block(self, block_number):
        self.last_indexed = block_number
        self.set_calls.append(block_number)

    async def get_recovery_path(self):
        return []

    async def get_block_coverage(self):
        return self.coverage

    async def save_block_coverage(self, state):
        self.coverage = copy.deepcopy(state)
        self.saved.append(self.coverage)

    @asynccontextmanager
    async def session(self):
        yield self.session_state


class PageClient:
    def __init__(self, page):
        self.page = page

    async def get_last_finalized_block(self):
        return {"blockNumber": 12}

    async def get_blocks_by_height(self, start, end):
        return self.page

    async def get_block_details(self, block_hash):
        return {
            "blockInfo": {"blockNumber": int(block_hash[:2]), "blockHash": block_hash},
            "deploys": [],
        }

    async def get_block_events(self, *_args, **_kwargs):
        return []


def served(height):
    return {"blockNumber": height, "blockHash": f"{height:02d}" + "a" * 62}


def setup(monkeypatch, page, last_indexed=9):
    database = FakeDatabase(last_indexed=last_indexed)
    monkeypatch.setattr("src.block_indexer.db", database)
    monkeypatch.setattr("src.block_indexer.settings.batch_size", 3)
    monkeypatch.setattr("src.block_indexer.settings.start_from_block", 0)
    monkeypatch.setattr("src.block_indexer.settings.delay_before_node", 0)
    indexer = BlockIndexer()
    indexer.client = PageClient(page)
    indexer._process_block = AsyncMock()
    return indexer, database


@pytest.mark.asyncio
async def test_a_fully_pruned_height_is_covered_and_recorded(monkeypatch):
    page = BlocksByHeightPage(
        blocks=[served(11), served(12)],
        pruned=[MissingBlock(HASH_X, 10, 10)],
        unavailable=[],
    )
    indexer, database = setup(monkeypatch, page)

    await indexer._sync_blocks()

    assert database.last_indexed == 12
    assert database.set_calls == [12]
    assert database.coverage["heights"] == {"10": [HASH_X]}
    assert database.coverage["prune_floor"] == 10
    assert indexer._pruned_hashes == {HASH_X}
    assert indexer._process_block.await_count == 2


@pytest.mark.asyncio
async def test_a_retry_eligible_height_keeps_the_cursor(monkeypatch):
    page = BlocksByHeightPage(
        blocks=[served(11), served(12)],
        pruned=[],
        unavailable=[MissingBlock(HASH_Y, 10, 9)],
    )
    indexer, database = setup(monkeypatch, page)

    await indexer._sync_blocks()

    assert database.last_indexed == 9
    assert database.set_calls == []
    # An unavailable body is retry eligible, not a permanent gap.
    assert database.coverage == {}
    assert database.saved == []
    assert indexer._pruned_hashes == set()


@pytest.mark.asyncio
async def test_an_observer_without_the_split_never_advances_past_a_gap(monkeypatch):
    page = BlocksByHeightPage(
        blocks=[served(11), served(12)], pruned=[], unavailable=[]
    )
    indexer, database = setup(monkeypatch, page)

    await indexer._sync_blocks()

    assert database.last_indexed == 9
    assert database.set_calls == []
    assert database.coverage == {}
    assert database.saved == []


@pytest.mark.asyncio
async def test_a_page_with_both_served_and_pruned_siblings_records_the_gap(monkeypatch):
    page = BlocksByHeightPage(
        blocks=[served(10)],
        pruned=[MissingBlock(HASH_X, 10, 10)],
        unavailable=[],
    )
    indexer, database = setup(monkeypatch, page)

    await indexer._sync_blocks()

    # The served sibling is indexed and the height completes; the pruned hash
    # is still recorded because that sibling can never be read here.
    assert database.last_indexed == 10
    assert database.coverage["heights"] == {"10": [HASH_X]}
    assert page.fully_pruned_heights() == {}


@pytest.mark.asyncio
async def test_a_failed_range_read_does_not_move_the_cursor(monkeypatch):
    indexer, database = setup(monkeypatch, None)

    await indexer._sync_blocks()

    assert database.last_indexed == 9
    assert database.set_calls == []
    assert database.saved == []


@pytest.mark.asyncio
async def test_a_raised_prune_floor_is_persisted_without_a_new_hash(monkeypatch):
    page = BlocksByHeightPage(
        blocks=[served(11)], pruned=[MissingBlock(HASH_X, 10, 10)], unavailable=[]
    )
    indexer, database = setup(monkeypatch, page)

    await indexer._sync_blocks()
    assert database.coverage["prune_floor"] == 10
    assert database.coverage["heights"] == {"10": [HASH_X]}

    # The sweep advanced: the same hash is reported again with a higher floor
    # and no new hash. The raised floor must still reach the row.
    database.last_indexed = 9
    indexer.client.page = BlocksByHeightPage(
        blocks=[served(11)], pruned=[MissingBlock(HASH_X, 10, 25)], unavailable=[]
    )
    await indexer._sync_blocks()

    assert database.coverage["prune_floor"] == 25
    assert database.coverage["heights"] == {"10": [HASH_X]}


@pytest.mark.asyncio
async def test_a_classified_hash_joins_the_ledger(monkeypatch):
    indexer, database = setup(monkeypatch, None)
    indexer.client = parent_client("pruned")

    await indexer._visit_recovery_block([(HASH_PARENT, 3, False)], {})
    assert HASH_PARENT in indexer._pruned_hashes
    assert database.saved == []

    await indexer._commit_coverage()

    # The 410 answer names no height, so the hash lands in its own bucket and
    # still shows up in the operator summary.
    assert database.coverage["classified_hashes"] == [HASH_PARENT]
    assert database.coverage["heights"] == {}
    assert indexer.coverage_summary()["pruned_hashes"] == 1
    assert indexer.coverage_summary()["classified_hashes"] == 1


def parent_client(availability, details=None):
    return SimpleNamespace(
        get_block_details=AsyncMock(return_value=details),
        get_block_availability=AsyncMock(return_value=availability),
    )


@pytest.mark.asyncio
async def test_a_pruned_parent_is_classified_once_and_skipped(monkeypatch):
    monkeypatch.setattr("src.native_transfers.settings.start_from_block", 0)
    session = FakeSession(missing=[HASH_PARENT])
    client = parent_client("pruned")
    pruned = set()

    await require_indexed_parents(
        session,
        {"blockHash": "child", "blockNumber": 5},
        client,
        pruned,
    )

    assert pruned == {HASH_PARENT}
    client.get_block_availability.assert_awaited_once_with(HASH_PARENT)

    await require_indexed_parents(
        session,
        {"blockHash": "child", "blockNumber": 5},
        client,
        pruned,
    )
    # The ledger already names it: no second HTTP probe.
    assert client.get_block_availability.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("availability", ["unavailable", "available", None, "missing"])
async def test_a_retry_eligible_parent_still_fails_the_block(monkeypatch, availability):
    monkeypatch.setattr("src.native_transfers.settings.start_from_block", 0)
    session = FakeSession(missing=[HASH_PARENT])
    pruned = set()

    with pytest.raises(MissingParent):
        await require_indexed_parents(
            session,
            {"blockHash": "child", "blockNumber": 5},
            parent_client(availability),
            pruned,
        )

    assert pruned == set()


@pytest.mark.asyncio
async def test_recovery_classifies_and_skips_a_pruned_ancestor(monkeypatch):
    database = FakeDatabase()
    monkeypatch.setattr("src.block_indexer.db", database)
    monkeypatch.setattr("src.block_indexer.settings.delay_before_node", 0)
    indexer = BlockIndexer()
    indexer.client = parent_client("pruned")

    path = [(HASH_PARENT, 3, False)]
    await indexer._visit_recovery_block(path, {})

    assert path == []
    assert HASH_PARENT in indexer._pruned_hashes
    indexer.client.get_block_availability.assert_awaited_once_with(HASH_PARENT)


@pytest.mark.asyncio
async def test_recovery_skips_a_recorded_pruned_ancestor_without_a_read(monkeypatch):
    database = FakeDatabase()
    monkeypatch.setattr("src.block_indexer.db", database)
    indexer = BlockIndexer()
    indexer.client = parent_client("pruned")
    indexer._pruned_hashes = {HASH_PARENT}

    path = [(HASH_PARENT, 3, False)]
    await indexer._visit_recovery_block(path, {})

    assert path == []
    indexer.client.get_block_details.assert_not_awaited()
    indexer.client.get_block_availability.assert_not_awaited()
