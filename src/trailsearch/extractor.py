"""
Lightweight HTTP extraction helpers.
"""

import asyncio
import re
from typing import Optional

import httpx
import trafilatura
from loguru import logger

SHELL_PAGE_PATTERNS = (
    r"__NEXT_DATA__",
    r' id=["\']root["\']',
    r' id=["\']app["\']',
    r' id=["\']__nuxt["\']',
    r"window\.__INITIAL_STATE__",
)


def extract_markdown_from_html(html: str, url: str) -> Optional[str]:
    """Extract markdown-like content from HTML."""
    try:
        content = trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            include_links=False,
            include_images=False,
            favor_precision=True,
            deduplicate=True,
        )
        if content:
            return content.strip()
    except Exception as exc:
        logger.debug(f"Trafilatura extraction failed for {url}: {exc}")
    return None


def normalize_text_length(content: str) -> int:
    """Measure extracted content length without whitespace noise."""
    return len(re.sub(r"\s+", "", content))


def looks_like_shell_page(html: str) -> bool:
    """Detect pages that likely require client-side rendering."""
    text_length = len(re.sub(r"\s+", "", re.sub(r"<[^>]+>", " ", html)))
    has_shell_marker = any(
        re.search(pattern, html, flags=re.IGNORECASE) for pattern in SHELL_PAGE_PATTERNS
    )
    return has_shell_marker and text_length < 1500


def is_content_usable(content: Optional[str], min_content_length: int) -> bool:
    """Check whether extracted content is good enough to return."""
    if not content:
        return False
    return normalize_text_length(content) >= min_content_length


async def fetch_with_http_extractor(
    url: str,
    client: httpx.AsyncClient,
    semaphore: Optional[asyncio.Semaphore],
    timeout_seconds: float,
    min_content_length: int,
) -> Optional[dict[str, str]]:
    """Fetch a page over HTTP and extract markdown content."""

    async def _run() -> Optional[dict[str, str]]:
        try:
            response = await client.get(
                url,
                timeout=httpx.Timeout(timeout_seconds),
                follow_redirects=True,
                headers={
                    "User-Agent": "TrailSearch/1.0",
                    "Accept": "text/html,application/xhtml+xml",
                },
            )
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            if content_type and "html" not in content_type and "xml" not in content_type:
                logger.debug(f"Skipping non-HTML response for {url}: {content_type}")
                return None

            html = response.text
            if looks_like_shell_page(html):
                logger.debug(f"Detected shell page for {url}, escalating to fallback stages")
                return None

            extracted_content = extract_markdown_from_html(html, url)
            if not is_content_usable(extracted_content, min_content_length):
                logger.debug(f"HTTP extraction produced low-quality content for {url}")
                return None

            return {"content": extracted_content, "reference": url}
        except httpx.HTTPError as exc:
            logger.debug(f"HTTP extractor failed for {url}: {exc}")
            return None
        except Exception as exc:
            logger.debug(f"Unexpected HTTP extraction failure for {url}: {exc}")
            return None

    if semaphore is None:
        return await _run()

    async with semaphore:
        return await _run()
