"""
Configuration Module - Loads configuration from environment variables

This module loads configuration from environment variables and provides
default values when environment variables are not set.
"""

import os
from typing import Any

from dotenv import load_dotenv
from loguru import logger

# Load environment variables from .env file
load_dotenv()
logger.info("Loading environment variable configuration")

# SearXNG Configuration
SEARXNG_HOST = os.getenv("SEARXNG_HOST", "localhost")
SEARXNG_PORT = int(os.getenv("SEARXNG_PORT", "8080"))
SEARXNG_BASE_PATH = os.getenv("SEARXNG_BASE_PATH", "/search")
SEARXNG_URL = os.getenv("SEARXNG_URL", "").strip().rstrip("/")
SEARXNG_API_BASE = (
    f"{SEARXNG_URL}{SEARXNG_BASE_PATH}"
    if SEARXNG_URL
    else f"http://{SEARXNG_HOST}:{SEARXNG_PORT}{SEARXNG_BASE_PATH}"
)

# API Service Configuration
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "3000"))

# Reader Service Configuration
READER_ENABLED = os.getenv("READER_ENABLED", "true").lower() == "true"
READER_URL = os.getenv("READER_URL", "http://reader:3000")
READER_URLS = os.getenv("READER_URLS", "").strip()
READER_API_KEY = os.getenv("READER_API_KEY", "")
READER_TIMEOUT_SECONDS = float(os.getenv("READER_TIMEOUT_SECONDS", "30"))
READER_MAX_CONCURRENCY = int(os.getenv("READER_MAX_CONCURRENCY", "20"))
READER_MIN_CONTENT_LENGTH = int(os.getenv("READER_MIN_CONTENT_LENGTH", "300"))

# Crawler Configuration
DEFAULT_SEARCH_LIMIT = int(os.getenv("DEFAULT_SEARCH_LIMIT", "10"))
CONTENT_FILTER_THRESHOLD = float(os.getenv("CONTENT_FILTER_THRESHOLD", "0.6"))
WORD_COUNT_THRESHOLD = int(os.getenv("WORD_COUNT_THRESHOLD", "10"))
CRAWLER_POOL_SIZE = int(os.getenv("CRAWLER_POOL_SIZE", "4"))
SEARXNG_TIMEOUT_SECONDS = float(os.getenv("SEARXNG_TIMEOUT_SECONDS", "15"))
HTTP_EXTRACTOR_ENABLED = os.getenv("HTTP_EXTRACTOR_ENABLED", "true").lower() == "true"
HTTP_EXTRACTOR_TIMEOUT_SECONDS = float(os.getenv("HTTP_EXTRACTOR_TIMEOUT_SECONDS", "10"))
HTTP_EXTRACTOR_MAX_CONCURRENCY = int(os.getenv("HTTP_EXTRACTOR_MAX_CONCURRENCY", "20"))
HTTP_EXTRACTOR_MIN_CONTENT_LENGTH = int(os.getenv("HTTP_EXTRACTOR_MIN_CONTENT_LENGTH", "300"))
CRAWL_EXTRACTION_STRATEGY = os.getenv("CRAWL_EXTRACTION_STRATEGY", "reader_first").lower().strip()
CRAWL_QUALITY_GATE_ENABLED = (
    os.getenv("CRAWL_QUALITY_GATE_ENABLED", "true").lower() == "true"
)
CRAWL_MIN_QUALITY_SCORE = float(os.getenv("CRAWL_MIN_QUALITY_SCORE", "0.35"))

