"""TrailSearch - a self-hosted web search, crawl, and content extraction API."""

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any, Literal, Optional
from urllib.parse import urlparse

import aiohttp
import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

import trailsearch.logger as log_module
from trailsearch.backfill import BackfillWorker
from trailsearch.backfill_queue import RedisBackfillQueue
from trailsearch.cache import CacheManager
from trailsearch.config import (
    API_HOST,
    API_PORT,
    BACKFILL_BASE_DELAY_SECONDS,
    BACKFILL_BATCH_SIZE,
    BACKFILL_CLAIM_TTL_SECONDS,
    BACKFILL_ENABLED,
    BACKFILL_MAX_ATTEMPTS,
    BACKFILL_MAX_DELAY_SECONDS,
    BACKFILL_QUEUE_BACKEND,
    BACKFILL_REDIS_KEY_PREFIX,
    BACKFILL_WORKER_INTERVAL_SECONDS,
    CACHE_ENABLED,
    CACHE_TTL_HOURS,
    DEDUP_CONTENT_ENABLED,
    DEDUP_ENABLED,
    DEDUP_SIMILARITY_THRESHOLD,
    DEDUP_URL_ENABLED,
    DEFAULT_SEARCH_LIMIT,
    DISABLED_ENGINES,
    ENABLED_ENGINES,
    ETCD_DISCOVER_READERS,
    ETCD_ENABLED,
    ETCD_ENDPOINTS,
    ETCD_NAMESPACE,
    ETCD_NODE_ENDPOINT,
    ETCD_NODE_ID,
    ETCD_REFRESH_SECONDS,
    ETCD_REGISTER_READER_URLS,
    ETCD_REGISTER_SELF,
    ETCD_SELF_SERVICES,
    ETCD_TTL_SECONDS,
    HTTP_EXTRACTOR_ENABLED,
    HTTP_EXTRACTOR_MAX_CONCURRENCY,
    HTTP_EXTRACTOR_TIMEOUT_SECONDS,
    LOCAL_INDEX_ENABLED,
    LOCAL_INDEX_PATH,
    READER_ENABLED,
    READER_MAX_CONCURRENCY,
    READER_TIMEOUT_SECONDS,
    REDIS_URL,
    SEARCH_CACHE_TTL_SECONDS,
    SEARCH_PROVIDER,
    SEARXNG_TIMEOUT_SECONDS,
)
from trailsearch.crawler import WebCrawler
from trailsearch.deduplication import deduplicate_search_results
from trailsearch.local_index import LocalIndex, default_local_index_path
from trailsearch.quality import assess_content_quality, chunk_text, tokenize
from trailsearch.reader import parse_reader_urls
from trailsearch.search_providers import SearchProviderRequest, create_search_provider
from trailsearch.service_registry import (
    EtcdServiceRegistry,
    default_node_endpoint,
    default_node_id,
)

cache_manager: Optional[CacheManager] = None
local_index: Optional[LocalIndex] = None
backfill_queue = None
backfill_worker: Optional[BackfillWorker] = None
search_client: Optional[httpx.AsyncClient] = None
page_client: Optional[httpx.AsyncClient] = None
reader_session: Optional[aiohttp.ClientSession] = None
reader_semaphore: Optional[asyncio.Semaphore] = None
http_semaphore: Optional[asyncio.Semaphore] = None
crawler_service: Optional[WebCrawler] = None
service_registry: Optional[EtcdServiceRegistry] = None


def _configured_self_services() -> list[str]:
    """Parse self-registration service names."""
    services = [service.strip() for service in ETCD_SELF_SERVICES.split(",") if service.strip()]
    return list(dict.fromkeys(services)) or ["crawler"]


def _registry_node_endpoint() -> str:
    """Resolve the endpoint this node should register in etcd."""
    return ETCD_NODE_ENDPOINT or default_node_endpoint(API_PORT)


def _static_reader_urls() -> list[str]:
    """Return statically configured Reader endpoints."""
    return parse_reader_urls()


async def _discover_reader_urls() -> list[str]:
    """Discover Reader endpoints from etcd, falling back to static config."""
    static_urls = _static_reader_urls()
    if service_registry is None or not ETCD_DISCOVER_READERS:
        return static_urls

    instances = await service_registry.discover("reader")
    discovered_urls = [instance.endpoint.rstrip("/") for instance in instances if instance.endpoint]
    return list(dict.fromkeys(discovered_urls)) or static_urls


