"""
Tests for search provider normalization and selection.
"""

import httpx
import pytest

from trailsearch.local_index import LocalIndex
from trailsearch.search_providers import (
    BraveSearchProvider,
    SearchProviderRequest,
    SearXNGSearchProvider,
    create_search_provider,
)


@pytest.mark.asyncio
async def test_searxng_provider_normalizes_results():
    """SearXNG provider should normalize URLs, titles, and snippets."""

    async def handler(request):
        assert request.method == "POST"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://example.com/result",
                        "title": "Example Result",
                        "content": "Snippet from SearXNG",
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    provider = SearXNGSearchProvider(client=client)

    response = await provider.search(
        SearchProviderRequest(
            query="example",
            limit=5,
            provider="searxng",
            disabled_engines="",
            enabled_engines="google__general",
        )
    )

    assert response.provider == "searxng"
    assert len(response.hits) == 1
    assert response.hits[0].url == "https://example.com/result"
    assert response.hits[0].title == "Example Result"
    assert response.hits[0].snippet == "Snippet from SearXNG"

    await client.aclose()


@pytest.mark.asyncio
async def test_brave_provider_normalizes_web_results():
    """Brave provider should normalize web results into shared hit shape."""

    async def handler(request):
        assert request.method == "GET"
        assert request.headers["X-Subscription-Token"] == "test-key"
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "url": "https://example.com/brave",
                            "title": "Brave Result",
                            "description": "Snippet from Brave",
                        }
                    ]
                }
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    provider = BraveSearchProvider(client=client, api_key="test-key")

    response = await provider.search(
        SearchProviderRequest(
            query="example",
            limit=3,
            provider="brave",
        )
    )

    assert response.provider == "brave"
    assert len(response.hits) == 1
    assert response.hits[0].url == "https://example.com/brave"
    assert response.hits[0].title == "Brave Result"
    assert response.hits[0].snippet == "Snippet from Brave"

    await client.aclose()


def test_create_search_provider_rejects_unknown_provider():
    """Factory should reject unsupported provider names."""
    with pytest.raises(ValueError):
        create_search_provider("unknown-provider")


@pytest.mark.asyncio
async def test_router_provider_can_use_local_index(tmp_path):
    """Router provider should return local index hits without external APIs."""
    local_index = LocalIndex(str(tmp_path / "trailsearch.sqlite3"))
    await local_index.initialize()
    try:
        await local_index.upsert_many(
            [
                {
                    "url": "https://example.com/local",
                    "title": "Local Tavily Result",
                    "snippet": "Local result from accumulated index",
                    "content": "Local accumulated index content for Tavily-like search.",
                }
            ]
        )
        provider = create_search_provider("router", local_index=local_index)

        response = await provider.search(
            SearchProviderRequest(query="local tavily", limit=5, provider="router")
        )

        assert response.provider.startswith("router")
        assert response.hits[0].url == "https://example.com/local"
        assert response.hits[0].provider == "local"
    finally:
        await local_index.close()