# Browser Fallback Configuration
BROWSER_BACKEND = os.getenv("BROWSER_BACKEND", "local").lower()
BROWSERLESS_WS_URL = os.getenv("BROWSERLESS_WS_URL", "").strip()
BROWSER_REMOTE_TIMEOUT_SECONDS = float(os.getenv("BROWSER_REMOTE_TIMEOUT_SECONDS", "45"))
BROWSER_REMOTE_MAX_CONCURRENCY = int(os.getenv("BROWSER_REMOTE_MAX_CONCURRENCY", "2"))
BROWSER_TEXT_MODE = os.getenv("BROWSER_TEXT_MODE", "false").lower() == "true"
BROWSER_LIGHT_MODE = os.getenv("BROWSER_LIGHT_MODE", "false").lower() == "true"
BROWSER_LOCAL_FALLBACK_ENABLED = (
    os.getenv("BROWSER_LOCAL_FALLBACK_ENABLED", "true").lower() == "true"
)
BROWSER_LOCAL_MAX_CONCURRENCY = int(os.getenv("BROWSER_LOCAL_MAX_CONCURRENCY", "1"))
OBSCURA_BINARY = os.getenv("OBSCURA_BINARY", "obscura").strip()
OBSCURA_TIMEOUT_SECONDS = float(
    os.getenv("OBSCURA_TIMEOUT_SECONDS", str(BROWSER_REMOTE_TIMEOUT_SECONDS))
)
OBSCURA_MAX_CONCURRENCY = int(os.getenv("OBSCURA_MAX_CONCURRENCY", "4"))
OBSCURA_STEALTH_ENABLED = os.getenv("OBSCURA_STEALTH_ENABLED", "false").lower() == "true"
OBSCURA_WAIT_UNTIL = os.getenv("OBSCURA_WAIT_UNTIL", "networkidle0").strip()
OBSCURA_DUMP_FORMAT = os.getenv("OBSCURA_DUMP_FORMAT", "text").strip()
OBSCURA_ALLOW_PRIVATE_NETWORK = (
    os.getenv("OBSCURA_ALLOW_PRIVATE_NETWORK", "false").lower() == "true"
)

# Cache Configuration
CACHE_ENABLED = os.getenv("CACHE_ENABLED", "true").lower() == "true"
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CACHE_TTL_HOURS = int(os.getenv("CACHE_TTL_HOURS", "24"))
SEARCH_CACHE_TTL_SECONDS = int(os.getenv("SEARCH_CACHE_TTL_SECONDS", "300"))

# Search Engine Configuration
SEARCH_PROVIDER = os.getenv("SEARCH_PROVIDER", "router").strip().lower()
SEARCH_ROUTE_PROVIDERS = os.getenv("SEARCH_ROUTE_PROVIDERS", "local,searxng,brave").strip()
EXTERNAL_SEARCH_ENABLED = os.getenv("EXTERNAL_SEARCH_ENABLED", "false").lower() == "true"
EXTERNAL_SEARCH_FALLBACK_ONLY = (
    os.getenv("EXTERNAL_SEARCH_FALLBACK_ONLY", "true").lower() == "true"
)
SEARCH_ROUTER_MIN_RESULTS = int(os.getenv("SEARCH_ROUTER_MIN_RESULTS", "3"))
DISABLED_ENGINES = os.getenv(
    "DISABLED_ENGINES",
    "wikipedia__general,currency__general,wikidata__general,duckduckgo__general,"
    "google__general,lingva__general,qwant__general,startpage__general,"
    "dictzone__general,mymemory translated__general,brave__general",
)
ENABLED_ENGINES = os.getenv("ENABLED_ENGINES", "baidu__general")
SEARCH_LANGUAGE = os.getenv("SEARCH_LANGUAGE", "auto")

# Deduplication Configuration
DEDUP_ENABLED = os.getenv("DEDUP_ENABLED", "true").lower() == "true"
DEDUP_URL_ENABLED = os.getenv("DEDUP_URL_ENABLED", "true").lower() == "true"
DEDUP_CONTENT_ENABLED = os.getenv("DEDUP_CONTENT_ENABLED", "true").lower() == "true"
DEDUP_SIMILARITY_THRESHOLD = float(os.getenv("DEDUP_SIMILARITY_THRESHOLD", "0.85"))
BRAVE_SEARCH_API_BASE = os.getenv("BRAVE_SEARCH_API_BASE", "https://api.search.brave.com/res/v1")
BRAVE_SEARCH_API_KEY = os.getenv("BRAVE_SEARCH_API_KEY", "").strip()

