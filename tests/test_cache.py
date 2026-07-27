"""
Tests for cache key generation.
"""

from trailsearch.cache import CacheManager


def test_search_cache_key_includes_request_fingerprint():
    """Search cache keys should vary by provider, mode, and other request settings."""
    cache_manager = CacheManager("redis://localhost:6379/0")

    searxng_key = cache_manager._generate_search_cache_key(
        "example query",
        {
            "provider": "searxng",
            "mode": "crawl",
            "limit": 5,
            "enabled_engines": "google__general",
        },
    )
    brave_key = cache_manager._generate_search_cache_key(
        "example query",
        {
            "provider": "brave",
            "mode": "crawl",
            "limit": 5,
            "enabled_engines": "google__general",
        },
    )
    search_mode_key = cache_manager._generate_search_cache_key(
        "example query",
        {
            "provider": "searxng",
            "mode": "search",
            "limit": 5,
            "enabled_engines": "google__general",
        },
    )

    assert searxng_key != brave_key
    assert searxng_key != search_mode_key
