"""
Cache Module - Provides distributed caching functionality for crawl results

This module provides a distributed cache system using Redis as the backend,
allowing multiple instances to share cached crawl results and reduce redundant
crawling operations.
"""

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Optional

import redis.asyncio as redis
from loguru import logger
from redis.asyncio import Redis


class CacheManager:
    """Distributed cache manager using Redis backend"""

    def __init__(
        self,
        redis_url: str,
        ttl_hours: int = 24,
        search_ttl_seconds: int = 300,
    ):
        """Initialize cache manager with Redis connection

        Args:
            redis_url: Redis connection URL (e.g., 'redis://localhost:6379/0')
            ttl_hours: Time-to-live for cached items in hours, default is 24
            search_ttl_seconds: Time-to-live for cached search results in seconds
        """
        self.redis_url = redis_url
        self.redis_client: Optional[Redis] = None
        self.ttl_seconds = ttl_hours * 3600
        self.search_ttl_seconds = search_ttl_seconds
        self._available = False

    async def initialize(self) -> bool:
        """Initialize the async Redis client and verify connectivity."""
        try:
            self.redis_client = redis.from_url(self.redis_url, decode_responses=True)
            assert self.redis_client is not None
            await self.redis_client.ping()
            self._available = True
            logger.info(
                f"Cache manager initialized with Redis: {self.redis_url}, "
                f"crawl TTL: {self.ttl_seconds} seconds, "
                f"search TTL: {self.search_ttl_seconds} seconds"
            )
            return True
        except Exception as e:
            logger.error(f"Failed to initialize cache manager: {str(e)}")
            self.redis_client = None
            self._available = False
            return False

    async def close(self) -> None:
        """Close the Redis client if it was initialized."""
        if not self.redis_client:
            return

        try:
            await self.redis_client.aclose()
        except Exception as e:
            logger.warning(f"Error closing Redis client: {str(e)}")
        finally:
            self.redis_client = None
            self._available = False

    def _generate_cache_key(self, url: str, instruction: str = "") -> str:
        """Generate a cache key from URL and instruction

        Args:
            url: The URL to cache
            instruction: Optional instruction/query string

        Returns:
            str: Generated cache key
        """
        # Create a hash of URL and instruction to generate cache key
        cache_input = f"{url}:{instruction}".encode()
        cache_hash = hashlib.md5(cache_input).hexdigest()
        return f"crawl_cache:{cache_hash}"

    def _generate_search_cache_key(
        self,
        query: str,
        request_fingerprint: Optional[dict[str, Any]] = None,
    ) -> str:
        """Generate a cache key from a search query

        Args:
            query: The search query string
            request_fingerprint: Optional normalized request metadata

        Returns:
            str: Generated cache key
        """
        normalized_fingerprint = json.dumps(
            request_fingerprint or {},
            ensure_ascii=False,
            sort_keys=True,
        )
        cache_input = f"search:{query}:{normalized_fingerprint}".encode()
        cache_hash = hashlib.md5(cache_input).hexdigest()
        return f"search_cache:{cache_hash}"

    def _build_crawl_key_map(
        self, urls: Sequence[str], instruction: str = ""
    ) -> list[tuple[str, str]]:
        """Build ordered crawl cache keys for batched Redis operations."""
        return [(url, self._generate_cache_key(url, instruction)) for url in urls]

    async def get_search_cache(
        self,
        query: str,
        request_fingerprint: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """Get cached search result for a query

        Args:
            query: The search query to retrieve from cache
            request_fingerprint: Optional normalized request metadata

        Returns:
            Dict or None: Cached result if found, None otherwise
        """
        if not self.is_available() or not self.redis_client:
            logger.debug("Redis client not available, skipping cache retrieval")
            return None

        try:
            cache_key = self._generate_search_cache_key(query, request_fingerprint)
            cached_data = await self.redis_client.get(cache_key)

            if cached_data:
                result = json.loads(str(cached_data))
                logger.debug(f"Search cache hit for query: {query}")
                # The actual result is nested inside the 'result' key
                return result.get("result")
            else:
                logger.debug(f"Search cache miss for query: {query}")
                return None
        except Exception as e:
            logger.warning(f"Error retrieving from search cache: {str(e)}")
            return None

    async def set_search_cache(
        self,
        query: str,
        result: dict[str, Any],
        request_fingerprint: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Cache a search result for a query.

        Args:
            query: The search query being cached
            result: The search result to cache
            request_fingerprint: Optional normalized request metadata

        Returns:
            bool: True if caching succeeded, False otherwise
        """
        if not self.is_available() or not self.redis_client:
            logger.debug("Redis client not available, skipping cache storage")
            return False

        try:
            cache_key = self._generate_search_cache_key(query, request_fingerprint)
            cache_data = {
                "result": result,
                "cached_at": datetime.now().isoformat(),
                "query": query,
                "request_fingerprint": request_fingerprint or {},
            }

            await self.redis_client.setex(
                cache_key, self.search_ttl_seconds, json.dumps(cache_data, ensure_ascii=False)
            )
            logger.debug(f"Cached search result for query: {query}")
            return True
        except Exception as e:
            logger.warning(f"Error storing in search cache: {str(e)}")
            return False

    async def get(self, url: str, instruction: str = "") -> Optional[dict[str, Any]]:
        """Get cached crawl result for a URL

        Args:
            url: The URL to retrieve from cache
            instruction: Optional instruction/query string

        Returns:
            Dict or None: Cached result if found, None otherwise
        """
        if not self.is_available() or not self.redis_client:
            logger.debug("Redis client not available, skipping cache retrieval")
            return None

        try:
            cache_key = self._generate_cache_key(url, instruction)
            cached_data = await self.redis_client.get(cache_key)

            if cached_data:
                result = json.loads(cached_data)
                logger.debug(f"Cache hit for URL: {url}")
                return result
            else:
                logger.debug(f"Cache miss for URL: {url}")
                return None
        except Exception as e:
            logger.warning(f"Error retrieving from cache: {str(e)}")
            return None

    async def set(
        self,
        url: str,
        content: str,
        reference: str,
        instruction: str = "",
        source_stage: str = "",
        quality_score: str = "",
    ) -> bool:
        """Cache a crawl result for a URL

        Args:
            url: The URL being cached
            content: The crawled content
            reference: The reference URL
            instruction: Optional instruction/query string

        Returns:
            bool: True if caching succeeded, False otherwise
        """
        if not self.is_available() or not self.redis_client:
            logger.debug("Redis client not available, skipping cache storage")
            return False

        try:
            cache_key = self._generate_cache_key(url, instruction)
            cache_data = {
                "content": content,
                "reference": reference,
                "cached_at": datetime.now().isoformat(),
                "url": url,
                "source_stage": source_stage,
                "quality_score": quality_score,
            }

            await self.redis_client.setex(
                cache_key, self.ttl_seconds, json.dumps(cache_data, ensure_ascii=False)
            )
            logger.debug(f"Cached result for URL: {url}")
            return True
        except Exception as e:
            logger.warning(f"Error storing in cache: {str(e)}")
            return False

    async def get_batch(
        self,
        urls: Sequence[str],
        instruction: str = "",
    ) -> dict[str, Optional[dict[str, Any]]]:
        """Get cached results for multiple URLs

        Args:
            urls: List of URLs to retrieve from cache
            instruction: Optional instruction/query string

        Returns:
            Dict: Mapping of URLs to cached results (None if not found)
        """
        if not self.is_available() or not self.redis_client:
            logger.debug("Redis client not available, skipping batch cache retrieval")
            return dict.fromkeys(urls)

        try:
            key_pairs = self._build_crawl_key_map(urls, instruction)
            keys = [cache_key for _, cache_key in key_pairs]
            cached_values = await self.redis_client.mget(keys)
            results: dict[str, Optional[dict[str, Any]]] = {}
            for (url, _), cached_value in zip(key_pairs, cached_values):
                results[url] = json.loads(cached_value) if cached_value else None
            return results
        except Exception as e:
            logger.warning(f"Error retrieving batch from cache: {str(e)}")
            return dict.fromkeys(urls)

    async def set_batch(self, items: Sequence[dict[str, str]], instruction: str = "") -> int:
        """Cache multiple crawl results

        Args:
            items: List of dicts with 'url', 'content', and 'reference' keys
            instruction: Optional instruction/query string

        Returns:
            int: Number of items successfully cached
        """
        if not self.is_available() or not self.redis_client:
            logger.debug("Redis client not available, skipping batch cache storage")
            return 0

        try:
            async with self.redis_client.pipeline(transaction=False) as pipeline:
                key_order: list[str] = []
                success_count = 0

                for item in items:
                    url = item.get("url")
                    content = item.get("content")
                    reference = item.get("reference")
                    if not url or content is None or reference is None:
                        continue

                    cache_key = self._generate_cache_key(url, instruction)
                    cache_data = {
                        "content": content,
                        "reference": reference,
                        "cached_at": datetime.now().isoformat(),
                        "url": url,
                        "source_stage": item.get("source_stage", ""),
                        "quality_score": item.get("quality_score", ""),
                    }
                    key_order.append(url)
                    pipeline.setex(
                        cache_key,
                        self.ttl_seconds,
                        json.dumps(cache_data, ensure_ascii=False),
                    )

                if not key_order:
                    return 0

                results = await pipeline.execute()
                success_count = sum(1 for result in results if result)
            return success_count
        except Exception as e:
            logger.warning(f"Error storing batch in cache: {str(e)}")
            return 0

    async def clear_url(self, url: str, instruction: str = "") -> bool:
        """Clear cache for a specific URL

        Args:
            url: The URL to clear from cache
            instruction: Optional instruction/query string

        Returns:
            bool: True if cleared successfully, False otherwise
        """
        if not self.is_available() or not self.redis_client:
            return False

        try:
            cache_key = self._generate_cache_key(url, instruction)
            await self.redis_client.delete(cache_key)
            logger.debug(f"Cleared cache for URL: {url}")
            return True
        except Exception as e:
            logger.warning(f"Error clearing cache: {str(e)}")
            return False

    async def _clear_pattern(self, pattern: str) -> int:
        """Delete all keys matching a Redis pattern."""
        if not self.redis_client:
            return 0

        cursor = 0
        deleted_count = 0
        while True:
            cursor, keys = await self.redis_client.scan(cursor, match=pattern, count=100)
            if keys:
                deleted_count += await self.redis_client.delete(*keys)
            if cursor == 0:
                break
        return deleted_count

    async def _count_pattern(self, pattern: str) -> int:
        """Count keys matching a Redis pattern."""
        if not self.redis_client:
            return 0

        cursor = 0
        total_entries = 0
        while True:
            cursor, keys = await self.redis_client.scan(cursor, match=pattern, count=100)
            total_entries += len(keys)
            if cursor == 0:
                break
        return total_entries

    async def clear_all(self) -> bool:
        """Clear all crawl and search cache entries.

        Returns:
            bool: True if cleared successfully, False otherwise
        """
        if not self.is_available() or not self.redis_client:
            return False

        try:
            deleted_crawl_count = await self._clear_pattern("crawl_cache:*")
            deleted_search_count = await self._clear_pattern("search_cache:*")
            logger.info(
                f"Cleared {deleted_crawl_count} crawl cache entries and "
                f"{deleted_search_count} search cache entries"
            )
            return True
        except Exception as e:
            logger.warning(f"Error clearing all cache: {str(e)}")
            return False

    async def get_cache_stats(self) -> dict[str, Any]:
        """Get cache statistics

        Returns:
            Dict: Cache statistics including total entries and memory usage
        """
        if not self.is_available() or not self.redis_client:
            return {"status": "unavailable"}

        try:
            crawl_entries = await self._count_pattern("crawl_cache:*")
            search_entries = await self._count_pattern("search_cache:*")
            info = await self.redis_client.info()
            return {
                "status": "available",
                "total_entries": crawl_entries + search_entries,
                "crawl_entries": crawl_entries,
                "search_entries": search_entries,
                "memory_used": info.get("used_memory_human", "unknown"),
                "redis_version": info.get("redis_version", "unknown"),
            }
        except Exception as e:
            logger.warning(f"Error getting cache stats: {str(e)}")
            return {"status": "error", "error": str(e)}

    def is_available(self) -> bool:
        """Check if cache is available

        Returns:
            bool: True if Redis connection is available, False otherwise
        """
        return self._available and self.redis_client is not None
