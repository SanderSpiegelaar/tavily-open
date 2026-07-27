"""
Tests for content quality and chunking helpers.
"""

from trailsearch.quality import assess_content_quality, chunk_text


def test_assess_content_quality_accepts_relevant_content():
    """Quality gate should accept long content with query overlap."""
    content = (
        "Reader fallback extraction keeps article content clean. "
        "The benchmark shows reader fallback extraction is useful for Tavily-like search. "
        "This paragraph adds enough words to pass the configured content threshold."
    )

    result = assess_content_quality(
        content,
        query="reader fallback extraction",
        min_content_length=80,
    )

    assert result.usable is True
    assert result.query_overlap > 0
    assert result.score >= 0.35


def test_assess_content_quality_rejects_short_content():
    """Quality gate should reject content that is too short."""
    result = assess_content_quality("short", query="reader", min_content_length=80)

    assert result.usable is False
    assert "short_content" in result.reasons


def test_chunk_text_splits_long_content():
    """Chunking should keep all chunks under the target size."""
    content = "\n\n".join([f"Paragraph {index} with stable content." for index in range(20)])

    chunks = chunk_text(content, max_chars=120, overlap_chars=20)

    assert len(chunks) > 1
    assert all(len(chunk) <= 120 for chunk in chunks)
