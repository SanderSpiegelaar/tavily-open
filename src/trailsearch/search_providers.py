"""
Search provider abstractions and concrete implementations.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Protocol

import httpx
from loguru import logger

from trailsearch.config import (
    BRAVE_SEARCH_API_BASE,
    BRAVE_SEARCH_API_KEY,
    EXTERNAL_SEARCH_ENABLED,
    EXTERNAL_SEARCH_FALLBACK_ONLY,
    LOCAL_INDEX_MIN_RESULTS,
    SEARCH_LANGUAGE,
    SEARCH_ROUTE_PROVIDERS,
    SEARCH_ROUTER_MIN_RESULTS,
    SEARXNG_API_BASE,
    SEARXNG_HOST,
    SEARXNG_PORT,
    SEARXNG_TIMEOUT_SECONDS,
    SEARXNG_URL,
)
from trailsearch.local_index import LocalIndex

# Topic to SearXNG engine mapping configuration
TOPIC_ENGINE_CONFIG = {
    "general": ["google", "duckduckgo", "bing"],
    "news": ["google news", "bing news", "reddit"],
    "academic": ["google scholar", "arxiv", "pubmed"],
    "code": ["github", "stackoverflow", "gitlab"],
    "images": ["google images", "bing images", "unsplash"],
    "videos": ["youtube", "vimeo", "dailymotion"],
    "social": ["reddit", "twitter", "mastodon"],
    "shopping": ["amazon", "ebay", "walmart"],
}


@dataclass
class SearchHit:
    """Normalized search result hit."""

    url: str
    title: str
    snippet: str
    provider: str

    def to_dict(self) -> dict[str, str]:
        """Serialize the hit for API responses."""
        return asdict(self)


@dataclass
class SearchProviderRequest:
    """Normalized request passed into search providers."""

    query: str
    limit: int
    provider: str
    disabled_engines: str = ""
    enabled_engines: str = ""
    days: int | None = None  # Time range filter in days
    topic: str | None = None  # Topic for search classification


@dataclass
class SearchProviderResponse:
    """Normalized provider response."""

    provider: str
    hits: list[SearchHit]
    request_ms: float


class SearchProvider(Protocol):
    """Shared protocol for search providers."""

    async def search(self, request: SearchProviderRequest) -> SearchProviderResponse:
        """Run a search and return normalized hits."""


class SearXNGSearchProvider:
    """Provider backed by a SearXNG instance."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client

    def _map_days_to_time_range(self, days: int | None) -> str:
        """Map days parameter to SearXNG time_range."""
        if days is None:
            return "week"
        if days <= 1:
            return "day"
        if days <= 7:
            return "week"
        if days <= 30:
            return "month"
        return "year"

    def _get_engines_for_topic(self, topic: str | None) -> str:
        """Get recommended engines for a given topic as comma-separated string."""
        if topic is None:
            return ""
        engines = TOPIC_ENGINE_CONFIG.get(topic.lower(), [])
        return ",".join(engines) if engines else ""

    async def search(self, request: SearchProviderRequest) -> SearchProviderResponse:
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=httpx.Timeout(SEARXNG_TIMEOUT_SECONDS))
        started = time.perf_counter()
        try:
            # Build form data with dynamic time range
            time_range = self._map_days_to_time_range(request.days)

            form_data = {
                "q": request.query,
                "format": "json",
                "language": SEARCH_LANGUAGE,
                "time_range": time_range,
                "safesearch": "2",
                "pageno": "1",
                "category_general": "1",
            }

            # Build enabled_engines based on priority:
            # 1. Explicit enabled_engines overrides everything
            # 2. Topic-based engine selection
            enabled_engines = request.enabled_engines
            if not enabled_engines and request.topic:
                topic_engines = self._get_engines_for_topic(request.topic)
                if topic_engines:
                    enabled_engines = topic_engines

            headers = {
                "Cookie": (
                    "disabled_engines="
                    f"{request.disabled_engines};enabled_engines={enabled_engines};method=POST"
                ),
                "User-Agent": "TrailSearch/1.0.0",
                "Accept": "*/*",
                "Connection": "keep-alive",
            }
            if not SEARXNG_URL:
                headers["Host"] = f"{SEARXNG_HOST}:{SEARXNG_PORT}"
            response = await client.post(SEARXNG_API_BASE, data=form_data, headers=headers)
            response.raise_for_status()
            payload = response.json()
            hits = [
                SearchHit(
                    url=result["url"],
                    title=result.get("title", ""),
                    snippet=result.get("content", ""),
                    provider="searxng",
                )
                for result in payload.get("results", [])[: request.limit]
                if "url" in result
            ]
            request_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.info(f"SearXNG provider request completed in {request_ms:.2f}ms")
            return SearchProviderResponse(provider="searxng", hits=hits, request_ms=request_ms)
        except httpx.HTTPStatusError as exc:
            logger.error(
                f"SearXNG request failed with status {exc.response.status_code}: {exc.response.text}"
            )
            raise Exception(f"Search request failed: {exc.response.text}") from exc
        except Exception as exc:
            logger.error(f"SearXNG request failed: {str(exc)}")
            raise Exception(f"Search request failed: {str(exc)}") from exc
        finally:
            if owns_client:
                await client.aclose()