# Local Index Configuration
LOCAL_INDEX_ENABLED = os.getenv("LOCAL_INDEX_ENABLED", "true").lower() == "true"
LOCAL_INDEX_PATH = os.getenv("LOCAL_INDEX_PATH", "").strip()
LOCAL_INDEX_MIN_RESULTS = int(os.getenv("LOCAL_INDEX_MIN_RESULTS", "3"))

# Background Backfill Configuration
BACKFILL_ENABLED = os.getenv("BACKFILL_ENABLED", "true").lower() == "true"
BACKFILL_QUEUE_BACKEND = os.getenv("BACKFILL_QUEUE_BACKEND", "redis").strip().lower()
BACKFILL_REDIS_KEY_PREFIX = os.getenv(
    "BACKFILL_REDIS_KEY_PREFIX",
    "trailsearch:backfill",
).strip()
BACKFILL_CLAIM_TTL_SECONDS = int(os.getenv("BACKFILL_CLAIM_TTL_SECONDS", "900"))
BACKFILL_BATCH_SIZE = int(os.getenv("BACKFILL_BATCH_SIZE", "3"))
BACKFILL_WORKER_INTERVAL_SECONDS = float(os.getenv("BACKFILL_WORKER_INTERVAL_SECONDS", "30"))
BACKFILL_MAX_ATTEMPTS = int(os.getenv("BACKFILL_MAX_ATTEMPTS", "5"))
BACKFILL_BASE_DELAY_SECONDS = float(os.getenv("BACKFILL_BASE_DELAY_SECONDS", "300"))
BACKFILL_MAX_DELAY_SECONDS = float(os.getenv("BACKFILL_MAX_DELAY_SECONDS", "86400"))

# Service Registry Configuration
ETCD_ENABLED = os.getenv("ETCD_ENABLED", "false").lower() == "true"
ETCD_ENDPOINTS = os.getenv("ETCD_ENDPOINTS", "http://etcd:2379").strip()
ETCD_NAMESPACE = os.getenv("ETCD_NAMESPACE", "trailsearch").strip()
ETCD_TTL_SECONDS = int(os.getenv("ETCD_TTL_SECONDS", "30"))
ETCD_REFRESH_SECONDS = float(os.getenv("ETCD_REFRESH_SECONDS", "10"))
ETCD_NODE_ID = os.getenv("ETCD_NODE_ID", "").strip()
ETCD_NODE_ENDPOINT = os.getenv("ETCD_NODE_ENDPOINT", "").strip()
ETCD_DISCOVER_READERS = os.getenv("ETCD_DISCOVER_READERS", "true").lower() == "true"
ETCD_REGISTER_SELF = os.getenv("ETCD_REGISTER_SELF", "true").lower() == "true"
ETCD_SELF_SERVICES = os.getenv("ETCD_SELF_SERVICES", "crawler").strip()
ETCD_REGISTER_READER_URLS = os.getenv("ETCD_REGISTER_READER_URLS", "false").lower() == "true"

