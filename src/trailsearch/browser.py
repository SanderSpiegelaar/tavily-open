"""
Browser-backed extraction helpers.
"""

import asyncio
import json
import subprocess
import sys
from typing import Any, Optional

from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
from loguru import logger

from .anti_crawl import AntiCrawlConfig


class BrowserBackend:
    """Run browser-based extraction against local or remote Chrome backends."""

    _install_lock = asyncio.Lock()
    _local_browser_installed = False

    def __init__(
        self,
        name: str,
        anti_crawl_config: AntiCrawlConfig,
        max_concurrency: int = 1,
        cdp_url: str = "",
        enabled: bool = True,
        install_local_browser: bool = False,
        text_mode: bool = False,
        light_mode: bool = False,
    ) -> None:
        self.name = name
        self.anti_crawl_config = anti_crawl_config
        self.cdp_url = cdp_url
        self.enabled = enabled
        self.install_local_browser = install_local_browser
        self.text_mode = text_mode
        self.light_mode = light_mode
        self.semaphore = asyncio.Semaphore(max(1, max_concurrency))

    async def _ensure_local_browser_installed(self) -> None:
        if not self.install_local_browser or BrowserBackend._local_browser_installed:
            return

        async with BrowserBackend._install_lock:
            if BrowserBackend._local_browser_installed:
                return

            logger.info("Installing local Chromium for fallback browser backend")
            await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-m", "playwright", "install", "chromium"],
                check=True,
            )
            BrowserBackend._local_browser_installed = True

    def _build_browser_config(self) -> BrowserConfig:
        headers = self.anti_crawl_config.get_headers()
        proxy = self.anti_crawl_config.get_proxy()
        extra_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-web-security",
            "--disable-features=IsolateOrigins,site-per-process",
        ]

        if self.cdp_url:
            return BrowserConfig(
                headless=True,
                verbose=False,
                cdp_url=self.cdp_url,
                headers=headers if headers else {},
                proxy=proxy if proxy else "",
                text_mode=self.text_mode,
                light_mode=self.light_mode,
            )

        return BrowserConfig(
            headless=True,
            verbose=False,
            extra_args=extra_args,
            headers=headers if headers else {},
            proxy=proxy if proxy else "",
            text_mode=self.text_mode,
            light_mode=self.light_mode,
        )

    async def fetch_urls(
        self,
        urls: list[str],
        run_config: CrawlerRunConfig,
    ) -> tuple[list[dict[str, str]], list[str]]:
        """Fetch URLs with a browser backend and normalize successful results."""
        if not self.enabled or not urls:
            return [], urls

        async with self.semaphore:
            try:
                if not self.cdp_url:
                    await self._ensure_local_browser_installed()

                browser_config = self._build_browser_config()
                crawler = await AsyncWebCrawler(config=browser_config).__aenter__()
            except Exception as exc:
                logger.warning(f"Failed to initialize {self.name} browser backend: {exc}")
                return [], urls

            try:
                crawl_result = await crawler.arun_many(urls=urls, config=run_config)
                results: list[Any] = []
                if hasattr(crawl_result, "__aiter__"):
                    async for result in crawl_result:  # type: ignore[union-attr]
                        results.append(result)
                else:
                    results = list(crawl_result) if crawl_result else []  # type: ignore[arg-type]

                return self._normalize_results(urls, results)
            except Exception as exc:
                logger.warning(f"{self.name} browser crawl batch failed: {exc}")
                return [], urls
            finally:
                await crawler.__aexit__(None, None, None)

    def _normalize_results(
        self,
        urls: list[str],
        results: list[Any],
    ) -> tuple[list[dict[str, str]], list[str]]:
        successful_results: list[dict[str, str]] = []
        retry_urls: list[str] = []
        known_urls = set(urls)
        seen_urls: set[str] = set()

        for index, result in enumerate(results):
            result_url = getattr(result, "url", None) if result is not None else None
            url = result_url if result_url in known_urls else urls[index] if index < len(urls) else ""
            if not url:
                continue
            seen_urls.add(url)
            try:
                if result is None or not getattr(result, "success", False):
                    retry_urls.append(url)
                    continue

                markdown_result = getattr(result, "markdown", None)
                fit_markdown = getattr(markdown_result, "fit_markdown", None)
                if not fit_markdown:
                    retry_urls.append(url)
                    continue

                successful_results.append(
                    {
                        "content": fit_markdown,
                        "reference": url,
                    }
                )
            except Exception as exc:
                logger.warning(f"{self.name} browser crawl failed for {url}: {exc}")
                retry_urls.append(url)

        remaining_urls = retry_urls + [url for url in urls if url not in seen_urls]
        return successful_results, remaining_urls

    async def close(self) -> None:
        """Close any owned resources."""
        return None


