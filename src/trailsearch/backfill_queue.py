"""
Backfill queue implementations for local and distributed deployments.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterable
from datetime import datetime, timezone

from redis.asyncio import Redis

from trailsearch.local_index import CrawlJob


class RedisBackfillQueue:
    """Redis-backed distributed queue for multiple crawler workers."""

    def __init__(
        self,
        redis_client: Redis,
        key_prefix: str = "trailsearch:backfill",
        claim_ttl_seconds: int = 900,
    ) -> None:
        self.redis = redis_client
        self.key_prefix = key_prefix.rstrip(":")
        self.claim_ttl_seconds = claim_ttl_seconds

    async def enqueue_crawl_jobs(
        self,
        urls: Iterable[str],
        query: str,
        reason: str = "crawl_failed",
    ) -> int:
        normalized_urls = list(dict.fromkeys(url for url in urls if url))
        if not normalized_urls:
            return 0

        now_ts = time.time()
        now_iso = datetime.now(timezone.utc).isoformat()
        queued_count = 0
        async with self.redis.pipeline(transaction=True) as pipeline:
            for url in normalized_urls:
                job_key = self._job_key(url)
                existing = await self.redis.hgetall(job_key)
                status = existing.get("status", "") if existing else ""
                if status in {"succeeded", "running", "failed", "abandoned"}:
                    pipeline.hset(
                        job_key,
                        mapping={
                            "source_query": query,
                            "reason": reason,
                            "updated_at": now_iso,
                        },
                    )
                    continue

                mapping = {
                    "url": url,
                    "source_query": query,
                    "reason": reason,
                    "status": "queued",
                    "attempts": int(existing.get("attempts", 0)) if existing else 0,
                    "next_run_at": now_ts,
                    "last_error": existing.get("last_error", "") if existing else "",
                    "created_at": existing.get("created_at", now_iso) if existing else now_iso,
                    "updated_at": now_iso,
                }
                pipeline.hset(job_key, mapping=mapping)
                pipeline.zadd(self._due_key(), {url: now_ts})
                queued_count += 1
            await pipeline.execute()
        return queued_count

    async def claim_due_crawl_jobs(self, limit: int, max_attempts: int) -> list[CrawlJob]:
        if limit <= 0:
            return []

        now_ts = time.time()
        now_iso = datetime.now(timezone.utc).isoformat()
        due_urls = await self.redis.zrangebyscore(
            self._due_key(),
            min=0,
            max=now_ts,
            start=0,
            num=limit * 4,
        )
        claimed_jobs: list[CrawlJob] = []
        for url in due_urls:
            if len(claimed_jobs) >= limit:
                break

            lock_key = self._lock_key(url)
            locked = await self.redis.set(lock_key, "1", nx=True, ex=self.claim_ttl_seconds)
            if not locked:
                continue

            job_key = self._job_key(url)
            job = await self.redis.hgetall(job_key)
            if not job:
                await self.redis.delete(lock_key)
                await self.redis.zrem(self._due_key(), url)
                continue

            status = job.get("status", "")
            attempts = int(job.get("attempts", 0))
            if status not in {"queued", "failed"} or attempts >= max_attempts:
                await self.redis.delete(lock_key)
                await self.redis.zrem(self._due_key(), url)
                continue

            attempts += 1
            await self.redis.hset(
                job_key,
                mapping={
                    "status": "running",
                    "attempts": attempts,
                    "updated_at": now_iso,
                },
            )
            await self.redis.zrem(self._due_key(), url)
            job.update({"status": "running", "attempts": str(attempts), "updated_at": now_iso})
            claimed_jobs.append(self._dict_to_job(job))

        return claimed_jobs

    async def complete_crawl_job(
        self,
        url: str,
        succeeded: bool,
        error: str = "",
        max_attempts: int = 5,
        base_delay_seconds: float = 300.0,
        max_delay_seconds: float = 86400.0,
    ) -> None:
        job_key = self._job_key(url)
        job = await self.redis.hgetall(job_key)
        if not job:
            await self.redis.delete(self._lock_key(url))
            return

        attempts = int(job.get("attempts", 0))
        now_iso = datetime.now(timezone.utc).isoformat()
        if succeeded:
            await self.redis.hset(
                job_key,
                mapping={"status": "succeeded", "last_error": "", "updated_at": now_iso},
            )
            await self.redis.zrem(self._due_key(), url)
        elif attempts >= max_attempts:
            await self.redis.hset(
                job_key,
                mapping={
                    "status": "abandoned",
                    "last_error": error[:1000],
                    "updated_at": now_iso,
                },
            )
            await self.redis.zrem(self._due_key(), url)
        else:
            delay = min(base_delay_seconds * (2 ** max(attempts - 1, 0)), max_delay_seconds)
            next_run_at = time.time() + delay
            await self.redis.hset(
                job_key,
                mapping={
                    "status": "failed",
                    "next_run_at": next_run_at,
                    "last_error": error[:1000],
                    "updated_at": now_iso,
                },
            )
            await self.redis.zadd(self._due_key(), {url: next_run_at})
        await self.redis.delete(self._lock_key(url))

    async def get_crawl_job_stats(self) -> dict[str, int]:
        stats: dict[str, int] = {}
        async for key in self.redis.scan_iter(match=f"{self.key_prefix}:job:*", count=100):
            job = await self.redis.hgetall(key)
            status = job.get("status", "unknown")
            stats[status] = stats.get(status, 0) + 1
        stats["total"] = sum(stats.values())
        return stats

    async def list_crawl_jobs(self, limit: int = 50) -> list[CrawlJob]:
        jobs = []
        async for key in self.redis.scan_iter(match=f"{self.key_prefix}:job:*", count=100):
            job = await self.redis.hgetall(key)
            if job:
                jobs.append(self._dict_to_job(job))
        return sorted(jobs, key=lambda job: job.updated_at, reverse=True)[:limit]

    async def get_crawl_jobs_many(self, urls: Iterable[str]) -> dict[str, CrawlJob]:
        result = {}
        for url in list(dict.fromkeys(item for item in urls if item)):
            job = await self.redis.hgetall(self._job_key(url))
            if job:
                result[url] = self._dict_to_job(job)
        return result

    def _job_key(self, url: str) -> str:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return f"{self.key_prefix}:job:{digest}"

    def _lock_key(self, url: str) -> str:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return f"{self.key_prefix}:lock:{digest}"

    def _due_key(self) -> str:
        return f"{self.key_prefix}:due"

    @staticmethod
    def _dict_to_job(job: dict[str, str]) -> CrawlJob:
        return CrawlJob(
            url=job.get("url", ""),
            source_query=job.get("source_query", ""),
            reason=job.get("reason", ""),
            status=job.get("status", ""),
            attempts=int(float(job.get("attempts", 0) or 0)),
            next_run_at=float(job.get("next_run_at", 0) or 0),
            last_error=job.get("last_error", ""),
            created_at=job.get("created_at", ""),
            updated_at=job.get("updated_at", ""),
        )
