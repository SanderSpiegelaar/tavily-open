import asyncio
import hashlib
from collections.abc import Sequence
from typing import Any, Optional
from urllib.parse import quote

import aiohttp

from .config import (
    READER_API_KEY,
    READER_MIN_CONTENT_LENGTH,
    READER_TIMEOUT_SECONDS,
    READER_URL,
    READER_URLS,
)
from .extractor import is_content_usable
from .logger import logger


def build_reader_api_url(url: str, reader_url: str = READER_URL) -> str:
    """Build a Reader API URL while preserving the target URL shape."""
    return f"{reader_url.rstrip('/')}/{quote(url, safe=':/?&=%')}"


def parse_reader_urls(reader_urls: str = READER_URLS, fallback_url: str = READER_URL) -> list[str]:
    """Parse configured Reader endpoints, falling back to READER_URL."""
    urls = [item.strip().rstrip("/") for item in reader_urls.split(",") if item.strip()]
    if not urls and fallback_url:
        urls = [fallback_url.strip().rstrip("/")]
    return list(dict.fromkeys(urls))


def ordered_reader_urls(url: str, reader_urls: Sequence[str]) -> list[str]:
    """Return endpoints in a stable per-target order for load spreading and failover."""
    endpoints = [endpoint.rstrip("/") for endpoint in reader_urls if endpoint]
    if len(endpoints) <= 1:
        return endpoints

    digest = hashlib.md5(url.encode("utf-8")).hexdigest()
    start_index = int(digest[:8], 16) % len(endpoints)
    return endpoints[start_index:] + endpoints[:start_index]


async def _fetch_with_session(
    session: aiohttp.ClientSession,
    url: str,
    reader_url: str,
    timeout_seconds: float,
    min_content_length: int,
) -> Optional[dict[str, Any]]:
    """Fetch a URL using a provided aiohttp session."""
    reader_api_url = build_reader_api_url(url, reader_url=reader_url)
    headers = {
        "Accept": "application/json",
        "X-Respond-With": "markdown",
    }
    if READER_API_KEY:
        headers["Authorization"] = f"Bearer {READER_API_KEY}"

    try:
        async with session.get(
            reader_api_url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout_seconds),
        ) as response:
            if response.status == 200:
                content = await response.text()
                if is_content_usable(content, min_content_length):
                    logger.debug(f"Successfully fetched content for {url} with Reader {reader_url}.")
                    return {"content": content, "reference": url}

                logger.warning(f"Reader {reader_url} returned no content for {url}.")
                return None

            error_text = await response.text()
            logger.error(
                f"Failed to fetch {url} with Reader {reader_url}. "
                f"Status: {response.status}, Response: {error_text}"
            )
            return None
    except asyncio.TimeoutError:
        logger.error(f"Timeout error when fetching {url} with Reader.")
        return None
    except aiohttp.ClientError as e:
        logger.error(f"AIOHTTP client error when fetching {url} with Reader: {e}")
        return None
    except Exception as e:
        logger.error(f"An unexpected error occurred when fetching {url} with Reader: {e}")
        return None


async def fetch_with_reader(
    url: str,
    session: Optional[aiohttp.ClientSession] = None,
    semaphore: Optional[asyncio.Semaphore] = None,
    timeout_seconds: float = READER_TIMEOUT_SECONDS,
    min_content_length: int = READER_MIN_CONTENT_LENGTH,
    reader_urls: Optional[Sequence[str]] = None,
) -> Optional[dict[str, Any]]:
    """
    Asynchronously fetches the content of a URL using the Reader service.

    Args:
        url (str): The URL to fetch.
        session: Optional shared aiohttp session for connection reuse.
        semaphore: Optional semaphore to cap concurrent Reader requests.
        timeout_seconds: Per-request timeout.

    Returns:
        Optional[Dict[str, Any]]: A dictionary containing the fetched content and metadata,
                                     or None if the fetch failed.
    """
    endpoints = ordered_reader_urls(url, reader_urls or parse_reader_urls())
    if not endpoints:
        logger.error("No Reader endpoints configured.")
        return None

    async def _run_request(request_session: aiohttp.ClientSession) -> Optional[dict[str, Any]]:
        async def _try_endpoints() -> Optional[dict[str, Any]]:
            for reader_url in endpoints:
                result = await _fetch_with_session(
                    request_session,
                    url,
                    reader_url,
                    timeout_seconds,
                    min_content_length,
                )
                if result:
                    result["reader_url"] = reader_url
                    return result
            return None

        if semaphore is None:
            return await _try_endpoints()

        async with semaphore:
            return await _try_endpoints()

    if session is not None:
        return await _run_request(session)

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout_seconds)
    ) as owned_session:
        return await _run_request(owned_session)