def _start_service_registration() -> None:
    """Register this API/crawler node in etcd."""
    if service_registry is None or not ETCD_REGISTER_SELF:
        return

    node_id = ETCD_NODE_ID or default_node_id("trailsearch")
    endpoint = _registry_node_endpoint()
    for service_name in _configured_self_services():
        service_registry.start_registration(
            service=service_name,
            node_id=node_id,
            endpoint=endpoint,
            metadata={
                "api_port": API_PORT,
                "backfill_enabled": BACKFILL_ENABLED,
                "backfill_queue_backend": BACKFILL_QUEUE_BACKEND,
            },
        )
        logger.info(f"Registering {service_name}/{node_id} in etcd at {endpoint}")


def _start_static_reader_registration() -> None:
    """Register statically configured Reader endpoints as service instances."""
    if service_registry is None or not ETCD_REGISTER_READER_URLS:
        return

    for index, endpoint in enumerate(_static_reader_urls()):
        service_registry.start_registration(
            service="reader",
            node_id=f"reader-{index}",
            endpoint=endpoint,
            metadata={"source": "static_config"},
        )
        logger.info(f"Registering reader/reader-{index} in etcd at {endpoint}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan context manager."""
    global backfill_queue, backfill_worker, cache_manager, crawler_service, http_semaphore, local_index, page_client, reader_semaphore, reader_session, search_client, service_registry

    log_module.setup_logger("INFO")
    logger.info("TrailSearch service starting...")

    if ETCD_ENABLED:
        service_registry = EtcdServiceRegistry(
            endpoints=ETCD_ENDPOINTS,
            namespace=ETCD_NAMESPACE,
            ttl_seconds=ETCD_TTL_SECONDS,
            refresh_seconds=ETCD_REFRESH_SECONDS,
        )
        _start_service_registration()
        _start_static_reader_registration()
        logger.info(f"etcd service registry enabled: {ETCD_ENDPOINTS}")

    if CACHE_ENABLED:
        try:
            logger.info(f"Initializing cache manager with Redis: {REDIS_URL}")
            cache_manager = CacheManager(REDIS_URL, CACHE_TTL_HOURS, SEARCH_CACHE_TTL_SECONDS)
            if await cache_manager.initialize():
                logger.info("Cache manager initialized successfully")
                logger.info(f"Cache stats: {await cache_manager.get_cache_stats()}")
            else:
                logger.warning("Cache manager initialized but Redis is not available")
                cache_manager = None
        except Exception as exc:
            logger.error(f"Failed to initialize cache manager: {str(exc)}")
            logger.warning("Continuing without cache")
            cache_manager = None

    if LOCAL_INDEX_ENABLED:
        try:
            index_path = LOCAL_INDEX_PATH or default_local_index_path()
            local_index = LocalIndex(index_path)
            await local_index.initialize()
            logger.info(f"Local search index initialized at: {index_path}")
        except Exception as exc:
            logger.warning(f"Failed to initialize local search index: {exc}")
            local_index = None

    search_client = httpx.AsyncClient(
        timeout=httpx.Timeout(SEARXNG_TIMEOUT_SECONDS),
        limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
    )
    page_client = httpx.AsyncClient(
        timeout=httpx.Timeout(HTTP_EXTRACTOR_TIMEOUT_SECONDS),
        limits=httpx.Limits(max_keepalive_connections=50, max_connections=200),
    )
    logger.info("Initialized shared HTTP clients for SearXNG and page extraction")

    if READER_ENABLED:
        reader_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=READER_TIMEOUT_SECONDS),
            connector=aiohttp.TCPConnector(
                limit=READER_MAX_CONCURRENCY,
                limit_per_host=READER_MAX_CONCURRENCY,
            ),
        )
        reader_semaphore = asyncio.Semaphore(READER_MAX_CONCURRENCY)
    else:
        reader_session = None
        reader_semaphore = None

    http_semaphore = (
        asyncio.Semaphore(HTTP_EXTRACTOR_MAX_CONCURRENCY) if HTTP_EXTRACTOR_ENABLED else None
    )
    crawler_service = WebCrawler(
        cache_manager=cache_manager,
        reader_session=reader_session,
        reader_semaphore=reader_semaphore,
        page_client=page_client,
        http_semaphore=http_semaphore,
        reader_url_provider=_discover_reader_urls if ETCD_ENABLED and ETCD_DISCOVER_READERS else None,
    )
    if BACKFILL_ENABLED and local_index is not None:
        if (
            BACKFILL_QUEUE_BACKEND == "redis"
            and cache_manager
            and cache_manager.is_available()
            and cache_manager.redis_client
        ):
            backfill_queue = RedisBackfillQueue(
                redis_client=cache_manager.redis_client,
                key_prefix=BACKFILL_REDIS_KEY_PREFIX,
                claim_ttl_seconds=BACKFILL_CLAIM_TTL_SECONDS,
            )
            logger.info("Using Redis-backed distributed backfill queue")
        else:
            backfill_queue = local_index
            logger.info("Using local SQLite backfill queue")

        backfill_worker = BackfillWorker(
            crawler=crawler_service,
            local_index=local_index,
            backfill_queue=backfill_queue,
            interval_seconds=BACKFILL_WORKER_INTERVAL_SECONDS,
            batch_size=BACKFILL_BATCH_SIZE,
            max_attempts=BACKFILL_MAX_ATTEMPTS,
            base_delay_seconds=BACKFILL_BASE_DELAY_SECONDS,
            max_delay_seconds=BACKFILL_MAX_DELAY_SECONDS,
        )
        backfill_worker.start()
    logger.info(f"API service running at: http://{API_HOST}:{API_PORT}")
    logger.info("TrailSearch service startup completed")

    yield

    logger.info("Starting graceful shutdown...")
    if backfill_worker:
        await backfill_worker.stop()
        backfill_worker = None
    backfill_queue = None
    if crawler_service:
        await crawler_service.close()
        crawler_service = None
    if reader_session:
        await reader_session.close()
        reader_session = None
    if page_client:
        await page_client.aclose()
        page_client = None
    if search_client:
        await search_client.aclose()
        search_client = None
    if local_index:
        await local_index.close()
        local_index = None
    if service_registry:
        await service_registry.close()
        service_registry = None
    if cache_manager:
        await cache_manager.close()
        cache_manager = None
    logger.info("TrailSearch service shut down")


