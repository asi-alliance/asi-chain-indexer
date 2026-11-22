from src.block_coverage import (
    MAX_TRACKED_HEIGHTS,
    BlocksByHeightPage,
    MissingBlock,
    empty_state,
    normalize_state,
    pruned_hashes,
    record_degraded,
    record_pruned,
    summarize,
)

HASH_A = "aa" * 32
HASH_B = "bb" * 32
HASH_C = "cc" * 32


def entry(block_hash=HASH_A, height=10, floor=10):
    return MissingBlock(block_hash=block_hash, block_height=height, prune_floor=floor)


def test_page_without_missing_bodies_is_empty_and_has_no_floor():
    page = BlocksByHeightPage(blocks=[], pruned=[], unavailable=[])

    assert page.is_empty
    assert page.prune_floor is None
    assert page.fully_pruned_heights() == {}


def test_page_reports_the_highest_floor_it_saw():
    page = BlocksByHeightPage(
        blocks=[],
        pruned=[entry(height=8, floor=9)],
        unavailable=[entry(HASH_C, height=20, floor=12)],
    )

    assert page.prune_floor == 12


def test_only_heights_with_no_served_or_retryable_hash_are_fully_pruned():
    page = BlocksByHeightPage(
        blocks=[{"blockNumber": 10, "blockHash": HASH_A}],
        pruned=[entry(HASH_B, height=10), entry(HASH_C, height=11)],
        unavailable=[entry(HASH_B, height=12)],
    )

    # Height 10 also has a served sibling and height 12 a retry-eligible one:
    # only height 11 is proven complete for this observer.
    assert page.fully_pruned_heights() == {11: [HASH_C]}


def test_normalize_state_ignores_a_damaged_row():
    state = normalize_state(
        {
            "prune_floor": "high",
            "heights": {"10": [HASH_A, "", 3], "not-a-height": [HASH_B], "11": "nope"},
            "degraded_blocks": {"child": {"block_height": 12}, "broken": "x"},
            "updated_at": 7,
        }
    )

    assert state == {
        "prune_floor": None,
        "heights": {"10": [HASH_A]},
        "degraded_blocks": {},
        "updated_at": None,
    }


def test_normalize_state_keeps_a_well_formed_row():
    raw = {
        "prune_floor": 9,
        "heights": {"10": [HASH_A]},
        "degraded_blocks": {"child": {"block_height": 11, "missing_parents": [HASH_A]}},
        "updated_at": "2026-01-01T00:00:00+00:00",
    }

    assert normalize_state(raw) == raw


def test_record_pruned_counts_only_first_sightings_and_keeps_the_highest_floor():
    state, added = record_pruned(empty_state(), [entry(height=8, floor=8)])
    assert added == 1
    assert state["heights"] == {"8": [HASH_A]}
    assert state["prune_floor"] == 8

    state, added = record_pruned(
        state, [entry(height=8, floor=8), entry(HASH_B, height=9, floor=6)]
    )
    assert added == 1
    assert state["heights"]["8"] == [HASH_A]
    assert state["heights"]["9"] == [HASH_B]
    assert state["prune_floor"] == 8

    state, added = record_pruned(state, [entry(height=8, floor=8)])
    assert added == 0
    assert pruned_hashes(state) == {HASH_A, HASH_B}


def test_record_degraded_counts_only_new_blocks():
    state, added = record_degraded(empty_state(), [("child", 11, [HASH_A, HASH_A])])
    assert added == 1
    assert state["degraded_blocks"]["child"] == {
        "block_height": 11,
        "missing_parents": [HASH_A],
    }

    state, added = record_degraded(state, [("child", 11, [HASH_A])])
    assert added == 0

    state, added = record_degraded(state, [("other", 12, [])])
    assert added == 0
    assert set(state["degraded_blocks"]) == {"child"}


def test_ledger_is_bounded_by_dropping_the_lowest_heights():
    state = empty_state()
    state, _ = record_pruned(
        state,
        [
            entry(f"{height:064x}", height=height, floor=height)
            for height in range(MAX_TRACKED_HEIGHTS + 5)
        ],
    )

    heights = sorted(int(height) for height in state["heights"])
    assert len(heights) == MAX_TRACKED_HEIGHTS
    assert heights[0] == 5


def test_summarize_reports_totals_and_the_last_height():
    state, _ = record_pruned(empty_state(), [entry(height=8), entry(HASH_B, height=9)])
    state, _ = record_degraded(state, [("child", 10, [HASH_A])])

    summary = summarize(state)

    assert summary["pruned_heights"] == 2
    assert summary["pruned_hashes"] == 2
    assert summary["degraded_blocks"] == 1
    assert summary["prune_floor"] == 10
    assert summary["last_pruned_height"] == 9
    assert summary["updated_at"] is not None