class ObscuraBrowserBackend:
    """Run browser-backed extraction through the Obscura CLI."""

    def __init__(
        self,
        anti_crawl_config: AntiCrawlConfig,
        binary: str = "obscura",
        enabled: bool = False,
        max_concurrency: int = 4,
        timeout_seconds: float = 45,
        stealth_enabled: bool = False,
        wait_until: str = "networkidle0",
        dump_format: str = "text",
        allow_private_network: bool = False,
    ) -> None:
        self.name = "obscura"
        self.anti_crawl_config = anti_crawl_config
        self.binary = binary
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self.stealth_enabled = stealth_enabled
        self.wait_until = wait_until
        self.dump_format = dump_format
        self.allow_private_network = allow_private_network
        self.semaphore = asyncio.Semaphore(max(1, max_concurrency))

    def _build_command(self, url: str) -> list[str]:
        command = [self.binary]

        if self.allow_private_network:
            command.append("--allow-private-network")

        proxy = self.anti_crawl_config.get_proxy()
        if proxy:
            command.extend(["--proxy", proxy])

        command.extend(
            [
                "fetch",
                url,
                "--dump",
                self.dump_format,
                "--timeout",
                str(int(self.timeout_seconds)),
            ]
        )

        if self.wait_until:
            command.extend(["--wait-until", self.wait_until])

        if self.stealth_enabled:
            command.append("--stealth")

        return command

    @staticmethod
    def _extract_content(stdout: str) -> str:
        text = stdout.strip()
        if not text:
            return ""

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return text

        if isinstance(payload, str):
            return payload.strip()

        if isinstance(payload, dict):
            for key in ("markdown", "content", "text", "body", "html"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

        return text

    async def _fetch_one(
        self,
        url: str,
        min_content_length: int,
    ) -> Optional[dict[str, str]]:
        async with self.semaphore:
            command = self._build_command(url)
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError:
                logger.warning(
                    f"Obscura binary not found: {self.binary}. "
                    "Install Obscura or set OBSCURA_BINARY."
                )
                self.enabled = False
                return None
            except Exception as exc:
                logger.warning(f"Failed to start Obscura for {url}: {exc}")
                return None

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout_seconds + 2,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                logger.warning(f"Obscura timed out for {url}")
                return None

            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            if process.returncode != 0:
                logger.warning(f"Obscura failed for {url}: {stderr_text}")
                return None

            content = self._extract_content(stdout.decode("utf-8", errors="replace"))
            if len("".join(content.split())) < min_content_length:
                logger.debug(f"Obscura produced low-quality content for {url}")
                return None

            return {"content": content, "reference": url}

    async def fetch_urls(
        self,
        urls: list[str],
        min_content_length: int,
    ) -> tuple[list[dict[str, str]], list[str]]:
        """Fetch URLs with Obscura and normalize successful results."""
        if not self.enabled or not urls:
            return [], urls

        tasks = [self._fetch_one(url, min_content_length) for url in urls]
        stage_results = await asyncio.gather(*tasks)

        successful_results: list[dict[str, str]] = []
        pending_urls: list[str] = []
        for url, result in zip(urls, stage_results):
            if result:
                successful_results.append(result)
            else:
                pending_urls.append(url)

        return successful_results, pending_urls

    async def close(self) -> None:
        """Close any owned resources."""
        return None