# Anti-Crawl Configuration
ANTI_CRAWL_ENABLED = os.getenv("ANTI_CRAWL_ENABLED", "true").lower() == "true"
ENABLE_PROXY_ROTATION = os.getenv("ENABLE_PROXY_ROTATION", "false").lower() == "true"
ENABLE_USER_AGENT_ROTATION = os.getenv("ENABLE_USER_AGENT_ROTATION", "true").lower() == "true"
ENABLE_REQUEST_DELAY = os.getenv("ENABLE_REQUEST_DELAY", "true").lower() == "true"
ENABLE_RANDOM_HEADERS = os.getenv("ENABLE_RANDOM_HEADERS", "true").lower() == "true"
ENABLE_BROWSER_HEADERS = os.getenv("ENABLE_BROWSER_HEADERS", "true").lower() == "true"
MIN_REQUEST_DELAY = float(os.getenv("MIN_REQUEST_DELAY", "0.5"))
MAX_REQUEST_DELAY = float(os.getenv("MAX_REQUEST_DELAY", "3.0"))
PROXY_ROTATION_MODE = os.getenv("PROXY_ROTATION_MODE", "random")  # "random" or "sequential"
USE_MOBILE_AGENTS = os.getenv("USE_MOBILE_AGENTS", "false").lower() == "true"

# Proxy Configuration (comma-separated list of proxy URLs)
# Format: http://proxy1:port,http://proxy2:port or http://user:pass@proxy:port
PROXY_LIST = os.getenv("PROXY_LIST", "").strip()

# Custom User-Agents (comma-separated list)
CUSTOM_USER_AGENTS = os.getenv("CUSTOM_USER_AGENTS", "").strip()


