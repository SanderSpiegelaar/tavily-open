"""
Asynchronous background crawl backfill worker.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from trailsearch.crawler import WebCrawler
from trailsearch.local_index import CrawlJob, LocalIndex


class BackfillWorker:
    """Drain persisted crawl jobs without blocking foreground search requests."""

    def __init__(
        self,
        crawler: WebCrawler,
        local_index: LocalIndex,
        backfill_queue=None,
        interval_seconds: float = 30.0,
        batch_size: int = 3,
        max_attempts: int = 5,
        base_delay_seconds: float = 300.0,
        max_delay_seconds: float = 86400.0,
    ) -> None:
        self.crawler = crawler
        self.local_index = local_index
        self.backfill_queue = backfill_queue or local_index
        self.interval_seconds = interval_seconds
        self.batch_size = batch_size
        self.max_attempts = max_attempts
        self.base_delay_seconds = base_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        """Start the worker loop if it is not already running."""
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="trailsearch-backfill-worker")
        logger.info("Backfill worker started")

    def is_running(self) -> bool:
        """Return whether the worker task is active."""
        return bool(self._task and not self._task.done())

    async def stop(self) -> None:
        """Stop the worker loop and wait for it to exit."""
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Backfill worker stopped")

    async def run_once(self) -> int:
        """Process one due batch. Useful for tests and manual maintenance."""
        jobs = await self.backfill_queue.claim_due_crawl_jobs(
            limit=self.batch_size,
            max_attempts=self.max_attempts,
        )
        if not jobs:
            return 0

        processed_count = 0
        for job in jobs:
            await self._process_job(job)
            processed_count += 1
        return processed_count

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                processed_count = await self.run_once()
                if processed_count == 0:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.interval_seconds,
                    )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"Backfill worker loop error: {exc}")
                await asyncio.sleep(self.interval_seconds)

    async def _process_job(self, job: CrawlJob) -> None:
        logger.info(
            f"Backfill crawling {job.url}, attempt {job.attempts}/{self.max_attempts}, "
            f"reason={job.reason}"
        )
        try:
            crawl_result = await self.crawler.crawl_urls([job.url], job.source_query)
            indexed_count = await self._index_successes(job, crawl_result)
            succeeded = indexed_count > 0
            error = "" if succeeded else "no usable content returned"
        except Exception as exc:
            succeeded = False
            error = str(exc)

        await self.backfill_queue.complete_crawl_job(
            job.url,
            succeeded=succeeded,
            error=error,
            max_attempts=self.max_attempts,
            base_delay_seconds=self.base_delay_seconds,
            max_delay_seconds=self.max_delay_seconds,
        )

    async def _index_successes(self, job: CrawlJob, crawl_result: dict[str, Any]) -> int:
        items = []
        for result in crawl_result.get("results", []):
            url = result.get("reference", "")
            content = result.get("content", "")
            if not url or not content:
                continue
            items.append(
                {
                    "url": url,
                    "title": "",
                    "snippet": "",
                    "content": content,
                    "provider": "backfill",
                    "source_query": job.source_query,
                }
            )
        return await self.local_index.upsert_many(items)
