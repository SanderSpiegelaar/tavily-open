"""
Lightweight local search index backed by SQLite FTS.
"""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from loguru import logger


@dataclass(frozen=True)
class LocalIndexDocument:
    """A document returned by the local index."""

    url: str
    title: str
    snippet: str
    content: str
    domain: str
    score: float


@dataclass(frozen=True)
class CrawlJob:
    """A pending or historical background crawl job."""

    url: str
    source_query: str
    reason: str
    status: str
    attempts: int
    next_run_at: float
    last_error: str
    created_at: str
    updated_at: str


class LocalIndex:
    """Small local index that accumulates successfully extracted pages."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._fts_available = True

    async def initialize(self) -> None:
        """Create the database and FTS tables."""
        await asyncio.to_thread(self._initialize_sync)

    async def close(self) -> None:
        """Close the SQLite connection."""
        await asyncio.to_thread(self._close_sync)

    async def upsert_many(self, items: Iterable[dict[str, str]]) -> int:
        """Insert or update extracted documents."""
        return await asyncio.to_thread(self._upsert_many_sync, list(items))

    async def search(self, query: str, limit: int = 10) -> list[LocalIndexDocument]:
        """Search indexed content."""
        return await asyncio.to_thread(self._search_sync, query, limit)

    async def get_many(self, urls: Iterable[str]) -> dict[str, LocalIndexDocument]:
        """Load indexed documents by URL."""
        return await asyncio.to_thread(self._get_many_sync, list(urls))

    async def enqueue_crawl_jobs(
        self,
        urls: Iterable[str],
        query: str,
        reason: str = "crawl_failed",
    ) -> int:
        """Persist URLs for asynchronous background retry."""
        return await asyncio.to_thread(self._enqueue_crawl_jobs_sync, list(urls), query, reason)

    async def claim_due_crawl_jobs(
        self,
        limit: int,
        max_attempts: int,
    ) -> list[CrawlJob]:
        """Claim due jobs so one worker can process them."""
        return await asyncio.to_thread(self._claim_due_crawl_jobs_sync, limit, max_attempts)

    async def complete_crawl_job(
        self,
        url: str,
        succeeded: bool,
        error: str = "",
        max_attempts: int = 5,
        base_delay_seconds: float = 300.0,
        max_delay_seconds: float = 86400.0,
    ) -> None:
        """Mark a background crawl job as succeeded or schedule a retry."""
        await asyncio.to_thread(
            self._complete_crawl_job_sync,
            url,
            succeeded,
            error,
            max_attempts,
            base_delay_seconds,
            max_delay_seconds,
        )

    async def get_crawl_job_stats(self) -> dict[str, int]:
        """Return job counts by status."""
        return await asyncio.to_thread(self._get_crawl_job_stats_sync)

    async def list_crawl_jobs(self, limit: int = 50) -> list[CrawlJob]:
        """List recent crawl jobs for observability."""
        return await asyncio.to_thread(self._list_crawl_jobs_sync, limit)

    async def get_crawl_jobs_many(self, urls: Iterable[str]) -> dict[str, CrawlJob]:
        """Load crawl jobs by URL."""
        return await asyncio.to_thread(self._get_crawl_jobs_many_sync, list(urls))

    def _connect(self) -> sqlite3.Connection:
        if self._connection is None:
            db_path = Path(self.path)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(
                str(db_path),
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
        return self._connection

    def _initialize_sync(self) -> None:
        with self._lock:
            connection = self._connect()
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    url TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    snippet TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL DEFAULT '',
                    domain TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT '',
                    source_query TEXT NOT NULL DEFAULT '',
                    fetched_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS crawl_jobs (
                    url TEXT PRIMARY KEY,
                    source_query TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'queued',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_run_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_crawl_jobs_due
                ON crawl_jobs(status, next_run_at, attempts)
                """
            )
            try:
                connection.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts
                    USING fts5(url UNINDEXED, title, snippet, content, tokenize='unicode61')
                    """
                )
                self._fts_available = True
            except sqlite3.OperationalError as exc:
                logger.warning(f"SQLite FTS5 unavailable, local index will use LIKE search: {exc}")
                self._fts_available = False
            connection.commit()

    def _close_sync(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def _upsert_many_sync(self, items: list[dict[str, str]]) -> int:
        if not items:
            return 0

        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for item in items:
            url = item.get("url") or item.get("reference") or ""
            content = item.get("content") or ""
            if not url or not content:
                continue

            parsed = urlparse(url)
            rows.append(
                {
                    "url": url,
                    "title": item.get("title", ""),
                    "snippet": item.get("snippet", ""),
                    "content": content,
                    "domain": parsed.netloc.lower(),
                    "provider": item.get("provider", ""),
                    "source_query": item.get("source_query", ""),
                    "fetched_at": item.get("fetched_at", now),
                    "updated_at": now,
                }
            )

        if not rows:
            return 0

        with self._lock:
            connection = self._connect()
            for row in rows:
                connection.execute(
                    """
                    INSERT INTO documents (
                        url, title, snippet, content, domain, provider,
                        source_query, fetched_at, updated_at
                    )
                    VALUES (
                        :url, :title, :snippet, :content, :domain, :provider,
                        :source_query, :fetched_at, :updated_at
                    )
                    ON CONFLICT(url) DO UPDATE SET
                        title=excluded.title,
                        snippet=excluded.snippet,
                        content=excluded.content,
                        domain=excluded.domain,
                        provider=excluded.provider,
                        source_query=excluded.source_query,
                        updated_at=excluded.updated_at
                    """,
                    row,
                )
                if self._fts_available:
                    connection.execute("DELETE FROM documents_fts WHERE url = ?", (row["url"],))
                    connection.execute(
                        """
                        INSERT INTO documents_fts (url, title, snippet, content)
                        VALUES (?, ?, ?, ?)
                        """,
                        (row["url"], row["title"], row["snippet"], row["content"]),
                    )
            connection.commit()
        return len(rows)

    def _search_sync(self, query: str, limit: int = 10) -> list[LocalIndexDocument]:
        normalized_query = (query or "").strip()
        if not normalized_query:
            return []

        with self._lock:
            connection = self._connect()
            if self._fts_available:
                try:
                    return self._search_fts(connection, normalized_query, limit)
                except sqlite3.OperationalError as exc:
                    logger.debug(f"FTS search failed, falling back to LIKE: {exc}")
            return self._search_like(connection, normalized_query, limit)

    def _get_many_sync(self, urls: list[str]) -> dict[str, LocalIndexDocument]:
        if not urls:
            return {}

        placeholders = ",".join("?" for _ in urls)
        with self._lock:
            connection = self._connect()
            rows = connection.execute(
                f"""
                SELECT url, title, snippet, content, domain, 0.0 AS rank
                FROM documents
                WHERE url IN ({placeholders})
                """,
                urls,
            ).fetchall()
        return {row["url"]: self._row_to_document(row, rank=row["rank"]) for row in rows}

    def _enqueue_crawl_jobs_sync(self, urls: list[str], query: str, reason: str) -> int:
        normalized_urls = list(dict.fromkeys(url for url in urls if url))
        if not normalized_urls:
            return 0

        now_iso = datetime.now(timezone.utc).isoformat()
        now_ts = time.time()
        queued_count = 0
        with self._lock:
            connection = self._connect()
            for url in normalized_urls:
                existing = connection.execute(
                    "SELECT status FROM crawl_jobs WHERE url = ?",
                    (url,),
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO crawl_jobs (
                        url, source_query, reason, status, attempts,
                        next_run_at, last_error, created_at, updated_at
                    )
                    VALUES (?, ?, ?, 'queued', 0, ?, '', ?, ?)
                    ON CONFLICT(url) DO UPDATE SET
                        source_query=excluded.source_query,
                        reason=excluded.reason,
                        status=CASE
                            WHEN crawl_jobs.status = 'succeeded' THEN crawl_jobs.status
                            WHEN crawl_jobs.status = 'running' THEN crawl_jobs.status
                            WHEN crawl_jobs.status = 'abandoned' THEN crawl_jobs.status
                            ELSE 'queued'
                        END,
                        next_run_at=CASE
                            WHEN crawl_jobs.status IN ('succeeded', 'running', 'failed', 'abandoned')
                                THEN crawl_jobs.next_run_at
                            ELSE MIN(crawl_jobs.next_run_at, excluded.next_run_at)
                        END,
                        updated_at=excluded.updated_at
                    """,
                    (url, query, reason, now_ts, now_iso, now_iso),
                )
                if existing is None or existing["status"] not in {"succeeded", "running", "abandoned"}:
                    queued_count += 1
            connection.commit()
        return queued_count

    def _claim_due_crawl_jobs_sync(self, limit: int, max_attempts: int) -> list[CrawlJob]:
        if limit <= 0:
            return []

        now_ts = time.time()
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            connection = self._connect()
            rows = connection.execute(
                """
                SELECT *
                FROM crawl_jobs
                WHERE status IN ('queued', 'failed')
                  AND next_run_at <= ?
                  AND attempts < ?
                ORDER BY next_run_at ASC, updated_at ASC
                LIMIT ?
                """,
                (now_ts, max_attempts, limit),
            ).fetchall()
            urls = [row["url"] for row in rows]
            if urls:
                placeholders = ",".join("?" for _ in urls)
                connection.execute(
                    f"""
                    UPDATE crawl_jobs
                    SET status = 'running',
                        attempts = attempts + 1,
                        updated_at = ?
                    WHERE url IN ({placeholders})
                    """,
                    [now_iso, *urls],
                )
                connection.commit()

        claimed_jobs = []
        for row in rows:
            row_dict = dict(row)
            row_dict["status"] = "running"
            row_dict["attempts"] = int(row_dict["attempts"]) + 1
            row_dict["updated_at"] = now_iso
            claimed_jobs.append(self._row_to_crawl_job(row_dict))
        return claimed_jobs

    def _complete_crawl_job_sync(
        self,
        url: str,
        succeeded: bool,
        error: str,
        max_attempts: int,
        base_delay_seconds: float,
        max_delay_seconds: float,
    ) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            connection = self._connect()
            row = connection.execute(
                "SELECT attempts FROM crawl_jobs WHERE url = ?",
                (url,),
            ).fetchone()
            if row is None:
                return

            attempts = int(row["attempts"])
            if succeeded:
                connection.execute(
                    """
                    UPDATE crawl_jobs
                    SET status = 'succeeded',
                        last_error = '',
                        updated_at = ?
                    WHERE url = ?
                    """,
                    (now_iso, url),
                )
            elif attempts >= max_attempts:
                connection.execute(
                    """
                    UPDATE crawl_jobs
                    SET status = 'abandoned',
                        last_error = ?,
                        updated_at = ?
                    WHERE url = ?
                    """,
                    (error[:1000], now_iso, url),
                )
            else:
                delay = min(base_delay_seconds * (2 ** max(attempts - 1, 0)), max_delay_seconds)
                connection.execute(
                    """
                    UPDATE crawl_jobs
                    SET status = 'failed',
                        next_run_at = ?,
                        last_error = ?,
                        updated_at = ?
                    WHERE url = ?
                    """,
                    (time.time() + delay, error[:1000], now_iso, url),
                )
            connection.commit()

    def _get_crawl_job_stats_sync(self) -> dict[str, int]:
        with self._lock:
            connection = self._connect()
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM crawl_jobs GROUP BY status"
            ).fetchall()
        stats = {row["status"]: int(row["count"]) for row in rows}
        stats["total"] = sum(stats.values())
        return stats

    def _list_crawl_jobs_sync(self, limit: int) -> list[CrawlJob]:
        with self._lock:
            connection = self._connect()
            rows = connection.execute(
                """
                SELECT *
                FROM crawl_jobs
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_crawl_job(row) for row in rows]

    def _get_crawl_jobs_many_sync(self, urls: list[str]) -> dict[str, CrawlJob]:
        normalized_urls = list(dict.fromkeys(url for url in urls if url))
        if not normalized_urls:
            return {}

        placeholders = ",".join("?" for _ in normalized_urls)
        with self._lock:
            connection = self._connect()
            rows = connection.execute(
                f"""
                SELECT *
                FROM crawl_jobs
                WHERE url IN ({placeholders})
                """,
                normalized_urls,
            ).fetchall()
        return {row["url"]: self._row_to_crawl_job(row) for row in rows}

    def _search_fts(
        self,
        connection: sqlite3.Connection,
        query: str,
        limit: int,
    ) -> list[LocalIndexDocument]:
        fts_query = self._build_fts_query(query)
        rows = connection.execute(
            """
            SELECT
                d.url,
                d.title,
                d.snippet,
                d.content,
                d.domain,
                bm25(documents_fts) AS rank
            FROM documents_fts
            JOIN documents d ON d.url = documents_fts.url
            WHERE documents_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (fts_query, limit),
        ).fetchall()
        return [self._row_to_document(row, rank=row["rank"]) for row in rows]

    def _search_like(
        self,
        connection: sqlite3.Connection,
        query: str,
        limit: int,
    ) -> list[LocalIndexDocument]:
        terms = self._search_terms(query)
        if not terms:
            return []

        where = " OR ".join(["content LIKE ? OR title LIKE ? OR snippet LIKE ?" for _ in terms])
        params: list[str | int] = []
        for term in terms:
            pattern = f"%{term}%"
            params.extend([pattern, pattern, pattern])
        params.append(limit)

        rows = connection.execute(
            f"""
            SELECT url, title, snippet, content, domain, 0.0 AS rank
            FROM documents
            WHERE {where}
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [self._row_to_document(row, rank=row["rank"]) for row in rows]

    @staticmethod
    def _search_terms(query: str) -> list[str]:
        return [
            term.lower()
            for term in re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]+", query)
            if term.strip()
        ]

    @classmethod
    def _build_fts_query(cls, query: str) -> str:
        terms = cls._search_terms(query)
        if not terms:
            return '""'
        escaped_terms = [term.replace('"', '""') for term in terms[:8]]
        return " OR ".join(f'"{term}"' for term in escaped_terms)

    @staticmethod
    def _row_to_document(sqlite_row: sqlite3.Row, rank: float) -> LocalIndexDocument:
        content = sqlite_row["content"] or ""
        snippet = sqlite_row["snippet"] or content[:280]
        score = 1 / (1 + max(float(rank or 0), 0))
        return LocalIndexDocument(
            url=sqlite_row["url"],
            title=sqlite_row["title"] or sqlite_row["domain"] or sqlite_row["url"],
            snippet=snippet,
            content=content,
            domain=sqlite_row["domain"] or "",
            score=round(score, 4),
        )

    @staticmethod
    def _row_to_crawl_job(sqlite_row) -> CrawlJob:
        return CrawlJob(
            url=sqlite_row["url"],
            source_query=sqlite_row["source_query"] or "",
            reason=sqlite_row["reason"] or "",
            status=sqlite_row["status"] or "",
            attempts=int(sqlite_row["attempts"] or 0),
            next_run_at=float(sqlite_row["next_run_at"] or 0),
            last_error=sqlite_row["last_error"] or "",
            created_at=sqlite_row["created_at"] or "",
            updated_at=sqlite_row["updated_at"] or "",
        )


def default_local_index_path() -> str:
    """Return a docker-friendly default local index path."""
    return os.path.join(os.getcwd(), "data", "trailsearch.sqlite3")