def get_config_info() -> dict[str, Any]:
    """Returns a dictionary of current configuration information

    Returns:
        dict: Dictionary containing all configuration parameters
    """
    return {
        "searxng": {
            "host": SEARXNG_HOST,
            "port": SEARXNG_PORT,
            "base_path": SEARXNG_BASE_PATH,
            "url": SEARXNG_URL,
            "api_base": SEARXNG_API_BASE,
        },
        "api": {"host": API_HOST, "port": API_PORT},
        "reader": {
            "enabled": READER_ENABLED,
            "url": READER_URL,
            "urls": READER_URLS,
            "timeout_seconds": READER_TIMEOUT_SECONDS,
            "max_concurrency": READER_MAX_CONCURRENCY,
            "min_content_length": READER_MIN_CONTENT_LENGTH,
        },
        "crawler": {
            "default_search_limit": DEFAULT_SEARCH_LIMIT,
            "content_filter_threshold": CONTENT_FILTER_THRESHOLD,
            "word_count_threshold": WORD_COUNT_THRESHOLD,
            "pool_size": CRAWLER_POOL_SIZE,
            "searxng_timeout_seconds": SEARXNG_TIMEOUT_SECONDS,
            "http_extractor_enabled": HTTP_EXTRACTOR_ENABLED,
            "http_extractor_timeout_seconds": HTTP_EXTRACTOR_TIMEOUT_SECONDS,
            "http_extractor_max_concurrency": HTTP_EXTRACTOR_MAX_CONCURRENCY,
            "http_extractor_min_content_length": HTTP_EXTRACTOR_MIN_CONTENT_LENGTH,
            "extraction_strategy": CRAWL_EXTRACTION_STRATEGY,
            "quality_gate_enabled": CRAWL_QUALITY_GATE_ENABLED,
            "min_quality_score": CRAWL_MIN_QUALITY_SCORE,
        },
        "browser": {
            "backend": BROWSER_BACKEND,
            "browserless_ws_url": BROWSERLESS_WS_URL,
            "remote_timeout_seconds": BROWSER_REMOTE_TIMEOUT_SECONDS,
            "remote_max_concurrency": BROWSER_REMOTE_MAX_CONCURRENCY,
            "text_mode": BROWSER_TEXT_MODE,
            "light_mode": BROWSER_LIGHT_MODE,
            "local_fallback_enabled": BROWSER_LOCAL_FALLBACK_ENABLED,
            "local_max_concurrency": BROWSER_LOCAL_MAX_CONCURRENCY,
            "obscura_binary": OBSCURA_BINARY,
            "obscura_timeout_seconds": OBSCURA_TIMEOUT_SECONDS,
            "obscura_max_concurrency": OBSCURA_MAX_CONCURRENCY,
            "obscura_stealth_enabled": OBSCURA_STEALTH_ENABLED,
            "obscura_wait_until": OBSCURA_WAIT_UNTIL,
            "obscura_dump_format": OBSCURA_DUMP_FORMAT,
            "obscura_allow_private_network": OBSCURA_ALLOW_PRIVATE_NETWORK,
        },
        "cache": {
            "enabled": CACHE_ENABLED,
            "redis_url": REDIS_URL,
            "crawl_ttl_hours": CACHE_TTL_HOURS,
            "search_ttl_seconds": SEARCH_CACHE_TTL_SECONDS,
        },
        "search_engines": {"disabled": DISABLED_ENGINES, "enabled": ENABLED_ENGINES},
        "search_provider": {
            "default": SEARCH_PROVIDER,
            "route_providers": SEARCH_ROUTE_PROVIDERS,
            "external_search_enabled": EXTERNAL_SEARCH_ENABLED,
            "external_search_fallback_only": EXTERNAL_SEARCH_FALLBACK_ONLY,
            "router_min_results": SEARCH_ROUTER_MIN_RESULTS,
            "brave_api_base": BRAVE_SEARCH_API_BASE,
            "brave_api_key_configured": bool(BRAVE_SEARCH_API_KEY),
        },
        "local_index": {
            "enabled": LOCAL_INDEX_ENABLED,
            "path": LOCAL_INDEX_PATH,
            "min_results": LOCAL_INDEX_MIN_RESULTS,
        },
        "backfill": {
            "enabled": BACKFILL_ENABLED,
            "queue_backend": BACKFILL_QUEUE_BACKEND,
            "redis_key_prefix": BACKFILL_REDIS_KEY_PREFIX,
            "claim_ttl_seconds": BACKFILL_CLAIM_TTL_SECONDS,
            "batch_size": BACKFILL_BATCH_SIZE,
            "worker_interval_seconds": BACKFILL_WORKER_INTERVAL_SECONDS,
            "max_attempts": BACKFILL_MAX_ATTEMPTS,
            "base_delay_seconds": BACKFILL_BASE_DELAY_SECONDS,
            "max_delay_seconds": BACKFILL_MAX_DELAY_SECONDS,
        },
        "service_registry": {
            "etcd_enabled": ETCD_ENABLED,
            "etcd_endpoints": ETCD_ENDPOINTS,
            "etcd_namespace": ETCD_NAMESPACE,
            "etcd_ttl_seconds": ETCD_TTL_SECONDS,
            "etcd_refresh_seconds": ETCD_REFRESH_SECONDS,
            "etcd_node_id": ETCD_NODE_ID,
            "etcd_node_endpoint": ETCD_NODE_ENDPOINT,
            "etcd_discover_readers": ETCD_DISCOVER_READERS,
            "etcd_register_self": ETCD_REGISTER_SELF,
            "etcd_self_services": ETCD_SELF_SERVICES,
            "etcd_register_reader_urls": ETCD_REGISTER_READER_URLS,
        },
        "anti_crawl": {
            "enabled": ANTI_CRAWL_ENABLED,
            "enable_proxy_rotation": ENABLE_PROXY_ROTATION,
            "enable_user_agent_rotation": ENABLE_USER_AGENT_ROTATION,
            "enable_request_delay": ENABLE_REQUEST_DELAY,
            "enable_random_headers": ENABLE_RANDOM_HEADERS,
            "enable_browser_headers": ENABLE_BROWSER_HEADERS,
            "min_request_delay": MIN_REQUEST_DELAY,
            "max_request_delay": MAX_REQUEST_DELAY,
            "proxy_rotation_mode": PROXY_ROTATION_MODE,
            "use_mobile_agents": USE_MOBILE_AGENTS,
            "proxy_count": len(PROXY_LIST.split(",")) if PROXY_LIST else 0,
            "custom_user_agents_count": (
                len(CUSTOM_USER_AGENTS.split(",")) if CUSTOM_USER_AGENTS else 0
            ),
        },
    }
