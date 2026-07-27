"""
Tests for the background backfill worker.
"""

import pytest

from trailsearch.backfill import BackfillWorker
from trailsearch.local_index import LocalIndex


class FakeCrawler:
    """Minimal crawler test double."""

    async def crawl_urls(self, urls, instruction):
        return {
            "results": [
                {
                    "reference": urls[0],
                    "content": f"Recovered content for {instruction}",
                }
            ],
            "failed_urls": [],
        }


@pytest.mark.asyncio
async def test_backfill_worker_indexes_successful_job(tmp_path):
    """A successful backfill job should write content and mark the job succeeded."""
    index = LocalIndex(str(tmp_path / "trailsearch.sqlite3"))
    await index.initialize()
    try:
        await index.enqueue_crawl_jobs(
            ["https://example.com/recovered"],
            query="recovered page",
            reason="crawl_failed",
        )
        worker = BackfillWorker(
            crawler=FakeCrawler(),
            local_index=index,
            interval_seconds=0.01,
            batch_size=1,
            max_attempts=2,
            base_delay_seconds=0,
            max_delay_seconds=0,
        )

        processed = await worker.run_once()
        docs = await index.get_many(["https://example.com/recovered"])
        stats = await index.get_crawl_job_stats()

        assert processed == 1
        assert docs["https://example.com/recovered"].content == "Recovered content for recovered page"
        assert stats["succeeded"] == 1
    finally:
        await index.close()
