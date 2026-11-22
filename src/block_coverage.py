"""Bounded ledger of block bodies the observer no longer holds.

The observer can answer ``getBlocksByHeights`` with the pruned / unavailable
split added in F1R3FLY-io/f1r3node-rust#1097: a hash whose body was deleted by
the retention sweep is reported as ``pruned`` (permanent on that observer),
while a body that is merely absent below the sweep floor is ``unavailable``
(retry eligible). This module keeps the permanent part in a small, durable
record so the sync loop can explain a height it deliberately passed and an
ancestor walk can tell a pruned parent from one that is still arriving.

The record lives in the existing ``indexer_state`` row named
``block_coverage``. It is bounded and best-effort: it is evidence of what the
observer reported, not a per-block provenance store, and it never substitutes
for the blocks and deploy reports themselves.
"""

import copy
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from src.time_utils import utc_now

STATE_KEY = "block_coverage"

# Bound the row so a long-lived pruned observer cannot grow it without limit.
MAX_TRACKED_HEIGHTS = 1000
MAX_TRACKED_DEGRADED_BLOCKS = 1000


@dataclass(frozen=True)
class MissingBlock:
    """One hash the observer named at a height whose body it does not hold."""

    block_hash: str
    block_height: int
    prune_floor: int


@dataclass(frozen=True)
class BlocksByHeightPage:
    """One ``getBlocksByHeights`` response with its opt-in missing bodies."""

    blocks: List[Dict[str, Any]]
    pruned: List[MissingBlock]
    unavailable: List[MissingBlock]

    @property
    def is_empty(self) -> bool:
        return not (self.blocks or self.pruned or self.unavailable)

    @property
    def prune_floor(self) -> Optional[int]:
        entries = [*self.pruned, *self.unavailable]
        return max(entry.prune_floor for entry in entries) if entries else None

    def fully_pruned_heights(self) -> Dict[int, List[str]]:
        """Heights whose every reported hash is permanently pruned.

        A height qualifies only when the response proves it: the height has at
        least one pruned hash and no served block and no retry-eligible hash at
        the same height. Anything less stays with the caller to retry.
        """

        served = {block.get("blockNumber") for block in self.blocks}
        retryable = {entry.block_height for entry in self.unavailable}
        heights: Dict[int, List[str]] = {}
        for entry in self.pruned:
            heights.setdefault(entry.block_height, []).append(entry.block_hash)
        return {
            height: hashes
            for height, hashes in heights.items()
            if height not in served and height not in retryable
        }


def empty_state() -> Dict[str, Any]:
    """Return a coverage state with no recorded gaps."""

    return {
        "prune_floor": None,
        "heights": {},
        "degraded_blocks": {},
        "updated_at": None,
    }


def normalize_state(raw: Any) -> Dict[str, Any]:
    """Return a state matching the documented shape, tolerating a damaged row."""

    state = empty_state()
    if not isinstance(raw, dict):
        return state

    floor = raw.get("prune_floor")
    if type(floor) is int:
        state["prune_floor"] = floor

    heights = raw.get("heights")
    if isinstance(heights, dict):
        for key, hashes in heights.items():
            try:
                height = int(key)
            except (TypeError, ValueError):
                continue
            if height < 0 or not isinstance(hashes, list):
                continue
            cleaned = [h for h in hashes if isinstance(h, str) and h]
            if cleaned:
                state["heights"][str(height)] = cleaned

    degraded = raw.get("degraded_blocks")
    if isinstance(degraded, dict):
        for key, entry in degraded.items():
            if not isinstance(key, str) or not isinstance(entry, dict):
                continue
            height = entry.get("block_height")
            parents = entry.get("missing_parents")
            if type(height) is not int or not isinstance(parents, list):
                continue
            cleaned = [p for p in parents if isinstance(p, str) and p]
            if cleaned:
                state["degraded_blocks"][key] = {
                    "block_height": height,
                    "missing_parents": cleaned,
                }

    updated = raw.get("updated_at")
    if isinstance(updated, str):
        state["updated_at"] = updated

    return _bounded(state)


def record_pruned(
    state: Dict[str, Any], entries: Iterable[MissingBlock]
) -> Tuple[Dict[str, Any], int]:
    """Merge pruned hashes into a copy of the state.

    Returns the new state and the number of hashes that were not recorded
    before, so a caller can count only first sightings.
    """

    merged = copy.deepcopy(state)
    added = 0
    for entry in entries:
        hashes = merged["heights"].setdefault(str(entry.block_height), [])
        if entry.block_hash not in hashes:
            hashes.append(entry.block_hash)
            added += 1
        if merged["prune_floor"] is None or entry.prune_floor > merged["prune_floor"]:
            merged["prune_floor"] = entry.prune_floor
    if not added and merged == state:
        return merged, 0
    return _stamp(_bounded(merged)), added


def record_degraded(
    state: Dict[str, Any],
    blocks: Iterable[Tuple[str, int, Iterable[str]]],
) -> Tuple[Dict[str, Any], int]:
    """Merge blocks whose ancestry crosses pruned bodies into a copy of the state."""

    merged = copy.deepcopy(state)
    added = 0
    for block_hash, block_height, missing_parents in blocks:
        parents = sorted({parent for parent in missing_parents if parent})
        if not parents:
            continue
        if block_hash not in merged["degraded_blocks"]:
            added += 1
        merged["degraded_blocks"][block_hash] = {
            "block_height": block_height,
            "missing_parents": parents,
        }
    if not added and merged == state:
        return merged, 0
    return _stamp(_bounded(merged)), added


def pruned_hashes(state: Dict[str, Any]) -> Set[str]:
    """Every hash recorded as permanently pruned."""

    return {block_hash for hashes in state["heights"].values() for block_hash in hashes}


def summarize(state: Dict[str, Any]) -> Dict[str, Any]:
    """Operator-facing totals for ``/status`` and metric gauges."""

    heights = state["heights"]
    pruned_heights = sorted(int(height) for height in heights)
    return {
        "pruned_heights": len(heights),
        "pruned_hashes": sum(len(hashes) for hashes in heights.values()),
        "degraded_blocks": len(state["degraded_blocks"]),
        "prune_floor": state["prune_floor"],
        "last_pruned_height": pruned_heights[-1] if pruned_heights else None,
        "updated_at": state["updated_at"],
    }


def _bounded(state: Dict[str, Any]) -> Dict[str, Any]:
    _trim(state["heights"], MAX_TRACKED_HEIGHTS, lambda item: int(item[0]))
    _trim(
        state["degraded_blocks"],
        MAX_TRACKED_DEGRADED_BLOCKS,
        lambda item: item[1].get("block_height", -1),
    )
    return state


def _trim(mapping: Dict[str, Any], limit: int, sort_key) -> None:
    if len(mapping) <= limit:
        return
    keep = dict(sorted(mapping.items(), key=sort_key)[-limit:])
    mapping.clear()
    mapping.update(keep)


def _stamp(state: Dict[str, Any]) -> Dict[str, Any]:
    state["updated_at"] = utc_now().isoformat()
    return state
