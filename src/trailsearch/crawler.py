"""
Crawler Module - Provides web crawling and content processing functionality.
"""

import asyncio
import inspect
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Optional

import aiohttp
import httpx
import markdown
from bs4 import BeautifulSoup
from crawl4ai import CacheMode, CrawlerRunConfig
from crawl4ai.content_filter_strategy import PruningContentFilter
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
from fastapi import HTTPException
from loguru import logger

from .anti_crawl import AntiCrawlConfig, ProxyConfig, ProxyType
from .browser import BrowserBackend, ObscuraBrowserBackend
from .cache import CacheManager
from .config import (
    ANTI_CRAWL_ENABLED,
    BROWSER_BACKEND,
    BROWSER_LIGHT_MODE,
    BROWSER_LOCAL_FALLBACK_ENABLED,
    BROWSER_LOCAL_MAX_CONCURRENCY,
    BROWSER_REMOTE_MAX_CONCURRENCY,
    BROWSER_REMOTE_TIMEOUT_SECONDS,
    BROWSER_TEXT_MODE,
    BROWSERLESS_WS_URL,
    CONTENT_FILTER_THRESHOLD,
    CRAWL_EXTRACTION_STRATEGY,
    CRAWL_MIN_QUALITY_SCORE,
    CRAWL_QUALITY_GATE_ENABLED,
    CUSTOM_USER_AGENTS,
    DISABLED_ENGINES,
    ENABLE_BROWSER_HEADERS,
    ENABLE_PROXY_ROTATION,
    ENABLE_RANDOM_HEADERS,
    ENABLE_REQUEST_DELAY,
    ENABLE_USER_AGENT_ROTATION,
    ENABLED_ENGINES,
    HTTP_EXTRACTOR_ENABLED,
    HTTP_EXTRACTOR_MIN_CONTENT_LENGTH,
    HTTP_EXTRACTOR_TIMEOUT_SECONDS,
    MAX_REQUEST_DELAY,
    MIN_REQUEST_DELAY,
    OBSCURA_ALLOW_PRIVATE_NETWORK,
    OBSCURA_BINARY,
    OBSCURA_DUMP_FORMAT,
    OBSCURA_MAX_CONCURRENCY,
    OBSCURA_STEALTH_ENABLED,
    OBSCURA_TIMEOUT_SECONDS,
    OBSCURA_WAIT_UNTIL,
    PROXY_LIST,
    PROXY_ROTATION_MODE,
    READER_ENABLED,
    READER_MIN_CONTENT_LENGTH,
    READER_TIMEOUT_SECONDS,
    SEARCH_LANGUAGE,
    SEARXNG_API_BASE,
    SEARXNG_HOST,
    SEARXNG_PORT,
    SEARXNG_TIMEOUT_SECONDS,
    SEARXNG_URL,
    USE_MOBILE_AGENTS,
    WORD_COUNT_THRESHOLD,
)
from .extractor import fetch_with_http_extractor
from .quality import assess_content_quality
from .reader import fetch_with_reader

ReaderUrlProvider = Callable[[], Awaitable[Sequence[str]] | Sequence[str]]


