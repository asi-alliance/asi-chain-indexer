"""In-process read-through cache for the indexer's own read paths.

The indexer is the only writer of the cached tables, so it knows when they
change. Every committed block, reorg detection or explicit flush drops all
entries; there is no TTL. A load that overlaps an invalidation is returned
but not stored, so an entry never outlives the data it was read from.

The cache lives in one process. A restart starts empty, and a second replica
or an out-of-process writer (for example python -m src.reindex_transfers,
which runs with indexer writes stopped) does not share it.
"""

from collections import OrderedDict
from typing import Any, Awaitable, Callable, Hashable

from prometheus_client import Counter, Gauge
import structlog

from src.config import settings

logger = structlog.get_logger(__name__)

cache_hits = Counter(
    "indexer_read_cache_hits_total",
    "Read cache hits",
    ["endpoint"]
)

cache_misses = Counter(
    "indexer_read_cache_misses_total",
    "Read cache misses",
    ["endpoint"]
)

cache_evictions = Counter(
    "indexer_read_cache_evictions_total",
    "Entries removed from the read cache",
    ["reason"]
)

cache_errors = Counter(
    "indexer_read_cache_errors_total",
    "Read cache failures that fell back to a direct query",
    ["endpoint"]
)

cache_enabled = Gauge(
    "indexer_read_cache_enabled",
    "1 when the read cache is enabled, 0 otherwise"
)

cache_entries = Gauge(
    "indexer_read_cache_entries",
    "Entries held by the read cache"
)


class ReadCache:
    """Bounded LRU map whose entries are valid for one data generation."""

    def __init__(self, enabled: bool, max_entries: int):
        self.enabled = enabled
        self.max_entries = max_entries
        self.generation = 0
        self._entries: "OrderedDict[Hashable, Any]" = OrderedDict()
        cache_enabled.set(1 if enabled else 0)
        cache_entries.set(0)

    def invalidate(self, reason: str) -> None:
        """Drop every entry; call after each commit that changes cached tables."""
        self.generation += 1
        if self._entries:
            cache_evictions.labels(reason).inc(len(self._entries))
            self._entries.clear()
        cache_entries.set(0)

    async def get_or_load(
        self, endpoint: str, key: Hashable, load: Callable[[], Awaitable[Any]]
    ) -> Any:
        """Return the cached value for (endpoint, key), or load and store it.

        A cache failure is logged and counted, and the value comes from `load`.
        Errors from `load` itself propagate unchanged.
        """
        if not self.enabled:
            return await load()

        generation = self.generation
        entry_key = (endpoint, key)
        try:
            if entry_key in self._entries:
                self._entries.move_to_end(entry_key)
                cache_hits.labels(endpoint).inc()
                return self._entries[entry_key]
        except Exception as e:
            cache_errors.labels(endpoint).inc()
            logger.warning("Read cache lookup failed; querying directly",
                           endpoint=endpoint, error=str(e))
            return await load()

        cache_misses.labels(endpoint).inc()
        value = await load()
        # A block committed while the query ran: the value may predate it
        if generation == self.generation:
            try:
                self._store(entry_key, value)
            except Exception as e:
                cache_errors.labels(endpoint).inc()
                logger.warning("Read cache store failed", endpoint=endpoint, error=str(e))
        return value

    def _store(self, entry_key: Hashable, value: Any) -> None:
        self._entries[entry_key] = value
        self._entries.move_to_end(entry_key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
            cache_evictions.labels("capacity").inc()
        cache_entries.set(len(self._entries))


read_cache = ReadCache(settings.read_cache_enabled, settings.read_cache_max_entries)
