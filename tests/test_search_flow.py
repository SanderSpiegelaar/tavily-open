"""
Tests for search flow modes and timing metadata.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import trailsearch.main as main_module


class FakeProvider:
    """Simple async provider stub for API tests."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    async def search(self, request):
        self.calls.append(request)
        return self.response


@pytest.fixture
def client():
    """Create test client."""
    return TestClient(main_module.app)


def test_search_mode_returns_normalized_results_without_crawling(client, monkeypatch):
    """Search-only mode should skip crawling and return normalized hits."""
    provider = FakeProvider(
        SimpleNamespace(
            provider="searxng",
            request_ms=12.5,
            hits=[
                SimpleNamespace(
                    url="https://example.com/result",
                    title="Example Result",
                    snippet="A normalized snippet",
                    provider="searxng",
                )
            ],
        )
    )

    def fail_if_crawl_called(_request):
        raise AssertionError("crawl should not run in search mode")

    monkeypatch.setattr(
        main_module,
        "get_search_provider",
        lambda provider_name, client: provider,
        raising=False,
    )
    monkeypatch.setattr(main_module, "cache_manager", None)
    monkeypatch.setattr(main_module, "crawl", fail_if_crawl_called)

    response = client.post(
        "/search",
        json={"query": "example", "mode": "search", "provider": "searxng"},
    )

    assert response.status_code == 200
    payload = response.json()

    assert payload["mode"] == "search"
    assert payload["provider"] == "searxng"
    assert payload["success_count"] == 1
    assert payload["results"] == [
        {
            "url": "https://example.com/result",
            "title": "Example Result",
            "snippet": "A normalized snippet",
            "provider": "searxng",
        }
    ]
    assert payload["timings_ms"]["search_provider_request"] == 12.5
    assert payload["timings_ms"]["search_total"] >= 0


def test_crawl_mode_preserves_crawl_pipeline_and_adds_search_timings(client, monkeypatch):
    """Default crawl mode should use provider hits as crawl inputs and report provider timing."""
    provider = FakeProvider(
        SimpleNamespace(
            provider="brave",
            request_ms=7.25,
            hits=[
                SimpleNamespace(
                    url="https://example.com/a",
                    title="A",
                    snippet="A snippet",
                    provider="brave",
                ),
                SimpleNamespace(
                    url="https://example.com/b",
                    title="B",
                    snippet="B snippet",
                    provider="brave",
                ),
            ],
        )
    )
    captured = {}

    async def fake_crawl(request):
        captured["request"] = request
        return {
            "results": [{"content": "page body", "reference": "https://example.com/a"}],
            "success_count": 1,
            "failed_urls": [],
            "cache_hits": 0,
            "newly_crawled": 1,
            "timings_ms": {"total": 50.0},
        }

    monkeypatch.setattr(
        main_module,
        "get_search_provider",
        lambda provider_name, client: provider,
        raising=False,
    )
    monkeypatch.setattr(main_module, "cache_manager", None)
    monkeypatch.setattr(main_module, "DEDUP_ENABLED", False)
    monkeypatch.setattr(main_module, "crawl", fake_crawl)

    response = client.post("/search", json={"query": "example", "provider": "brave"})

    assert response.status_code == 200
    payload = response.json()

    assert payload["search_provider"] == "brave"
    assert payload["timings_ms"]["search_provider_request"] == 7.25
    assert payload["timings_ms"]["search_total"] >= payload["timings_ms"]["search_provider_request"]
    assert captured["request"].instruction == "example"
    assert captured["request"].urls == ["https://example.com/a", "https://example.com/b"]


def test_tavily_search_returns_extracted_content_shape(client, monkeypatch):
    """Tavily-like endpoint should return extracted content, chunks, and optional answer."""
    provider = FakeProvider(
        SimpleNamespace(
            provider="router:searxng",
            request_ms=5.0,
            hits=[
                SimpleNamespace(
                    url="https://example.com/reader",
                    title="Reader Result",
                    snippet="Reader benchmark snippet",
                    provider="searxng",
                )
            ],
        )
    )

    async def fake_crawl(request):
        return {
            "results": [
                {
                    "content": (
                        "Reader benchmark extraction keeps useful content clean. "
                        "Reader benchmark extraction also gives citations for Tavily-like search."
                    ),
                    "reference": "https://example.com/reader",
                    "source_stage": "reader",
                    "quality_score": 0.91,
                }
            ],
            "success_count": 1,
            "failed_urls": [],
            "cache_hits": 0,
            "newly_crawled": 1,
            "timings_ms": {"total": 25.0},
        }

    monkeypatch.setattr(
        main_module,
        "get_search_provider",
        lambda provider_name, client: provider,
        raising=False,
    )
    monkeypatch.setattr(main_module, "cache_manager", None)
    monkeypatch.setattr(main_module, "local_index", None)
    monkeypatch.setattr(main_module, "crawl", fake_crawl)

    response = client.post(
        "/tavily/search",
        json={
            "query": "reader benchmark extraction",
            "max_results": 1,
            "include_answer": True,
            "include_raw_content": True,
            "chunks_per_source": 1,
        },
    )

    assert response.status_code == 200
    payload = response.json()

    assert payload["query"] == "reader benchmark extraction"
    assert payload["answer"]
    assert payload["results"][0]["title"] == "Reader Result"
    assert payload["results"][0]["url"] == "https://example.com/reader"
    assert payload["results"][0]["source_stage"] == "reader"
    assert payload["results"][0]["raw_content"].startswith("Reader benchmark extraction")
    assert len(payload["results"][0]["chunks"]) == 1