# Initialize FastAPI application with lifespan
app = FastAPI(
    title="TrailSearch API",
    description=(
        "A self-hosted web search, crawl, and content extraction API with "
        "Tavily-compatible endpoints."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/healthz", include_in_schema=False)
async def healthcheck():
    """Return a dependency-free liveness signal for container platforms."""
    return {"status": "ok"}


# Request model definitions
class SearchRequest(BaseModel):
    """Search request model

    Attributes:
        query: Search query string
        limit: Limit on number of results to return, default is 10
        disabled_engines: List of disabled search engines, comma-separated
        enabled_engines: List of enabled search engines, comma-separated
        days: Time range filter in days (1=day, 7=week, 30=month, 365=year)
        topic: Topic for search classification (general, news, academic, code, etc.)
    """

    query: str
    limit: int = DEFAULT_SEARCH_LIMIT
    disabled_engines: str = DISABLED_ENGINES
    enabled_engines: str = ENABLED_ENGINES
    mode: Literal["crawl", "search"] = "crawl"
    provider: str = SEARCH_PROVIDER
    response_format: Literal["legacy", "tavily"] = "legacy"
    search_depth: Literal["basic", "advanced"] = "basic"
    include_answer: bool = False
    include_raw_content: bool = False
    chunks_per_source: int = Field(default=0, ge=0, le=8)
    include_domains: list[str] = Field(default_factory=list)
    exclude_domains: list[str] = Field(default_factory=list)
    days: int | None = None
    topic: str | None = None


class TavilySearchRequest(BaseModel):
    """Tavily-like search request model."""

    query: str
    max_results: int = Field(default=DEFAULT_SEARCH_LIMIT, ge=1, le=20)
    mode: Literal["crawl", "search"] = "crawl"
    search_depth: Literal["basic", "advanced"] = "basic"
    include_answer: bool = False
    include_raw_content: bool = False
    chunks_per_source: int = Field(default=0, ge=0, le=8)
    include_domains: list[str] = Field(default_factory=list)
    exclude_domains: list[str] = Field(default_factory=list)
    provider: str = SEARCH_PROVIDER
    days: int | None = None
    topic: str | None = None


class CrawlRequest(BaseModel):
    """
    Crawl request model

    Attributes:
        urls: List of URLs to crawl
        instruction: Crawling instruction, typically a search query
    """

    urls: list[str]
    instruction: str


class ExtractRequest(BaseModel):
    """Extract clean content from known URLs."""

    urls: list[str]
    query: str = ""
    include_raw_content: bool = True
    chunks_per_source: int = Field(default=0, ge=0, le=8)


class BackfillEnqueueRequest(BaseModel):
    """Manually enqueue URLs for background crawl backfill."""

    urls: list[str]
    query: str = ""
    reason: str = "manual"


async def crawl(request: CrawlRequest):
    """
    API endpoint function to crawl multiple URLs and process content

    Args:
        request: Crawl request containing URLs and instruction

    Returns:
        Dict: Dictionary containing processed content, success count, and failed URLs

    Raises:
        HTTPException: Raised when an error occurs during crawling
    """
    global crawler_service
    if crawler_service is None:
        raise HTTPException(status_code=503, detail="Crawler service not initialized")

    result = await crawler_service.crawl_urls(request.urls, request.instruction)
    result.setdefault("timings_ms", {})["pool_wait"] = 0.0
    return result


def build_search_cache_fingerprint(request: SearchRequest) -> dict[str, str | int]:
    """Build a stable fingerprint for search cache isolation."""
    return {
        "provider": request.provider,
        "mode": request.mode,
        "limit": request.limit,
        "response_format": request.response_format,
        "search_depth": request.search_depth,
        "include_answer": int(request.include_answer),
        "include_raw_content": int(request.include_raw_content),
        "chunks_per_source": request.chunks_per_source,
        "include_domains": ",".join(sorted(request.include_domains)),
        "exclude_domains": ",".join(sorted(request.exclude_domains)),
        "disabled_engines": request.disabled_engines,
        "enabled_engines": request.enabled_engines,
    }


def build_search_only_response(
    provider_response,
    search_total_ms: float,
) -> dict:
    """Build the response for search-only mode."""
    effective_total_ms = max(search_total_ms, provider_response.request_ms)
    timings_ms = {
        "search_provider_request": provider_response.request_ms,
        "search_total": round(effective_total_ms, 2),
    }
    if provider_response.provider == "searxng":
        timings_ms["searxng_request"] = provider_response.request_ms

    return {
        "mode": "search",
        "provider": provider_response.provider,
        "results": [serialize_search_hit(hit) for hit in provider_response.hits],
        "success_count": len(provider_response.hits),
        "failed_urls": [],
        "cache_hits": 0,
        "newly_crawled": 0,
        "timings_ms": timings_ms,
    }


def get_search_provider(provider_name: str, client: Optional[httpx.AsyncClient]):
    """Resolve a search provider by name."""
    return create_search_provider(provider_name, client=client, local_index=local_index)


def serialize_search_hit(hit) -> dict[str, str]:
    """Serialize a normalized hit from either a dataclass or a test double."""
    if hasattr(hit, "to_dict"):
        return hit.to_dict()
    return {
        "url": hit.url,
        "title": hit.title,
        "snippet": hit.snippet,
        "provider": hit.provider,
    }


def _normalize_domain(domain: str) -> str:
    return domain.lower().removeprefix("www.").strip()


def _domain_for_url(url: str) -> str:
    return _normalize_domain(urlparse(url).netloc)


def _filter_hits_by_domain(hits, include_domains: list[str], exclude_domains: list[str]):
    include_set = {_normalize_domain(domain) for domain in include_domains if domain.strip()}
    exclude_set = {_normalize_domain(domain) for domain in exclude_domains if domain.strip()}
    if not include_set and not exclude_set:
        return hits

    filtered_hits = []
    for hit in hits:
        domain = _domain_for_url(hit.url)
        if include_set and not any(domain == item or domain.endswith(f".{item}") for item in include_set):
            continue
        if exclude_set and any(domain == item or domain.endswith(f".{item}") for item in exclude_set):
            continue
        filtered_hits.append(hit)
    return filtered_hits


def _empty_crawl_result() -> dict[str, Any]:
    return {
        "results": [],
        "success_count": 0,
        "failed_urls": [],
        "pending_backfill_urls": [],
        "cache_hits": 0,
        "newly_crawled": 0,
        "fast_path_hits": 0,
        "reader_hits": 0,
        "obscura_browser_hits": 0,
        "remote_browser_hits": 0,
        "local_browser_fallback_hits": 0,
        "local_index_hits": 0,
        "timings_ms": {"total": 0.0},
    }


def _failed_crawl_result(urls: list[str]) -> dict[str, Any]:
    result = _empty_crawl_result()
    result["failed_urls"] = urls
    return result


ACTIVE_BACKFILL_STATUSES = {"queued", "running", "failed"}


async def _materialize_local_hits(
    hits,
    query: str,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Reuse indexed content and skip URLs currently owned by the backfill queue."""
    if local_index is None:
        return [], [hit.url for hit in hits], []

    hit_urls = [hit.url for hit in hits]
    local_docs = await local_index.get_many(hit_urls)
    crawl_jobs = await backfill_queue.get_crawl_jobs_many(hit_urls) if backfill_queue else {}
    prefetched_results: list[dict[str, Any]] = []
    urls_to_crawl: list[str] = []
    pending_backfill_urls: list[str] = []

    for hit in hits:
        document = local_docs.get(hit.url)
        if document:
            quality = assess_content_quality(document.content, query=query)
            prefetched_results.append(
                {
                    "content": document.content,
                    "reference": document.url,
                    "source_stage": "local_index",
                    "quality_score": max(document.score, quality.score),
                }
            )
            continue

        crawl_job = crawl_jobs.get(hit.url)
        if crawl_job and crawl_job.status in ACTIVE_BACKFILL_STATUSES:
            pending_backfill_urls.append(hit.url)
            continue

        if crawl_job and crawl_job.status == "abandoned":
            pending_backfill_urls.append(hit.url)
            continue

        urls_to_crawl.append(hit.url)

    return prefetched_results, urls_to_crawl, pending_backfill_urls


def _merge_prefetched_results(
    crawl_result: dict[str, Any],
    prefetched_results: list[dict[str, Any]],
) -> dict[str, Any]:
    if not prefetched_results:
        return crawl_result

    merged_result = {**crawl_result}
    merged_result["results"] = prefetched_results + list(crawl_result.get("results", []))
    merged_result["success_count"] = len(merged_result["results"])
    merged_result["local_index_hits"] = len(prefetched_results)
    return merged_result


async def _index_crawl_results(query: str, hits, crawl_result: dict[str, Any]) -> None:
    """Write successful crawl results back into the local index."""
    if local_index is None:
        return

    hits_by_url = {hit.url: hit for hit in hits}
    items = []
    for result in crawl_result.get("results", []):
        url = result.get("reference", "")
        content = result.get("content", "")
        if not url or not content or result.get("source_stage") == "local_index":
            continue

        hit = hits_by_url.get(url)
        items.append(
            {
                "url": url,
                "title": getattr(hit, "title", "") if hit else "",
                "snippet": getattr(hit, "snippet", "") if hit else "",
                "content": content,
                "provider": getattr(hit, "provider", "") if hit else "",
                "source_query": query,
            }
        )

    if items:
        await local_index.upsert_many(items)


async def _enqueue_backfill_urls(
    urls: list[str],
    query: str,
    reason: str = "crawl_failed",
) -> int:
    """Queue failed URLs for async backfill without blocking the foreground response."""
    if not BACKFILL_ENABLED or backfill_queue is None:
        return 0
    return await backfill_queue.enqueue_crawl_jobs(urls, query=query, reason=reason)


def _result_score(result: dict[str, Any], query: str) -> float:
    explicit_score = result.get("quality_score")
    try:
        if explicit_score is not None and explicit_score != "":
            return round(float(explicit_score), 4)
    except (TypeError, ValueError):
        pass
    return assess_content_quality(result.get("content", ""), query=query).score


def _build_tavily_results(
    query: str,
    hits,
    crawl_result: dict[str, Any],
    include_raw_content: bool,
    chunks_per_source: int,
) -> list[dict[str, Any]]:
    hits_by_url = {hit.url: hit for hit in hits}
    tavily_results: list[dict[str, Any]] = []

    for result in crawl_result.get("results", []):
        url = result.get("reference", "")
        content = result.get("content", "")
        if not url or not content:
            continue

        hit = hits_by_url.get(url)
        chunks = chunk_text(content) if chunks_per_source else []
        display_content = chunks[0] if chunks else content[:2000]
        item: dict[str, Any] = {
            "title": getattr(hit, "title", "") if hit else _domain_for_url(url),
            "url": url,
            "content": display_content,
            "score": _result_score(result, query),
            "source_stage": result.get("source_stage", ""),
        }
        if chunks_per_source:
            item["chunks"] = chunks[:chunks_per_source]
        if include_raw_content:
            item["raw_content"] = content
        tavily_results.append(item)

    return sorted(tavily_results, key=lambda item: item.get("score", 0), reverse=True)


def _generate_extractive_answer(query: str, results: list[dict[str, Any]]) -> str:
    """Generate a small non-LLM answer from the highest scoring source sentences."""
    query_terms = set(tokenize(query))
    selected_sentences: list[str] = []
    for result in results:
        text = result.get("raw_content") or result.get("content", "")
        sentences = [
            sentence.strip()
            for sentence in text.replace("\n", " ").split(".")
            if len(sentence.strip()) >= 40
        ]
        for sentence in sentences:
            sentence_terms = set(tokenize(sentence))
            if query_terms and not (query_terms & sentence_terms):
                continue
            selected_sentences.append(sentence)
            break
        if len(selected_sentences) >= 3:
            break

    if not selected_sentences:
        return ""
    answer = ". ".join(selected_sentences)
    return answer[:1200].strip()


def _build_tavily_response(
    query: str,
    hits,
    crawl_result: dict[str, Any],
    include_answer: bool,
    include_raw_content: bool,
    chunks_per_source: int,
    total_ms: float,
) -> dict[str, Any]:
    results = _build_tavily_results(
        query=query,
        hits=hits,
        crawl_result=crawl_result,
        include_raw_content=include_raw_content,
        chunks_per_source=chunks_per_source,
    )
    response = {
        "query": query,
        "answer": _generate_extractive_answer(query, results) if include_answer else None,
        "results": results,
        "failed_results": [
            {"url": url, "error": "crawl_failed"} for url in crawl_result.get("failed_urls", [])
        ],
        "response_time": round(total_ms / 1000, 3),
        "timings_ms": crawl_result.get("timings_ms", {}),
    }
    if "backfill_queued" in crawl_result:
        response["backfill"] = {
            "queued": crawl_result.get("backfill_queued", 0),
            "pending": len(crawl_result.get("pending_backfill_urls", [])),
            "pending_urls": crawl_result.get("pending_backfill_urls", []),
            "enabled": BACKFILL_ENABLED and backfill_queue is not None,
        }
    return response


@app.get("/cache/stats")
async def get_cache_stats():
    """
    Get cache statistics endpoint

    Returns cache statistics including total entries and memory usage

    Returns:
        Dict: Cache statistics
    """
    global cache_manager
    try:
        if not cache_manager:
            logger.warning("Cache manager not available")
            return {"status": "unavailable", "message": "Cache is not enabled or not available"}

        stats = await cache_manager.get_cache_stats()
        logger.info(f"Cache stats retrieved: {stats}")
        return stats
    except Exception as e:
        logger.error(f"Error getting cache stats: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/cache/clear")
async def clear_cache():
    """
    Clear all cache entries endpoint

    Clears all cached crawl results

    Returns:
        Dict: Operation result
    """
    global cache_manager
    try:
        if not cache_manager:
            logger.warning("Cache manager not available")
            return {"status": "unavailable", "message": "Cache is not enabled or not available"}

        success = await cache_manager.clear_all()
        if success:
            logger.info("Cache cleared successfully")
            return {"status": "success", "message": "Cache cleared successfully"}
        else:
            logger.warning("Failed to clear cache")
            return {"status": "error", "message": "Failed to clear cache"}
    except Exception as e:
        logger.error(f"Error clearing cache: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/backfill/stats")
async def get_backfill_stats():
    """Return background crawl queue status."""
    if backfill_queue is None:
        return {"enabled": False, "status": "unavailable", "stats": {}}
    return {
        "enabled": BACKFILL_ENABLED,
        "worker_running": bool(backfill_worker and backfill_worker.is_running()),
        "queue_backend": "redis" if isinstance(backfill_queue, RedisBackfillQueue) else "local",
        "stats": await backfill_queue.get_crawl_job_stats(),
    }


@app.get("/backfill/jobs")
async def list_backfill_jobs(limit: int = 50):
    """List recent background crawl jobs."""
    if backfill_queue is None:
        return {"enabled": False, "jobs": []}
    jobs = await backfill_queue.list_crawl_jobs(limit=max(1, min(limit, 200)))
    return {
        "enabled": BACKFILL_ENABLED,
        "jobs": [job.__dict__ for job in jobs],
    }


@app.post("/backfill/enqueue")
async def enqueue_backfill(request: BackfillEnqueueRequest):
    """Manually enqueue URLs for asynchronous background crawl."""
    queued = await _enqueue_backfill_urls(request.urls, request.query, reason=request.reason)
    return {
        "enabled": BACKFILL_ENABLED and backfill_queue is not None,
        "queued": queued,
    }


@app.post("/backfill/run-once")
async def run_backfill_once():
    """Run one due batch immediately."""
    if backfill_worker is None:
        return {"enabled": False, "processed": 0}
    return {"enabled": True, "processed": await backfill_worker.run_once()}


@app.get("/registry/services/{service_name}")
async def discover_registry_service(service_name: str):
    """Return service instances discovered from etcd."""
    if service_registry is None:
        return {"enabled": False, "service": service_name, "instances": []}

    instances = await service_registry.discover(service_name)
    return {
        "enabled": ETCD_ENABLED,
        "service": service_name,
        "instances": [instance.__dict__ for instance in instances],
    }


@app.post("/search")
async def search(request: SearchRequest):
    """
    Search API endpoint

    Receives search request, calls SearXNG search engine to get results,
    then crawls the search result pages

    Args:
        request: Search request object containing query string and configuration parameters

    Returns:
        Dict: Dictionary containing processed content, success count, and failed URLs

    Raises:
        HTTPException: Raised when an error occurs during search or crawling
    """
    global cache_manager
    global search_client
    search_started = time.perf_counter()
    try:
        # Add status feedback
        logger.info(f"Starting search: {request.query}")
        cache_fingerprint = build_search_cache_fingerprint(request)

        # Check cache for search results
        if cache_manager:
            cached_result = await cache_manager.get_search_cache(request.query, cache_fingerprint)
            if cached_result:
                logger.info(f"Search cache hit for query: {request.query}")
                cached_result.setdefault("timings_ms", {})["search_total"] = round(
                    (time.perf_counter() - search_started) * 1000,
                    2,
                )
                return cached_result

        provider = get_search_provider(request.provider, search_client)
        provider_response = await provider.search(
            SearchProviderRequest(
                query=request.query,
                limit=max(request.limit * 2, request.limit) if request.search_depth == "advanced" else request.limit,
                provider=request.provider,
                disabled_engines=request.disabled_engines,
                enabled_engines=request.enabled_engines,
                days=request.days,
                topic=request.topic,
            )
        )

        # Apply deduplication if enabled
        if DEDUP_ENABLED and provider_response.hits:
            results_to_dedup = [
                {"url": hit.url, "content": hit.snippet or hit.title}
                for hit in provider_response.hits
            ]
            deduped_results = deduplicate_search_results(
                results_to_dedup,
                url_dedup=DEDUP_URL_ENABLED,
                content_dedup=DEDUP_CONTENT_ENABLED,
                similarity_threshold=DEDUP_SIMILARITY_THRESHOLD,
            )
            # Filter hits to keep only deduplicated ones
            deduped_urls = {r["url"] for r in deduped_results}
            provider_response.hits = [hit for hit in provider_response.hits if hit.url in deduped_urls]
            logger.info(
                f"Deduplication: {len(results_to_dedup)} -> {len(deduped_results)} results "
                f"(removed {len(results_to_dedup) - len(deduped_results)} duplicates)"
            )

        provider_response.hits = _filter_hits_by_domain(
            provider_response.hits,
            include_domains=request.include_domains,
            exclude_domains=request.exclude_domains,
        )[: request.limit]

        if not provider_response.hits:
            logger.warning("No search results found")
            raise HTTPException(status_code=404, detail="No search results found")

        if request.mode == "search":
            search_total_ms = (time.perf_counter() - search_started) * 1000
            if request.response_format == "tavily":
                search_result = {
                    "query": request.query,
                    "answer": None,
                    "results": [
                        {
                            "title": hit.title,
                            "url": hit.url,
                            "content": hit.snippet,
                            "score": 0.0,
                            "source_stage": f"{hit.provider}_search",
                        }
                        for hit in provider_response.hits
                    ],
                    "failed_results": [],
                    "response_time": round(search_total_ms / 1000, 3),
                    "timings_ms": {
                        "search_provider_request": provider_response.request_ms,
                        "search_total": round(search_total_ms, 2),
                    },
                }
            else:
                search_result = build_search_only_response(
                    provider_response=provider_response,
                    search_total_ms=search_total_ms,
                )
            if cache_manager:
                await cache_manager.set_search_cache(request.query, search_result, cache_fingerprint)
            return search_result

        # Limit result count and extract URLs
        prefetched_results, urls, pending_backfill_urls = await _materialize_local_hits(
            provider_response.hits,
            query=request.query,
        )
        if not urls:
            crawl_result = _empty_crawl_result()
        else:
            logger.info(f"Found {len(urls)} URLs, starting to crawl")

            # Call crawl function to process URLs
            try:
                crawl_result = await crawl(CrawlRequest(urls=urls, instruction=request.query))
            except HTTPException as exc:
                if exc.status_code != 500:
                    raise
                logger.warning(f"Foreground crawl failed; queuing URLs for backfill: {exc.detail}")
                crawl_result = _failed_crawl_result(urls)

        crawl_result = _merge_prefetched_results(crawl_result, prefetched_results)
        crawl_result["pending_backfill_urls"] = pending_backfill_urls
        backfill_queued = await _enqueue_backfill_urls(
            crawl_result.get("failed_urls", []),
            query=request.query,
            reason="search_crawl_failed",
        )
        crawl_result["backfill_queued"] = backfill_queued
        crawl_result["search_provider"] = provider_response.provider
        crawl_result.setdefault("timings_ms", {})["search_provider_request"] = (
            provider_response.request_ms
        )
        if provider_response.provider == "searxng":
            crawl_result["timings_ms"]["searxng_request"] = provider_response.request_ms
        crawl_result.setdefault("timings_ms", {})["search_total"] = round(
            max((time.perf_counter() - search_started) * 1000, provider_response.request_ms),
            2,
        )
        await _index_crawl_results(request.query, provider_response.hits, crawl_result)
        should_cache_search_result = not crawl_result.get("failed_urls") and not crawl_result.get(
            "pending_backfill_urls"
        )

        if request.response_format == "tavily":
            crawl_result = _build_tavily_response(
                query=request.query,
                hits=provider_response.hits,
                crawl_result=crawl_result,
                include_answer=request.include_answer,
                include_raw_content=request.include_raw_content,
                chunks_per_source=request.chunks_per_source,
                total_ms=(time.perf_counter() - search_started) * 1000,
            )

        # Cache the search result
        if cache_manager and should_cache_search_result:
            await cache_manager.set_search_cache(request.query, crawl_result, cache_fingerprint)

        return crawl_result
    except HTTPException:
        # Directly re-raise HTTP exceptions
        raise
    except Exception as e:
        # Log other exceptions and convert to HTTP exception
        logger.error(f"Exception occurred during search: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/tavily/search")
async def tavily_search(request: TavilySearchRequest):
    """Tavily-like search endpoint with low-cost routing and extracted content."""
    return await search(
        SearchRequest(
            query=request.query,
            limit=request.max_results,
            mode=request.mode,
            provider=request.provider,
            response_format="tavily",
            search_depth=request.search_depth,
            include_answer=request.include_answer,
            include_raw_content=request.include_raw_content,
            chunks_per_source=request.chunks_per_source,
            include_domains=request.include_domains,
            exclude_domains=request.exclude_domains,
            days=request.days,
            topic=request.topic,
        )
    )


@app.post("/extract")
async def extract(request: ExtractRequest):
    """Extract clean content from known URLs and index successful pages."""
    extract_started = time.perf_counter()
    try:
        crawl_result = await crawl(CrawlRequest(urls=request.urls, instruction=request.query))
    except HTTPException as exc:
        if exc.status_code != 500:
            raise
        logger.warning(f"Foreground extract failed; queuing URLs for backfill: {exc.detail}")
        crawl_result = _failed_crawl_result(request.urls)
    crawl_result["backfill_queued"] = await _enqueue_backfill_urls(
        crawl_result.get("failed_urls", []),
        query=request.query,
        reason="extract_failed",
    )
    await _index_crawl_results(request.query, [], crawl_result)
    return _build_tavily_response(
        query=request.query,
        hits=[],
        crawl_result=crawl_result,
        include_answer=False,
        include_raw_content=request.include_raw_content,
        chunks_per_source=request.chunks_per_source,
        total_ms=(time.perf_counter() - extract_started) * 1000,
    )


@app.post("/tavily/extract")
async def tavily_extract(request: ExtractRequest):
    """Alias for clients that prefer Tavily-like route names."""
    return await extract(request)


def main():
    """Main entry point for the application"""
    logger.info("Starting TrailSearch service via command line")
    uvicorn.run(app, host=API_HOST, port=API_PORT)


if __name__ == "__main__":
    main()
