"""Worker processes for CPU-heavy work that must not stall the event loop."""

import asyncio
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
import multiprocessing


class WorkerPool:
    """A lazily started pool of spawned worker processes.

    The function and its arguments must be picklable, so the function must be
    defined at module level. A broken pool is shut down and replaced on the
    next call.
    """

    def __init__(self, workers: int):
        self.workers = workers
        self._executor = None

    def _pool(self) -> ProcessPoolExecutor:
        if self._executor is None:
            self._executor = ProcessPoolExecutor(
                self.workers, mp_context=multiprocessing.get_context("spawn")
            )
        return self._executor

    async def run(self, function, *args):
        pool = self._pool()
        try:
            return await asyncio.get_running_loop().run_in_executor(pool, function, *args)
        except BrokenProcessPool:
            if self._executor is pool:
                self._executor = None
            pool.shutdown(wait=False)
            raise


amount_worker = WorkerPool(1)


async def run_off_loop(function, *args):
    """Run a function in the single worker that formats large amounts."""
    return await amount_worker.run(function, *args)
