"""Worker processes for CPU-heavy work that must not stall the event loop."""

import asyncio
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
import heapq
import itertools
import multiprocessing
import time


def timed(function, *args):
    """Run the function in a worker and return its run time with its result."""
    started = time.perf_counter()
    result = function(*args)
    return time.perf_counter() - started, result


class WorkerPool:
    """A lazily started pool of spawned worker processes.

    The function and its arguments must be picklable, so the function must be
    defined at module level. A broken pool is shut down and replaced on the
    next call.

    A call is submitted only when a worker is free. A worker that runs a job
    stays taken until the job ends, also when the caller is cancelled, because
    a running job cannot be stopped. Waiting calls get the next free worker in
    the order of their priority, lowest first, then in the order of arrival.
    """

    def __init__(self, workers: int):
        self.workers = workers
        self._executor = None
        self._loop = None
        self._busy = 0
        self._waiting = []
        self._arrival = itertools.count()

    def _pool(self) -> ProcessPoolExecutor:
        if self._executor is None:
            self._executor = ProcessPoolExecutor(
                self.workers, mp_context=multiprocessing.get_context("spawn")
            )
        return self._executor

    def _drop(self, pool):
        if self._executor is pool:
            self._executor = None
        pool.shutdown(wait=False)

    def _bind(self):
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop, self._busy, self._waiting = loop, 0, []
        return loop

    async def _acquire(self, priority):
        if self._busy < self.workers:
            self._busy += 1
            return
        turn = self._loop.create_future()
        heapq.heappush(self._waiting, (priority, next(self._arrival), turn))
        try:
            await turn
        except asyncio.CancelledError:
            if turn.done() and not turn.cancelled():
                self._release()
            raise

    def _release(self):
        while self._waiting:
            turn = heapq.heappop(self._waiting)[2]
            if not turn.done():
                turn.set_result(None)
                return
        self._busy -= 1

    async def run_timed(self, function, *args, priority=0):
        """Return the time in the queue, the run time in the worker and the result."""
        loop = self._bind()
        queued = time.perf_counter()
        await self._acquire(priority)
        waited = time.perf_counter() - queued
        pool = self._pool()
        try:
            job = pool.submit(timed, function, *args)
        except BaseException as error:
            self._release()
            if isinstance(error, BrokenProcessPool):
                self._drop(pool)
            raise

        def finished(future):
            if not future.cancelled():
                future.exception()
            if self._loop is loop:
                self._release()

        done = asyncio.wrap_future(job)
        done.add_done_callback(finished)
        try:
            elapsed, result = await asyncio.shield(done)
        except asyncio.CancelledError:
            job.cancel()
            raise
        except BrokenProcessPool:
            self._drop(pool)
            raise
        return waited, elapsed, result

    async def run(self, function, *args, priority=0):
        return (await self.run_timed(function, *args, priority=priority))[2]


amount_worker = WorkerPool(1)


async def run_off_loop(function, *args):
    """Run a function in the single worker that formats large amounts."""
    return await amount_worker.run(function, *args)
