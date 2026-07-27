"""
Tests for the lightweight local SQLite index.
"""

import pytest

from trailsearch.local_index import LocalIndex


@pytest.mark.asyncio
async def test_local_index_upserts_searches_and_loads_documents(tmp_path):
    """Local index should persist extracted content and return reusable documents."""
    index = LocalIndex(str(tmp_path / "trailsearch.sqlite3"))
    await index.initialize()
    try:
        stored_count = await index.upsert_many(
            [
                {
                    "url": "https://example.com/reader",
                    "title": "Reader First",
                    "snippet": "Reader fallback benchmark",
                    "content": "Reader fallback extraction makes Tavily-like search cheaper.",
                    "provider": "test",
                    "source_query": "reader fallback",
                }
            ]
        )

        search_results = await index.search("reader fallback", limit=5)
        documents = await index.get_many(["https://example.com/reader"])

        assert stored_count == 1
        assert search_results
        assert search_results[0].url == "https://example.com/reader"
        assert documents["https://example.com/reader"].content.startswith("Reader fallback")
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_local_index_crawl_jobs_retry_and_abandon(tmp_path):
    """Crawl jobs should be claimed, retried with backoff, then abandoned."""
    index = LocalIndex(str(tmp_path / "trailsearch.sqlite3"))
    await index.initialize()
    try:
        queued_count = await index.enqueue_crawl_jobs(
            ["https://example.com/blocked"],
            query="blocked page",
            reason="crawl_failed",
        )
        claimed_jobs = await index.claim_due_crawl_jobs(limit=1, max_attempts=2)
        await index.complete_crawl_job(
            claimed_jobs[0].url,
            succeeded=False,
            error="blocked",
            max_attempts=2,
            base_delay_seconds=0,
            max_delay_seconds=0,
        )
        second_claim = await index.claim_due_crawl_jobs(limit=1, max_attempts=2)
        await index.complete_crawl_job(
            second_claim[0].url,
            succeeded=False,
            error="still blocked",
            max_attempts=2,
            base_delay_seconds=0,
            max_delay_seconds=0,
        )
        stats = await index.get_crawl_job_stats()

        assert queued_count == 1
        assert claimed_jobs[0].attempts == 1
        assert second_claim[0].attempts == 2
        assert stats["abandoned"] == 1
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_reenqueue_does_not_reset_failed_backoff(tmp_path):
    """A failed job should keep its delayed next run when the same URL is re-enqueued."""
    index = LocalIndex(str(tmp_path / "trailsearch.sqlite3"))
    await index.initialize()
    try:
        await index.enqueue_crawl_jobs(
            ["https://example.com/backoff"],
            query="blocked page",
            reason="crawl_failed",
        )
        claimed_jobs = await index.claim_due_crawl_jobs(limit=1, max_attempts=5)
        await index.complete_crawl_job(
            claimed_jobs[0].url,
            succeeded=False,
            error="blocked",
            max_attempts=5,
            base_delay_seconds=3600,
            max_delay_seconds=3600,
        )

        await index.enqueue_crawl_jobs(
            ["https://example.com/backoff"],
            query="blocked page",
            reason="foreground_failed_again",
        )
        due_jobs = await index.claim_due_crawl_jobs(limit=1, max_attempts=5)

        assert due_jobs == []
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_local_index_loads_crawl_jobs_by_url(tmp_path):
    """Local index should expose active job state for foreground skip decisions."""
    index = LocalIndex(str(tmp_path / "trailsearch.sqlite3"))
    await index.initialize()
    try:
        await index.enqueue_crawl_jobs(
            ["https://example.com/pending"],
            query="pending page",
            reason="anti_crawl",
        )
        jobs = await index.get_crawl_jobs_many(
            ["https://example.com/pending", "https://example.com/missing"]
        )

        assert set(jobs) == {"https://example.com/pending"}
        assert jobs["https://example.com/pending"].status == "queued"
        assert jobs["https://example.com/pending"].reason == "anti_crawl"
    finally:
        await index.close()
