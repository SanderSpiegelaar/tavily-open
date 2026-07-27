"""
Tests for browser-backed extraction helpers.
"""

import pytest

from trailsearch.anti_crawl import AntiCrawlConfig
from trailsearch.browser import BrowserBackend, ObscuraBrowserBackend


class _MarkdownResult:
    def __init__(self, fit_markdown: str) -> None:
        self.fit_markdown = fit_markdown


class _CrawlResult:
    def __init__(self, url: str, content: str, success: bool = True) -> None:
        self.url = url
        self.success = success
        self.markdown = _MarkdownResult(content)


def test_obscura_extract_content_accepts_plain_text():
    """Obscura output parser should keep plain text intact."""
    content = ObscuraBrowserBackend._extract_content("Rendered page content\n")

    assert content == "Rendered page content"


def test_obscura_extract_content_accepts_json_fields():
    """Obscura output parser should accept common structured content keys."""
    content = ObscuraBrowserBackend._extract_content(
        '{"url":"https://example.com","markdown":"# Rendered Title"}'
    )

    assert content == "# Rendered Title"


def test_obscura_build_command_uses_documented_flags():
    """Obscura command construction should stay shell-free and configurable."""
    backend = ObscuraBrowserBackend(
        anti_crawl_config=AntiCrawlConfig(enable_proxy_rotation=False),
        binary="obscura",
        enabled=True,
        timeout_seconds=12,
        stealth_enabled=True,
        wait_until="networkidle",
        dump_format="text",
        allow_private_network=True,
    )

    command = backend._build_command("https://example.com")

    assert command == [
        "obscura",
        "--allow-private-network",
        "fetch",
        "https://example.com",
        "--dump",
        "text",
        "--timeout",
        "12",
        "--wait-until",
        "networkidle",
        "--stealth",
    ]


def test_browser_normalize_results_uses_result_url_not_batch_order():
    """Crawl4AI may return arun_many results out of input order."""
    backend = BrowserBackend(
        name="local",
        anti_crawl_config=AntiCrawlConfig(enable_proxy_rotation=False),
    )
    urls = ["https://example.com/a", "https://example.com/b"]
    results = [
        _CrawlResult("https://example.com/b", "content b"),
        _CrawlResult("https://example.com/a", "content a"),
    ]

    normalized, pending = backend._normalize_results(urls, results)

    assert pending == []
    assert normalized == [
        {"content": "content b", "reference": "https://example.com/b"},
        {"content": "content a", "reference": "https://example.com/a"},
    ]


@pytest.mark.asyncio
async def test_obscura_missing_binary_fails_softly():
    """Missing Obscura binary should return pending URLs instead of raising."""
    backend = ObscuraBrowserBackend(
        anti_crawl_config=AntiCrawlConfig(enable_proxy_rotation=False),
        binary="definitely-not-obscura",
        enabled=True,
        timeout_seconds=1,
    )

    results, pending = await backend.fetch_urls(["https://example.com"], min_content_length=10)

    assert results == []
    assert pending == ["https://example.com"]
    assert backend.enabled is False
