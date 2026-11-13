"""A worker process for CPU-heavy work that must not stall the event loop."""

import asyncio
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
import multiprocessing

_worker = None


def _worker_pool() -> ProcessPoolExecutor:
    global _worker
    if _worker is None:
        _worker = ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("spawn"))
    return _worker


async def run_off_loop(function, *args):
    """Run a module-level function in the worker process and await its result."""
    global _worker
    pool = _worker_pool()
    try:
        return await asyncio.get_running_loop().run_in_executor(pool, function, *args)
    except BrokenProcessPool:
        if _worker is pool:
            _worker = None
        raise