class BraveSearchProvider:
    """Provider backed by Brave Search API."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        api_key: str = BRAVE_SEARCH_API_KEY,
        api_base: str = BRAVE_SEARCH_API_BASE,
    ) -> None:
        self.client = client
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")

    async def search(self, request: SearchProviderRequest) -> SearchProviderResponse:
        if not self.api_key:
            raise ValueError("BRAVE_SEARCH_API_KEY is required for the brave provider")

        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=httpx.Timeout(SEARXNG_TIMEOUT_SECONDS))
        started = time.perf_counter()
        try:
            response = await client.get(
                f"{self.api_base}/web/search",
                params={"q": request.query, "count": request.limit},
                headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
            hits = [
                SearchHit(
                    url=result["url"],
                    title=result.get("title", ""),
                    snippet=result.get("description", ""),
                    provider="brave",
                )
                for result in payload.get("web", {}).get("results", [])[: request.limit]
                if "url" in result
            ]
            request_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.info(f"Brave provider request completed in {request_ms:.2f}ms")
            return SearchProviderResponse(provider="brave", hits=hits, request_ms=request_ms)
        except httpx.HTTPStatusError as exc:
            logger.error(
                f"Brave request failed with status {exc.response.status_code}: {exc.response.text}"
            )
            raise Exception(f"Search request failed: {exc.response.text}") from exc
        except Exception as exc:
            logger.error(f"Brave request failed: {str(exc)}")
            raise Exception(f"Search request failed: {str(exc)}") from exc
        finally:
            if owns_client:
                await client.aclose()


class LocalIndexSearchProvider:
    """Provider backed by the accumulated local SQLite index."""

    def __init__(self, local_index: LocalIndex | None = None) -> None:
        self.local_index = local_index

    async def search(self, request: SearchProviderRequest) -> SearchProviderResponse:
        started = time.perf_counter()
        if self.local_index is None:
            return SearchProviderResponse(provider="local", hits=[], request_ms=0.0)

        documents = await self.local_index.search(request.query, request.limit)
        hits = [
            SearchHit(
                url=document.url,
                title=document.title,
                snippet=document.snippet,
                provider="local",
            )
            for document in documents
        ]
        request_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info(f"Local index provider returned {len(hits)} hits in {request_ms:.2f}ms")
        return SearchProviderResponse(provider="local", hits=hits, request_ms=request_ms)


class SearchRouterProvider:
    """
    Low-cost routing provider.

    It prefers local results, then SearXNG, and only calls external paid APIs when enabled.
    """

    EXTERNAL_PROVIDERS = {"brave"}

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        local_index: LocalIndex | None = None,
        route_providers: str = SEARCH_ROUTE_PROVIDERS,
        external_search_enabled: bool = EXTERNAL_SEARCH_ENABLED,
        external_search_fallback_only: bool = EXTERNAL_SEARCH_FALLBACK_ONLY,
        min_results: int = SEARCH_ROUTER_MIN_RESULTS,
    ) -> None:
        self.client = client
        self.local_index = local_index
        self.route_provider_names = [
            provider.strip().lower()
            for provider in route_providers.split(",")
            if provider.strip()
        ]
        self.external_search_enabled = external_search_enabled
        self.external_search_fallback_only = external_search_fallback_only
        self.min_results = min_results

    async def search(self, request: SearchProviderRequest) -> SearchProviderResponse:
        started = time.perf_counter()
        merged_hits: list[SearchHit] = []
        seen_urls: set[str] = set()
        used_providers: list[str] = []

        for provider_name in self.route_provider_names:
            normalized_name = "local" if provider_name == "local_index" else provider_name
            if normalized_name in self.EXTERNAL_PROVIDERS and not self.external_search_enabled:
                logger.debug(f"Skipping external search provider '{normalized_name}'")
                continue
            if (
                normalized_name in self.EXTERNAL_PROVIDERS
                and self.external_search_fallback_only
                and len(merged_hits) >= self.min_results
            ):
                logger.debug(
                    f"Skipping external search provider '{normalized_name}' because fallback "
                    "threshold is already satisfied"
                )
                continue
            if normalized_name == "local" and len(merged_hits) >= LOCAL_INDEX_MIN_RESULTS:
                continue

            try:
                provider = create_search_provider(
                    normalized_name,
                    client=self.client,
                    local_index=self.local_index,
                )
                provider_response = await provider.search(request)
            except Exception as exc:
                logger.warning(f"Search provider '{normalized_name}' failed: {exc}")
                continue

            used_providers.append(provider_response.provider)
            for hit in provider_response.hits:
                if hit.url in seen_urls:
                    continue
                seen_urls.add(hit.url)
                merged_hits.append(hit)
                if len(merged_hits) >= request.limit:
                    break

            if len(merged_hits) >= request.limit:
                break
            if len(merged_hits) >= self.min_results and normalized_name != "local":
                break

        request_ms = round((time.perf_counter() - started) * 1000, 2)
        provider_label = "router" if not used_providers else f"router:{'+'.join(used_providers)}"
        return SearchProviderResponse(
            provider=provider_label,
            hits=merged_hits[: request.limit],
            request_ms=request_ms,
        )


def create_search_provider(
    provider_name: str,
    client: httpx.AsyncClient | None = None,
    local_index: LocalIndex | None = None,
) -> SearchProvider:
    """Create a search provider by name."""
    normalized_name = provider_name.strip().lower()
    if normalized_name in {"router", "auto"}:
        return SearchRouterProvider(client=client, local_index=local_index)
    if normalized_name in {"local", "local_index"}:
        return LocalIndexSearchProvider(local_index=local_index)
    if normalized_name == "searxng":
        return SearXNGSearchProvider(client=client)
    if normalized_name == "brave":
        return BraveSearchProvider(client=client)
    raise ValueError(f"Unsupported search provider: {provider_name}")
