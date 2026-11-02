"""In-process job worker (design §8.7): an asyncio loop claiming jobs from SQLite."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import timedelta

from alexandria.library import Library
from alexandria.models import utcnow
from alexandria.storage.index import Job

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, library: Library) -> None:
        self.library = library
        self.settings = library.settings.worker
        self.handlers = library.handlers()
        self._tasks: list[asyncio.Task[None]] = []
        self._wake = asyncio.Event()
        self._stopping = False

    def wake(self) -> None:
        """Called after enqueueing so a job starts without waiting for the next poll."""
        self._wake.set()

    async def start(self) -> None:
        reset = self.library.startup()
        if reset:
            log.info("re-queued %d interrupted job(s)", reset)
        self._tasks = [
            asyncio.create_task(self._loop(n), name=f"worker-{n}")
            for n in range(max(1, self.settings.concurrency))
        ]

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []

    async def _loop(self, n: int) -> None:
        while not self._stopping:
            job = self.library.index.claim_job()
            if job is None:
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), self.settings.poll_interval_s)
                continue
            await self.run(job)

    async def run(self, job: Job) -> None:
        handler = self.handlers.get(job.type)
        index = self.library.index
        if handler is None:
            index.fail_job(job.id, f"unknown job type {job.type!r}", None)
            return
        log.info("job %s %s (%s) attempt %d", job.id, job.type, job.resource_id, job.attempts)
        try:
            await handler(job)
        except asyncio.CancelledError:
            raise  # shutdown: the job stays 'running' and is re-queued at next start
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            if self.library.is_final_attempt(job, exc):
                log.warning("job %s failed permanently: %s", job.id, message)
                index.fail_job(job.id, message, None)
            else:
                backoff = self.settings.backoff_s
                delay = backoff[min(job.attempts - 1, len(backoff) - 1)] if backoff else 60
                log.info("job %s failed (%s); retry in %ss", job.id, message, delay)
                index.fail_job(job.id, message, utcnow() + timedelta(seconds=delay))
        else:
            index.finish_job(job.id)

    async def drain(self, timeout: float = 30) -> None:
        """Run queued jobs until none are due. For tests and the CLI."""
        async with asyncio.timeout(timeout):
            while (job := self.library.index.claim_job()) is not None:
                await self.run(job)
