"""
Tests for reader helpers.
"""

from trailsearch.reader import build_reader_api_url, ordered_reader_urls, parse_reader_urls


def test_build_reader_api_url_encodes_target_url():
    """Reader URL builder should preserve URL syntax and encode unsafe characters."""
    target_url = "https://example.com/path?q=hello world&lang=zh-CN"

    result = build_reader_api_url(target_url, reader_url="http://reader:3000")

    assert result.startswith("http://reader:3000/")
    assert "https://example.com/path?q=hello%20world&lang=zh-CN" in result


def test_parse_reader_urls_deduplicates_and_falls_back():
    """Reader endpoint parser should support comma-separated node lists."""
    result = parse_reader_urls(
        "http://reader-1:3000, http://reader-2:3000, http://reader-1:3000/",
        fallback_url="http://fallback:3000",
    )

    assert result == ["http://reader-1:3000", "http://reader-2:3000"]
    assert parse_reader_urls("", fallback_url="http://fallback:3000") == [
        "http://fallback:3000"
    ]


def test_ordered_reader_urls_is_stable_and_rotates():
    """Same target URL should get the same Reader order for stable load spreading."""
    endpoints = ["http://reader-1:3000", "http://reader-2:3000", "http://reader-3:3000"]

    first = ordered_reader_urls("https://example.com/a", endpoints)
    second = ordered_reader_urls("https://example.com/a", endpoints)

    assert first == second
    assert sorted(first) == sorted(endpoints)
