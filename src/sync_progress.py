"""Progress tracking for DAG block batches."""

from dataclasses import dataclass, field
from typing import Optional, Set


@dataclass
class BlockBatchProgress:
    """Track successful siblings across consecutive block heights."""

    start_height: int
    end_height: int
    seen_heights: Set[int] = field(default_factory=set)
    failed_heights: Set[int] = field(default_factory=set)
    has_unmapped_failure: bool = False

    def mark_seen(self, block_height: Optional[int]) -> None:
        """Record a block summary returned for this batch."""
        if not self._contains(block_height):
            self.has_unmapped_failure = True
            return
        self.seen_heights.add(block_height)

    def mark_failed(self, block_height: Optional[int]) -> None:
        """Record a failed sibling without allowing the cursor to pass it."""
        if not self._contains(block_height):
            self.has_unmapped_failure = True
            return
        self.seen_heights.add(block_height)
        self.failed_heights.add(block_height)

    @property
    def last_completed_height(self) -> int:
        """Return the highest successful consecutive height."""
        if self.has_unmapped_failure:
            return self.start_height - 1

        completed_height = self.start_height - 1
        for height in range(self.start_height, self.end_height + 1):
            height_was_seen = height in self.seen_heights
            height_failed = height in self.failed_heights
            if not height_was_seen or height_failed:
                break
            completed_height = height
        return completed_height

    def _contains(self, block_height: Optional[int]) -> bool:
        return (
            isinstance(block_height, int)
            and not isinstance(block_height, bool)
            and self.start_height <= block_height <= self.end_height
        )
