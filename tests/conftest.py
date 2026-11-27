import pytest

from src.read_cache import ReadCache


@pytest.fixture(autouse=True)
def read_cache(monkeypatch):
    """A fresh process cache per test, so cached reads never cross tests."""
    cache = ReadCache(enabled=True, max_entries=256)
    for module in ("src.read_cache", "src.monitoring", "src.block_indexer"):
        monkeypatch.setattr(f"{module}.read_cache", cache)
    return cache