class WebCrawler:
    """Web crawler class that encapsulates web crawling and content processing functionality."""

    def __init__(
        self,
        cache_manager: Optional[CacheManager] = None,
        anti_crawl_config: Optional[AntiCrawlConfig] = None,
        reader_session: Optional[aiohttp.ClientSession] = None,
        reader_semaphore: Optional[asyncio.Semaphore] = None,
        page_client: Optional[httpx.AsyncClient] = None,
        http_semaphore: Optional[asyncio.Semaphore] = None,
        reader_url_provider: Optional[ReaderUrlProvider] = None,
    ) -> None:
        self.crawler = None
        self.cache_manager = cache_manager
        self.anti_crawl_config = anti_crawl_config or self._create_default_anti_crawl_config()
        self.reader_session = reader_session
        self.reader_semaphore = reader_semaphore
        self.reader_url_provider = reader_url_provider
        self.page_client = page_client
        self.http_semaphore = http_semaphore
        self.obscura_browser_backend = ObscuraBrowserBackend(
            anti_crawl_config=self.anti_crawl_config,
            binary=OBSCURA_BINARY,
            enabled=BROWSER_BACKEND in {"obscura", "hybrid"},
            max_concurrency=OBSCURA_MAX_CONCURRENCY,
            timeout_seconds=OBSCURA_TIMEOUT_SECONDS,
            stealth_enabled=OBSCURA_STEALTH_ENABLED,
            wait_until=OBSCURA_WAIT_UNTIL,
            dump_format=OBSCURA_DUMP_FORMAT,
            allow_private_network=OBSCURA_ALLOW_PRIVATE_NETWORK,
        )
        self.remote_browser_backend = BrowserBackend(
            name="remote",
            anti_crawl_config=self.anti_crawl_config,
            enabled=BROWSER_BACKEND in {"remote", "hybrid"} and bool(BROWSERLESS_WS_URL),
            cdp_url=BROWSERLESS_WS_URL,
            max_concurrency=BROWSER_REMOTE_MAX_CONCURRENCY,
            text_mode=BROWSER_TEXT_MODE,
            light_mode=BROWSER_LIGHT_MODE,
        )
        self.local_browser_backend = BrowserBackend(
            name="local",
            anti_crawl_config=self.anti_crawl_config,
            enabled=BROWSER_BACKEND in {"local", "hybrid"} and BROWSER_LOCAL_FALLBACK_ENABLED,
            max_concurrency=BROWSER_LOCAL_MAX_CONCURRENCY,
            install_local_browser=True,
            text_mode=BROWSER_TEXT_MODE,
            light_mode=BROWSER_LIGHT_MODE,
        )
        logger.info("Initializing WebCrawler instance")
        logger.info(f"Anti-crawl configuration: {self.anti_crawl_config.to_dict()}")

    def _create_default_anti_crawl_config(self) -> AntiCrawlConfig:
        """Create default anti-crawl configuration from environment variables."""
        if not ANTI_CRAWL_ENABLED:
            logger.info("Anti-crawl features disabled")
            return AntiCrawlConfig(
                enable_proxy_rotation=False,
                enable_user_agent_rotation=False,
                enable_request_delay=False,
                enable_random_headers=False,
                enable_browser_headers=False,
            )

        proxies = []
        if PROXY_LIST:
            for proxy_str in PROXY_LIST.split(","):
                proxy_str = proxy_str.strip()
                if not proxy_str:
                    continue

                try:
                    if "@" in proxy_str:
                        parts = proxy_str.split("://")
                        if len(parts) == 2:
                            protocol = parts[0]
                            auth_and_host = parts[1].split("@")
                            if len(auth_and_host) == 2:
                                auth = auth_and_host[0].split(":")
                                host = auth_and_host[1]
                                if len(auth) == 2:
                                    proxies.append(
                                        ProxyConfig(
                                            url=host,
                                            proxy_type=ProxyType(protocol),
                                            username=auth[0],
                                            password=auth[1],
                                        )
                                    )
                    else:
                        parts = proxy_str.split("://")
                        if len(parts) == 2:
                            protocol = parts[0]
                            host = parts[1]
                            proxies.append(
                                ProxyConfig(
                                    url=host,
                                    proxy_type=ProxyType(protocol),
                                )
                            )
                except Exception as exc:
                    logger.warning(f"Failed to parse proxy: {proxy_str}, error: {exc}")

        custom_agents = []
        if CUSTOM_USER_AGENTS:
            custom_agents = [ua.strip() for ua in CUSTOM_USER_AGENTS.split(",") if ua.strip()]

        return AntiCrawlConfig(
            enable_proxy_rotation=ENABLE_PROXY_ROTATION,
            enable_user_agent_rotation=ENABLE_USER_AGENT_ROTATION,
            enable_request_delay=ENABLE_REQUEST_DELAY,
            enable_random_headers=ENABLE_RANDOM_HEADERS,
            enable_browser_headers=ENABLE_BROWSER_HEADERS,
            min_delay=MIN_REQUEST_DELAY,
            max_delay=MAX_REQUEST_DELAY,
            proxy_rotation_mode=PROXY_ROTATION_MODE,
            custom_user_agents=custom_agents,
            use_mobile_agents=USE_MOBILE_AGENTS,
            proxies=proxies,
        )

    async def initialize(self) -> None:
        """Retained for compatibility; browser backends are initialized lazily."""
        return None

    async def close(self) -> None:
        """Close any owned resources."""
        await self.obscura_browser_backend.close()
        await self.remote_browser_backend.close()
        await self.local_browser_backend.close()

    @staticmethod
    def markdown_to_text_regex(markdown_str: str) -> str:
        """Convert Markdown text to plain text using regular expressions."""
        text = re.sub(r"#+\s*", "", markdown_str)
        text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
        text = re.sub(r"(\*\*|__)(.*?)\1", r"\2", text)
        text = re.sub(r"(\*|_)(.*?)\1", r"\2", text)
        text = re.sub(r"^[\*\-\+]\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"`{3}.*?`{3}", "", text, flags=re.DOTALL)
        text = re.sub(r"`(.*?)`", r"\1", text)
        text = re.sub(r"^>\s*", "", text, flags=re.MULTILINE)
        return text.strip()

    @staticmethod
    def markdown_to_text(markdown_str: str) -> str:
        """Convert Markdown text to plain text using markdown and BeautifulSoup."""
        html = markdown.markdown(markdown_str, extensions=["fenced_code"])
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(separator="\n")
        cleaned_text = "\n".join(line.strip() for line in text.split("\n") if line.strip())
        return cleaned_text

    @classmethod
    def convert_markdown_to_plain_text(cls, markdown_str: str) -> str:
        """Normalize markdown into plain text."""
        return cls.markdown_to_text_regex(cls.markdown_to_text(markdown_str))

    @classmethod
    def process_result_content(cls, result: dict[str, str]) -> dict[str, str]:
        """Convert a crawl result into the response payload."""
        plain_text = cls.convert_markdown_to_plain_text(result["content"])
        processed = {
            "content": plain_text,
            "reference": result["reference"],
        }
        if result.get("source_stage"):
            processed["source_stage"] = result["source_stage"]
        if result.get("quality_score") is not None:
            processed["quality_score"] = result["quality_score"]
        return processed

    @staticmethod
    async def make_searxng_request(
        query: str,
        limit: int = 10,
        disabled_engines: str = DISABLED_ENGINES,
        enabled_engines: str = ENABLED_ENGINES,
        client: Optional[httpx.AsyncClient] = None,
    ) -> dict:
        """Send search request to SearXNG."""
        owns_client = client is None
        request_started = time.perf_counter()
        try:
            form_data = {
                "q": query,
                "format": "json",
                "language": SEARCH_LANGUAGE,
                "time_range": "week",
                "safesearch": "2",
                "pageno": "1",
                "category_general": "1",
            }

            headers = {
                "Cookie": f"disabled_engines={disabled_engines};enabled_engines={enabled_engines};method=POST",
                "User-Agent": "TrailSearch/1.0.0",
                "Accept": "*/*",
                "Connection": "keep-alive",
            }
            if not SEARXNG_URL:
                headers["Host"] = f"{SEARXNG_HOST}:{SEARXNG_PORT}"

            if client is None:
                client = httpx.AsyncClient(timeout=httpx.Timeout(SEARXNG_TIMEOUT_SECONDS))

            logger.info(f"Sending search request to SearXNG: {query}")
            response = await client.post(SEARXNG_API_BASE, data=form_data, headers=headers)
            response.raise_for_status()
            logger.info(
                f"SearXNG request completed in {(time.perf_counter() - request_started) * 1000:.2f}ms"
            )
            return response.json()
        except httpx.HTTPStatusError as exc:
            logger.error(
                f"SearXNG request failed with status {exc.response.status_code}: {exc.response.text}"
            )
            raise Exception(f"Search request failed: {exc.response.text}") from exc
        except Exception as exc:
            logger.error(f"SearXNG request failed: {str(exc)}")
            raise Exception(f"Search request failed: {str(exc)}") from exc
        finally:
            if owns_client and client is not None:
                await client.aclose()

    def _create_browser_run_config(self) -> CrawlerRunConfig:
        md_generator = DefaultMarkdownGenerator(
            content_filter=PruningContentFilter(threshold=CONTENT_FILTER_THRESHOLD),
            options={
                "ignore_links": True,
                "ignore_images": True,
                "escape_html": False,
            },
        )
        return CrawlerRunConfig(
            word_count_threshold=WORD_COUNT_THRESHOLD,
            exclude_external_links=True,
            remove_overlay_elements=True,
            excluded_tags=["img", "header", "footer", "iframe", "nav"],
            process_iframes=True,
            markdown_generator=md_generator,
            cache_mode=CacheMode.BYPASS,
            page_timeout=int(BROWSER_REMOTE_TIMEOUT_SECONDS * 1000),
        )

    @staticmethod
    def _merge_stage_results(
        urls: list[str],
        stage_results: list[Optional[dict[str, str]]],
        stage_name: str,
        instruction: str = "",
        quality_gate_enabled: bool = False,
        min_content_length: int = 300,
    ) -> tuple[list[dict[str, str]], list[str]]:
        successful_results: list[dict[str, str]] = []
        pending_urls: list[str] = []
        for url, result in zip(urls, stage_results):
            if not result:
                pending_urls.append(url)
                continue

            quality = assess_content_quality(
                result.get("content"),
                query=instruction,
                min_content_length=min_content_length,
                min_score=CRAWL_MIN_QUALITY_SCORE,
            )
            if quality_gate_enabled and not quality.usable:
                logger.debug(
                    f"{stage_name} result for {url} rejected by quality gate: {quality.reasons}"
                )
                pending_urls.append(url)
                continue

            enriched_result = {
                **result,
                "source_stage": stage_name,
                "quality_score": quality.score,
            }
            successful_results.append(enriched_result)
        return successful_results, pending_urls

    async def _run_http_stage(
        self,
        urls: list[str],
        instruction: str,
        quality_gate_enabled: bool = False,
    ) -> tuple[list[dict[str, str]], list[str]]:
        if not urls or not HTTP_EXTRACTOR_ENABLED or self.page_client is None:
            return [], urls

        tasks = [
            fetch_with_http_extractor(
                url,
                client=self.page_client,
                semaphore=self.http_semaphore,
                timeout_seconds=HTTP_EXTRACTOR_TIMEOUT_SECONDS,
                min_content_length=HTTP_EXTRACTOR_MIN_CONTENT_LENGTH,
            )
            for url in urls
        ]
        stage_results = await asyncio.gather(*tasks)
        return self._merge_stage_results(
            urls,
            stage_results,
            stage_name="fast_http",
            instruction=instruction,
            quality_gate_enabled=quality_gate_enabled,
            min_content_length=HTTP_EXTRACTOR_MIN_CONTENT_LENGTH,
        )

    async def _run_reader_stage(
        self,
        urls: list[str],
        instruction: str,
        quality_gate_enabled: bool = False,
    ) -> tuple[list[dict[str, str]], list[str]]:
        if not urls or not READER_ENABLED:
            return [], urls

        reader_urls = await self._get_reader_urls()
        tasks = [
            fetch_with_reader(
                url,
                session=self.reader_session,
                semaphore=self.reader_semaphore,
                timeout_seconds=READER_TIMEOUT_SECONDS,
                min_content_length=READER_MIN_CONTENT_LENGTH,
                reader_urls=reader_urls,
            )
            for url in urls
        ]
        stage_results = await asyncio.gather(*tasks)
        return self._merge_stage_results(
            urls,
            stage_results,
            stage_name="reader",
            instruction=instruction,
            quality_gate_enabled=quality_gate_enabled,
            min_content_length=READER_MIN_CONTENT_LENGTH,
        )

    async def _get_reader_urls(self) -> Optional[list[str]]:
        """Resolve dynamic Reader endpoints, falling back inside fetch_with_reader."""
        if self.reader_url_provider is None:
            return None

        try:
            configured_urls = self.reader_url_provider()
            if inspect.isawaitable(configured_urls):
                configured_urls = await configured_urls
            endpoints = [endpoint.strip().rstrip("/") for endpoint in configured_urls if endpoint]
            return list(dict.fromkeys(endpoints)) or None
        except Exception as exc:
            logger.warning(f"Failed to resolve dynamic Reader endpoints: {exc}")
            return None

    async def _run_browser_stage(
        self,
        backend: BrowserBackend,
        urls: list[str],
    ) -> tuple[list[dict[str, str]], list[str]]:
        if not urls or not backend.enabled:
            return [], urls

        if ANTI_CRAWL_ENABLED and self.anti_crawl_config.enable_request_delay:
            await self.anti_crawl_config.apply_delay_async()

        results, pending_urls = await backend.fetch_urls(urls, self._create_browser_run_config())
        merged_results, _ = self._merge_stage_results(
            urls,
            [
                next((result for result in results if result["reference"] == url), None)
                for url in urls
            ],
            stage_name=f"{backend.name}_browser",
            instruction="",
            quality_gate_enabled=False,
            min_content_length=HTTP_EXTRACTOR_MIN_CONTENT_LENGTH,
        )
        return merged_results, pending_urls

    async def _run_obscura_stage(
        self,
        urls: list[str],
    ) -> tuple[list[dict[str, str]], list[str]]:
        if not urls or not self.obscura_browser_backend.enabled:
            return [], urls

        if ANTI_CRAWL_ENABLED and self.anti_crawl_config.enable_request_delay:
            await self.anti_crawl_config.apply_delay_async()

        results, pending_urls = await self.obscura_browser_backend.fetch_urls(
            urls,
            min_content_length=HTTP_EXTRACTOR_MIN_CONTENT_LENGTH,
        )
        merged_results, _ = self._merge_stage_results(
            urls,
            [
                next((result for result in results if result["reference"] == url), None)
                for url in urls
            ],
            stage_name="obscura_browser",
            instruction="",
            quality_gate_enabled=False,
            min_content_length=HTTP_EXTRACTOR_MIN_CONTENT_LENGTH,
        )
        return merged_results, pending_urls

    async def _run_configured_extraction_stages(
        self,
        pending_urls: list[str],
        instruction: str,
        timings_ms: dict[str, float],
        stage_counts: dict[str, int],
    ) -> tuple[list[dict[str, str]], list[str]]:
        """Run extraction stages in the configured benchmark-informed order."""
        all_results: list[dict[str, str]] = []
        strategy = CRAWL_EXTRACTION_STRATEGY if CRAWL_EXTRACTION_STRATEGY else "reader_first"

        if strategy == "http_first":
            stage_order = ["fast_http", "reader", "obscura_browser", "remote_browser", "local_browser"]
        elif strategy == "quality_gated":
            stage_order = ["fast_http", "reader", "obscura_browser", "remote_browser", "local_browser"]
        else:
            stage_order = ["reader", "fast_http", "obscura_browser", "remote_browser", "local_browser"]

        for stage_name in stage_order:
            if not pending_urls:
                break

            stage_started = time.perf_counter()
            if stage_name == "fast_http":
                stage_results, pending_urls = await self._run_http_stage(
                    pending_urls,
                    instruction=instruction,
                    quality_gate_enabled=strategy == "quality_gated" and CRAWL_QUALITY_GATE_ENABLED,
                )
                timings_ms["fast_http"] = round((time.perf_counter() - stage_started) * 1000, 2)
                stage_counts["fast_path_hits"] = len(stage_results)
            elif stage_name == "reader":
                stage_results, pending_urls = await self._run_reader_stage(
                    pending_urls,
                    instruction=instruction,
                    quality_gate_enabled=strategy == "quality_gated" and CRAWL_QUALITY_GATE_ENABLED,
                )
                timings_ms["reader"] = round((time.perf_counter() - stage_started) * 1000, 2)
                stage_counts["reader_hits"] = len(stage_results)
            elif stage_name == "obscura_browser":
                stage_results, pending_urls = await self._run_obscura_stage(pending_urls)
                timings_ms["obscura_browser"] = round(
                    (time.perf_counter() - stage_started) * 1000,
                    2,
                )
                stage_counts["obscura_browser_hits"] = len(stage_results)
            elif stage_name == "remote_browser":
                stage_results, pending_urls = await self._run_browser_stage(
                    self.remote_browser_backend,
                    pending_urls,
                )
                timings_ms["remote_browser"] = round(
                    (time.perf_counter() - stage_started) * 1000,
                    2,
                )
                stage_counts["remote_browser_hits"] = len(stage_results)
            else:
                stage_results, pending_urls = await self._run_browser_stage(
                    self.local_browser_backend,
                    pending_urls,
                )
                timings_ms["local_browser"] = round(
                    (time.perf_counter() - stage_started) * 1000,
                    2,
                )
                stage_counts["local_browser_fallback_hits"] = len(stage_results)

            all_results.extend(stage_results)

        return all_results, pending_urls

    async def crawl_urls(self, urls: list[str], instruction: str) -> dict[str, Any]:
        """Crawl multiple URLs and process content."""
        total_started = time.perf_counter()
        try:
            cached_results: list[dict[str, str]] = []
            pending_urls: list[str] = []
            cache_lookup_ms = 0.0
            text_processing_ms = 0.0
            cache_write_ms = 0.0
            timings_ms = {
                "cache_lookup": 0.0,
                "fast_http": 0.0,
                "reader": 0.0,
                "obscura_browser": 0.0,
                "remote_browser": 0.0,
                "local_browser": 0.0,
                "text_processing": 0.0,
                "cache_write": 0.0,
            }
            stage_counts = {
                "fast_path_hits": 0,
                "reader_hits": 0,
                "obscura_browser_hits": 0,
                "remote_browser_hits": 0,
                "local_browser_fallback_hits": 0,
            }

            if self.cache_manager and self.cache_manager.is_available():
                logger.info(f"Checking cache for {len(urls)} URLs")
                cache_lookup_started = time.perf_counter()
                cache_hits = await self.cache_manager.get_batch(urls, instruction)
                cache_lookup_ms = (time.perf_counter() - cache_lookup_started) * 1000
                timings_ms["cache_lookup"] = round(cache_lookup_ms, 2)

                for url in urls:
                    cached_data = cache_hits.get(url)
                    if cached_data:
                        cached_results.append(
                            {
                                "content": cached_data.get("content"),
                                "reference": cached_data.get("reference"),
                                "source_stage": cached_data.get("source_stage", "cache"),
                                "quality_score": cached_data.get("quality_score"),
                            }
                        )
                    else:
                        pending_urls.append(url)
            else:
                pending_urls = urls.copy()

            if not pending_urls:
                return {
                    "results": cached_results,
                    "success_count": len(cached_results),
                    "failed_urls": [],
                    "cache_hits": len(cached_results),
                    "newly_crawled": 0,
                    **stage_counts,
                    "timings_ms": {
                        **timings_ms,
                        "total": round((time.perf_counter() - total_started) * 1000, 2),
                    },
                }

            all_results, pending_urls = await self._run_configured_extraction_stages(
                pending_urls=pending_urls,
                instruction=instruction,
                timings_ms=timings_ms,
                stage_counts=stage_counts,
            )

            failed_urls = pending_urls
            if not all_results and not cached_results:
                logger.error("All URL crawls failed")
                raise HTTPException(status_code=500, detail="All URL crawls failed")

            text_processing_started = time.perf_counter()
            loop = asyncio.get_running_loop()
            processing_tasks = [
                loop.run_in_executor(None, self.process_result_content, result)
                for result in all_results
            ]
            processed_results = await asyncio.gather(*processing_tasks)
            text_processing_ms = (time.perf_counter() - text_processing_started) * 1000
            timings_ms["text_processing"] = round(text_processing_ms, 2)

            if self.cache_manager and self.cache_manager.is_available() and processed_results:
                cache_write_started = time.perf_counter()
                cache_items = [
                    {
                        "url": result["reference"],
                        "content": result["content"],
                        "reference": result["reference"],
                        "source_stage": result.get("source_stage", ""),
                        "quality_score": str(result.get("quality_score", "")),
                    }
                    for result in processed_results
                ]
                await self.cache_manager.set_batch(cache_items, instruction)
                cache_write_ms = (time.perf_counter() - cache_write_started) * 1000
                timings_ms["cache_write"] = round(cache_write_ms, 2)

            all_processed_results = cached_results + processed_results
            response = {
                "results": all_processed_results,
                "success_count": len(all_processed_results),
                "failed_urls": failed_urls,
                "cache_hits": len(cached_results),
                "newly_crawled": len(processed_results),
                **stage_counts,
                "timings_ms": {
                    **timings_ms,
                    "total": round((time.perf_counter() - total_started) * 1000, 2),
                },
            }
            logger.info(
                f"Crawl completed, total: {len(all_processed_results)}, "
                f"cache hits: {len(cached_results)}, newly crawled: {len(processed_results)}, "
                f"failed: {len(failed_urls)}, stage_counts={stage_counts}, timings_ms={response['timings_ms']}"
            )
            return response
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(f"Exception occurred during crawling: {str(exc)}")
            raise HTTPException(status_code=500, detail=str(exc)) from exc