def test_tavily_search_mode_skips_crawling(client, monkeypatch):
    """Tavily-like clients can explicitly request the low-latency search-only path."""
    provider = FakeProvider(
        SimpleNamespace(
            provider="searxng",
            request_ms=4.5,
            hits=[
                SimpleNamespace(
                    url="https://example.com/fast",
                    title="Fast Result",
                    snippet="Provider snippet without page extraction",
                    provider="searxng",
                )
            ],
        )
    )

    async def fail_if_crawl_called(_request):
        raise AssertionError("crawl should not run in Tavily search-only mode")

    monkeypatch.setattr(
        main_module,
        "get_search_provider",
        lambda provider_name, client: provider,
        raising=False,
    )
    monkeypatch.setattr(main_module, "cache_manager", None)
    monkeypatch.setattr(main_module, "crawl", fail_if_crawl_called)

    response = client.post(
        "/tavily/search",
        json={"query": "fast lookup", "max_results": 1, "mode": "search"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["query"] == "fast lookup"
    assert payload["results"][0]["source_stage"] == "searxng_search"
    assert payload["results"][0]["content"] == "Provider snippet without page extraction"
    assert payload["timings_ms"]["search_provider_request"] == 4.5


def test_tavily_search_queues_failed_urls_for_backfill(client, monkeypatch):
    """When foreground crawling fails, URLs should be queued for async backfill."""
    provider = FakeProvider(
        SimpleNamespace(
            provider="router:searxng",
            request_ms=5.0,
            hits=[
                SimpleNamespace(
                    url="https://example.com/blocked",
                    title="Blocked Result",
                    snippet="Blocked snippet",
                    provider="searxng",
                )
            ],
        )
    )

    async def fake_crawl(_request):
        raise HTTPException(status_code=500, detail="All URL crawls failed")

    async def fake_enqueue(urls, query, reason):
        assert urls == ["https://example.com/blocked"]
        assert query == "blocked page"
        assert reason == "search_crawl_failed"
        return len(urls)

    monkeypatch.setattr(
        main_module,
        "get_search_provider",
        lambda provider_name, client: provider,
        raising=False,
    )
    monkeypatch.setattr(main_module, "cache_manager", None)
    monkeypatch.setattr(main_module, "local_index", None)
    monkeypatch.setattr(main_module, "crawl", fake_crawl)
    monkeypatch.setattr(main_module, "_enqueue_backfill_urls", fake_enqueue)

    response = client.post(
        "/tavily/search",
        json={"query": "blocked page", "max_results": 1},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["results"] == []
    assert payload["failed_results"] == [
        {"url": "https://example.com/blocked", "error": "crawl_failed"}
    ]
    assert payload["backfill"]["queued"] == 1


def test_tavily_search_skips_urls_with_active_backfill_job(client, monkeypatch):
    """URLs already queued for backfill should not be crawled in the foreground."""
    provider = FakeProvider(
        SimpleNamespace(
            provider="router:searxng",
            request_ms=5.0,
            hits=[
                SimpleNamespace(
                    url="https://example.com/waiting",
                    title="Waiting Result",
                    snippet="Waiting snippet",
                    provider="searxng",
                )
            ],
        )
    )

    async def fail_if_crawl_called(_request):
        raise AssertionError("foreground crawl should skip active backfill URLs")

    async def fake_materialize(_hits, query):
        assert query == "waiting page"
        return [], [], ["https://example.com/waiting"]

    async def fake_enqueue(_urls, query, reason):
        assert query == "waiting page"
        assert reason == "search_crawl_failed"
        return 0

    monkeypatch.setattr(
        main_module,
        "get_search_provider",
        lambda provider_name, client: provider,
        raising=False,
    )
    monkeypatch.setattr(main_module, "cache_manager", None)
    monkeypatch.setattr(main_module, "local_index", object())
    monkeypatch.setattr(main_module, "crawl", fail_if_crawl_called)
    monkeypatch.setattr(main_module, "_materialize_local_hits", fake_materialize)
    monkeypatch.setattr(main_module, "_enqueue_backfill_urls", fake_enqueue)

    response = client.post(
        "/tavily/search",
        json={"query": "waiting page", "max_results": 1},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["results"] == []
    assert payload["backfill"]["queued"] == 0
    assert payload["backfill"]["pending"] == 1
    assert payload["backfill"]["pending_urls"] == ["https://example.com/waiting"]
